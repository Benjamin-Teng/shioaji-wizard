"""桌面 app 殼：以系統 Chromium ``--app`` 無邊框視窗開啟本機服務，三階
fallback＋殼視窗主行程追蹤。

原樣移植自 fcn-pricing 的 ``fcn_report.app.shell``（該專案已真機驗證：Edge
launcher handoff、購物 extension 自彈、AUMID 工作列身份等踩雷皆已修復）。
探測器改用本專案的 :func:`shioaji_wizard.chromium.find_chromium`（Edge 優先
次 Chrome，含 ``%LOCALAPPDATA%`` 使用者安裝與登錄 App Paths）：
- ①②：找得到 Chromium → ``<browser> --app=<url> --user-data-dir=<暫時 profile>``
  （保住無邊框；Edge 或 Chrome 由探測器優先序決定）。
- ③：完全找不到 Chromium（``FileNotFoundError``）→ 預設瀏覽器普通視窗
  （末路，通常僅 Firefox 機；Firefox 不支援同款 ``--app``），頁面另提示。

**專屬 ``--user-data-dir`` 的關鍵作用**：唯一暫時 profile 強制瀏覽器起「獨立
新實例」（不併入使用者既有瀏覽器），且該路徑成為在行程表上辨識這個實例的
唯一指紋。

**launcher handoff（fcn-pricing 2026-07-18 本機實測）**：即使帶專屬 profile，
Edge spawn 出的行程仍可能只是 launcher——把視窗交給另一個真正的主行程後隨即
以 returncode 0 退出。故「spawn 行程存活＝視窗存活」不可靠；:func:`resolve_window`
以 CIM 一次性查詢，用 profile 路徑指紋找出真主行程（命令列含 ``--app=`` 且非
``--type=`` 子行程），回傳 :class:`WindowTracker`（ctypes process handle）供
``desktop`` 廉價輪詢視窗存活、收攤時連帶關窗。

``finder``／``spawn``／``open_default``／``user_data_dir``／``query`` 皆可注入
以利單測。
"""

from __future__ import annotations

import ctypes
import functools
import json
import os
import subprocess
import sys
import tempfile
import threading
import time
import webbrowser
from collections.abc import Callable
from ctypes import wintypes
from pathlib import Path
from typing import Any, Literal, NamedTuple

from shioaji_wizard import chromium

LaunchMode = Literal["app", "default_browser"]


class LaunchResult(NamedTuple):
    """開窗結果：``mode``＝走到哪一階；``browser``＝實際使用的 Chromium 路徑
    （末階為 None）；``process``＝spawn 出的子行程 handle（末階為 None）——
    **可能只是 launcher**（handoff 後早退），偵測視窗開關一律走
    :func:`resolve_window`／:class:`WindowTracker`，此欄僅供 launcher 尚存活
    時的收攤終止；``profile_dir``＝專屬暫時 ``--user-data-dir``（末階為
    None，供收攤後清理）。"""

    mode: LaunchMode
    browser: Path | None
    process: Any | None
    profile_dir: Path | None


