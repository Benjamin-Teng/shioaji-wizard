"""Shioaji 測試精靈的 FastAPI 後端（只綁 127.0.0.1，由 __main__ 啟動）。

端點：
  GET  /                 單頁前端
  GET  /api/state        .env 檢查（值遮罩）、工作資料夾、雲端同步警告、目前 job、總覽
  POST /api/env          寫入 .env（只改有送的欄位；其他既有內容不動）
  POST /api/profile      切換目前作用中的帳號（eLeader 憑證資料夾）或切回程式資料夾
  POST /api/browse       開系統檔案對話框選憑證（Windows：PowerShell OpenFileDialog）
  GET  /api/window       台灣時間＋測試時段＋（18–20 點）IP 檢查
  POST /api/run          啟動 A／B 測試（子行程），同一時間只跑一個
  GET  /api/job?since=N  輪詢：新的輸出行、是否結束、該次結果
  GET  /api/summary      本次所有測試的總覽（E／A／B 合併）
  POST /api/heartbeat    桌面殼的心跳（視窗關了、心跳停了就自動關）
"""

from __future__ import annotations

import contextlib
import json
import os
import re
import subprocess
import sys
import tempfile
import threading
import time
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse
from pydantic import BaseModel

from shioaji_wizard.guards import install_guards
from shioaji_wizard.shell import _POWERSHELL
from shioaji_wizard.sjenv import (
    DEFAULT_PFX,
    FAIL,
    OPTIONAL_KEYS,
    PASS,
    REQUIRED_KEYS,
    ROOT,
    RUNTIME,
    atomic_write_text,
    ensure_env_keys,
    ensure_runtime_dir,
    parse_env_text,
    pfx_path,
)

try:
    from importlib.metadata import version as _pkg_version

    APP_VERSION = _pkg_version("shioaji-wizard")
except Exception:  # noqa: BLE001 — 開發模式沒裝 wheel 時
    APP_VERSION = "dev"

STATIC = Path(__file__).resolve().parent / "static"
KNOWN_KEYS = REQUIRED_KEYS + OPTIONAL_KEYS
E_FORMAT = "E1 .env 格式／必填金鑰"
E_CA_PWD = "E2 憑證密碼已填（SJ_CA_PASSWD）"  # noqa: S105 — 是測試項目名稱，不是密碼
E_PFX = "E3 憑證檔存在（SJ_CA_PATH）"
ORDER = ["E1", "E2", "E3", "A1", "A2", "A3", "A4", "A5", "B1", "B2", "B3", "B4", "B5", "B6"]

app = FastAPI(title="Shioaji 測試精靈", docs_url=None, redoc_url=None)
install_guards(app)  # Host／Origin 防線在 app 建構時就掛上，任何入口（uvicorn 直跑、TestClient）都有

# ---------------------------------------------------------------- 狀態
SESSION: dict[str, dict[str, str]] = {}  # name -> {name,status,reason}
_lock = threading.Lock()
_last_heartbeat = time.monotonic()


class Job:
    def __init__(self, kind: str) -> None:
        self.kind = kind
        self.lines: list[str] = []
        self.report: list[dict[str, str]] = []
        self.rc: int | None = None
        self.started = time.time()
        self.proc: subprocess.Popen[str] | None = None  # 子行程；關窗／逾時時要能殺
        self.secrets: list[str] = []  # 啟動當下 .env 的金鑰值（即時輸出逐行遮罩用）
        self.timed_out = False

    @property
    def running(self) -> bool:
        return self.rc is None

    def kill(self) -> None:
        """強制結束子行程（best-effort）。"""
        p = self.proc
        if p is not None and p.poll() is None:
            try:
                p.kill()
            except OSError:
                pass


# 子行程輸出逾時：沒有新輸出超過 NO_OUTPUT_TIMEOUT 秒、或總時長超過 TOTAL_TIMEOUT 秒就殺掉
# （永豐無回應時不能讓 app 永遠卡在「執行中」——job 執行中是不關的，所以 job 本身必須有上限）。
NO_OUTPUT_TIMEOUT = 120.0
TOTAL_TIMEOUT = 600.0
_SEEN_SECRETS: set[str] = set()  # 本次 session 跑測試時用過的所有金鑰值（含後來改掉的舊值）
# 欄位「更新過、但還沒被涵蓋它的測試驗過」：前端對這些欄位不標 ✓／✗。
# A 涵蓋 api／sec；B 涵蓋 api／sec／pwd／path。跑完對應測試才從集合移除。
_STALE_FIELDS: set[str] = set()
_COVERS = {"a": {"api", "sec"}, "b": {"api", "sec", "pwd", "path"}}


_job: Job | None = None


def record(name: str, status: str, reason: str = "") -> None:
    SESSION[name] = {"name": name, "status": status, "reason": reason}


def summary_items() -> list[dict[str, str]]:
    def key(item: dict[str, str]) -> tuple[int, str]:
        code = item["name"][:2]
        return (ORDER.index(code) if code in ORDER else 99, item["name"])

    return sorted(SESSION.values(), key=key)


def heartbeat_age() -> float:
    return time.monotonic() - _last_heartbeat


# ---------------------------------------------------------------- 多帳號（eLeader 憑證資料夾）
# 永豐 eLeader 申請的憑證預設存放：C:\ekey\551\<身分證字號>\S\*.pfx。
# SJ_EKEY_BASE 可覆寫這個位置（QA 用假樹模擬，不動真的 C:\ekey）。
EKEY_BASE = Path(os.environ.get("SJ_EKEY_BASE") or r"C:\ekey\551")


def find_eleader_pfx(base: Path | None = None) -> list[str]:
    """列出 eLeader 預設位置的憑證檔（只有裝過 eLeader 的電腦才會有）；沒有就回空清單。"""
    base = base if base is not None else EKEY_BASE
    if sys.platform != "win32" or not base.is_dir():
        return []
    try:
        return sorted(str(p) for p in base.glob("*/S/*.pfx") if p.is_file())
    except OSError:
        return []


def _normalize_path(p: str) -> str:
    """路徑比較用正規化：統一斜線分隔字元、忽略大小寫（Windows 路徑不分大小寫、
    正反斜線皆可）。"""
    return str(Path(p)).lower()


