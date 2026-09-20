"""server／sjenv 的回歸測試：.env 解析與寫入、端點行為、白名單、匯出紀錄。不連永豐。"""

from __future__ import annotations

import importlib
import os
import sys
import time
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

FAKE_KEY = "FAKEAPIKEY_1234567890abcdefghijklmnopqrstuvw"
FAKE_SEC = "FAKESECKEY_1234567890abcdefghijklmnopqrstuvw"


def _reload_modules(monkeypatch: pytest.MonkeyPatch, tmp_path: Path, ekey_base: Path | None = None):
    """以 tmp_path 當 ROOT 重新載入 sjenv／server（ROOT／EKEY_BASE 在 import 時算好）。

    SJ_EKEY_BASE 預設指到 tmp_path 下一個不存在的資料夾：server import 時
    ``_read_active_profile()`` 會呼叫一次 ``find_eleader_pfx()``，沒有這行會在
    真的裝過 eLeader 的開發機上讀到 C:\\ekey\\551——絕不能讓測試碰到真的 ekey 樹。
    """
    monkeypatch.setenv("SJ_ENV_DIR", str(tmp_path))
    monkeypatch.setenv("SJ_EKEY_BASE", str(ekey_base if ekey_base is not None else tmp_path / "no-ekey-here"))
    for name in ("shioaji_wizard.server", "shioaji_wizard.sjenv"):
        sys.modules.pop(name, None)
    sjenv = importlib.import_module("shioaji_wizard.sjenv")
    server = importlib.import_module("shioaji_wizard.server")
    assert sjenv.ROOT == tmp_path.resolve()
    return sjenv, server