def launch(
    url: str,
    *,
    finder: Callable[[], Path] = chromium.find_chromium,
    spawn: Callable[..., Any] = subprocess.Popen,
    open_default: Callable[[str], Any] = webbrowser.open,
    user_data_dir: Path | None = None,
    window_size: tuple[int, int] | None = None,
) -> LaunchResult:
    """開啟 ``url``：找得到 Chromium 就以專屬暫時 profile 開 ``--app`` 無邊框
    獨立視窗（可被 ``desktop`` 關閉）；否則退回預設瀏覽器普通視窗。``user_data_dir``
    未指定時自建暫時目錄（供收攤後清理）。回傳 :class:`LaunchResult`。"""
    try:
        browser = finder()
    except FileNotFoundError:
        open_default(url)
        return LaunchResult(mode="default_browser", browser=None, process=None, profile_dir=None)
    profile = (
        Path(user_data_dir)
        if user_data_dir is not None
        else Path(tempfile.mkdtemp(prefix="sjw-app-profile-"))
    )
    cmd = [
        str(browser),
        f"--app={url}",
        f"--user-data-dir={profile}",
        "--no-first-run",
        "--no-default-browser-check",
        # 一律停用擴充功能與同步（不分 Edge／Chrome）：app 是純本機頁面，不需要任何
        # extension；擴充功能會注入腳本、彈視窗、偷 focus，對小白是純風險。
        "--disable-extensions",
        "--disable-sync",
    ]
    if window_size is not None:
        # 殼每次都用全新暫時 profile（見上），Chromium 自己記不住視窗大小，
        # 沒設定就不塞，交給 Chromium 預設——不要自作主張決定一個尺寸。
        cmd.append(f"--window-size={window_size[0]},{window_size[1]}")
    if browser.name.lower() == "msedge.exe":
        # Edge 在新 profile 啟動約 30s 後，內建購物 extension（比個夠小幫手）
        # 會自發彈出普通瀏覽器視窗（fcn-pricing 2026-07-18 實測；
        # --disable-component-extensions-with-background-pages 壓不住）。
        # 2026-07-19 A/B 實測改用 --disable-extensions：90s soak 兩輪皆單一
        # 視窗（app 為純本機頁面、不需任何 extension）。舊解 --inprivate
        # 同樣壓得住，但 InPrivate 已無必要性、其工作列 InPrivate 徽章又更
        # 誤導，故棄用。注意 favicon 只決定「視窗 icon」；工作列按鈕 icon
        # 由 AUMID 分組決定，另由 :func:`brand_window` 處理。
        pass  # --disable-extensions 已在基本旗標裡（所有 Chromium 一律停用）
        # 新 profile 首啟會被 Windows 帳號自動登入 Edge 並彈「同步您的
        # 瀏覽資料」FRE 對話框（--no-first-run 壓不住，fcn-pricing 2026-07-19
        # 真機驗收實測）；殼的暫時 profile 每次全新＝每次啟動都要點一次
        # 「了解」。app 為純本機頁面無同步需求，停用 sync 機制以抑制該對話框。
        pass  # --disable-sync 同上
    process = spawn(cmd)
    return LaunchResult(mode="app", browser=browser, process=process, profile_dir=profile)


# ---------------------------------------------------------------------------
# 殼視窗主行程追蹤（launcher handoff 對策）
# ---------------------------------------------------------------------------

_SYNCHRONIZE = 0x0010_0000
_PROCESS_TERMINATE = 0x0001
_WAIT_TIMEOUT = 0x0000_0102


@functools.cache
def _kernel32() -> Any:
    """kernel32 handle（明確 argtypes/restype，避免 64 位 HANDLE 截斷）。"""
    k = ctypes.WinDLL("kernel32", use_last_error=True)
    k.OpenProcess.argtypes = (wintypes.DWORD, wintypes.BOOL, wintypes.DWORD)
    k.OpenProcess.restype = wintypes.HANDLE
    k.WaitForSingleObject.argtypes = (wintypes.HANDLE, wintypes.DWORD)
    k.WaitForSingleObject.restype = wintypes.DWORD
    k.TerminateProcess.argtypes = (wintypes.HANDLE, wintypes.UINT)
    k.TerminateProcess.restype = wintypes.BOOL
    k.CloseHandle.argtypes = (wintypes.HANDLE,)
    k.CloseHandle.restype = wintypes.BOOL
    return k


