"""build_bundle 純函式層守衛（不跑真實打包——那屬發版流程手動執行，見
``uv run python tools/build_bundle.py [--verify]``）。"""

import http.server
import json
import os
import sys
import threading
import zipfile
from pathlib import Path

import pytest

if sys.platform != "win32":
    pytest.skip("Windows-only：桌面殼／bundle 打包", allow_module_level=True)

import build_bundle as bb  # sys.path 補進 tools/，見 conftest.py


class TestBundleName:
    def test_ignores_version_and_returns_fixed_ascii_name(self):
        assert bb.bundle_name("1.0.0") == "shioaji_wizard"
        assert bb.bundle_name("9.9.9") == "shioaji_wizard"
        assert bb.bundle_name("1.0.0").isascii()


class TestParseSemver:
    def test_strips_v_prefix(self):
        assert bb.parse_semver("v1.2.3") == (1, 2, 3)
        assert bb.parse_semver("1.2.3") == (1, 2, 3)

    def test_rejects_non_triplet(self):
        with pytest.raises(ValueError):
            bb.parse_semver("1.2")


class TestMissingAssets:
    def test_all_present_returns_empty(self, tmp_path):
        for rel in bb.ASSET_CHECKLIST:
            p = tmp_path / "shioaji_wizard" / rel
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_text("x", encoding="utf-8")
        assert bb.missing_assets(tmp_path) == []

    def test_reports_absent_files(self, tmp_path):
        assert bb.missing_assets(tmp_path) == list(bb.ASSET_CHECKLIST)

    def test_checklist_covers_every_static_file(self):
        """清單漏列＝守衛有缺口：真的漏包時 build 不會炸，錯誤留到收件者
        機器。改由目錄內容反推——日後新增任何靜態資產，忘了加進清單就在
        這裡紅燈。"""
        static_dir = Path(__file__).resolve().parent.parent / "src" / "shioaji_wizard" / "static"
        listed = set(bb.ASSET_CHECKLIST)
        missing = [
            f"static/{f.name}"
            for f in sorted(static_dir.iterdir())
            if f.is_file() and f"static/{f.name}" not in listed
        ]
        assert missing == [], f"ASSET_CHECKLIST 漏列：{missing}"


class TestRenderers:
    def test_debug_bat_is_ascii_and_launches_module(self):
        content = bb.render_debug_bat()
        content.encode("ascii")  # 內容必須全 ASCII（codepage 雷）
        assert "python\\python.exe" in content
        assert "-m shioaji_wizard --root" in content
        assert "pause" in content

    def test_readme_carries_version_and_key_facts(self):
        text = bb.render_readme("1.0.0")
        assert "v1.0.0" in text
        assert "wizard.exe" in text
        assert "SmartScreen" in text
        assert "Sinopac.pfx" in text

    def test_readme_documents_durable_smartscreen_unblock(self):
        text = bb.render_readme("1.0.0")
        assert "解除封鎖" in text
        assert "Mark of the Web" in text
        assert "僅此一次" not in text

    def test_readme_warns_against_cloud_sync_folders(self):
        text = bb.render_readme("1.0.0")
        assert "雲端同步" in text

    def test_readme_mentions_export_log_feature(self):
        text = bb.render_readme("1.0.0")
        assert "匯出除錯紀錄" in text


class TestRenderLauncherVersionCs:
    def test_carries_four_part_version_and_metadata(self):
        src = bb.render_launcher_version_cs("1.0.2")
        assert 'AssemblyFileVersion("1.0.2.0")' in src
        assert 'AssemblyInformationalVersion("1.0.2")' in src
        assert "Shioaji 測試精靈" in src

    def test_rejects_non_semver(self):
        with pytest.raises(ValueError):
            bb.render_launcher_version_cs("1.0")


class TestCompileLauncher:
    @pytest.mark.skipif(not bb.CSC_PATH.is_file(), reason="需 Windows 內建 csc.exe")
    def test_produces_winexe_without_icon(self, tmp_path):
        """目前 static/favicon.ico 尚未建立：compile_launcher 必須在沒有
        icon 檔的情況下仍能編出可用的 exe（/win32icon 是條件式加旗標）。"""
        exe = tmp_path / "wizard.exe"
        bb.compile_launcher(exe, "9.9.9")
        assert exe.is_file()
        assert exe.stat().st_size > 2048