def profile_dir_for(pfx: str) -> Path:
    """這個憑證檔屬於哪個 profile 目錄：在 find_eleader_pfx() 清單內就回它的父目錄
    （……\\<身分證字號>\\S），否則回 ROOT（沿用程式資料夾）。"""
    norm = _normalize_path(pfx)
    for c in find_eleader_pfx():
        if _normalize_path(c) == norm:
            return Path(c).parent
    return ROOT


_ACTIVE_PROFILE_FILE = RUNTIME / "active-profile"


def _read_active_profile() -> Path:
    """讀取持久化的目前 profile；只信任 ROOT 或目前偵測到的候選之父目錄，否則退回
    ROOT（避免舊紀錄指到已拔除的憑證資料夾，或被竄改成任意路徑）。"""
    if not _ACTIVE_PROFILE_FILE.is_file():
        return ROOT
    try:
        text = _ACTIVE_PROFILE_FILE.read_text(encoding="utf-8").strip()
    except OSError:
        return ROOT
    if not text:
        return ROOT
    candidate_dir = Path(text)
    if candidate_dir == ROOT:
        return ROOT
    valid_dirs = {Path(c).parent for c in find_eleader_pfx()}
    return candidate_dir if candidate_dir in valid_dirs else ROOT


def _persist_active_profile(path: Path) -> None:
    """寫不進去就往上拋（OSError）：記不住的切換下次啟動會默默回到 ROOT，等於用錯帳號，
    所以呼叫端一律「先持久化、成功才改記憶體狀態」。"""
    _write_active_profile_text(str(path))


def _write_active_profile_text(text: str) -> None:
    ensure_runtime_dir()
    atomic_write_text(_ACTIVE_PROFILE_FILE, text)


_profile_dir: Path = _read_active_profile()  # 目前作用中的 profile 目錄；.env 實際落在這裡


def env_path() -> Path:
    return _profile_dir / ".env"


def _snapshot_profile_state() -> tuple[Path, dict[str, dict[str, str]], set[str]]:
    """切換前拍照（只有記憶體狀態）：目前 profile 目錄、SESSION、_STALE_FIELDS。"""
    return _profile_dir, dict(SESSION), set(_STALE_FIELDS)


def _restore_profile_state(snapshot: tuple[Path, dict[str, dict[str, str]], set[str]]) -> None:
    """切換失敗時退回切換前。active-profile 一律「最後才提交」，失敗路徑上磁碟紀錄
    從沒被動過，所以這裡只需要還原記憶體——不存在「回滾持久化又失敗」的雙重故障。"""
    global _profile_dir
    _profile_dir, prev_session, prev_stale = snapshot
    SESSION.clear()
    SESSION.update(prev_session)
    _STALE_FIELDS.clear()
    _STALE_FIELDS.update(prev_stale)


def _enter_profile_dir(target_dir: Path) -> None:
    """只改記憶體狀態：換 profile、清空檢核單、四欄標為未知（新帳號一律視為未驗證，
    金鑰不沿用鎖定狀態）。呼叫端做完該做的 .env 寫入後，必須再呼叫
    ``_persist_active_profile`` 提交；任何一步 OSError 就 ``_restore_profile_state``。"""
    global _profile_dir
    _profile_dir = target_dir
    SESSION.clear()
    _STALE_FIELDS.clear()
    _STALE_FIELDS.update({"api", "sec", "pwd", "path"})


def _switch_profile_dir(target_dir: Path) -> None:
    """不需要寫 .env 的單純切換：失敗（OSError）→ 什麼都不變、往上拋。"""
    snapshot = _snapshot_profile_state()
    _enter_profile_dir(target_dir)
    try:
        _persist_active_profile(target_dir)
    except OSError:
        _restore_profile_state(snapshot)
        raise


def _decodes_as_env(data: bytes) -> bool:
    try:
        data.decode("utf-8-sig")
    except UnicodeDecodeError:
        return False
    return True


def _switch_to_pfx(pfx: str) -> str | None:
    """切到 pfx 所屬的 profile（沿用 profile_dir_for 的判斷）。目標目錄已有 .env→只
    把 SJ_CA_PATH 改寫為這個 pfx（其他鍵原封不動，等同沿用既有金鑰）；沒有 .env→
    不建立任何檔案，回傳 pending_ca_path 讓呼叫端／前端知道要等使用者按儲存。

    順序：先寫 .env、最後才提交 active-profile；任何一步 OSError → 還原記憶體再上拋。"""
    target_dir = profile_dir_for(pfx)
    # 目標檔快照要在動任何狀態之前拍：這一步讀取失敗（OSError）就是什麼都還沒變
    target_env = target_dir / ".env"
    target_before = target_env.read_bytes() if target_dir != ROOT and target_env.is_file() else None
    snapshot = _snapshot_profile_state()
    _enter_profile_dir(target_dir)
    try:
        pending = None
        if target_dir != ROOT:
            if target_before is None:
                pending = pfx
            elif _decodes_as_env(target_before):
                write_env_values({"SJ_CA_PATH": pfx})
            # 編碼壞掉的 .env：單純切換帳號不重建它（畫面會顯示編碼問題，使用者按儲存才重建）
        _persist_active_profile(target_dir)
    except OSError:
        if target_before is not None:  # 切換沒成立就不該留下改過的 .env
            with contextlib.suppress(OSError):
                target_env.write_bytes(target_before)
        _restore_profile_state(snapshot)
        raise
    return pending


# ---------------------------------------------------------------- .env
def _mask(v: str) -> str:
    if not v:
        return ""
    if len(v) <= 8:
        return "•" * len(v)
    return f"{v[:4]}…{v[-4:]}（{len(v)} 字）"


def read_env_text() -> str | None:
    p = env_path()
    if not p.is_file():
        return ""
    try:
        return p.read_text(encoding="utf-8-sig")
    except UnicodeDecodeError:
        return None
    except OSError:
        return None