class WindowTracker:
    """殼視窗主行程 handle：視窗存活＝主行程存活。

    以 ctypes 持有 Windows process handle——持有期間該 PID 不會被回收重用，
    :meth:`alive` 為廉價的 ``WaitForSingleObject(0)`` 檢查（無 subprocess、
    無輪詢成本）；:meth:`terminate` 供 ``desktop`` 收攤時連帶關窗。行程已消失
    （無任何 handle 存留）時建構丟 ``OSError``。handle 操作以 ``_lock``
    序列化（watchdog 執行緒讀 ``alive``、主執行緒收攤時 ``terminate``／
    ``close``）。"""

    def __init__(self, pid: int) -> None:
        self.pid = pid
        handle = _kernel32().OpenProcess(_SYNCHRONIZE | _PROCESS_TERMINATE, False, pid)
        if not handle:
            raise OSError(f"OpenProcess 失敗（pid={pid}，GetLastError={ctypes.get_last_error()}）")
        self._handle = handle
        self._lock = threading.Lock()

    def alive(self) -> bool:
        with self._lock:
            if not self._handle:
                return False
            # WAIT_TIMEOUT＝仍在跑；signaled（0）＝已結束。WAIT_FAILED 理論上
            # 不會發生（持有合法 SYNCHRONIZE handle），發生時視同「非存活」
            # ——寧可收攤也不無限空轉。
            return _kernel32().WaitForSingleObject(self._handle, 0) == _WAIT_TIMEOUT

    def terminate(self) -> None:
        with self._lock:
            if self._handle and _kernel32().WaitForSingleObject(self._handle, 0) == _WAIT_TIMEOUT:
                _kernel32().TerminateProcess(self._handle, 1)

    def close(self) -> None:
        with self._lock:
            if self._handle:
                _kernel32().CloseHandle(self._handle)
                self._handle = None


_POWERSHELL = (
    Path(os.environ.get("SystemRoot", r"C:\Windows"))
    / "System32"
    / "WindowsPowerShell"
    / "v1.0"
    / "powershell.exe"
)
"""Windows PowerShell 絕對路徑（不走 PATH，避免劫持面）。"""


def _parse_process_json(stdout: str) -> list[dict[str, Any]]:
    """``ConvertTo-Json`` 輸出 → 行程 record 清單：單筆是 dict、多筆是
    list；空白／壞 JSON／非預期型別一律回空。"""
    if not stdout.strip():
        return []
    try:
        data = json.loads(stdout)
    except json.JSONDecodeError:
        return []
    if isinstance(data, dict):
        return [data]
    if isinstance(data, list):
        return [d for d in data if isinstance(d, dict)]
    return []


def _query_processes(image_name: str, *, timeout: float = 5.0) -> list[dict[str, Any]]:
    """列出指定映像名的行程（``ProcessId``／``CommandLine``），經 Windows
    PowerShell CIM 一次性查詢（list-argv 無 shell、輸出 JSON、UTF-8）。

    ``image_name`` 以 ``chromium._CHROMIUM_EXES`` 白名單把關——它會被內插進
    PowerShell 命令字串，而其來源（登錄 App Paths）非完全可信；白名單外
    一律回空、不 spawn。任何失敗同樣回空清單，交由呼叫端重試或放棄。"""
    if image_name not in chromium._CHROMIUM_EXES:
        return []
    ps = (
        "[Console]::OutputEncoding=[System.Text.Encoding]::UTF8; "
        f"Get-CimInstance Win32_Process -Filter \"Name='{image_name}'\" | "
        "Select-Object ProcessId,CommandLine | ConvertTo-Json -Compress"
    )
    try:
        result = subprocess.run(
            [str(_POWERSHELL), "-NoProfile", "-NonInteractive", "-Command", ps],
            capture_output=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout,
            check=False,
            creationflags=subprocess.CREATE_NO_WINDOW,
        )
    except (OSError, subprocess.SubprocessError):
        return []
    if result.returncode != 0:
        return []
    return _parse_process_json(result.stdout)


def _pick_candidates(records: list[dict[str, Any]], profile_dir: Path) -> list[int]:
    """從行程清單挑出符合殼視窗主行程指紋的 pid：命令列含專屬 profile 路徑
    （唯一指紋；Windows 路徑大小寫不敏感）、帶 ``--app=``、非 ``--type=``
    子行程（renderer/gpu/utility）。注意 **launcher 自己也符合指紋**——
    launcher vs 真主行程的排除邏輯在 :func:`resolve_window`。"""
    needle = str(profile_dir).casefold()
    out: list[int] = []
    for r in records:
        cl = (r.get("CommandLine") or "").casefold()
        if needle in cl and "--app=" in cl and "--type=" not in cl:
            pid = r.get("ProcessId")
            if isinstance(pid, int):
                out.append(pid)
    return out