@pytest.fixture
def env_mods(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    return _reload_modules(monkeypatch, tmp_path)


@pytest.fixture
def client(env_mods, monkeypatch: pytest.MonkeyPatch):
    _, server = env_mods
    monkeypatch.setattr(server, "find_eleader_pfx", lambda: [])
    return TestClient(server.app, base_url="http://127.0.0.1")  # guards 只放行本機 Host


@pytest.fixture
def ekey_tree(tmp_path: Path) -> tuple[Path, dict[str, Path]]:
    """假 eLeader 憑證樹：兩個假身分證各一張 pfx（絕不動真的 C:\\ekey）。"""
    base = tmp_path / "ekey" / "551"
    paths: dict[str, Path] = {}
    for person_id in ("A123456789", "B223456789"):
        d = base / person_id / "S"
        d.mkdir(parents=True)
        p = d / "Sinopac.pfx"
        p.write_bytes(b"fake-pfx-bytes")
        paths[person_id] = p
    return base, paths


@pytest.fixture
def env_mods_ekey(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, ekey_tree: tuple[Path, dict[str, Path]]):
    """用真的 SJ_EKEY_BASE（指到假樹）重新載入，走 find_eleader_pfx() 的真實路徑（非 monkeypatch stub）。"""
    base, _ = ekey_tree
    return _reload_modules(monkeypatch, tmp_path, ekey_base=base)


@pytest.fixture
def client_ekey(env_mods_ekey):
    _, server = env_mods_ekey
    return TestClient(server.app, base_url="http://127.0.0.1")


# ---------------------------------------------------------------- sjenv
def test_parse_env_text_handles_quotes_export_dupes_and_bad_lines(env_mods):
    sjenv, _ = env_mods
    text = "export SJ_API_KEY=\"abc\"\nSJ_SEC_KEY='x=y'\nBROKEN\nSJ_API_KEY=def\n# c\n"
    values, problems = sjenv.parse_env_text(text)
    assert values == {"SJ_API_KEY": "def", "SJ_SEC_KEY": "x=y"}
    assert any("沒有「=」" in p for p in problems)
    assert any("重複設定 SJ_API_KEY" in p for p in problems)


def test_pfx_path_default_relative_absolute(env_mods, tmp_path: Path):
    sjenv, _ = env_mods
    assert sjenv.pfx_path({}) == tmp_path.resolve() / "Sinopac.pfx"
    assert sjenv.pfx_path({"SJ_CA_PATH": "certs/my.pfx"}) == tmp_path.resolve() / "certs" / "my.pfx"
    abs_p = (tmp_path / "elsewhere" / "a.pfx").resolve()
    assert sjenv.pfx_path({"SJ_CA_PATH": str(abs_p)}) == abs_p


def test_ensure_env_keys_adds_missing_only(env_mods, tmp_path: Path):
    sjenv, _ = env_mods
    p = tmp_path / ".env"
    p.write_text("SJ_API_KEY=a\nSJ_CA_PASSWD=keep", encoding="utf-8")
    added = sjenv.ensure_env_keys(p, {"SJ_CA_PATH": "X", "SJ_CA_PASSWD": ""})
    assert added == ["SJ_CA_PATH"]
    values, _ = sjenv.parse_env_text(p.read_text(encoding="utf-8"))
    assert values == {"SJ_API_KEY": "a", "SJ_CA_PASSWD": "keep", "SJ_CA_PATH": "X"}


def test_report_writes_json_when_env_set(env_mods, tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    sjenv, _ = env_mods
    out = tmp_path / "r.json"
    monkeypatch.setenv("SJ_REPORT_FILE", str(out))
    rep = sjenv.Report()
    rep.ok("A1 x")
    rep.fail("A2 y", "因為")
    rep.skip("A3 z", "未要求")
    assert out.is_file()
    assert not rep.all_passed
    assert [i["status"] for i in rep.items] == ["PASS", "FAIL", "SKIP"]


# ---------------------------------------------------------------- server：.env
def test_state_without_env(client):
    d = client.get("/api/state").json()
    assert d["env"]["exists"] is False
    assert d["env"]["ok_a"] is False
    assert [i["name"][:2] for i in d["summary"]] == ["E1", "E2", "E3"]
    assert all(i["status"] == "FAIL" for i in d["summary"])


def test_post_env_creates_file_and_masks(client, tmp_path: Path):
    r = client.post(
        "/api/env", json={"api_key": FAKE_KEY, "sec_key": FAKE_SEC, "ca_passwd": "", "ca_path": ""}
    )
    assert r.status_code == 200
    e = r.json()["env"]
    assert e["ok_a"] is True and e["ok_b"] is False
    assert e["api_key_masked"].startswith("FAKE") and FAKE_KEY not in e["api_key_masked"]
    text = (tmp_path / ".env").read_text(encoding="utf-8")
    assert f"SJ_API_KEY={FAKE_KEY}" in text and f"SJ_SEC_KEY={FAKE_SEC}" in text
    assert "SJ_CA_PATH=" in text and "SJ_CA_PASSWD=" in text
    # 第二次只改憑證密碼：金鑰保留
    r = client.post("/api/env", json={"ca_passwd": "pw", "ca_path": ""})
    e = r.json()["env"]
    assert e["ca_passwd_set"] is True and e["api_key_masked"].startswith("FAKE")
    # 清除密碼
    r = client.post("/api/env", json={"clear_ca_passwd": True, "ca_path": ""})
    assert r.json()["env"]["ca_passwd_set"] is False


def test_post_env_fixes_lowercase_key_and_keeps_comments(client, tmp_path: Path):
    (tmp_path / ".env").write_text(
        f"# 註解\nsj_api_key={FAKE_KEY}\nSJ_SEC_KEY={FAKE_SEC}\n", encoding="utf-8"
    )
    d = client.get("/api/state").json()
    assert any("大小寫不對" in p for p in d["env"]["problems"])
    client.post("/api/env", json={"api_key": FAKE_KEY, "ca_path": ""})
    text = (tmp_path / ".env").read_text(encoding="utf-8")
    assert "# 註解" in text and "sj_api_key" not in text and f"SJ_API_KEY={FAKE_KEY}" in text


def test_state_with_non_utf8_env_does_not_crash(client, tmp_path: Path):
    (tmp_path / ".env").write_bytes("SJ_API_KEY=中文".encode("big5"))
    d = client.get("/api/state").json()
    assert d["env"]["decode_error"] is True and d["env"]["ok_a"] is False


def test_custom_ca_path_respected(client, tmp_path: Path):
    (tmp_path / "certs").mkdir()
    (tmp_path / "certs" / "my.pfx").write_bytes(b"x")
    client.post(
        "/api/env",
        json={"api_key": FAKE_KEY, "sec_key": FAKE_SEC, "ca_passwd": "pw", "ca_path": "certs/my.pfx"},
    )
    e = client.get("/api/state").json()["env"]
    assert e["pfx_exists"] is True and e["ca_path_custom"] is True and e["ok_b"] is True


# ---------------------------------------------------------------- server：其他端點
def test_run_validation_errors(client):
    assert client.post("/api/run", json={"kind": "a"}).status_code == 400  # 沒金鑰
    client.post("/api/env", json={"api_key": FAKE_KEY, "sec_key": FAKE_SEC, "ca_path": ""})
    r = client.post("/api/run", json={"kind": "b"})
    assert r.status_code == 400 and "憑證密碼" in r.json()["detail"]
    assert client.post("/api/run", json={"kind": "zzz"}).status_code == 400


def test_open_whitelist(client):
    assert client.post("/api/open", json={"url": "https://evil.example/"}).status_code == 400
    assert (
        client.post("/api/open", json={"url": "http://www.sinotrade.com.tw/"}).status_code == 400
    )  # 非 https


def test_window_shape(client):
    d = client.get("/api/window").json()
    assert set(d) >= {"now", "weekday", "in_window", "good", "notes"}


def test_export_log_masks_secrets(client, tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(sys, "platform", "linux")  # 別在測試裡開 explorer
    client.post(
        "/api/env", json={"api_key": FAKE_KEY, "sec_key": FAKE_SEC, "ca_passwd": "secretpw99", "ca_path": ""}
    )
    r = client.post("/api/export-log")
    assert r.status_code == 200
    out = Path(r.json()["path"])
    assert out.parent == tmp_path.resolve() and out.name.startswith("shioaji_wizard-debug-")
    text = out.read_text(encoding="utf-8")
    assert FAKE_KEY not in text and FAKE_SEC not in text and "secretpw99" not in text
    assert "E1" in text and "遮罩" in text


def test_heartbeat_updates_age(client, env_mods):
    _, server = env_mods
    server._last_heartbeat -= 100
    assert server.heartbeat_age() > 50
    client.post("/api/heartbeat")
    assert server.heartbeat_age() < 5


def test_runtime_dir_is_created_hidden(env_mods, tmp_path: Path):
    sjenv, _ = env_mods
    d = sjenv.ensure_runtime_dir()
    assert d.is_dir() and d.name == ".runtime"
    if os.name == "nt":
        import ctypes

        attrs = ctypes.windll.kernel32.GetFileAttributesW(str(d))  # type: ignore[attr-defined]
        assert attrs & 0x02


def test_export_log_redacts_secrets_inside_job_output_and_person_id(
    client, env_mods, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    """憑證密碼剛好等於身分證字號（eLeader 舊預設）時，子行程輸出的 person_id 就是密碼——匯出必須蓋掉。"""
    monkeypatch.setattr(sys, "platform", "linux")
    _, server = env_mods
    pwd = "A123456789"  # noqa: S105 — 測試用假身分證字號
    client.post("/api/env", json={"api_key": FAKE_KEY, "sec_key": FAKE_SEC, "ca_passwd": pwd, "ca_path": ""})
    job = server.Job("b")
    job.lines = [
        f"      S 9A9P-1 person_id={pwd} signed=True",
        f"token {FAKE_SEC} leaked",
        "other B287654321 id",
    ]
    job.rc = 0
    server._job = job
    r = client.post("/api/export-log")
    text = Path(r.json()["path"]).read_text(encoding="utf-8")
    assert pwd not in text and FAKE_SEC not in text
    assert "B28*****21" in text  # 密碼＝身分證字號時整串已被 [已遮罩] 蓋掉，另一個 ID 只打碼中段
    assert "[已遮罩]" in text


def test_run_job_times_out_and_kills_silent_child(env_mods, monkeypatch: pytest.MonkeyPatch):
    """子行程印一行後不再輸出（模擬永豐無回應）→ 逾時被殺、job 有結束、原因寫逾時。"""
    _, server = env_mods
    monkeypatch.setattr(server, "NO_OUTPUT_TIMEOUT", 1.5)
    job = server.Job("a")
    server._run_job(job, "http.server", ["0", "--bind", "127.0.0.1"])  # 印 Serving… 後永遠等待
    assert not job.running and job.timed_out
    assert job.proc is not None and job.proc.poll() is not None
    assert any("逾時" in line for line in job.lines)
    assert server.SESSION["A1 模擬環境登入"]["status"] == "FAIL"


def test_run_job_redacts_live_lines(env_mods, client, tmp_path: Path):
    """子行程輸出裡的金鑰值與身分證字號在寫進 job.lines 的當下就遮罩（畫面即時輸出也安全）。"""
    _, server = env_mods
    client.post(
        "/api/env", json={"api_key": FAKE_KEY, "sec_key": FAKE_SEC, "ca_passwd": "A123456789", "ca_path": ""}
    )
    job = server.Job("b")
    stmt = f"print('k={FAKE_KEY} id=A123456789 other=B287654321')"
    server._run_job(job, "timeit", ["-n", "1", "-r", "1", stmt])
    joined = "\n".join(job.lines)
    assert FAKE_KEY not in joined and "A123456789" not in joined
    assert "[已遮罩]" in joined and "B28*****21" in joined
    assert FAKE_KEY in server._SEEN_SECRETS


def test_cancel_job_kills_running_child(env_mods, monkeypatch: pytest.MonkeyPatch):
    import threading as _th
    import time as _time

    _, server = env_mods
    monkeypatch.setattr(server, "NO_OUTPUT_TIMEOUT", 30.0)
    job = server.Job("a")
    server._job = job
    t = _th.Thread(
        target=server._run_job, args=(job, "http.server", ["0", "--bind", "127.0.0.1"]), daemon=True
    )
    t.start()
    for _ in range(50):
        if job.proc is not None:
            break
        _time.sleep(0.1)
    assert server.cancel_job() is True
    t.join(timeout=10)
    assert not job.running


def test_guards_installed_on_app_by_default(client):
    r = client.get("/api/state", headers={"Host": "evil.example"})
    assert r.status_code == 403
    r = client.post("/api/heartbeat", headers={"Origin": "https://evil.example"})
    assert r.status_code == 403


def test_favicon_served(client):
    r = client.get("/favicon.ico")
    assert r.status_code == 200 and r.content[:4] == b"\x00\x00\x01\x00"


def test_browse_script_forces_utf8_and_owner_window(env_mods, tmp_path: Path):
    """選檔腳本必須先把 PowerShell 輸出編碼設成 UTF-8（中文路徑才不會變亂碼），且有 TopMost owner。"""
    _, server = env_mods
    ps = server._browse_script(tmp_path / "中文 o'neil")
    assert ps.startswith("[Console]::OutputEncoding = [System.Text.Encoding]::UTF8;")
    assert "$o.TopMost = $true" in ps and "ShowDialog($o)" in ps
    assert "o''neil" in ps  # 單引號已跳脫


def test_powershell_roundtrips_chinese_path(env_mods):
    """實際叫 powershell 印中文路徑，伺服器端以 UTF-8 解回來要一字不差（模擬 api_browse 的輸出鏈）。"""
    import shutil
    import subprocess

    if not shutil.which("powershell"):
        pytest.skip("no powershell")
    ps = "[Console]::OutputEncoding = [System.Text.Encoding]::UTF8; [Console]::Out.Write('C:/測試中文路徑/Sinopac.pfx')"
    out = subprocess.run(
        ["powershell", "-NoProfile", "-STA", "-ExecutionPolicy", "Bypass", "-Command", ps],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        check=False,
    )
    assert out.stdout.strip() == "C:/測試中文路徑/Sinopac.pfx"


def test_print_summary_suppressed_in_gui_mode(env_mods, monkeypatch: pytest.MonkeyPatch, capsys):
    sjenv, _ = env_mods
    items = [{"name": "A1 x", "status": "PASS", "reason": ""}]
    monkeypatch.setenv("SJ_NO_TEXT_SUMMARY", "1")
    sjenv.print_summary(items)
    assert capsys.readouterr().out == ""
    monkeypatch.delenv("SJ_NO_TEXT_SUMMARY")
    sjenv.print_summary(items)
    assert "A1 x" in capsys.readouterr().out


def test_stale_fields_cleared_only_by_covering_test(env_mods, client):
    """更新過的欄位在涵蓋它的測試跑完前都算「未知」：A 只涵蓋金鑰，B 才涵蓋憑證密碼／憑證檔。"""
    _, server = env_mods
    client.post(
        "/api/env", json={"api_key": FAKE_KEY, "sec_key": FAKE_SEC, "ca_passwd": "pw1", "ca_path": ""}
    )
    assert set(client.get("/api/state").json()["stale"]) == {"api", "sec", "pwd"}  # 路徑沿用預設＝沒變
    # 跑一個（假的）A：只清 api／sec
    job = server.Job("a")
    server._run_job(job, "timeit", ["-n", "1", "-r", "1", "pass"])
    assert set(client.get("/api/state").json()["stale"]) == {"pwd"}
    # 再改密碼 → pwd 仍 stale；跑 B 才清掉
    client.post("/api/env", json={"ca_passwd": "pw2", "ca_path": ""})
    assert "pwd" in client.get("/api/state").json()["stale"]
    job = server.Job("b")
    server._run_job(job, "timeit", ["-n", "1", "-r", "1", "pass"])
    assert client.get("/api/state").json()["stale"] == []
    # 存一樣的值 → 不算更新
    client.post("/api/env", json={"ca_passwd": "pw2", "ca_path": ""})
    assert client.get("/api/state").json()["stale"] == []


def test_keys_locked_after_verified_login_until_unlock(env_mods, client):
    """A1 通過後金鑰鎖定：改金鑰回 409；帶 unlock_keys 才能改，改完又變未驗（不再鎖）。"""
    _, server = env_mods
    client.post("/api/env", json={"api_key": FAKE_KEY, "sec_key": FAKE_SEC, "ca_path": ""})
    assert client.get("/api/state").json()["keys_locked"] is False
    server._STALE_FIELDS.clear()
    server.record("A1 模擬環境登入", "PASS")
    assert client.get("/api/state").json()["keys_locked"] is True
    r = client.post("/api/env", json={"api_key": "NEWKEY_" + FAKE_KEY, "ca_path": ""})
    assert r.status_code == 409
    r = client.post("/api/env", json={"ca_passwd": "pw", "ca_path": ""})  # 只改憑證密碼不受鎖影響
    assert r.status_code == 200
    r = client.post("/api/env", json={"api_key": "NEWKEY_" + FAKE_KEY, "ca_path": "", "unlock_keys": True})
    assert r.status_code == 200
    assert client.get("/api/state").json()["keys_locked"] is False  # 改過 → 未驗 → 不鎖


def test_find_eleader_pfx(env_mods, tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    _, server = env_mods
    monkeypatch.setattr(sys, "platform", "win32")
    base = tmp_path / "ekey" / "551"
    (base / "A123456789" / "S").mkdir(parents=True)
    (base / "A123456789" / "S" / "Sinopac.pfx").write_bytes(b"x")
    (base / "A123456789" / "F").mkdir()
    (base / "A123456789" / "F" / "Other.pfx").write_bytes(b"x")  # 非 S 目錄不算
    found = server.find_eleader_pfx(base)
    assert found == [str(base / "A123456789" / "S" / "Sinopac.pfx")]
    assert server.find_eleader_pfx(tmp_path / "nope") == []


def test_state_auto_adopts_only_eleader_pfx_when_current_path_missing(
    env_mods, client, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    """唯一候選、目前沒有 pfx、尚未有人切換過、ROOT 已經有金鑰（升級前的單人設定）
    → 自動把 ROOT 的 SJ_CA_PATH 指到候選，緊接著同一次 /api/state 就把整份 .env
    搬過去（migrate）：profile 切到候選目錄，金鑰原封不動，不會落到「pending 但
    金鑰消失」（這是先前版本的失效鏈：auto-adopt 整份切走、目標目錄空白）。"""
    sjenv, server = env_mods
    client.post("/api/env", json={"api_key": FAKE_KEY, "sec_key": FAKE_SEC, "ca_path": ""})
    candidate = tmp_path / "ekey" / "551" / "A123456789" / "S" / "Sinopac.pfx"
    candidate.parent.mkdir(parents=True)
    candidate.write_bytes(b"pfx")
    monkeypatch.setattr(server, "find_eleader_pfx", lambda: [str(candidate)])
    server._STALE_FIELDS.clear()

    state = client.get("/api/state").json()

    assert state["pfx_auto_adopted"] is True
    assert state["profile_migrated"] is True
    assert Path(state["root"]) == candidate.parent  # 目前 profile 已切到 …\A123456789\S
    assert state["env"]["exists"] is True  # 整份搬過去了，不是 pending
    assert state["env"]["api_key_masked"].startswith("FAKE")
    values, _ = sjenv.parse_env_text((candidate.parent / ".env").read_text(encoding="utf-8"))
    assert values["SJ_API_KEY"] == FAKE_KEY and values["SJ_SEC_KEY"] == FAKE_SEC
    assert (tmp_path / ".env").is_file()  # ROOT 的舊檔留著沒刪


def test_state_does_not_guess_between_multiple_eleader_pfx(
    env_mods, client, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    sjenv, server = env_mods
    client.post("/api/env", json={"api_key": FAKE_KEY, "sec_key": FAKE_SEC, "ca_path": ""})
    candidates = []
    for person_id in ("A123456789", "B223456789"):
        candidate = tmp_path / "ekey" / "551" / person_id / "S" / "Sinopac.pfx"
        candidate.parent.mkdir(parents=True)
        candidate.write_bytes(b"pfx")
        candidates.append(str(candidate))
    monkeypatch.setattr(server, "find_eleader_pfx", lambda: candidates)

    state = client.get("/api/state").json()
    values, _ = sjenv.parse_env_text((tmp_path / ".env").read_text(encoding="utf-8"))

    assert Path(values["SJ_CA_PATH"]) == sjenv.DEFAULT_PFX
    assert state["env"]["pfx_exists"] is False
    assert state["pfx_auto_adopted"] is False
    assert Path(state["root"]) == tmp_path.resolve()  # 沒猜 → 仍在程式資料夾
    assert [p["pfx"] for p in state["profiles"]] == candidates
    assert all(not p["active"] and not p["configured"] for p in state["profiles"])


def test_state_preserves_valid_configured_pfx_over_eleader_candidate(
    env_mods, client, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    sjenv, server = env_mods
    configured = tmp_path / "certs" / "configured.pfx"
    configured.parent.mkdir()
    configured.write_bytes(b"configured")
    candidate = tmp_path / "ekey" / "551" / "A123456789" / "S" / "Sinopac.pfx"
    candidate.parent.mkdir(parents=True)
    candidate.write_bytes(b"candidate")
    client.post(
        "/api/env",
        json={"api_key": FAKE_KEY, "sec_key": FAKE_SEC, "ca_path": str(configured)},
    )
    monkeypatch.setattr(server, "find_eleader_pfx", lambda: [str(candidate)])

    state = client.get("/api/state").json()
    values, _ = sjenv.parse_env_text(sjenv.ENV_PATH.read_text(encoding="utf-8"))

    assert Path(values["SJ_CA_PATH"]) == configured
    assert Path(state["env"]["pfx_resolved"]) == configured
    assert state["pfx_auto_adopted"] is False


def test_state_replaces_broken_configured_pfx_with_only_eleader_candidate(
    env_mods, client, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    """目前指到的 pfx 不存在（壞掉的自訂路徑）、唯一候選、ROOT 已有金鑰 → 自動改
    ROOT 的 SJ_CA_PATH 指到候選，緊接著同一次 /api/state 就整份搬過去；金鑰跟著走，
    不會停在 pending 狀態。"""
    sjenv, server = env_mods
    broken = tmp_path / "certs" / "missing.pfx"
    candidate = tmp_path / "ekey" / "551" / "A123456789" / "S" / "Sinopac.pfx"
    candidate.parent.mkdir(parents=True)
    candidate.write_bytes(b"candidate")
    client.post(
        "/api/env",
        json={"api_key": FAKE_KEY, "sec_key": FAKE_SEC, "ca_path": str(broken)},
    )
    monkeypatch.setattr(server, "find_eleader_pfx", lambda: [str(candidate)])

    state = client.get("/api/state").json()

    assert state["pfx_auto_adopted"] is True
    assert state["profile_migrated"] is True
    assert Path(state["root"]) == candidate.parent
    assert state["env"]["exists"] is True
    values, _ = sjenv.parse_env_text((candidate.parent / ".env").read_text(encoding="utf-8"))
    assert values["SJ_API_KEY"] == FAKE_KEY and Path(values["SJ_CA_PATH"]) == candidate


def test_report_reason_redacted_before_session(
    env_mods, client, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    """子行程 report.json 的 reason（永豐錯誤原文可能回顯金鑰）進 SESSION／總覽前也要遮罩。"""
    _, server = env_mods
    client.post("/api/env", json={"api_key": FAKE_KEY, "sec_key": FAKE_SEC, "ca_passwd": "", "ca_path": ""})
    mod = tmp_path / "fakereport.py"
    mod.write_text(
        "from shioaji_wizard.sjenv import Report\n"
        f"Report().add('A1 模擬環境登入', 'FAIL', 'key {FAKE_KEY} not exist; id A123456789')\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("PYTHONPATH", str(tmp_path))
    job = server.Job("a")
    server._run_job(job, "fakereport", [])
    reason = server.SESSION["A1 模擬環境登入"]["reason"]
    assert FAKE_KEY not in reason and "A123456789" not in reason
    assert "[已遮罩]" in reason and "A12*****89" in reason
    assert FAKE_KEY not in client.get("/api/summary").text
    assert FAKE_KEY not in client.get("/api/job").text  # 輪詢端點回的 job.report 也必須是遮罩版
    assert FAKE_KEY not in client.get("/api/state").text


def test_post_env_rejects_newlines(client, tmp_path: Path):
    """值含換行會在 .env 注入第二個鍵（繞過金鑰鎖定），一律 400 且不寫檔。"""
    r = client.post(
        "/api/env",
        json={
            "api_key": FAKE_KEY,
            "sec_key": FAKE_SEC,
            "ca_passwd": "",
            "ca_path": "C:/a.pfx\nSJ_API_KEY=INJECTED",
        },
    )
    assert r.status_code == 400
    assert not (tmp_path / ".env").exists() or "INJECTED" not in (tmp_path / ".env").read_text(
        encoding="utf-8"
    )
    r = client.post(
        "/api/env",
        json={"api_key": FAKE_KEY, "sec_key": "x\r\nSJ_API_KEY=INJECTED", "ca_passwd": "", "ca_path": ""},
    )
    assert r.status_code == 400


def test_redact_masks_person_id_even_when_short_secret_is_substring(env_mods):
    """憑證密碼 1234 是身分證字號 A123456789 的子字串：先遮身分證再換密碼，中段不能露出。"""
    _, server = env_mods
    out = server._redact_text("id A123456789 pw 1234", {"1234"})
    assert "1234" not in out and "56789" not in out and "A123456789" not in out
    assert "B28*****21" in server._redact_text("x B287654321 y", {"1234"})


def test_redact_secret_containing_person_id_fragment_is_fully_masked(env_mods):
    """密碼本身含身分證格式片段（pw-A123456789-x）：整段 [已遮罩]，前後段不能露；另一個純身分證只打碼中段。"""
    _, server = env_mods
    out = server._redact_text("pw=pw-A123456789-x id=B287654321", {"pw-A123456789-x"})
    assert out == "pw=[已遮罩] id=B28*****21"
    out = server._redact_text("k=" + FAKE_KEY + " A123456789 " + FAKE_SEC, {FAKE_KEY, FAKE_SEC})
    assert FAKE_KEY not in out and FAKE_SEC not in out and "A123456789" not in out


def test_api_responses_are_no_store(client):
    assert client.get("/api/state").headers["Cache-Control"] == "no-store"


def test_redact_partially_overlapping_secrets_are_unioned(env_mods):
    """兩個秘密在原文上部分重疊（互不包含）：取區間聯集整段遮罩，不能只遮前一個而露出後一個尾段。"""
    _, server = env_mods
    out = server._redact_text("x ABCDEFGHIJKLMN y", {"ABCDEFGH", "HIJKLMN"})
    assert out == "x [已遮罩] y"


def test_redact_is_near_linear_on_repetitive_text(env_mods):
    """短秘密（aaaa）在重複文字上：匯出 log 可達數百 KB，遮罩不能是平方時間。"""
    import time as _t

    _, server = env_mods
    text = "a" * 400_000
    t0 = _t.perf_counter()
    out = server._redact_text(text, {"aaaa"})
    assert "aaaa" not in out
    assert _t.perf_counter() - t0 < 2.0


def test_post_env_rejects_unicode_line_separators(client, tmp_path: Path):
    """parse_env_text 用 splitlines()：U+2028／U+2029／NEL／VT／FF 也算分行，守衛必須用同一套判準。"""
    for code in (0x2028, 0x2029, 0x85, 0x0B, 0x0C):
        body = {
            "api_key": "",
            "sec_key": "",
            "ca_passwd": "",
            "ca_path": "C:/x" + chr(code) + "SJ_API_KEY=INJECTED",
        }
        r = client.post("/api/env", json=body)
        assert r.status_code == 400, hex(code)
    env = tmp_path / ".env"
    assert not env.exists() or "INJECTED" not in env.read_text(encoding="utf-8")


# ---------------------------------------------------------------- （甲）每次執行先清空檢核單
def _wait_job_done(server, timeout: float = 2.0) -> None:
    deadline = time.monotonic() + timeout
    while server._job is not None and server._job.running and time.monotonic() < deadline:
        time.sleep(0.01)


def test_api_run_clears_session_immediately(env_mods, client, monkeypatch: pytest.MonkeyPatch):
    """/api/run 成功啟動 job 時清空 SESSION：回應一回來，殘留的上一輪項目就要不見。"""
    _, server = env_mods
    client.post("/api/env", json={"api_key": FAKE_KEY, "sec_key": FAKE_SEC, "ca_path": ""})
    server.record("B1 正式環境登入", "PASS")  # 模擬殘留的上一輪結果
    monkeypatch.setattr(server, "_run_job", lambda job, module, args: None)  # 別真的跑子行程
    r = client.post("/api/run", json={"kind": "a"})
    assert r.status_code == 200
    assert "B1 正式環境登入" not in server.SESSION
    names = [i["name"] for i in client.get("/api/state").json()["summary"]]
    assert "B1 正式環境登入" not in names
    assert any(n.startswith("E1") for n in names)  # E1～E3 由 inspect_env() 當場重算，會自然回來


def test_run_clears_previous_session_and_lock_reflects_latest_login(
    env_mods, client, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
):
    """先 A 過、再跑 B 且 B1 失敗 → 不再鎖定（keys_verified 自然只看最近一次登入結果），
    且上一輪的 A1 不會殘留在這一輪的檢核單裡。"""
    _, server = env_mods
    (tmp_path / "Sinopac.pfx").write_bytes(b"pfx")
    client.post("/api/env", json={"api_key": FAKE_KEY, "sec_key": FAKE_SEC, "ca_passwd": "pw", "ca_path": ""})

    def fake_a(job, module, args):
        server.record("A1 模擬環境登入", "PASS")
        server._STALE_FIELDS.difference_update(server._COVERS["a"])  # 比照 _run_job：測完就不再是「未知」
        job.rc = 0

    monkeypatch.setattr(server, "_run_job", fake_a)
    assert client.post("/api/run", json={"kind": "a"}).status_code == 200
    _wait_job_done(server)
    assert client.get("/api/state").json()["keys_locked"] is True

    def fake_b(job, module, args):
        server.record("B1 正式環境登入", "FAIL", "憑證密碼錯")
        server._STALE_FIELDS.difference_update(server._COVERS["b"])
        job.rc = 1

    monkeypatch.setattr(server, "_run_job", fake_b)
    assert client.post("/api/run", json={"kind": "b"}).status_code == 200
    _wait_job_done(server)
    state = client.get("/api/state").json()
    names = [i["name"] for i in state["summary"]]
    assert state["keys_locked"] is False
    assert "A1 模擬環境登入" not in names  # 上一輪的結果沒有殘留
    assert any(n.startswith("B1") for n in names)


# ---------------------------------------------------------------- （乙）多帳號：eLeader profile 切換
def test_profiles_returned_even_when_pfx_exists(client_ekey, ekey_tree):
    """核心 bug 修復：偵測到的憑證套用一次後仍要常駐在清單裡，不能消失。"""
    _base, paths = ekey_tree
    target = paths["A123456789"]
    client_ekey.post("/api/profile", json={"pfx": str(target)})
    client_ekey.post(
        "/api/env",
        json={"api_key": FAKE_KEY, "sec_key": FAKE_SEC, "ca_passwd": "pw", "ca_path": str(target)},
    )
    state = client_ekey.get("/api/state").json()
    assert state["env"]["pfx_exists"] is True
    assert len(state["profiles"]) == 2
    active = next(p for p in state["profiles"] if p["pfx"] == str(target))
    assert active["active"] is True
    assert active["configured"] is True


def test_switch_profile_with_existing_env_only_rewrites_ca_path(env_mods_ekey, client_ekey, ekey_tree):
    """切到已有 .env 的 profile：只改 SJ_CA_PATH，其他鍵原封不動（等同沿用既有金鑰），
    回應不含任何金鑰明文。"""
    sjenv, server = env_mods_ekey
    _base, paths = ekey_tree
    profile_a_dir = paths["A123456789"].parent
    (profile_a_dir / ".env").write_text(
        f"SJ_API_KEY={FAKE_KEY}\nSJ_SEC_KEY={FAKE_SEC}\nSJ_CA_PASSWD=oldpw\nSJ_CA_PATH=C:/somewhere/old.pfx\n",
        encoding="utf-8",
    )
    r = client_ekey.post("/api/profile", json={"pfx": str(paths["A123456789"])})
    assert r.status_code == 200
    body = r.json()
    assert body["pending_ca_path"] is None
    assert FAKE_KEY not in r.text and FAKE_SEC not in r.text and "oldpw" not in r.text
    values, _ = sjenv.parse_env_text((profile_a_dir / ".env").read_text(encoding="utf-8"))
    assert values["SJ_API_KEY"] == FAKE_KEY
    assert values["SJ_SEC_KEY"] == FAKE_SEC
    assert values["SJ_CA_PASSWD"] == "oldpw"  # noqa: S105 — 測試用假密碼
    assert Path(values["SJ_CA_PATH"]) == paths["A123456789"]
    assert server.env_path() == profile_a_dir / ".env"
    assert set(server._STALE_FIELDS) == {"api", "sec", "pwd", "path"}
    # 切換清空檢核單：只剩 inspect_env() 當場重算的 E1～E3，沒有殘留任何 A／B 測試結果
    assert not any(name.startswith(("A", "B")) for name in server.SESSION)


def test_switch_profile_without_env_returns_pending_and_creates_nothing(client_ekey, ekey_tree):
    _base, paths = ekey_tree
    target = paths["B223456789"]
    r = client_ekey.post("/api/profile", json={"pfx": str(target)})
    assert r.status_code == 200
    body = r.json()
    assert body["pending_ca_path"] == str(target)
    assert not (target.parent / ".env").exists()
    assert body["env"]["exists"] is False


def test_env_save_after_pending_switch_lands_in_profile_dir_not_root(client_ekey, ekey_tree, tmp_path: Path):
    _base, paths = ekey_tree
    target = paths["A123456789"]
    pending = client_ekey.post("/api/profile", json={"pfx": str(target)}).json()["pending_ca_path"]
    assert pending == str(target)
    r = client_ekey.post(
        "/api/env",
        json={"api_key": FAKE_KEY, "sec_key": FAKE_SEC, "ca_passwd": "A123456789", "ca_path": pending},
    )
    assert r.status_code == 200
    assert (target.parent / ".env").is_file()
    assert not (tmp_path / ".env").exists()


def test_switch_profile_rejects_path_outside_whitelist(client_ekey, tmp_path: Path):
    r = client_ekey.post("/api/profile", json={"pfx": str(tmp_path / "not-a-real-candidate.pfx")})
    assert r.status_code == 400


def test_switch_profile_conflicts_with_running_job(env_mods_ekey, client_ekey):
    _, server = env_mods_ekey
    server._job = server.Job("a")  # rc 預設 None → running
    r = client_ekey.post("/api/profile", json={"pfx": ""})
    assert r.status_code == 409


def test_root_env_available_flag_toggles_with_profile_switch(client_ekey, ekey_tree):
    _base, paths = ekey_tree
    client_ekey.post("/api/env", json={"api_key": FAKE_KEY, "sec_key": FAKE_SEC, "ca_path": ""})
    assert client_ekey.get("/api/state").json()["root_env_available"] is False
    client_ekey.post("/api/profile", json={"pfx": str(paths["A123456789"])})
    assert client_ekey.get("/api/state").json()["root_env_available"] is True
    client_ekey.post("/api/profile", json={"pfx": ""})
    assert client_ekey.get("/api/state").json()["root_env_available"] is False


def test_active_profile_persists_across_reload_and_falls_back_when_invalid(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, ekey_tree: tuple[Path, dict[str, Path]]
):
    base, paths = ekey_tree
    target = paths["A123456789"]
    _, server = _reload_modules(monkeypatch, tmp_path, ekey_base=base)
    client = TestClient(server.app, base_url="http://127.0.0.1")
    client.post("/api/profile", json={"pfx": str(target)})
    active_file = tmp_path / ".runtime" / "active-profile"
    assert active_file.is_file()
    assert Path(active_file.read_text(encoding="utf-8").strip()) == target.parent

    # 重新載入（模擬重啟）：仍讀到同一個 profile
    _, server2 = _reload_modules(monkeypatch, tmp_path, ekey_base=base)
    assert server2._profile_dir == target.parent

    # 憑證資料夾被拔掉後再重啟：候選消失，不再信任舊紀錄，退回 ROOT
    import shutil

    shutil.rmtree(base)
    sjenv3, server3 = _reload_modules(monkeypatch, tmp_path, ekey_base=base)
    assert server3._profile_dir == sjenv3.ROOT


def test_runtime_dir_never_created_inside_ekey_tree(client_ekey, ekey_tree, tmp_path: Path):
    _base, paths = ekey_tree
    client_ekey.post("/api/profile", json={"pfx": str(paths["A123456789"])})
    client_ekey.post(
        "/api/env",
        json={
            "api_key": FAKE_KEY,
            "sec_key": FAKE_SEC,
            "ca_path": str(paths["A123456789"]),
        },
    )
    assert not (paths["A123456789"].parent / ".runtime").exists()
    assert (tmp_path / ".runtime").is_dir()


def test_run_job_env_has_profile_dir_and_root_env_dir(
    env_mods_ekey, client_ekey, ekey_tree, monkeypatch: pytest.MonkeyPatch
):
    """_run_job 傳給子行程的環境含正確 SJ_PROFILE_DIR，且 SJ_ENV_DIR 仍是 ROOT（子行程
    不必改：test_ca／test_sim_order 只認 sjenv.ENV_PATH，由 SJ_PROFILE_DIR 決定）。"""
    sjenv, server = env_mods_ekey
    _base, paths = ekey_tree
    target = paths["A123456789"]
    client_ekey.post("/api/profile", json={"pfx": str(target)})
    client_ekey.post(
        "/api/env",
        json={"api_key": FAKE_KEY, "sec_key": FAKE_SEC, "ca_passwd": "pw", "ca_path": str(target)},
    )
    captured: dict[str, Any] = {}

    def fake_popen(cmd, **kwargs):
        captured["env"] = kwargs.get("env")
        raise OSError("測試用：不要真的啟動子行程")

    monkeypatch.setattr(server.subprocess, "Popen", fake_popen)
    job = server.Job("a")
    server._run_job(job, "shioaji_wizard.test_sim_order", [])
    assert captured["env"]["SJ_ENV_DIR"] == str(sjenv.ROOT)
    assert Path(captured["env"]["SJ_PROFILE_DIR"]) == target.parent


def test_env_write_failure_returns_error_and_does_not_fallback_to_root(
    client, env_mods, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    """S\\ 目錄寫入失敗要回明確錯誤，絕不能偷偷改存到 ROOT。這裡用 monkeypatch 模擬
    OSError（真的把 Windows 目錄設成唯讀在測試環境不可靠）。"""
    _, server = env_mods

    def boom(updates: dict[str, str]) -> None:
        raise OSError("permission denied（模擬）")

    monkeypatch.setattr(server, "write_env_values", boom)
    r = client.post("/api/env", json={"api_key": FAKE_KEY, "sec_key": FAKE_SEC, "ca_path": ""})
    assert r.status_code == 500
    assert "寫入" in r.json()["detail"]
    assert not (tmp_path / ".env").exists()


# ---------------------------------------------------------------- 升級遷移漏洞＋切換回滾
def test_migrates_root_env_bytes_when_ca_path_points_to_ekey_candidate(
    env_mods, client, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    """(a) 重現失效鏈：ROOT/.env 有金鑰、SJ_CA_PATH 已指到 eLeader 候選（舊版單人使用者
    的既有設定）→ GET /api/state 後整份原樣搬到 S\\.env，profile 切過去，SESSION／
    stale／keys_locked 不受影響（金鑰沒變，只是搬家），回應不含金鑰明文。"""
    _, server = env_mods
    candidate = tmp_path / "ekey" / "551" / "A123456789" / "S" / "Sinopac.pfx"
    candidate.parent.mkdir(parents=True)
    candidate.write_bytes(b"pfx")
    monkeypatch.setattr(server, "find_eleader_pfx", lambda: [str(candidate)])
    root_env_text = (
        f"# 舊版設定\nSJ_API_KEY={FAKE_KEY}\nSJ_SEC_KEY={FAKE_SEC}\nSJ_CA_PASSWD=pw\nSJ_CA_PATH={candidate}\n"
    )
    (tmp_path / ".env").write_text(root_env_text, encoding="utf-8")
    server.record("A1 模擬環境登入", "PASS")  # 模擬升級前已經驗證過、金鑰鎖定
    server._STALE_FIELDS.clear()

    r = client.get("/api/state")
    st = r.json()

    assert st["profile_migrated"] is True
    assert Path(st["root"]) == candidate.parent
    assert (candidate.parent / ".env").read_bytes() == (tmp_path / ".env").read_bytes()
    assert st["keys_locked"] is True  # 鎖定狀態延續，不是新帳號
    assert "A1 模擬環境登入" in server.SESSION  # 不清 SESSION
    assert server._STALE_FIELDS == set()  # 不標 stale
    assert FAKE_KEY not in r.text and FAKE_SEC not in r.text
    assert (tmp_path / ".env").is_file()  # ROOT 的舊檔留著不刪


def test_env_save_after_migration_keeps_keys(
    env_mods, client, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    """(b) 遷移後只送 ca_passwd 儲存 → S\\.env 的兩把金鑰仍在（不會因為改密碼被清空）。"""
    sjenv, server = env_mods
    candidate = tmp_path / "ekey" / "551" / "A123456789" / "S" / "Sinopac.pfx"
    candidate.parent.mkdir(parents=True)
    candidate.write_bytes(b"pfx")
    monkeypatch.setattr(server, "find_eleader_pfx", lambda: [str(candidate)])
    (tmp_path / ".env").write_text(
        f"SJ_API_KEY={FAKE_KEY}\nSJ_SEC_KEY={FAKE_SEC}\nSJ_CA_PASSWD=pw\nSJ_CA_PATH={candidate}\n",
        encoding="utf-8",
    )
    client.get("/api/state")  # 觸發遷移

    r = client.post("/api/env", json={"ca_passwd": "newpw", "ca_path": str(candidate)})
    assert r.status_code == 200
    values, _ = sjenv.parse_env_text((candidate.parent / ".env").read_text(encoding="utf-8"))
    assert values["SJ_API_KEY"] == FAKE_KEY
    assert values["SJ_SEC_KEY"] == FAKE_SEC
    assert values["SJ_CA_PASSWD"] == "newpw"  # noqa: S105 — 測試用假密碼


def test_env_save_from_root_with_matching_ca_path_falls_back_to_root_keys(
    env_mods, client, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    """/api/env 自己的保底（不靠先跑過 /api/state 的遷移）：畫面上 ca_path 欄位還是
    ROOT 原本就指到的那張候選憑證，使用者只改了密碼存檔 → 空白欄位沿用 ROOT 原值，
    等同「先遷移再寫」，金鑰不會憑空消失。"""
    sjenv, server = env_mods
    candidate = tmp_path / "ekey" / "551" / "A123456789" / "S" / "Sinopac.pfx"
    candidate.parent.mkdir(parents=True)
    candidate.write_bytes(b"pfx")
    monkeypatch.setattr(server, "find_eleader_pfx", lambda: [str(candidate)])
    (tmp_path / ".env").write_text(
        f"SJ_API_KEY={FAKE_KEY}\nSJ_SEC_KEY={FAKE_SEC}\nSJ_CA_PASSWD=oldpw\nSJ_CA_PATH={candidate}\n",
        encoding="utf-8",
    )

    r = client.post("/api/env", json={"ca_passwd": "newpw", "ca_path": str(candidate)})
    assert r.status_code == 200
    values, _ = sjenv.parse_env_text((candidate.parent / ".env").read_text(encoding="utf-8"))
    assert values["SJ_API_KEY"] == FAKE_KEY
    assert values["SJ_SEC_KEY"] == FAKE_SEC
    assert values["SJ_CA_PASSWD"] == "newpw"  # noqa: S105
    assert (tmp_path / ".env").is_file()


def test_migration_skipped_when_root_ca_path_is_not_an_ekey_candidate(
    env_mods, client, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    """(c) ROOT 指向非 eLeader 憑證 → 不遷移，留在 ROOT。"""
    _, server = env_mods
    candidate = tmp_path / "ekey" / "551" / "A123456789" / "S" / "Sinopac.pfx"
    candidate.parent.mkdir(parents=True)
    candidate.write_bytes(b"pfx")
    monkeypatch.setattr(server, "find_eleader_pfx", lambda: [str(candidate)])
    other = tmp_path / "certs" / "other.pfx"
    other.parent.mkdir()
    other.write_bytes(b"x")
    (tmp_path / ".env").write_text(
        f"SJ_API_KEY={FAKE_KEY}\nSJ_SEC_KEY={FAKE_SEC}\nSJ_CA_PATH={other}\n", encoding="utf-8"
    )

    st = client.get("/api/state").json()

    assert not st["profile_migrated"]
    assert Path(st["root"]) == tmp_path.resolve()
    assert not (candidate.parent / ".env").exists()


def test_migration_skipped_when_target_already_has_env(
    env_mods, client, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    """(d) 候選目錄已有 .env → 不覆蓋，也不遷移過去。"""
    _, server = env_mods
    candidate = tmp_path / "ekey" / "551" / "A123456789" / "S" / "Sinopac.pfx"
    candidate.parent.mkdir(parents=True)
    candidate.write_bytes(b"pfx")
    monkeypatch.setattr(server, "find_eleader_pfx", lambda: [str(candidate)])
    (candidate.parent / ".env").write_text("SJ_API_KEY=existing\nSJ_SEC_KEY=existing2\n", encoding="utf-8")
    (tmp_path / ".env").write_text(
        f"SJ_API_KEY={FAKE_KEY}\nSJ_SEC_KEY={FAKE_SEC}\nSJ_CA_PATH={candidate}\n", encoding="utf-8"
    )

    st = client.get("/api/state").json()

    assert not st["profile_migrated"]
    assert Path(st["root"]) == tmp_path.resolve()  # 沒被搶走
    text = (candidate.parent / ".env").read_text(encoding="utf-8")
    assert "existing" in text and FAKE_KEY not in text


def test_migration_copy_failure_stays_on_root_and_leaves_no_partial_file(
    env_mods, client, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    """(e) 複製失敗（模擬 PermissionError）→ 留在 ROOT，候選目錄沒有殘檔。"""
    _, server = env_mods
    candidate = tmp_path / "ekey" / "551" / "A123456789" / "S" / "Sinopac.pfx"
    candidate.parent.mkdir(parents=True)
    candidate.write_bytes(b"pfx")
    monkeypatch.setattr(server, "find_eleader_pfx", lambda: [str(candidate)])
    (tmp_path / ".env").write_text(
        f"SJ_API_KEY={FAKE_KEY}\nSJ_SEC_KEY={FAKE_SEC}\nSJ_CA_PATH={candidate}\n", encoding="utf-8"
    )

    def boom(src, dst):
        raise PermissionError("模擬：無法寫入")

    monkeypatch.setattr(server, "_copy_env_bytes", boom)

    st = client.get("/api/state").json()

    assert not st["profile_migrated"]
    assert Path(st["root"]) == tmp_path.resolve()
    assert not (candidate.parent / ".env").exists()


def test_env_save_from_profile_a_to_profile_b_does_not_carry_keys(
    env_mods, client, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    """(f) 從帳號 A 存一個指向帳號 B 的 ca_path：A 的金鑰不能被帶到 B（migrating_from_root
    只在「從 ROOT 切走、且 ROOT 原本就指這張」才成立，帳號互切不算）。"""
    sjenv, server = env_mods
    a = tmp_path / "ekey" / "551" / "A123456789" / "S" / "Sinopac.pfx"
    b = tmp_path / "ekey" / "551" / "B223456789" / "S" / "Sinopac.pfx"
    a.parent.mkdir(parents=True)
    a.write_bytes(b"pfx")
    b.parent.mkdir(parents=True)
    b.write_bytes(b"pfx")
    monkeypatch.setattr(server, "find_eleader_pfx", lambda: [str(a), str(b)])
    client.post("/api/profile", json={"pfx": str(a)})
    client.post(
        "/api/env", json={"api_key": FAKE_KEY, "sec_key": FAKE_SEC, "ca_passwd": "pwA", "ca_path": str(a)}
    )

    r = client.post("/api/env", json={"ca_passwd": "pwB", "ca_path": str(b)})
    assert r.status_code == 200
    assert (b.parent / ".env").is_file()
    values, _ = sjenv.parse_env_text((b.parent / ".env").read_text(encoding="utf-8"))
    assert values.get("SJ_API_KEY", "") == ""
    assert values.get("SJ_SEC_KEY", "") == ""
    assert values["SJ_CA_PASSWD"] == "pwB"  # noqa: S105


def test_profile_switch_write_failure_rolls_back(
    env_mods, client, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    """(g) 切換寫入失敗要回滾：_profile_dir、active-profile 檔、SESSION、_STALE_FIELDS
    全部退回切換前。"""
    _, server = env_mods
    a = tmp_path / "ekey" / "551" / "A123456789" / "S" / "Sinopac.pfx"
    a.parent.mkdir(parents=True)
    a.write_bytes(b"pfx")
    (a.parent / ".env").write_text("SJ_API_KEY=x\nSJ_SEC_KEY=y\n", encoding="utf-8")
    monkeypatch.setattr(server, "find_eleader_pfx", lambda: [str(a)])
    server.record("Z leftover", "PASS")
    before_stale = set(server._STALE_FIELDS)

    def boom(updates):
        raise PermissionError("模擬：寫入失敗")

    monkeypatch.setattr(server, "write_env_values", boom)

    r = client.post("/api/profile", json={"pfx": str(a)})

    assert r.status_code == 500
    assert server._profile_dir == server.ROOT
    assert not (tmp_path / ".runtime" / "active-profile").exists()
    assert "Z leftover" in server.SESSION
    assert server._STALE_FIELDS == before_stale


def test_redact_masks_resident_certificate_numbers(env_mods):
    """外來人口統一證號（新式第二碼 8／9、舊式 A–D）也要遮罩，不只本國身分證。"""
    _sjenv, server = env_mods
    out = server._redact_text("person_id=A800000014 old=AC01234567 tw=A123456789", {})
    assert "A800000014" not in out and "AC01234567" not in out and "A123456789" not in out
    assert "A80*****14" in out and "AC0*****67" in out and "A12*****89" in out


def _fail_persist(*_a, **_k):
    raise PermissionError("runtime not writable")


def test_profile_switch_fails_loudly_when_active_profile_cannot_be_persisted(
    env_mods_ekey, client_ekey, ekey_tree, monkeypatch: pytest.MonkeyPatch
):
    """記不住的切換下次啟動會默默回到 ROOT（＝用錯帳號）：持久化失敗要 500 且完全不切。"""
    _, server = env_mods_ekey
    _, paths = ekey_tree
    before = server._profile_dir
    server.record("A1 模擬環境登入", "PASS")
    monkeypatch.setattr(server, "_persist_active_profile", _fail_persist)

    r = client_ekey.post("/api/profile", json={"pfx": str(paths["A123456789"])})

    assert r.status_code == 500
    assert server._profile_dir == before
    assert "A1 模擬環境登入" in server.SESSION  # 沒切成功就不該清檢核單


def test_migration_is_abandoned_when_active_profile_cannot_be_persisted(
    env_mods, client, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    _, server = env_mods
    candidate = tmp_path / "ekey" / "551" / "A123456789" / "S" / "Sinopac.pfx"
    candidate.parent.mkdir(parents=True)
    candidate.write_bytes(b"pfx")
    monkeypatch.setattr(server, "find_eleader_pfx", lambda: [str(candidate)])
    (tmp_path / ".env").write_text(
        f"SJ_API_KEY={FAKE_KEY}\nSJ_SEC_KEY={FAKE_SEC}\nSJ_CA_PASSWD=pw\nSJ_CA_PATH={candidate}\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(server, "_persist_active_profile", _fail_persist)

    st = client.get("/api/state").json()

    assert st["profile_migrated"] is False
    assert server._profile_dir == tmp_path.resolve()
    assert not (candidate.parent / ".env").exists()  # 不留第二份含金鑰的檔


def test_active_profile_write_is_atomic_and_keeps_old_record_on_failure(
    env_mods, tmp_path: Path, monkeypatch
):
    """寫到一半失敗不可把原本的 active-profile 截斷：走暫存檔＋os.replace，失敗不留 .tmp。"""
    _, server = env_mods
    server._persist_active_profile(tmp_path / "old")
    assert server._ACTIVE_PROFILE_FILE.read_text(encoding="utf-8") == str(tmp_path / "old")

    def fail_replace(*_a, **_k):
        raise PermissionError("locked")

    monkeypatch.setattr(server.os, "replace", fail_replace)
    with pytest.raises(PermissionError):
        server._persist_active_profile(tmp_path / "new")

    assert server._ACTIVE_PROFILE_FILE.read_text(encoding="utf-8") == str(tmp_path / "old")
    assert not list(server._ACTIVE_PROFILE_FILE.parent.glob("*.tmp"))


def test_env_write_failure_leaves_existing_key_file_byte_identical(
    env_mods, client, tmp_path: Path, monkeypatch
):
    """.env 是金鑰檔：寫入中途失敗不可把原檔截斷。走暫存檔＋os.replace，失敗時原檔位元組不變。"""
    _, server = env_mods
    env_file = tmp_path / ".env"
    original = (
        f"# 我的註解\nSJ_API_KEY={FAKE_KEY}\nSJ_SEC_KEY={FAKE_SEC}\nSJ_CA_PASSWD=pw\nSJ_CA_PATH=\n".encode()
    )
    env_file.write_bytes(original)

    def fail_replace(*_a, **_k):
        raise PermissionError("antivirus lock")

    monkeypatch.setattr(server.os, "replace", fail_replace)
    r = client.post("/api/env", json={"ca_passwd": "newpw"})

    assert r.status_code == 500
    assert env_file.read_bytes() == original
    assert not list(tmp_path.glob(".env.*.tmp"))


def test_env_save_commits_active_profile_last_and_restores_memory_when_commit_fails(
    env_mods_ekey, client_ekey, ekey_tree, tmp_path: Path, monkeypatch
):
    """順序＝先寫 .env、最後才提交 active-profile：提交失敗 → 500、記憶體回到原帳號、
    磁碟上的 active-profile 從頭到尾沒被動過（重啟後不會跑到另一個帳號）。"""
    _, server = env_mods_ekey
    _, paths = ekey_tree
    before_dir = server._profile_dir
    had_record = server._ACTIVE_PROFILE_FILE.is_file()
    monkeypatch.setattr(server, "_persist_active_profile", _fail_persist)

    r = client_ekey.post(
        "/api/env", json={"api_key": FAKE_KEY, "sec_key": FAKE_SEC, "ca_path": str(paths["A123456789"])}
    )

    assert r.status_code == 500
    assert server._profile_dir == before_dir
    assert server._ACTIVE_PROFILE_FILE.is_file() == had_record
    assert not (tmp_path / ".env").exists()  # 沒有偷偷改存到程式資料夾


def test_failed_commit_leaves_target_profile_env_byte_identical(
    env_mods_ekey, client_ekey, ekey_tree, monkeypatch
):
    """回 500 的儲存／切換不可留下改過的目標 .env：active-profile 提交失敗 → 目標檔還原成原位元組；
    原本沒有 .env 的目標則不留新檔。"""
    _, server = env_mods_ekey
    _, paths = ekey_tree
    b_env = paths["B223456789"].parent / ".env"
    original = f"SJ_API_KEY={FAKE_KEY}\nSJ_SEC_KEY={FAKE_SEC}\nSJ_CA_PASSWD=oldpw\nSJ_CA_PATH=\n".encode()
    b_env.write_bytes(original)
    monkeypatch.setattr(server, "_persist_active_profile", _fail_persist)

    r1 = client_ekey.post("/api/env", json={"ca_passwd": "newpw", "ca_path": str(paths["B223456789"])})
    r2 = client_ekey.post("/api/profile", json={"pfx": str(paths["B223456789"])})
    r3 = client_ekey.post(
        "/api/env", json={"api_key": FAKE_KEY, "sec_key": FAKE_SEC, "ca_path": str(paths["A123456789"])}
    )

    assert (r1.status_code, r2.status_code, r3.status_code) == (500, 500, 500)
    assert b_env.read_bytes() == original
    assert not (paths["A123456789"].parent / ".env").exists()


def test_ensure_env_keys_is_atomic(env_mods, tmp_path: Path, monkeypatch):
    sjenv, _ = env_mods
    env_file = tmp_path / ".env"
    original = f"SJ_API_KEY={FAKE_KEY}\nSJ_SEC_KEY={FAKE_SEC}\n".encode()
    env_file.write_bytes(original)

    def fail_replace(*_a, **_k):
        raise PermissionError("locked")

    monkeypatch.setattr(sjenv.os, "replace", fail_replace)
    with pytest.raises(PermissionError):
        sjenv.ensure_env_keys(env_file, {"SJ_CA_PASSWD": ""})

    assert env_file.read_bytes() == original
    assert not list(tmp_path.glob(".env.*.tmp"))


def test_unreadable_target_env_changes_nothing(env_mods_ekey, client_ekey, ekey_tree, monkeypatch):
    """目標帳號的 .env 暫時讀不到（ACL／防毒鎖定）：兩個入口都要在動任何狀態之前就失敗，
    profile、檢核單、stale、active-profile 完全不變——不可以停在半切換的帳號上。"""
    _, server = env_mods_ekey
    _, paths = ekey_tree
    b_env = paths["B223456789"].parent / ".env"
    b_env.write_text(f"SJ_API_KEY={FAKE_KEY}\nSJ_SEC_KEY={FAKE_SEC}\n", encoding="utf-8")
    server.record("A1 模擬環境登入", "PASS")
    server._STALE_FIELDS.clear()
    before_dir, had_record = server._profile_dir, server._ACTIVE_PROFILE_FILE.is_file()
    real_read_bytes = Path.read_bytes

    def locked(self: Path) -> bytes:
        if self == b_env:
            raise PermissionError("locked by antivirus")
        return real_read_bytes(self)

    monkeypatch.setattr(Path, "read_bytes", locked)

    r1 = client_ekey.post("/api/profile", json={"pfx": str(paths["B223456789"])})
    r2 = client_ekey.post("/api/env", json={"ca_passwd": "pw", "ca_path": str(paths["B223456789"])})

    assert (r1.status_code, r2.status_code) == (500, 500)
    assert server._profile_dir == before_dir
    assert "A1 模擬環境登入" in server.SESSION
    assert server._STALE_FIELDS == set()
    assert server._ACTIVE_PROFILE_FILE.is_file() == had_record


def test_transient_read_failure_during_write_never_rebuilds_key_file_as_empty(
    env_mods_ekey, client_ekey, ekey_tree, monkeypatch
):
    """暫時讀不到 ≠ 空檔：write_env_values 讀既有 .env 失敗（OSError）要上拋，不可重建成只剩
    這次欄位的檔（那會把金鑰整份洗掉）。快照用 read_bytes、寫入用 read_text——只讓後者失敗。"""
    _, server = env_mods_ekey
    _, paths = ekey_tree
    b_env = paths["B223456789"].parent / ".env"
    original = f"SJ_API_KEY={FAKE_KEY}\nSJ_SEC_KEY={FAKE_SEC}\nSJ_CA_PASSWD=oldpw\n".encode()
    b_env.write_bytes(original)
    before_dir = server._profile_dir
    real_read_text = Path.read_text

    def flaky(self: Path, *a, **k) -> str:
        if self == b_env:
            raise PermissionError("locked by antivirus")
        return real_read_text(self, *a, **k)

    monkeypatch.setattr(Path, "read_text", flaky)

    r1 = client_ekey.post("/api/profile", json={"pfx": str(paths["B223456789"])})
    r2 = client_ekey.post("/api/env", json={"ca_passwd": "pw", "ca_path": str(paths["B223456789"])})

    assert (r1.status_code, r2.status_code) == (500, 500)
    assert b_env.read_bytes() == original
    assert server._profile_dir == before_dir


def test_switching_to_profile_with_undecodable_env_does_not_rebuild_it(client_ekey, ekey_tree):
    """編碼壞掉的 .env：點一下切換帳號不可把它重建（金鑰會消失）；原位元組保留，畫面回報編碼問題。"""
    _, paths = ekey_tree
    b_env = paths["B223456789"].parent / ".env"
    original = "SJ_API_KEY=金鑰\n".encode("big5")
    b_env.write_bytes(original)

    r = client_ekey.post("/api/profile", json={"pfx": str(paths["B223456789"])})

    assert r.status_code == 200
    assert b_env.read_bytes() == original
    assert r.json()["env"]["decode_error"]