def inspect_env() -> dict[str, Any]:
    """檢查目前 profile 的 .env：回傳遮罩後的值、問題清單、E1～E3，並把 E1～E3 記進
    SESSION。缺鍵自動補。"""
    p = env_path()
    text = read_env_text()
    if text is None:
        record(E_FORMAT, FAIL, ".env 不是 UTF-8 編碼，讀不出來（請在此畫面重新儲存一次，或刪除 .env 重建）")
        record(E_CA_PWD, FAIL, "無法讀取 .env")
        record(E_PFX, FAIL, "無法讀取 .env")
        return {
            "exists": True,
            "decode_error": True,
            "problems": [".env 不是 UTF-8 編碼，讀不出來"],
            "ok_a": False,
            "ok_b": False,
        }
    exists = p.is_file()
    if exists:
        ensure_env_keys(p, {"SJ_CA_PATH": DEFAULT_PFX.as_posix(), "SJ_CA_PASSWD": ""})
        text = read_env_text() or ""
    values, problems = parse_env_text(text)
    ok_a = True
    for k in REQUIRED_KEYS:
        v = values.get(k, "")
        if not v:
            problems.append(f"缺少必填 {k}（或值是空的）")
            ok_a = False
        elif len(v) < 20:
            problems.append(f"{k} 只有 {len(v)} 個字，看起來不像完整金鑰（永豐目前是 44 字），請確認有貼完整")
    for k in list(values):
        if k not in KNOWN_KEYS:
            if k.upper() in KNOWN_KEYS:
                problems.append(f"{k} 大小寫不對，應為 {k.upper()}（程式與 shioaji server 都只認大寫）")
                if k.upper() in REQUIRED_KEYS:
                    ok_a = False
            else:
                problems.append(f"有不認識的設定 {k}（會被忽略）")
    if not exists:
        problems = ["還沒有 .env：請填入金鑰後按「儲存設定」"]
        ok_a = False
    record(E_FORMAT, PASS if ok_a else FAIL, "；".join(problems) if problems else "")

    has_pwd = bool(values.get("SJ_CA_PASSWD"))
    record(
        E_CA_PWD,
        PASS if has_pwd else FAIL,
        "" if has_pwd else "憑證密碼空白；密碼＝下載憑證時自己設定的（只跑 A 可忽略）",
    )
    pfx = pfx_path(values)
    custom = bool(values.get("SJ_CA_PATH")) and pfx.resolve() != DEFAULT_PFX.resolve()
    pfx_exists = pfx.is_file()
    if pfx_exists:
        record(E_PFX, PASS)
    elif custom:
        record(E_PFX, FAIL, f"SJ_CA_PATH 指向的憑證檔不存在：{pfx}（請修正路徑或把檔案放回去）")
    else:
        record(
            E_PFX, FAIL, f"找不到 {pfx}（請把下載的 Sinopac.pfx 放到這裡，或用「瀏覽」指定；只跑 A 可忽略）"
        )
    return {
        "exists": exists,
        "decode_error": False,
        "problems": problems,
        "ok_a": ok_a,
        "ok_b": ok_a and has_pwd and pfx_exists,
        "api_key_masked": _mask(values.get("SJ_API_KEY", "")),
        "sec_key_set": bool(values.get("SJ_SEC_KEY")),
        "sec_key_len": len(values.get("SJ_SEC_KEY", "")),
        "ca_passwd_set": has_pwd,
        "ca_passwd_len": len(values.get("SJ_CA_PASSWD", "")),
        "ca_path": values.get("SJ_CA_PATH", "") or DEFAULT_PFX.as_posix(),
        "ca_path_custom": custom,
        "pfx_exists": pfx_exists,
        "pfx_resolved": str(pfx),
    }


def write_env_values(updates: dict[str, str]) -> None:
    """只改指定的鍵：既有行就地替換值，沒有的補在檔尾；其他行（註解、別的設定）原樣保留。
    寫進目前 profile 目錄的 .env（``env_path()``）。

    既有檔案讀不到（OSError：防毒鎖定、ACL、I/O）→ 往上拋，**不可**當成空檔重建，那會
    把金鑰整份洗掉。只有「編碼壞掉」才重建（使用者按儲存時的既有語義）。"""
    p = env_path()
    text = ""
    if p.is_file():
        try:
            text = p.read_text(encoding="utf-8-sig")
        except UnicodeDecodeError:
            text = ""  # 編碼壞掉：重建
    lines = text.splitlines()
    done: set[str] = set()
    out: list[str] = []
    for raw in lines:
        line = raw.strip()
        if line and not line.startswith("#") and "=" in line:
            k = line.split("=", 1)[0].strip()
            if k.lower().startswith("export "):
                k = k[7:].strip()
            if k in updates:
                out.append(f"{k}={updates[k]}")
                done.add(k)
                continue
            if k.upper() in updates and k != k.upper():
                continue  # 丟掉大小寫錯的舊行，下面用正確名稱補
        out.append(raw)
    if not lines:
        out.append("# Shioaji 金鑰設定檔：請勿分享、勿上傳。每行格式 名稱=值，不要加空白或引號。")
    for k in ("SJ_API_KEY", "SJ_SEC_KEY", "SJ_CA_PASSWD", "SJ_CA_PATH"):
        if k in updates and k not in done:
            out.append(f"{k}={updates[k]}")
    atomic_write_text(env_path(), "\n".join(out) + "\n")


def auto_adopt_eleader_pfx(env: dict[str, Any], candidates: list[str]) -> bool:
    """尚未有人為切換過 profile（含切回程式資料夾）、目前 pfx 不存在、且候選恰好一筆
    → 自動切到該 profile（與 /api/profile 相同的切換規則）。多筆候選不猜。

    ROOT 的 .env 已經有金鑰（升級前的單人設定）時，不要整份跳過去——先用舊行為只改
    SJ_CA_PATH（留在 ROOT、標 path stale），讓 ``_migrate_root_env_if_pointing_to_profile``
    在同一次 /api/state 裡接著把整份搬過去，金鑰才不會憑空消失。ROOT 沒有 .env 或
    金鑰皆空（沒東西好保留）時，才直接切換＋pending。"""
    if _ACTIVE_PROFILE_FILE.is_file():
        return False
    if env.get("decode_error") or env.get("pfx_exists") or len(candidates) != 1:
        return False
    candidate = candidates[0]
    if not Path(candidate).is_file():
        return False
    if env.get("api_key_masked") or env.get("sec_key_set"):
        try:
            write_env_values({"SJ_CA_PATH": candidate})
        except OSError:
            return False
        _STALE_FIELDS.add("path")
        return True
    try:
        _switch_to_pfx(candidate)
    except OSError:
        return False
    return True