def resolve_window(
    result: LaunchResult,
    *,
    timeout: float = 10.0,
    poll: float = 0.5,
    grace: float = 3.0,
    query: Callable[[str], list[dict[str, Any]]] = _query_processes,
    clock: Callable[[], float] = time.monotonic,
    sleep: Callable[[float], None] = time.sleep,
    abort: Callable[[], bool] | None = None,
) -> WindowTracker | None:
    """找出真正持有殼視窗的瀏覽器主行程，回傳其 :class:`WindowTracker`。

    spawn 的行程可能只是 launcher（handoff 後早退）——而 launcher 的命令列
    同樣符合指紋，故排除規則：

    - 指紋符合且 **pid ≠ spawn pid** → 即為 handoff 後的真主行程，立即認列。
    - 唯一候選是 spawn 行程自己 → 可能是「spawn 即主行程」（Chrome／舊
      Edge），也可能是尚未交棒的 launcher；等它活過 ``grace`` 秒仍是唯一
      候選才認列，避免綁到即將退出的 launcher（原 bug 以競態復活）。

    認列前於 handle 開啟後**複查指紋**（消除快照→OpenProcess 間的 PID 重用
    窗口）。``timeout`` 秒內無法認列（或末階 fallback 無 profile、``abort``
    成立）→ 回 None，``desktop`` 退回心跳閒置逾時機制。"""
    if result.mode != "app" or result.profile_dir is None or result.browser is None:
        return None
    launcher = result.process
    launcher_pid = getattr(launcher, "pid", None)
    image = result.browser.name
    start = clock()
    deadline = start + timeout
    while True:
        if abort is not None and abort():
            return None
        candidates = _pick_candidates(query(image), result.profile_dir)
        chosen = next((p for p in candidates if p != launcher_pid), None)
        if chosen is None and launcher_pid is not None and launcher_pid in candidates:
            poll_fn = getattr(launcher, "poll", None)
            launcher_alive = poll_fn is not None and poll_fn() is None
            if launcher_alive and clock() - start >= grace:
                chosen = launcher_pid  # spawn 即主行程（無 handoff）
        if chosen is not None:
            tracker: WindowTracker | None = None
            try:
                tracker = WindowTracker(chosen)
            except OSError:
                tracker = None  # 已消失 → 下一輪重找
            if tracker is not None:
                if chosen in _pick_candidates(query(image), result.profile_dir):
                    return tracker
                tracker.close()  # 開 handle 後指紋已不符（PID 重用）→ 棄用
        if clock() >= deadline:
            return None
        sleep(poll)


# ---------------------------------------------------------------------------
# 工作列身份：Chromium ``--app`` 視窗掛在瀏覽器自身的 AppUserModelID 分組下，
# 工作列按鈕 icon 恆為瀏覽器 icon——與視窗 icon（favicon）分屬兩條路、與
# InPrivate 無關（fcn-pricing 2026-07-19 真機 A/B 拍板）。對主視窗的 shell
# property store 設獨立 AUMID＋Relaunch icon 後，按鈕脫離瀏覽器分組、顯示
# 本專案 favicon。
# ---------------------------------------------------------------------------

_APP_USER_MODEL_ID = "ShioajiWizard"
_APP_DISPLAY_NAME = "Shioaji 測試精靈"
_APP_ICON_PATH = Path(__file__).parent / "static" / "favicon.ico"

_PID_RELAUNCH_ICON = 3
_PID_RELAUNCH_NAME = 4
_PID_AUMID = 5
"""System.AppUserModel 屬性集（fmtid 9F4C2855-…）內的 property id：
3=RelaunchIconResource、4=RelaunchDisplayNameResource、5=ID。"""

_VT_LPWSTR = 31
_CHROMIUM_WINDOW_CLASS = "Chrome_WidgetWin_1"
_IPROPERTYSTORE_IID = "{886D8EEB-8CF2-4446-8D02-CDBA1DBDCF99}"
_APP_USER_MODEL_FMTID = "{9F4C2855-9F79-4B39-A8D0-E1D42DE1D5F3}"

if sys.platform == "win32":
    _WNDENUMPROC = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)
else:  # POSIX：殼功能不可用；僅為讓模組可 import
    _WNDENUMPROC = None