class TestIsolatedEnv:
    def test_path_is_bundle_python_plus_system_only(self, tmp_path):
        env = bb.isolated_env(tmp_path)
        parts = env["PATH"].split(os.pathsep)
        assert parts[0] == str(tmp_path / "python")
        assert len(parts) == 3  # bundle python＋System32＋SystemRoot
        assert "TEMP" in env


class TestRunStep:
    def test_returns_stdout_on_success(self):
        out = bb.run_step([sys.executable, "-c", "print('hi')"], timeout=30, what="測試")
        assert out.strip() == "hi"

    def test_nonzero_exit_raises_with_stderr(self):
        with pytest.raises(bb.BuildError, match="boom"):
            bb.run_step(
                [sys.executable, "-c", "import sys; sys.exit('boom')"],
                timeout=30,
                what="測試",
            )

    def test_missing_executable_raises(self):
        with pytest.raises(bb.BuildError, match="找不到執行檔"):
            bb.run_step(["no-such-tool-xyz.exe"], timeout=5, what="測試")


class TestDirSizeMb:
    def test_sums_recursive_file_sizes(self, tmp_path):
        (tmp_path / "a.bin").write_bytes(b"\x00" * 1_048_576)
        sub = tmp_path / "sub"
        sub.mkdir()
        (sub / "b.bin").write_bytes(b"\x00" * 1_048_576)
        assert bb.dir_size_mb(tmp_path) == pytest.approx(2.0)


class TestFinalize:
    def test_promotes_into_empty_dist(self, tmp_path):
        tmp_bundle = tmp_path / ".tmp" / "shioaji_wizard"
        (tmp_bundle / "sub").mkdir(parents=True)
        (tmp_bundle / "sub" / "f.txt").write_text("新", encoding="utf-8")
        final = tmp_path / "shioaji_wizard"
        bb.finalize(tmp_bundle, final, make_zip=False)
        assert (final / "sub" / "f.txt").read_text(encoding="utf-8") == "新"
        assert not tmp_bundle.exists()

    def test_replaces_previous_bundle_and_clears_old(self, tmp_path):
        final = tmp_path / "shioaji_wizard"
        final.mkdir()
        (final / "old.txt").write_text("舊", encoding="utf-8")
        tmp_bundle = tmp_path / ".tmp" / "shioaji_wizard"
        tmp_bundle.mkdir(parents=True)
        (tmp_bundle / "new.txt").write_text("新", encoding="utf-8")
        bb.finalize(tmp_bundle, final, make_zip=False)
        assert (final / "new.txt").exists()
        assert not (final / "old.txt").exists()
        assert not final.with_name("shioaji_wizard.old").exists()

    def test_zip_contains_top_level_folder(self, tmp_path):
        tmp_bundle = tmp_path / ".tmp" / "shioaji_wizard"
        tmp_bundle.mkdir(parents=True)
        (tmp_bundle / "f.txt").write_text("x", encoding="utf-8")
        final = tmp_path / "shioaji_wizard"
        bb.finalize(tmp_bundle, final, make_zip=True)
        with zipfile.ZipFile(tmp_path / "shioaji_wizard.zip") as z:
            assert "shioaji_wizard/f.txt" in z.namelist()

    def test_promotion_failure_keeps_old_bundle_recoverable(self, tmp_path, monkeypatch):
        final = tmp_path / "shioaji_wizard"
        final.mkdir()
        (final / "old.txt").write_text("舊", encoding="utf-8")
        tmp_bundle = tmp_path / ".tmp" / "shioaji_wizard"
        tmp_bundle.mkdir(parents=True)
        (tmp_bundle / "new.txt").write_text("新", encoding="utf-8")
        real_replace = os.replace
        calls = {"n": 0}

        def failing_replace(src, dst):
            calls["n"] += 1
            if calls["n"] == 2:  # 第二次呼叫＝promote 新 bundle 那一步
                raise OSError("模擬 promote 失敗")
            return real_replace(src, dst)

        monkeypatch.setattr("os.replace", failing_replace)
        with pytest.raises(OSError):
            bb.finalize(tmp_bundle, final, make_zip=False)
        old = final.with_name("shioaji_wizard.old")
        assert (old / "old.txt").read_text(encoding="utf-8") == "舊"
        assert not final.exists()

    def test_sweeps_stale_old_dir_before_promote(self, tmp_path):
        stale = tmp_path / "shioaji_wizard.old"
        stale.mkdir()
        (stale / "stale.txt").write_text("上次中斷的殘留", encoding="utf-8")
        final = tmp_path / "shioaji_wizard"
        final.mkdir()
        (final / "old.txt").write_text("舊", encoding="utf-8")
        tmp_bundle = tmp_path / ".tmp" / "shioaji_wizard"
        tmp_bundle.mkdir(parents=True)
        (tmp_bundle / "new.txt").write_text("新", encoding="utf-8")
        bb.finalize(tmp_bundle, final, make_zip=False)
        assert (final / "new.txt").exists()
        assert not stale.exists()