def _copy_env_bytes(src: Path, dst: Path) -> None:
    dst.write_bytes(src.read_bytes())


def _migrate_root_env_if_pointing_to_profile() -> bool:
    """升級遷移：目前 profile 還是 ROOT、ROOT/.env 存在、其 SJ_CA_PATH 命中某個
    eLeader 候選、且該候選的資料夾還沒有 .env → 把整份 .env 原樣（位元組）搬過去，
    只是換位置、金鑰沒變：不清 SESSION、不標 stale。ROOT 的舊檔留著不刪（使用者
    可能還沒信任新畫面）。複製失敗 → 留在 ROOT、不留半個檔、不拋錯。"""
    global _profile_dir
    if _profile_dir != ROOT:
        return False
    root_env = ROOT / ".env"
    if not root_env.is_file():
        return False
    values, _ = parse_env_text(read_env_text() or "")
    resolved = _normalize_path(str(pfx_path(values)))
    target_pfx = next((c for c in find_eleader_pfx() if _normalize_path(c) == resolved), None)
    if target_pfx is None:
        return False
    target_dir = Path(target_pfx).parent
    target_env = target_dir / ".env"
    if target_env.is_file():
        return False
    try:
        _copy_env_bytes(root_env, target_env)
    except OSError:
        try:
            target_env.unlink()
        except OSError:
            pass
        return False
    try:
        _persist_active_profile(target_dir)
    except OSError:  # 記不住就不搬：留在 ROOT，剛複製的那份收掉
        with contextlib.suppress(OSError):
            target_env.unlink()
        return False
    _profile_dir = target_dir
    return True


def _profile_configured(dir_: Path) -> bool:
    """該目錄的 .env 是否已有完整金鑰（只回布林，不回任何金鑰值）。"""
    p = dir_ / ".env"
    if not p.is_file():
        return False
    try:
        text = p.read_text(encoding="utf-8-sig")
    except (OSError, UnicodeDecodeError):  # 別的帳號的檔壞了，不能拖垮整個 /api/state
        return False
    values, _ = parse_env_text(text)
    return bool(values.get("SJ_API_KEY")) and bool(values.get("SJ_SEC_KEY"))


def _profiles_list(candidates: list[str]) -> list[dict[str, Any]]:
    """偵測到的每張 eLeader 憑證常駐一筆：不論目前 pfx_exists 與否都回傳。"""
    values, _ = parse_env_text(read_env_text() or "")
    active_norm = _normalize_path(str(pfx_path(values)))
    out = []
    for c in candidates:
        out.append(
            {
                "pfx": c,
                "dir": str(Path(c).parent),
                "configured": _profile_configured(Path(c).parent),
                "active": _normalize_path(c) == active_norm,
            }
        )
    return out


def cloud_sync_tag() -> str:
    low = str(ROOT).lower()
    for tag in ("onedrive", "dropbox", "google drive", "googledrive", "icloud"):
        if tag in low:
            return tag
    return ""


# ---------------------------------------------------------------- 時段／IP
TW_TZ = timezone(timedelta(hours=8))


def public_ip_country() -> tuple[str, str]:
    try:
        with urllib.request.urlopen("https://ipinfo.io/json", timeout=5) as r:
            d = json.loads(r.read().decode("utf-8"))
        return str(d.get("ip", "")), str(d.get("country", ""))
    except (OSError, ValueError):
        return "", ""


def window_check() -> dict[str, Any]:
    now = datetime.now(TW_TZ)
    wd = now.weekday()
    hm = now.hour * 60 + now.minute
    in_days = wd <= 4
    in_hours = 8 * 60 <= hm < 20 * 60
    evening = 18 * 60 <= hm < 20 * 60
    notes: list[str] = []
    good = True
    if not (in_days and in_hours):
        notes.append(
            "⚠ 不在永豐的測試時段（週一～五 08:00–20:00 台灣時間），現在送出的測試單很可能不被採計。"
        )
        good = False
    else:
        notes.append("✓ 在測試時段內（只看星期與時間，未排除國定假日；假日請改天再測）")
    ip = cc = ""
    if in_days and evening:
        ip, cc = public_ip_country()
        if cc == "TW":
            notes.append(f"✓ 18:00–20:00 限台灣 IP，你的 IP {ip} 是台灣（TW）")
        elif cc:
            notes.append(
                f"⚠ 18:00–20:00 限台灣 IP，但你的 IP {ip} 判定為 {cc}，測試可能不被採計（VPN 請先關）"
            )
            good = False
        else:
            notes.append("⚠ 18:00–20:00 限台灣 IP，但查不到你的對外 IP，無法確認")
    return {
        "now": now.strftime("%Y-%m-%d %H:%M"),
        "weekday": "一二三四五六日"[wd],
        "in_window": in_days and in_hours,
        "good": good,
        "notes": notes,
        "ip": ip,
        "country": cc,
    }


