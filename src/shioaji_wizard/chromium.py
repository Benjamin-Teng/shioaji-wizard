"""探測系統 Chromium 瀏覽器可執行檔（Edge 優先，次 Chrome）。

原樣移植自 fcn-pricing 的 ``fcn_report.report.pdf.find_chromium``（該專案為
pdf 列印版轉檔與桌面 app 殼共用同一支探測器）；本專案只用於桌面殼開窗，
不需要 pdf 轉檔部分，故只搬 :func:`find_chromium` 及其依賴的常數／helper。
環境變數名由 ``FCN_CHROMIUM_PATH`` 改為 ``SJW_CHROMIUM_PATH``。
"""

from __future__ import annotations

import os
import shutil
import sys
from pathlib import Path

if sys.platform == "win32":
    import winreg

# Chromium 瀏覽器可執行檔名（探測順序：Edge 優先，次 Chrome）與其在各安裝
# 根目錄下的相對路徑。
_CHROMIUM_EXES = ("msedge.exe", "chrome.exe")
_CHROMIUM_SUBPATH = {
    "msedge.exe": Path("Microsoft") / "Edge" / "Application" / "msedge.exe",
    "chrome.exe": Path("Google") / "Chrome" / "Application" / "chrome.exe",
}
# 檢查的安裝根目錄環境變數：系統 64／32 位＋使用者層安裝（%LOCALAPPDATA%）。
_INSTALL_ROOT_ENV_VARS = ("ProgramFiles(x86)", "ProgramFiles", "LocalAppData")
# 登錄 App Paths（HKLM／HKCU 皆查）；其 default 值即該 exe 的完整路徑。
_APP_PATHS_KEY = r"SOFTWARE\Microsoft\Windows\CurrentVersion\App Paths"


def _fs_candidates(exe: str) -> list[Path]:
    """``exe`` 在各標準安裝根目錄下的候選完整路徑（依環境變數順序）。"""
    sub = _CHROMIUM_SUBPATH[exe]
    out: list[Path] = []
    for env_var in _INSTALL_ROOT_ENV_VARS:
        base = os.environ.get(env_var)
        if base:
            out.append(Path(base) / sub)
    return out


def _registry_candidates(exe: str) -> list[Path]:
    """由登錄 App Paths（HKLM 與 HKCU）取 ``exe`` 註冊的完整路徑；查無鍵或
    讀取失敗時略過該 root（回傳可能為空），不拋錯。"""
    out: list[Path] = []
    for root in (winreg.HKEY_LOCAL_MACHINE, winreg.HKEY_CURRENT_USER):
        try:
            with winreg.OpenKey(root, rf"{_APP_PATHS_KEY}\{exe}") as key:
                value, _ = winreg.QueryValueEx(key, "")  # default（空名）值＝完整路徑
        except OSError:
            continue
        if value:
            out.append(Path(value))
    return out


_POSIX_CHROMIUM_NAMES = (
    "chromium",
    "chromium-browser",
    "google-chrome-stable",
    "google-chrome",
)


def find_chromium() -> Path:
    """探測 Chromium 可執行檔。Windows：Edge 優先次 Chrome（標準安裝路徑＋
    登錄 App Paths）。POSIX：``SJW_CHROMIUM_PATH`` 環境變數優先，否則以
    ``shutil.which`` 依序探測 chromium／chromium-browser／
    google-chrome-stable／google-chrome。"""
    if sys.platform == "win32":
        return _find_chromium_windows()
    return _find_chromium_posix()


def _find_chromium_windows() -> Path:
    """探測任一 Chromium 瀏覽器可執行檔（**Edge 優先，次 Chrome**），供桌面
    app 殼 ``--app`` 視窗使用。

    對每個瀏覽器依序檢查：標準安裝路徑（``%ProgramFiles(x86)%``／
    ``%ProgramFiles%``／``%LOCALAPPDATA%`` 使用者安裝）＋登錄 App Paths
    （HKLM／HKCU）。回傳第一個實際存在的路徑；全部找不到時丟明確
    ``FileNotFoundError``（含已檢查路徑清單），不靜默降級。
    """
    checked: list[Path] = []
    for exe in _CHROMIUM_EXES:
        for cand in (*_fs_candidates(exe), *_registry_candidates(exe)):
            checked.append(cand)
            if cand.exists():
                return cand
    checked_str = "、".join(str(c) for c in checked) or "（未設定 ProgramFiles／LocalAppData 環境變數）"
    raise FileNotFoundError(
        f"找不到任何 Chromium 瀏覽器（msedge.exe／chrome.exe）；已檢查："
        f"{checked_str}。請確認本機已安裝 Microsoft Edge 或 Google Chrome。"
    )


def _find_chromium_posix() -> Path:
    override = os.environ.get("SJW_CHROMIUM_PATH")
    if override:
        p = Path(override)
        if p.exists():
            return p
        raise FileNotFoundError(f"SJW_CHROMIUM_PATH 指向不存在的路徑：{p}")
    for name in _POSIX_CHROMIUM_NAMES:
        hit = shutil.which(name)
        if hit:
            return Path(hit)
    raise FileNotFoundError(
        "找不到任何 Chromium 瀏覽器（"
        + "／".join(_POSIX_CHROMIUM_NAMES)
        + "）；請安裝 chromium 或以 SJW_CHROMIUM_PATH 指定路徑。"
    )