class TestPruneBundle:
    def test_removes_existing_and_skips_missing(self, tmp_path):
        keep = tmp_path / "python" / "Lib" / "site-packages" / "shioaji"
        keep.mkdir(parents=True)
        (keep / "__init__.py").write_text("x", encoding="utf-8")
        victim = tmp_path / "python" / "Lib" / "site-packages" / "pip"
        victim.mkdir(parents=True)
        (victim / "y.py").write_text("y", encoding="utf-8")
        pruned, _n = bb.prune_bundle(tmp_path)
        assert "python/Lib/site-packages/pip" in pruned
        assert not victim.exists()
        assert keep.exists()  # 非清單目錄不動
        assert all(not (tmp_path / rel).exists() for rel in pruned)

    def test_never_deletes_native_extension_pyd(self, tmp_path):
        """shioaji 是本專案唯一的原生擴充相依：``.pyd`` 一旦被誤刪，bundle
        就是啟動即炸——這是最不可回復的一種瘦身錯誤，必須有守衛。"""
        shioaji_pkg = tmp_path / "python" / "Lib" / "site-packages" / "shioaji"
        shioaji_pkg.mkdir(parents=True)
        (shioaji_pkg / "_core.pyd").write_bytes(b"\x00" * 64)
        (shioaji_pkg / "_core.pyi").write_text("stub", encoding="utf-8")
        (shioaji_pkg / "__init__.py").write_text("x", encoding="utf-8")

        bb.prune_bundle(tmp_path)

        assert (shioaji_pkg / "_core.pyd").is_file()  # 原生擴充：絕不刪
        assert not (shioaji_pkg / "_core.pyi").exists()  # 型別存根：可刪
        assert (shioaji_pkg / "__init__.py").is_file()

    def test_file_level_prune_keeps_runtime_essentials(self, tmp_path):
        """檔案級瘦身只能砍執行期用不到的：型別存根、C 標頭、測試、安裝紀錄。
        ``METADATA`` 必須留——``server.APP_VERSION`` 靠 ``importlib.metadata``
        讀它。"""
        site = tmp_path / "python" / "Lib" / "site-packages"
        pkg = site / "pkg"
        (pkg / "tests").mkdir(parents=True)
        (pkg / "tests" / "t.py").write_text("x", encoding="utf-8")
        (pkg / "a.py").write_text("x", encoding="utf-8")
        (pkg / "a.pyi").write_text("x", encoding="utf-8")
        (pkg / "ext.h").write_text("x", encoding="utf-8")
        di = site / "pkg-1.0.dist-info"
        (di / "licenses").mkdir(parents=True)
        (di / "licenses" / "LICENSE").write_text("x", encoding="utf-8")
        (di / "RECORD").write_text("x", encoding="utf-8")
        (di / "METADATA").write_text("x", encoding="utf-8")

        _pruned, n = bb.prune_bundle(tmp_path)

        assert n == 5
        left = sorted(p.relative_to(tmp_path).as_posix() for p in tmp_path.rglob("*") if p.is_file())
        assert left == [
            "python/Lib/site-packages/pkg-1.0.dist-info/METADATA",
            "python/Lib/site-packages/pkg/a.py",
        ]