# ---------------------------------------------------------------- 跑測試（子行程）
def _run_job(job: Job, module: str, args: list[str]) -> None:
    env = dict(os.environ)
    env["SJ_ENV_DIR"] = str(ROOT)
    env["SJ_PROFILE_DIR"] = str(_profile_dir)  # 子行程（test_ca／test_sim_order）沿用目前 profile 的 .env
    env["PYTHONUTF8"] = "1"
    env["PYTHONDONTWRITEBYTECODE"] = "1"
    env["PYTHONUNBUFFERED"] = "1"
    with tempfile.TemporaryDirectory() as td:
        report_file = Path(td) / "report.json"
        env["SJ_REPORT_FILE"] = str(report_file)
        cmd = [sys.executable, "-m", module, *args]
        secrets = _secret_values()
        job.secrets = sorted(secrets, key=len, reverse=True)
        _SEEN_SECRETS.update(secrets)
        try:
            proc = subprocess.Popen(
                cmd,
                cwd=str(ensure_runtime_dir()),  # shioaji.log 會寫在 cwd → 藏進 .runtime
                env=env,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                encoding="utf-8",
                errors="replace",
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            )
        except OSError as e:
            job.lines.append(f"✗ 無法啟動測試子行程：{e}")
            job.rc = 2
            record(f"{module}", FAIL, f"無法啟動測試：{e}")
            return
        job.proc = proc
        last_output = time.monotonic()
        started = last_output

        def _reader() -> None:
            nonlocal last_output
            for raw in proc.stdout or []:
                line = raw.rstrip("\n")
                last_output = time.monotonic()
                if "Response Code:" in line:
                    # shioaji 的連線訊息（Session up）沒換行就接著我們的下一行印，會把 [A1 …] 吃掉：
                    # 只丟掉訊息本身，保留後面的內容
                    k = line.find("  [")
                    if k == -1:
                        continue
                    line = line[k:]
                if "DeprecationWarning" in line or not line.strip():
                    continue
                job.lines.append(_redact_text(line, job.secrets))  # 即時輸出就遮罩，不只匯出時

        reader = threading.Thread(target=_reader, daemon=True)
        reader.start()
        timeout_reason = ""
        while proc.poll() is None:
            time.sleep(0.25)
            now = time.monotonic()
            if now - last_output > NO_OUTPUT_TIMEOUT:
                timeout_reason = f"逾時：{int(NO_OUTPUT_TIMEOUT)} 秒沒有任何回應（永豐伺服器無回應或網路中斷），已強制結束；請檢查網路後重試"
            elif now - started > TOTAL_TIMEOUT:
                timeout_reason = (
                    f"逾時：測試超過 {int(TOTAL_TIMEOUT // 60)} 分鐘仍未完成，已強制結束；請稍後重試"
                )
            if timeout_reason:
                job.timed_out = True
                job.kill()
                break
        rc = proc.wait()
        reader.join(timeout=5)
        if timeout_reason:
            job.lines.append(f"✗ {timeout_reason}")
        if report_file.is_file():
            try:
                raw_report = json.loads(report_file.read_text(encoding="utf-8"))
            except (ValueError, OSError):
                raw_report = []
            # 永豐錯誤原文可能回顯金鑰（如「key … not exist」）：report 一進來就遮罩成唯一版本，
            # 之後 SESSION／/api/job／/api/summary 拿到的都是這份，不會有沒遮的副本。
            job.report = [
                {**item, "reason": _redact_text(str(item.get("reason", "")), job.secrets)}
                for item in raw_report
            ]
        with _lock:
            for item in job.report:
                record(item["name"], item["status"], item.get("reason", ""))
            if timeout_reason:
                record(("A1 模擬環境登入" if job.kind == "a" else "B1 正式環境登入"), FAIL, timeout_reason)
            elif not job.report and rc != 0:
                record(
                    module.rsplit(".", 1)[-1],
                    FAIL,
                    f"腳本異常結束（代碼 {rc}），沒有留下測試結果；請看輸出訊息",
                )
        if not timeout_reason:
            _STALE_FIELDS.difference_update(_COVERS.get(job.kind, set()))
        job.rc = rc if not timeout_reason else (rc or 124)


def cancel_job() -> bool:
    """殺掉進行中的測試子行程（關窗時由桌面殼呼叫）。回傳是否有殺到東西。"""
    job = _job
    if job is None or not job.running:
        return False
    job.lines.append("✗ 視窗已關閉，測試中止。")
    job.kill()
    return True


# ---------------------------------------------------------------- 路由
class EnvIn(BaseModel):
    api_key: str = ""
    sec_key: str = ""
    ca_passwd: str = ""
    ca_path: str = ""
    clear_ca_passwd: bool = False
    unlock_keys: bool = False  # 金鑰已被 A/B 驗證通過後預設鎖定；要換金鑰必須明示解鎖


class ProfileIn(BaseModel):
    pfx: str = ""  # 空字串＝切回程式資料夾；否則必須是 find_eleader_pfx() 清單內的路徑


def keys_verified() -> bool:
    """API Key／Secret Key 是否已被最近一次 A1 或 B1 驗證通過，且之後沒再改過。"""
    if "api" in _STALE_FIELDS or "sec" in _STALE_FIELDS:
        return False
    return any(SESSION.get(n, {}).get("status") == PASS for n in ("A1 模擬環境登入", "B1 正式環境登入"))


class RunIn(BaseModel):
    kind: str
    futures: bool = False


@app.get("/", response_class=HTMLResponse)
def index() -> str:
    return (STATIC / "index.html").read_text(encoding="utf-8")


@app.get("/favicon.ico", include_in_schema=False)
def favicon() -> FileResponse:
    """自製 icon（tools/make_icon.py 產生）；Chromium --app 視窗的視窗 icon 取自這裡。"""
    return FileResponse(STATIC / "favicon.ico", media_type="image/x-icon")


@app.get("/api/state")
def api_state() -> dict[str, Any]:
    with _lock:
        candidates = find_eleader_pfx()
        env = inspect_env()
        auto_adopted = auto_adopt_eleader_pfx(env, candidates)
        if auto_adopted:
            env = inspect_env()
        migrated = _migrate_root_env_if_pointing_to_profile()
        if migrated:
            env = inspect_env()
        return {
            "root": str(_profile_dir),
            "env_path": str(env_path()),
            "cloud_sync": cloud_sync_tag(),
            "env": env,
            "job": {"running": bool(_job and _job.running), "kind": _job.kind if _job else ""},
            "summary": summary_items(),
            "platform": sys.platform,
            "version": APP_VERSION,
            "stale": sorted(_STALE_FIELDS),
            "keys_locked": keys_verified(),
            "profiles": _profiles_list(candidates),
            "pfx_auto_adopted": auto_adopted,
            "profile_migrated": migrated,
            "root_env_available": _profile_dir != ROOT and (ROOT / ".env").is_file(),
        }