class _GUID(ctypes.Structure):
    _fields_ = [
        ("Data1", wintypes.DWORD),
        ("Data2", wintypes.WORD),
        ("Data3", wintypes.WORD),
        ("Data4", ctypes.c_ubyte * 8),
    ]


class _PROPERTYKEY(ctypes.Structure):
    _fields_ = [("fmtid", _GUID), ("pid", wintypes.DWORD)]


class _PROPVARIANT(ctypes.Structure):
    """PROPVARIANT（x64：vt＋3×reserved 共 8 bytes，資料聯集自 offset 8）。
    僅用 VT_LPWSTR 分支；``SetValue`` 會複製值（live 實測），指向 Python
    暫存字串緩衝即可、不需 CoTaskMem 配置。"""

    _fields_ = [
        ("vt", wintypes.WORD),
        ("wReserved1", wintypes.WORD),
        ("wReserved2", wintypes.WORD),
        ("wReserved3", wintypes.WORD),
        ("pwszVal", ctypes.c_wchar_p),
        ("_pad", ctypes.c_void_p),
    ]


# IPropertyStore vtable：[QI, AddRef, Release, GetCount, GetAt, GetValue,
# SetValue, Commit]；restype=HRESULT → 失敗由 ctypes 自動 raise OSError。
if sys.platform == "win32":
    _SET_VALUE_PROTO = ctypes.WINFUNCTYPE(
        ctypes.HRESULT,
        ctypes.c_void_p,
        ctypes.POINTER(_PROPERTYKEY),
        ctypes.POINTER(_PROPVARIANT),
    )
    _COMMIT_PROTO = ctypes.WINFUNCTYPE(ctypes.HRESULT, ctypes.c_void_p)
    _RELEASE_PROTO = ctypes.WINFUNCTYPE(ctypes.c_ulong, ctypes.c_void_p)
else:  # POSIX：殼功能不可用；僅為讓模組可 import
    _SET_VALUE_PROTO = None
    _COMMIT_PROTO = None
    _RELEASE_PROTO = None


@functools.cache
def _user32() -> Any:
    u = ctypes.WinDLL("user32", use_last_error=True)
    u.EnumWindows.argtypes = (_WNDENUMPROC, wintypes.LPARAM)
    u.EnumWindows.restype = wintypes.BOOL
    u.IsWindowVisible.argtypes = (wintypes.HWND,)
    u.IsWindowVisible.restype = wintypes.BOOL
    u.GetWindowThreadProcessId.argtypes = (
        wintypes.HWND,
        ctypes.POINTER(wintypes.DWORD),
    )
    u.GetWindowThreadProcessId.restype = wintypes.DWORD
    u.GetClassNameW.argtypes = (wintypes.HWND, wintypes.LPWSTR, ctypes.c_int)
    u.GetClassNameW.restype = ctypes.c_int
    u.GetWindowTextLengthW.argtypes = (wintypes.HWND,)
    u.GetWindowTextLengthW.restype = ctypes.c_int
    return u


def _find_main_hwnd(pid: int, *, timeout: float = 5.0, poll: float = 0.25) -> int | None:
    """找 ``pid`` 的可見頂層 Chromium 視窗（class＝``Chrome_WidgetWin_1``、
    有標題）；視窗尚未建立時輪詢至 ``timeout``。"""
    u = _user32()
    deadline = time.monotonic() + timeout
    while True:
        found: list[int] = []

        # ``found`` 綁成預設引數（早繫結）：本迴圈每輪重建 ``found`` 並在同一輪
        # 內同步呼叫 ``EnumWindows``（不跨輪存活），故遲繫結本無實際風險，但
        # 顯式綁定同時滿足 ruff B023 且更明確。
        @_WNDENUMPROC
        def _cb(hwnd: int, _lparam: int, _found: list[int] = found) -> bool:
            if not u.IsWindowVisible(hwnd):
                return True
            owner = wintypes.DWORD()
            u.GetWindowThreadProcessId(hwnd, ctypes.byref(owner))
            if owner.value != pid:
                return True
            buf = ctypes.create_unicode_buffer(64)
            u.GetClassNameW(hwnd, buf, 64)
            if buf.value != _CHROMIUM_WINDOW_CLASS:
                return True
            if u.GetWindowTextLengthW(hwnd) <= 0:
                return True
            _found.append(int(hwnd))
            return False  # 找到即停止列舉

        u.EnumWindows(_cb, 0)
        if found:
            return found[0]
        if time.monotonic() >= deadline:
            return None
        time.sleep(poll)