class TestAssertNativeExtensionPresent:
    def test_passes_when_pyd_present(self, tmp_path):
        pkg = tmp_path / "shioaji"
        pkg.mkdir(parents=True)
        (pkg / "_core.pyd").write_bytes(b"\x00")
        bb.assert_native_extension_present(tmp_path)  # 不拋即通過

    def test_raises_when_pyd_missing(self, tmp_path):
        pkg = tmp_path / "shioaji"
        pkg.mkdir(parents=True)
        with pytest.raises(bb.BuildError, match="_core"):
            bb.assert_native_extension_present(tmp_path)


class TestFileBudget:
    def test_under_budget_returns_count(self, tmp_path, monkeypatch):
        monkeypatch.setattr(bb, "MAX_FILES", 3)
        for i in range(3):
            (tmp_path / f"f{i}.txt").write_text("x", encoding="utf-8")
        assert bb.assert_file_budget(tmp_path) == 3

    def test_over_budget_raises_with_fattest_dirs(self, tmp_path, monkeypatch):
        monkeypatch.setattr(bb, "MAX_FILES", 2)
        fat = tmp_path / "python" / "Lib" / "site-packages" / "huge"
        fat.mkdir(parents=True)
        for i in range(5):
            (fat / f"f{i}.txt").write_text("x", encoding="utf-8")
        with pytest.raises(bb.BuildError) as ei:
            bb.assert_file_budget(tmp_path)
        msg = str(ei.value)
        assert "超過上限" in msg
        assert "huge" in msg
        assert "不要調高上限" in msg

    def test_over_budget_can_be_allowed_explicitly(self, tmp_path, monkeypatch):
        monkeypatch.setattr(bb, "MAX_FILES", 1)
        for i in range(4):
            (tmp_path / f"f{i}.txt").write_text("x", encoding="utf-8")
        assert bb.assert_file_budget(tmp_path, allow_oversize=True) == 4


class TestSweepPycache:
    def test_removes_all_pycache_dirs_and_returns_count(self, tmp_path):
        stdlib_cache = tmp_path / "python" / "Lib" / "asyncio" / "__pycache__"
        stdlib_cache.mkdir(parents=True)
        (stdlib_cache / "x.pyc").write_bytes(b"\x00")
        pkg_cache = tmp_path / "python" / "Lib" / "site-packages" / "fastapi" / "__pycache__"
        pkg_cache.mkdir(parents=True)
        keep = tmp_path / "python" / "Lib" / "site-packages" / "fastapi" / "applications.py"
        keep.write_text("x", encoding="utf-8")
        n = bb.sweep_pycache(tmp_path)
        assert n == 2
        assert not stdlib_cache.exists()
        assert not pkg_cache.exists()
        assert keep.exists()

    def test_no_pycache_returns_zero(self, tmp_path):
        (tmp_path / "python").mkdir()
        assert bb.sweep_pycache(tmp_path) == 0


class TestHideBundleTopLevel:
    def test_hides_everything_except_keep(self, tmp_path):
        (tmp_path / "wizard.exe").write_bytes(b"exe")
        (tmp_path / "python").mkdir()
        (tmp_path / "README.txt").write_text("x", encoding="utf-8")
        (tmp_path / "啟動（除錯）.bat").write_text("x", encoding="utf-8")

        hidden = bb.hide_bundle_top_level(tmp_path, keep="wizard.exe")

        assert set(hidden) == {"python", "README.txt"}  # 除錯 .bat 保持可見
        assert not bb._is_hidden(tmp_path / "wizard.exe")
        assert bb._is_hidden(tmp_path / "python")
        assert bb._is_hidden(tmp_path / "README.txt")
        assert not bb._is_hidden(tmp_path / "啟動（除錯）.bat")

    def test_is_idempotent(self, tmp_path):
        (tmp_path / "wizard.exe").write_bytes(b"exe")
        (tmp_path / "python").mkdir()
        bb.hide_bundle_top_level(tmp_path, keep="wizard.exe")
        bb.hide_bundle_top_level(tmp_path, keep="wizard.exe")  # 不拋、狀態不變
        assert bb._is_hidden(tmp_path / "python")