@app.post("/api/env")
def api_env(body: EnvIn) -> dict[str, Any]:
    with _lock:
        if _job and _job.running:
            raise HTTPException(409, "測試進行中，請等它結束再改設定")
        for v in (body.api_key, body.sec_key, body.ca_passwd, body.ca_path):
            # 用 splitlines 的同一套判準：CR／LF 之外，U+2028／U+2029／NEL／VT／FF 也算分行，
            # 否則能在 .env 注入第二個鍵（ca_path 不受金鑰鎖定保護）
            if "".join(v.splitlines()) != v:
                raise HTTPException(400, "值不可含換行（會破壞 .env 格式）")
        cap = body.ca_path.strip().strip('"')
        original_profile = _profile_dir
        original_values, _ = parse_env_text(read_env_text() or "")
        # 憑證檔指到某個 eLeader profile → 先切過去（沿用該資料夾既有金鑰）；留空 → 回程式資料夾。
        target_dir = profile_dir_for(cap) if cap else ROOT
        # 升級遷移的保底：這次儲存其實是從 ROOT 切到一個還沒有 .env 的 eLeader 資料夾，
        # 而且 ROOT 原本的 SJ_CA_PATH 就已經指到同一張憑證（畫面上欄位還沒變、使用者只改了
        # 別的欄位）——等同「先遷移再寫」，空白欄位要沿用 ROOT 的值，金鑰才不會憑空消失。
        # 其他情況（帳號 A 切帳號 B、或 ROOT 指的是別張憑證）一律不帶金鑰過去。
        migrating_from_root = bool(
            cap
            and original_profile == ROOT
            and target_dir != ROOT
            and not (target_dir / ".env").is_file()
            and _normalize_path(str(pfx_path(original_values)))
            == _normalize_path(str(pfx_path({"SJ_CA_PATH": cap})))
        )
        # 目標檔快照要在動任何狀態之前拍：這一步讀取失敗就是什麼都還沒變
        target_env = target_dir / ".env"
        try:
            target_before = target_env.read_bytes() if target_env.is_file() else None
        except OSError as e:
            raise HTTPException(500, f"讀取設定失敗（{target_env}）：{e}") from e
        snapshot = _snapshot_profile_state()
        switching = target_dir != _profile_dir
        if switching:
            _enter_profile_dir(target_dir)  # 先只改記憶體；.env 寫成功後才提交 active-profile
        updates: dict[str, str] = {}
        if (body.api_key.strip() or body.sec_key.strip()) and keys_verified() and not body.unlock_keys:
            raise HTTPException(409, "API Key／Secret Key 已驗證通過並鎖定；要更換請先按「解鎖修改」")
        if body.api_key.strip():
            updates["SJ_API_KEY"] = body.api_key.strip().strip('"').strip("'")
        if body.sec_key.strip():
            updates["SJ_SEC_KEY"] = body.sec_key.strip().strip('"').strip("'")
        if body.clear_ca_passwd:
            updates["SJ_CA_PASSWD"] = ""
        elif body.ca_passwd.strip():
            updates["SJ_CA_PASSWD"] = body.ca_passwd.strip()
        updates["SJ_CA_PATH"] = Path(cap).expanduser().as_posix() if cap else DEFAULT_PFX.as_posix()
        prev_values, _ = parse_env_text(read_env_text() or "")
        if "SJ_API_KEY" in updates and updates["SJ_API_KEY"] != prev_values.get("SJ_API_KEY"):
            _STALE_FIELDS.add("api")
        if "SJ_SEC_KEY" in updates and updates["SJ_SEC_KEY"] != prev_values.get("SJ_SEC_KEY"):
            _STALE_FIELDS.add("sec")
        if "SJ_CA_PASSWD" in updates and updates["SJ_CA_PASSWD"] != prev_values.get("SJ_CA_PASSWD", ""):
            _STALE_FIELDS.add("pwd")
        if updates["SJ_CA_PATH"] != (prev_values.get("SJ_CA_PATH") or DEFAULT_PFX.as_posix()):
            _STALE_FIELDS.add("path")
        if not env_path().is_file():
            # 第一次建立：四鍵都寫，沒給的原則上留空——除非這是從 ROOT 搬過來的同一張憑證
            # （migrating_from_root），這種情況沒給的欄位沿用 ROOT 原值。
            fallback_source = original_values if migrating_from_root else {}
            for k in ("SJ_API_KEY", "SJ_SEC_KEY", "SJ_CA_PASSWD"):
                updates.setdefault(k, fallback_source.get(k, ""))
        try:
            write_env_values(updates)
            if switching:
                try:
                    _persist_active_profile(target_dir)
                except OSError:
                    # 回 500 的請求不可以留下已改過的目標 .env：還原成寫入前的位元組
                    with contextlib.suppress(OSError):
                        if target_before is None:
                            target_env.unlink()
                        else:
                            target_env.write_bytes(target_before)
                    raise
        except OSError as e:
            _restore_profile_state(snapshot)  # 含這次才加的 stale 標記一併退回
            raise HTTPException(500, f"寫入設定失敗（{target_env}）：{e}") from e
        env = inspect_env()
        return {
            "ok": True,
            "env": env,
            "summary": summary_items(),
            "stale": sorted(_STALE_FIELDS),
            "profiles": _profiles_list(find_eleader_pfx()),
        }


@app.post("/api/profile")
def api_profile(body: ProfileIn) -> dict[str, Any]:
    """切換目前作用中的帳號：pfx 留空＝切回程式資料夾；否則必須是偵測到的 eLeader
    憑證（白名單，不接受任意路徑）。"""
    with _lock:
        if _job and _job.running:
            raise HTTPException(409, "測試進行中，請等它結束再切換帳號")
        pfx = body.pfx.strip()
        if pfx == "":
            try:
                _switch_profile_dir(ROOT)
            except OSError as e:
                raise HTTPException(500, f"切換帳號失敗：{e}") from e
            pending_ca_path: str | None = None
        else:
            candidates = find_eleader_pfx()
            norm_map = {_normalize_path(c): c for c in candidates}
            canon = norm_map.get(_normalize_path(pfx))
            if canon is None:
                raise HTTPException(400, "不是偵測到的 eLeader 憑證，無法切換")
            try:
                pending_ca_path = _switch_to_pfx(canon)
            except OSError as e:
                raise HTTPException(500, f"切換帳號失敗：{e}") from e
        env = inspect_env()
        return {
            "ok": True,
            "env": env,
            "summary": summary_items(),
            "stale": sorted(_STALE_FIELDS),
            "profiles": _profiles_list(find_eleader_pfx()),
            "pending_ca_path": pending_ca_path,
        }