_SWP_NOMOVE = 0x0002
_SWP_NOZORDER = 0x0004
_SWP_NOACTIVATE = 0x0010


def _set_window_pos(hwnd: int, width: int, height: int) -> None:
    """只改大小，不動位置／Z 序／焦點。失敗 raise ``OSError``。"""
    u = ctypes.WinDLL("user32", use_last_error=True)
    u.SetWindowPos.argtypes = (
        wintypes.HWND,
        wintypes.HWND,
        ctypes.c_int,
        ctypes.c_int,
        ctypes.c_int,
        ctypes.c_int,
        ctypes.c_uint,
    )
    u.SetWindowPos.restype = wintypes.BOOL
    ok = u.SetWindowPos(
        wintypes.HWND(hwnd),
        None,
        0,
        0,
        width,
        height,
        _SWP_NOMOVE | _SWP_NOZORDER | _SWP_NOACTIVATE,
    )
    if not ok:
        raise OSError(f"SetWindowPos 失敗（GetLastError={ctypes.get_last_error()}）")


_MIN_REAL_WINDOW = (300, 200)
"""真實應用視窗的最小合理尺寸。

Chromium 會另外建立一批 class 同為 ``Chrome_WidgetWin_1``、**有標題也可見**
的小視窗（實測 158×26）給工作列縮圖／Aero Peek 用。``_find_main_hwnd`` 取
EnumWindows 的第一筆，很容易就撈到那種代理視窗——對 ``brand_window`` 只是
屬性設錯地方（無害），對「改大小」卻會**回報成功但畫面毫無變化**。故改大小
另用一支更嚴格的找法。
"""


def _find_app_hwnd(pid: int, *, timeout: float = 2.0, poll: float = 0.25) -> int | None:
    """找 ``pid`` **面積最大**的可見頂層 Chromium 視窗（排除工作列代理視窗）。

    取最大而非第一個：代理視窗恆為極小尺寸，真視窗至少數百像素見方。
    """
    u = _user32()
    u.GetWindowRect.argtypes = (wintypes.HWND, ctypes.POINTER(wintypes.RECT))
    u.GetWindowRect.restype = wintypes.BOOL
    deadline = time.monotonic() + timeout
    while True:
        found: list[tuple[int, int]] = []  # (面積, hwnd)

        # ``found`` 綁成預設引數（早繫結，理由同 _find_main_hwnd）。
        @_WNDENUMPROC
        def _cb(hwnd: int, _lparam: int, _found: list[tuple[int, int]] = found) -> bool:
            if not u.IsWindowVisible(hwnd):
                return True
            owner = wintypes.DWORD()
            u.GetWindowThreadProcessId(hwnd, ctypes.byref(owner))
            if owner.value != pid:
                return True
            buf = ctypes.create_unicode_buffer(64)
            u.GetClassNameW(hwnd, buf, 64)
            if buf.value != _CHROMIUM_WINDOW_CLASS:
                return True
            rect = wintypes.RECT()
            if not u.GetWindowRect(wintypes.HWND(hwnd), ctypes.byref(rect)):
                return True
            w, h = rect.right - rect.left, rect.bottom - rect.top
            if w < _MIN_REAL_WINDOW[0] or h < _MIN_REAL_WINDOW[1]:
                return True  # 工作列縮圖代理視窗
            _found.append((w * h, int(hwnd)))
            return True

        u.EnumWindows(_cb, 0)
        if found:
            return max(found)[1]
        if time.monotonic() >= deadline:
            return None
        time.sleep(poll)