class TestHttpHelper:
    def test_non_2xx_returns_status_and_body_instead_of_raising(self):
        class H(http.server.BaseHTTPRequestHandler):
            def do_GET(self) -> None:
                payload = "找不到".encode()
                self.send_response(404)
                self.send_header("Content-Type", "text/plain; charset=utf-8")
                self.send_header("Content-Length", str(len(payload)))
                self.end_headers()
                self.wfile.write(payload)

            def log_message(self, format: str, *args: object) -> None:
                pass

        srv = http.server.HTTPServer(("127.0.0.1", 0), H)
        t = threading.Thread(target=srv.serve_forever, daemon=True)
        t.start()
        try:
            status, body = bb._http("GET", f"http://127.0.0.1:{srv.server_port}/x")
        finally:
            srv.shutdown()
        assert status == 404
        assert "找不到" in body


class TestReleaseZipName:
    def test_release_zip_name_is_ascii_dashed(self):
        assert bb.release_zip_name("1.1.0") == "shioaji-wizard-v1.1.0.zip"
        assert bb.release_zip_name("1.1.0").isascii()

    def test_finalize_writes_versioned_zip(self, tmp_path):
        tmp_bundle = tmp_path / "tmp"
        tmp_bundle.mkdir()
        (tmp_bundle / "x.txt").write_text("x", encoding="utf-8")
        final = tmp_path / bb.bundle_name("9.9.9")
        bb.finalize(tmp_bundle, final, make_zip=True, version="9.9.9")

        zips = sorted(p.name for p in tmp_path.glob("*.zip"))
        assert zips == ["shioaji-wizard-v9.9.9.zip"], zips
        with zipfile.ZipFile(tmp_path / "shioaji-wizard-v9.9.9.zip") as z:
            tops = {n.split("/")[0] for n in z.namelist()}
        assert tops == {"shioaji_wizard"}


class TestPreflightSkipsVersionTagGuard:
    """preflight 不含 fcn-pricing 版的版本↔GitHub tag 同步守衛（本專案尚無
    release tag 慣例）。此測試只斷言 :mod:`build_bundle` 沒有重新引入那組
    符號——真的跑 preflight() 需要網路與已建置的 repo 狀態，屬於
    ``--verify`` 涵蓋的整合層級，不在純函式測試範圍。"""

    def test_no_tag_sync_helpers_reintroduced(self):
        assert not hasattr(bb, "check_version_sync")
        assert not hasattr(bb, "latest_tag")


class TestJsonRoundTrip:
    """``json`` 模組僅供 ``verify_bundle`` 解析 ``/api/state``；這裡用一個
    最小 smoke 確認匯入沒被 lint 誤刪（純函式層看不到 verify_bundle 的
    整合行為）。"""

    def test_module_importable(self):
        assert json.loads('{"root": "x"}')["root"] == "x"


def test_finalize_preserves_user_files_and_zip_excludes_them(tmp_path):
    """重建時舊 bundle 的 .env／*.pfx 要搬回新 bundle；zip 一律從乾淨的 tmp bundle 打、不含使用者檔案。"""
    import zipfile

    from build_bundle import finalize

    dist = tmp_path / "dist"
    tmp = dist / ".tmp" / "shioaji_wizard"
    final = dist / "shioaji_wizard"
    tmp.mkdir(parents=True)
    (tmp / "wizard.exe").write_bytes(b"new")
    final.mkdir(parents=True)
    (final / "wizard.exe").write_bytes(b"old")
    (final / ".env").write_text("SJ_API_KEY=secret", encoding="utf-8")
    (final / "Sinopac.pfx").write_bytes(b"pfx")
    finalize(tmp, final, make_zip=True, version="9.9.9")
    assert (final / "wizard.exe").read_bytes() == b"new"
    assert (final / ".env").read_text(encoding="utf-8") == "SJ_API_KEY=secret"
    assert (final / "Sinopac.pfx").read_bytes() == b"pfx"
    assert not (dist / "shioaji_wizard.old").exists()
    zips = list(dist.glob("*.zip"))
    assert len(zips) == 1
    names = zipfile.ZipFile(zips[0]).namelist()
    assert any(n.endswith("wizard.exe") for n in names)
    assert not any(n.endswith(".env") or n.endswith(".pfx") for n in names)