def _browse_script(root: Path) -> str:
    """組 PowerShell 選檔腳本。兩個不能少：
    ① ``[Console]::OutputEncoding = UTF8``——Windows PowerShell 在 stdout 被導向管線時預設用系統 ANSI
       （台灣是 big5）輸出，含中文的路徑回來會是亂碼（實測 2026-08-22：小白的憑證常放在中文資料夾）。
    ② TopMost＋ShowInTaskbar 的隱形 owner 視窗——否則對話框被壓在 app 後面、工作列沒項目。"""
    root_q = str(root).replace("'", "''")
    return (
        "[Console]::OutputEncoding = [System.Text.Encoding]::UTF8; "
        "Add-Type -AssemblyName System.Windows.Forms; "
        "$o = New-Object System.Windows.Forms.Form; "
        "$o.Text = 'Shioaji 測試精靈 — 選擇憑證'; "
        "$o.TopMost = $true; $o.ShowInTaskbar = $true; "
        "$o.FormBorderStyle = 'None'; $o.Width = 1; $o.Height = 1; $o.Opacity = 0.01; "
        "$o.StartPosition = 'CenterScreen'; "
        "$o.Show(); $o.Activate(); "
        "$f = New-Object System.Windows.Forms.OpenFileDialog; "
        "$f.Title = '選擇永豐憑證 Sinopac.pfx'; "
        "$f.Filter = '憑證檔 (*.pfx)|*.pfx|所有檔案 (*.*)|*.*'; "
        f"$f.InitialDirectory = '{root_q}'; "
        "$r = $f.ShowDialog($o); "
        "$o.Close(); "
        "if ($r -eq 'OK') { [Console]::Out.Write($f.FileName) }"
    )