def resize_window(
    pid: int,
    width: int,
    height: int,
    *,
    timeout: float = 2.0,
    find_hwnd: Callable[..., int | None] = _find_app_hwnd,
    set_pos: Callable[[int, int, int], None] = _set_window_pos,
) -> bool:
    """把殼視窗立刻改成指定大小；成功回 ``True``。

    存在的理由：頁面內的 ``window.resizeTo()`` 對非 script 開啟的視窗會被
    Chromium 擋掉，所以改設定若只寫檔就得等下次啟動才看得到。殼視窗的 HWND
    本來就為了工作列身份（``brand_window``）找過一次，重用同一支即可。

    best-effort——``--no-shell``、找不到視窗、``SetWindowPos`` 失敗都回
    ``False``（那只代表「這次沒即時套用」），絕不上拋。
    """
    hwnd = find_hwnd(pid, timeout=timeout)
    if hwnd is None:
        return False
    try:
        set_pos(hwnd, width, height)
    except OSError:
        return False
    return True


def _guid_from_string(value: str) -> _GUID:
    guid = _GUID()
    ctypes.oledll.ole32.CLSIDFromString(value, ctypes.byref(guid))
    return guid


def _set_window_properties(hwnd: int, props: dict[int, str]) -> None:
    """對視窗 shell property store 寫入 System.AppUserModel 字串屬性。
    任一 COM 呼叫失敗 raise ``OSError``（oledll／HRESULT restype 自動轉），
    交由呼叫端 best-effort 處理。"""
    iid = _guid_from_string(_IPROPERTYSTORE_IID)
    fmtid = _guid_from_string(_APP_USER_MODEL_FMTID)
    store = ctypes.c_void_p()
    ctypes.oledll.shell32.SHGetPropertyStoreForWindow(
        wintypes.HWND(hwnd), ctypes.byref(iid), ctypes.byref(store)
    )
    vtbl = ctypes.cast(store, ctypes.POINTER(ctypes.POINTER(ctypes.c_void_p))).contents
    set_value = _SET_VALUE_PROTO(vtbl[6])
    commit = _COMMIT_PROTO(vtbl[7])
    release = _RELEASE_PROTO(vtbl[2])
    try:
        for prop_id, text in props.items():
            key = _PROPERTYKEY(fmtid=fmtid, pid=prop_id)
            pv = _PROPVARIANT(vt=_VT_LPWSTR, pwszVal=text)
            set_value(store, ctypes.byref(key), ctypes.byref(pv))
        commit(store)
    finally:
        release(store)


def brand_window(
    pid: int,
    *,
    timeout: float = 5.0,
    find_hwnd: Callable[..., int | None] = _find_main_hwnd,
    set_props: Callable[[int, dict[int, str]], None] = _set_window_properties,
) -> bool:
    """把殼視窗掛上獨立工作列身份（AUMID＋Relaunch icon／顯示名稱）。

    在 ``resolve_window`` 認列主行程後呼叫；best-effort——找不到視窗或
    COM 失敗回 ``False``（工作列退回瀏覽器 icon，不影響任何功能）。
    ``find_hwnd``／``set_props`` 可注入以利單測。
    """
    hwnd = find_hwnd(pid, timeout=timeout)
    if hwnd is None:
        return False
    try:
        set_props(
            hwnd,
            {
                _PID_AUMID: _APP_USER_MODEL_ID,
                _PID_RELAUNCH_ICON: str(_APP_ICON_PATH),
                _PID_RELAUNCH_NAME: _APP_DISPLAY_NAME,
            },
        )
    except OSError:
        return False
    return True


def kill_instance(
    result: LaunchResult,
    *,
    query: Callable[[str], list[dict[str, Any]]] = _query_processes,
) -> None:
    """best-effort：以 profile 指紋找出（未被追蹤到的）殼視窗主行程並終止。
    供 fallback 模式收攤時清殘留（launcher 早退且 ``resolve_window`` 失敗
    的情形），全程容錯。"""
    if result.mode != "app" or result.profile_dir is None or result.browser is None:
        return
    for pid in _pick_candidates(query(result.browser.name), result.profile_dir):
        try:
            tracker = WindowTracker(pid)
        except OSError:
            continue
        try:
            tracker.terminate()
        finally:
            tracker.close()
