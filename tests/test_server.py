"""server／sjenv 的回歸測試：.env 解析與寫入、端點行為、白名單、匯出紀錄。不連永豐。"""

from __future__ import annotations

import importlib
import os
import sys
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

FAKE_KEY = "FAKEAPIKEY_1234567890abcdefghijklmnopqrstuvw"
FAKE_SEC = "FAKESECKEY_1234567890abcdefghijklmnopqrstuvw"


@pytest.fixture
def env_mods(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """以 tmp_path 當 ROOT 重新載入 sjenv／server（ROOT 在 import 時算好）。"""
    monkeypatch.setenv("SJ_ENV_DIR", str(tmp_path))
    for name in ("shioaji_wizard.server", "shioaji_wizard.sjenv"):
        sys.modules.pop(name, None)
    sjenv = importlib.import_module("shioaji_wizard.sjenv")
    server = importlib.import_module("shioaji_wizard.server")
    assert sjenv.ROOT == tmp_path.resolve()
    return sjenv, server


@pytest.fixture
def client(env_mods):
    _, server = env_mods
    return TestClient(server.app, base_url="http://127.0.0.1")  # guards 只放行本機 Host


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