@app.post("/api/browse")
def api_browse() -> dict[str, Any]:
    if sys.platform != "win32":
        return {"path": None, "error": "此平台沒有內建檔案對話框，請直接把路徑貼到欄位裡"}
    # 對話框由背景行程開，沒有 owner 會被壓在 app 視窗後面、工作列也沒項目（使用者以為沒按到）。
    # 做法：先開一個 TopMost、會出現在工作列、幾乎透明的小 owner 視窗並 Activate，再以它為 owner
    # 開 OpenFileDialog → 對話框跟著浮到最前面，工作列也看得到「選擇憑證」。
    ps = _browse_script(ROOT)
    global _dialog_proc
    try:
        proc = subprocess.Popen(
            [str(_POWERSHELL), "-NoProfile", "-STA", "-ExecutionPolicy", "Bypass", "-Command", ps],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            text=True,
            encoding="utf-8",
            errors="replace",
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
    except OSError as e:
        return {"path": None, "error": f"開不了檔案對話框：{e}"}
    _dialog_proc = proc  # 關窗時 cancel_dialog() 會殺它，否則這個請求會卡住 uvicorn 的優雅關機
    try:
        out, _ = proc.communicate(timeout=600)
    except subprocess.TimeoutExpired:
        proc.kill()
        return {"path": None, "error": "選檔視窗逾時未關閉，已取消"}
    finally:
        _dialog_proc = None
    path = (out or "").strip()
    return {"path": path or None}


_dialog_proc: subprocess.Popen[str] | None = None


def cancel_dialog() -> bool:
    """殺掉還開著的檔案對話框（視窗已關、使用者看不到它時）。回傳是否有殺到。"""
    p = _dialog_proc
    if p is not None and p.poll() is None:
        try:
            p.kill()
        except OSError:
            return False
        return True
    return False


@app.get("/api/window")
def api_window() -> dict[str, Any]:
    return window_check()


@app.post("/api/run")
def api_run(body: RunIn) -> dict[str, Any]:
    global _job
    with _lock:
        if _job and _job.running:
            raise HTTPException(409, "已有測試在進行中")
        env = inspect_env()
        if body.kind == "a":
            if not env["ok_a"]:
                raise HTTPException(400, "先把 .env 的 SJ_API_KEY／SJ_SEC_KEY 填好並儲存")
            module, args = "shioaji_wizard.test_sim_order", (["--futures"] if body.futures else [])
        elif body.kind == "b":
            if not env["ok_a"]:
                raise HTTPException(400, "先把 .env 的 SJ_API_KEY／SJ_SEC_KEY 填好並儲存")
            if not env["ca_passwd_set"]:
                raise HTTPException(400, "B 需要憑證密碼（SJ_CA_PASSWD），請先填入並儲存")
            if not env["pfx_exists"]:
                raise HTTPException(400, f"找不到憑證檔：{env['pfx_resolved']}")
            module, args = "shioaji_wizard.test_ca", (["--futures"] if body.futures else [])
        else:
            raise HTTPException(400, "kind 必須是 a 或 b")
        # 每次執行先清空檢核單：只留這一次的結果（E1～E3 下次 /api/state 會自然重算回來）。
        SESSION.clear()
        job = Job(body.kind)
        _job = job
    threading.Thread(target=_run_job, args=(job, module, args), daemon=True).start()
    return {"ok": True, "kind": body.kind}


@app.get("/api/job")
def api_job(since: int = 0) -> dict[str, Any]:
    job = _job
    if job is None:
        return {"exists": False, "running": False, "lines": [], "next": 0, "rc": None, "report": []}
    lines = job.lines[since:]
    return {
        "exists": True,
        "kind": job.kind,
        "running": job.running,
        "lines": lines,
        "next": since + len(lines),
        "rc": job.rc,
        "report": job.report if not job.running else [],
        "summary": summary_items() if not job.running else [],
        "stale": sorted(_STALE_FIELDS),
    }


@app.get("/api/summary")
def api_summary() -> dict[str, Any]:
    with _lock:
        return {"summary": summary_items()}


@app.post("/api/heartbeat")
def api_heartbeat() -> JSONResponse:
    global _last_heartbeat
    _last_heartbeat = time.monotonic()
    return JSONResponse({"ok": True})


class OpenIn(BaseModel):
    url: str


_OPEN_ALLOWED_HOSTS = ("www.sinotrade.com.tw", "sinotrade.github.io")


@app.post("/api/open", status_code=204)
def api_open(body: OpenIn) -> None:
    """用系統瀏覽器開官方頁面（--app 殼內 target=_blank 會開同實例新視窗、干擾視窗追蹤）。只放行白名單網域。"""
    from urllib.parse import urlsplit

    parts = urlsplit(body.url)
    if parts.scheme != "https" or parts.hostname not in _OPEN_ALLOWED_HOSTS:
        raise HTTPException(400, "只允許開啟永豐官方頁面")
    import webbrowser

    webbrowser.open(body.url)


# 身分證字號／外來人口統一證號（第二碼 8、9 或舊式 A–D）：留頭三尾二
_PERSON_ID_RE = re.compile(r"\b([A-Z][1289A-D]\d)\d{5}(\d{2})\b")


def _secret_values() -> set[str]:
    """目前 .env 裡的金鑰／憑證密碼值（長度 ≥4 才算，避免把空字串或單字元全替換）。"""
    values, _ = parse_env_text(read_env_text() or "")
    return {v for k, v in values.items() if k in ("SJ_API_KEY", "SJ_SEC_KEY", "SJ_CA_PASSWD") and len(v) >= 4}


def _redact_text(text: str, secrets: set[str] | list[str]) -> str:
    """秘密值整段換成 [已遮罩]，身分證字號中段打碼；兩者在原文上用區間計算、不互相破壞。

    不能用「先換 A 再換 B」的兩趟 replace：短密碼（如 1234）是身分證的子字串時，先換密碼會把
    身分證切成 ``A[已遮罩]56789`` 讓 regex 對不上；反過來先遮身分證，含身分證片段的密碼
    （``pw-A123456789-x``）就對不上而露出前後段。所以在原文上找出所有秘密區間與身分證區間，
    排序後取聯集：只要一段裡碰到任何秘密就整段 [已遮罩]，純身分證的段才打碼中段。

    複雜度 O(總命中數 log)：每個秘密用 ``str.find`` 跳著找（不逐字元退回），匯出幾百 KB 的
    log 也不會退化成平方時間。"""
    SECRET, PID = 1, 0
    spans: list[tuple[int, int, int]] = []  # (start, end, kind)
    for s in {x for x in secrets if x}:
        start = 0
        while (i := text.find(s, start)) != -1:
            start = i + len(s)
            spans.append((i, start, SECRET))
    for m in _PERSON_ID_RE.finditer(text):
        spans.append((m.start(), m.end(), PID))
    if not spans:
        return text
    spans.sort()
    out: list[str] = []
    pos = 0
    cs, ce, ck = spans[0]
    for i, j, k in [*spans[1:], (len(text) + 1, len(text) + 1, PID)]:  # 哨兵：把最後一段也吐出來
        if i < ce:  # 重疊（含相鄰不算）：併成一段，秘密優先
            ce = max(ce, j)
            ck = max(ck, k)
            continue
        out.append(text[pos:cs])
        out.append("[已遮罩]" if ck == SECRET else f"{text[cs : cs + 3]}*****{text[ce - 2 : ce]}")
        pos = ce
        cs, ce, ck = i, j, k
    out.append(text[pos:])
    return "".join(out)


def _redact(text: str) -> str:
    """匯出紀錄前的遮罩：目前 .env 的金鑰值 ∪ 本次 session 跑測試時用過的值（使用者貼錯又改掉的舊值
    也要蓋）；shioaji 的 log 或子行程輸出可能含它們——例如憑證密碼剛好等於身分證字號。"""
    return _redact_text(text, _secret_values() | _SEEN_SECRETS)


def _masked_env_lines() -> list[str]:
    text = read_env_text()
    if text is None:
        return ["(.env 不是 UTF-8，讀不出來)"]
    values, problems = parse_env_text(text)
    out = [f"{k}={_mask(v) if v else '(空白)'}" for k, v in values.items()]
    out += [f"問題：{p}" for p in problems]
    return out or ["(沒有 .env)"]


@app.post("/api/export-log")
def api_export_log() -> dict[str, Any]:
    """把內部紀錄（app.log、shioaji.log）、版本、遮罩後的 .env、測試總覽合併成一個 .log 放到根層，方便回報問題。"""
    import platform

    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    out_path = ROOT / f"shioaji_wizard-debug-{stamp}.log"
    parts: list[str] = []
    parts.append("=== Shioaji 測試精靈 除錯紀錄 ===")
    parts.append(f"匯出時間：{datetime.now():%Y-%m-%d %H:%M:%S}")
    parts.append(f"版本：{APP_VERSION}　Python：{platform.python_version()}　平台：{platform.platform()}")
    try:
        # 用 metadata 讀版本，不 import shioaji（import 會在 cwd 寫 shioaji.log）
        parts.append(f"shioaji：{_pkg_version('shioaji')}")
    except Exception as e:  # noqa: BLE001
        parts.append(f"shioaji：讀不到版本 {type(e).__name__}: {e}")
    parts.append(f"工作資料夾：{ROOT}")
    parts.append(f"目前帳號 .env：{env_path()}")
    parts.append("")
    parts.append("=== .env（值已遮罩）===")
    parts += _masked_env_lines()
    parts.append("")
    parts.append("=== 測試總覽 ===")
    items = summary_items()
    parts += [
        f"[{i['status']}] {i['name']}" + (f" — {i['reason']}" if i["reason"] else "") for i in items
    ] or ["(尚未執行)"]
    parts.append("")
    if _job is not None:
        parts.append(f"=== 最近一次執行輸出（{_job.kind.upper()}）===")
        parts += _job.lines
        parts.append("")
    for name in ("app.log", "shioaji.log"):
        p = RUNTIME / name
        parts.append(f"=== {name}（{p}）===")
        if p.is_file():
            try:
                txt = p.read_text(encoding="utf-8", errors="replace")
                parts.append(txt[-200_000:])  # 只留最後 200KB
            except OSError as e:
                parts.append(f"(讀取失敗：{e})")
        else:
            parts.append("(不存在)")
        parts.append("")
    try:
        out_path.write_text(_redact("\n".join(parts)), encoding="utf-8")
    except OSError as e:
        raise HTTPException(500, f"寫入失敗：{e}") from e
    if sys.platform == "win32":
        try:
            subprocess.Popen(["explorer", "/select,", str(out_path)])
        except OSError:
            pass
    return {"ok": True, "path": str(out_path)}
