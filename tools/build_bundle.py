"""portable bundle 打包腳本（開發機工具）。

用法：``uv run python tools/build_bundle.py [--zip] [--verify] [--allow-oversize]``。

移植自 fcn-pricing 的 ``tools/build_bundle.py``（設計文件見該專案
``docs/superpowers/specs/2026-07-19-build-bundle-design.md``）：路線＝
「本機為源＋uv 供應」——runtime 複製 uv 管理的 3.12.13 standalone、套件由
uv.lock 同版 vendored、launcher 以 Windows 內建 csc.exe 現編。全程建在
dist/.tmp 下，最後一步才 os.replace 成正式名（交易式，失敗不污染前次成功
bundle）。

與 fcn 版的差異（2026-08-22 本專案拍板）：
- 本專案沒有 golden oracle／可離線生成的「示範票券」，故沒有
  preload／golden_check 這兩步——不對永豐下任何連線，``verify_bundle``
  只打 ``/`` 與 ``/api/state``。
- 尚無 release tag 慣例，``preflight`` 略去 fcn 版的版本↔GitHub tag 同步
  守衛（僅留 uv／git／csc 三個工具存在性檢查）。
- 使用者看到的資料夾刻意「隱藏到只剩必要幾項」：bundle 目錄名固定為
  ``shioaji_wizard``（不帶版本——每次都內容覆蓋式更新，版本號只留在 zip
  檔名＋launcher 版本資源）；頂層除 ``wizard.exe``／``.env``／``*.pfx``／
  ``*.log`` 外全設 Windows hidden 屬性（``python/``、啟動（除錯）.bat、
  README.txt、執行期才出現的 ``.runtime/``）。zip 條目也把 hidden 寫進
  ``external_attr``（Explorer／7-Zip 解壓會還原；``Expand-Archive``／``tar``
  不會），故 launcher.cs 啟動時也會 best-effort 重設一次。
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import socket
import subprocess
import sys
import time
import tomllib
import urllib.error
import urllib.request
from collections.abc import Callable
from pathlib import Path

PYTHON_VERSION_PIN = "3.12.13"
"""內嵌 runtime 版本：與開發環境同版（uv 已管理，見 `uv python list`）。"""

APP_TITLE = "Shioaji測試精靈"
"""人類可讀顯示名（視窗標題／README 標題／launcher 版本資源用）。"""

BUNDLE_DIR_NAME = "shioaji_wizard"
"""bundle 目錄名：ASCII、不帶版本（2026-08-22 使用者拍板——解壓／更新後
使用者看到的資料夾名稱固定，版本號只留在 zip 檔名與 exe 版本資源）。"""

LAUNCHER_EXE_NAME = "wizard.exe"
"""launcher 執行檔名：唯一在 bundle 頂層維持可見（不設 hidden）的項目。"""

RELEASE_SLUG = "shioaji-wizard"
"""發布 zip 的 ASCII 檔名前綴（bundle 目錄名已是 ASCII，這裡只是保留與
fcn-pricing 一致的『zip 另有專屬命名』慣例，供之後若改回中文顯示名時不必
再補一次這道 guard）。"""

CSC_PATH = Path(r"C:\Windows\Microsoft.NET\Framework64\v4.0.30319\csc.exe")
"""Windows 內建 .NET Framework C# 編譯器（每台 Win10/11 皆有）。"""

REPO_ROOT = Path(__file__).resolve().parents[1]
DIST_DIR = REPO_ROOT / "dist"

MAX_FILES = 5000
"""bundle 落地檔案數上限（硬性，超過即中止）。理由同 fcn-pricing：解壓／
防毒掃描時間隨檔案數線性、收件端環境對檔案數可能有硬限制。本專案依賴極
少（fastapi／uvicorn／shioaji 三家的傳遞相依，無 numpy／pandas／
matplotlib），預期遠低於此上限；上限沿用同一常數只是不重造一把尺。"""

PRUNE_PATHS = (
    # 下列全部來自 CPython standalone runtime 本身（與裝了哪些套件無關）：
    # GUI 工具鏈——server 端無 GUI、桌面殼吃系統 Chromium，不需要 tcl/tk。
    "python/tcl",
    "python/Lib/tkinter",
    "python/Lib/idlelib",
    "python/Lib/turtledemo",
    # bundle 永遠不裝套件。
    "python/Lib/site-packages/pip",
    # CPython 的 C 擴充編譯標頭：bundle 不編譯任何東西。
    "python/include",
)
"""目錄級瘦身清單。清單內路徑不存在時靜默跳過（相依演進容忍）。

fcn-pricing 版另有 jedi／parso／debugpy／numpy.f2py／matplotlib 相關／
pygments／rich／markdown_it／mdurl 等條目——**本專案的 vendored
site-packages 實測不含這些套件**（`uv export --no-dev` 已排除
pytest/ruff/ty/httpx 等開發群組；shioaji 是單一巨大 ``_core.pyd``，不像
fcn 那樣拉進 numpy/pandas/matplotlib），故未照抄，避免清單留著永遠命中
不到的死條目。每一條都必須有實測依據：拿掉後跑得起來才留在清單裡。
"""

_TEST_DIR_NAMES = frozenset({"tests", "test"})
"""wheel 隨附的測試套件。刻意不含 ``testing``——公開 API 慣例同 fcn 版。"""

_DEAD_SUFFIXES = (
    ".pyi",  # 型別存根：只有型別檢查器讀，直譯器不讀（含 shioaji/_core.pyi）
    ".h",
    ".hpp",
    ".c",
    ".pyx",
    ".pxd",
    ".pxi",  # 編譯擴充用的標頭與原始碼
)
"""**絕不含 ``.pyd``**——那是 shioaji 的原生擴充本體（``_core.pyd``），是
本專案唯一的執行期真風險點，刪了 bundle 就直接開不了機。"""

_DEAD_DISTINFO_NAMES = frozenset(
    {"RECORD", "WHEEL", "INSTALLER", "REQUESTED", "top_level.txt", "entry_points.txt"}
)
"""dist-info 內只留 ``METADATA``（``importlib.metadata.version`` 讀它——
``server.py`` 的 ``APP_VERSION``／``/api/export-log`` 都靠這個）。"""

ASSET_CHECKLIST = ("static/index.html", "static/favicon.ico")
"""wheel 必含的非 .py 資料檔：漏包在 build 時就炸，不留到收件者機器。
（``index.html`` 內嵌全部 CSS／JS，無外部靜態檔可漏；``favicon.ico`` 目前
只被 ``compile_launcher`` 直接從原始碼樹讀去當 exe 圖示，不是執行期靠
wheel 讀，但清單本就該跟著 ``static/`` 目錄實際內容走，見
``tests/test_build_bundle.py`` 的反推守衛。）"""


class BuildError(RuntimeError):
    """打包中止（訊息繁中、含修復指引）。"""


def bundle_name(version: str) -> str:
    """bundle 目錄名——**刻意不含版本**（見模組頂 docstring）。``version``
    參數保留供呼叫端一致的介面／未來若改回帶版本命名時少改一處，目前值
    本身不影響回傳。"""
    del version
    return BUNDLE_DIR_NAME


def parse_semver(v: str) -> tuple[int, int, int]:
    """``'v1.0.0'`` 或 ``'1.0.0'`` → ``(1, 0, 0)``；非三段整數即 ValueError。"""
    parts = v.lstrip("v").split(".")
    if len(parts) != 3:
        raise ValueError(f"版本號需為三段 x.y.z：{v!r}")
    return (int(parts[0]), int(parts[1]), int(parts[2]))


def read_project_version(pyproject: Path) -> str:
    data = tomllib.loads(pyproject.read_text(encoding="utf-8"))
    return data["project"]["version"]


def missing_assets(site_packages: Path) -> list[str]:
    pkg = site_packages / "shioaji_wizard"
    return [rel for rel in ASSET_CHECKLIST if not (pkg / rel).is_file()]


def _prune_files(root: Path, is_dead: Callable[[str], bool]) -> int:
    """刪掉 ``root`` 下所有 ``is_dead(相對posix路徑)`` 為真的檔案，回傳刪除數。"""
    n = 0
    for p in list(root.rglob("*")):
        if not p.is_file():
            continue
        if is_dead(p.relative_to(root).as_posix()):
            p.unlink(missing_ok=True)
            n += 1
    return n


def prune_bundle(bundle: Path) -> tuple[list[str], int]:
    """兩段瘦身：目錄清單 → site-packages 檔案級。

    回 ``(裁掉的目錄清單, 額外刪除的檔案數)``（供 log）。
    """
    pruned: list[str] = []
    for rel in PRUNE_PATHS:
        target = bundle / rel
        if target.is_dir():
            shutil.rmtree(target)
            pruned.append(rel)

    n_files = 0
    site = bundle / "python" / "Lib" / "site-packages"
    if site.is_dir():

        def dead_site(rel: str) -> bool:
            parts = rel.split("/")
            if any(seg in _TEST_DIR_NAMES for seg in parts[:-1]):
                return True
            if rel.endswith(_DEAD_SUFFIXES):
                return True
            if ".dist-info/" in rel:
                if "/licenses/" in rel:
                    return True
                if parts[-1] in _DEAD_DISTINFO_NAMES:
                    return True
            return False

        n_files += _prune_files(site, dead_site)

    return pruned, n_files


def count_files(root: Path) -> int:
    return sum(1 for p in root.rglob("*") if p.is_file())


def assert_file_budget(bundle: Path, *, allow_oversize: bool = False) -> int:
    """檔案數超過 ``MAX_FILES`` 即中止，並印出最肥的目錄供裁剪參考。

    ``allow_oversize``（對應 ``--allow-oversize``）是明示的逃生門：超標
    照樣產出 bundle，只把超額印在 log 上。預設仍是硬性中止。
    """
    n = count_files(bundle)
    if n <= MAX_FILES:
        return n
    if allow_oversize:
        print(
            f"警告：檔案數 {n:,} 超過上限 {MAX_FILES:,}（超出 {n - MAX_FILES:,}）"
            "——已明示放行，請記入下一輪瘦身項目。"
        )
        return n
    tally: dict[str, int] = {}
    for p in bundle.rglob("*"):
        if not p.is_file():
            continue
        parts = p.relative_to(bundle).parts
        key = "/".join(parts[: min(4, len(parts) - 1)]) or "."
        tally[key] = tally.get(key, 0) + 1
    top = sorted(tally.items(), key=lambda kv: -kv[1])[:15]
    detail = "\n".join(f"    {v:6,}  {k}" for k, v in top)
    raise BuildError(
        f"bundle 檔案數 {n:,} 超過上限 {MAX_FILES:,}（超出 {n - MAX_FILES:,}）。\n"
        f"  最肥的目錄：\n{detail}\n"
        "  請回頭裁剪（PRUNE_PATHS／_prune_files），**不要調高上限**；\n"
        "  每加一條裁剪都要先實測拿掉後仍跑得起來。"
    )


def sweep_pycache(bundle: Path) -> int:
    """清掉 bundle 內所有既有 ``__pycache__``。來源是 ``stage_runtime`` 對
    開發機 uv 管理 standalone runtime 的 ``copytree``——該 runtime 若已被
    用過，複製時會把已存在的 .pyc 一併帶進 bundle；``isolated_env`` 的
    ``PYTHONDONTWRITEBYTECODE`` 只擋 verify「之後新產生」的，擋不了「複製
    進來時就已存在」的。回傳清掉的目錄數（供 log）。"""
    n = 0
    for d in list(bundle.rglob("__pycache__")):  # 先物化清單：邊走邊刪會亂 rglob 走訪
        if d.is_dir():
            shutil.rmtree(d)
            n += 1
    return n


def assert_native_extension_present(site_packages: Path) -> None:
    """本專案唯一的原生擴充相依：shioaji。確認 vendored 進來的是真的
    Windows ``.pyd``（而非誤裝到跨平台 sdist 或漏裝），否則 bundle 一定是
    啟動即炸——這一步比 ``missing_assets`` 更關鍵，值得單獨一道明確錯誤。"""
    hits = list((site_packages / "shioaji").glob("_core*.pyd"))
    if not hits:
        raise BuildError(
            "vendored site-packages 找不到 shioaji/_core*.pyd——"
            "可能裝到非 Windows wheel、sdist 現場編譯失敗，或版本解析錯誤"
            "（bundle 啟動就會炸，已中止）"
        )


def render_debug_bat() -> str:
    """除錯啟動器（console 保留＋pause）。內容全 ASCII（中文只出現在
    檔名），避開 .bat 的 codepage 雷。"""
    return (
        "@echo off\r\n"
        'cd /d "%~dp0"\r\n'
        '"%~dp0python\\python.exe" -m shioaji_wizard --root "%~dp0."\r\n'
        "pause\r\n"
    )


def render_readme(version: str) -> str:
    return f"""Shioaji 測試精靈 v{version}
====================

永豐 Shioaji 開通測試精靈：填 API 金鑰／憑證 → 模擬下單測試／正式環境
測試 → 逐項列出通過與否及原因。

使用方式
--------
1. 雙擊「wizard.exe」即開啟應用視窗。
2. 第一次使用：把永豐提供的憑證檔複製到本資料夾，改名為
   「Sinopac.pfx」；並在畫面上填入 API 金鑰／憑證密碼。
3. 關閉視窗即自動結束背景服務，無需其他操作。

SmartScreen 一直跳「已保護您的電腦」？
------------------------------------
成因：從網路下載的檔案帶有「來源標記(Mark of the Web)」，部分解壓縮
軟體（WinRAR／7-Zip 等）會把它一併套到解出來的 .exe，導致每次啟動都被
攔。點「其他資訊」→「仍要執行」只對當次有效，故會反覆跳出。擇一根治：

- 立即解除（對現在這一份）：在「wizard.exe」上右鍵 → 內容 →
  勾「解除封鎖(Unblock)」→ 確定，之後啟動就不再跳。
- 下次下載新版時：先在下載的壓縮檔（.zip）上右鍵 → 內容 →
  勾「解除封鎖」→ 確定，再解壓縮，解出來的檔案就是乾淨的。

系統需求
--------
- Windows 10／11（x64），已安裝 Microsoft Edge 或 Google Chrome
  （Windows 內建 Edge 即可）。
- 不需安裝 Python 或任何開發環境。

安全與免責聲明
--------------
- 本軟體為個人開發的開源工具，非永豐金證券官方軟體，與永豐金證券無任何
  隸屬或合作關係。
- 金鑰只在你的電腦與永豐官方 API 之間傳輸：API Key／Secret Key／憑證密碼
  由你輸入後存在本資料夾的「.env」，測試時由本機子行程讀取、交給永豐官方
  shioaji SDK 以加密連線（HTTPS）連永豐伺服器。本軟體不會把金鑰、憑證或
  帳號資料送到永豐以外的伺服器，開發者也無從取得。唯一的第三方連線：
  週一～五 18:00–20:00（永豐限台灣 IP 的時段）開啟程式或執行 A 時，會向
  ipinfo.io 查詢你的公網 IP 所屬國家、提醒你測試是否會被採計。ipinfo.io
  因此會收到你的公網 IP（任何網路請求都會帶的資訊）並回傳 IP 與國家；這個
  請求不會送出 API 金鑰、憑證、密碼或帳號資料。
- 傳遞金鑰的唯一用途是「模擬環境連線測試（A）」與「正式環境憑證連線測試
  （B）」：A 只在永豐模擬環境下單（不影響真實帳戶），B 只登入與查詢、不送出
  任何委託。
- 金鑰與憑證由你自行保管：「.env」與「Sinopac.pfx」是明文檔案，請勿分享、
  上傳或放進雲端同步資料夾；本軟體與開發者不負任何金鑰（token）／憑證保管
  責任。若不放心，測試完成後可到永豐 API 管理頁重新產生金鑰。
- 本軟體按「現狀」提供，不提供任何明示或默示之擔保。因使用本軟體、金鑰或
  憑證外洩、帳戶操作、永豐服務或規則變更所造成之任何損失（含交易損失），
  開發者概不負責。繼續使用即表示你同意以上條款。

資料與疑難排解
--------------
- 「.env」（API 金鑰等設定）與「Sinopac.pfx」（憑證檔）務必放在本資料夾
  根層，**勿放進 OneDrive／Google 雲端硬碟等雲端同步資料夾**——同步軟體
  可能鎖檔或延遲寫入，造成憑證讀取失敗。
- 本資料夾內的「python」「.runtime」等其他項目是程式內部檔案，已設為
  隱藏，不需理會（若因解壓縮工具沒保留隱藏屬性而看得到，也不影響運作）。
- 遇到問題：先在畫面上按「匯出除錯紀錄」，會在本資料夾產生一個
  「shioaji_wizard-debug-*.log」檔案，回報問題時請附上（金鑰等機密資訊
  已遮罩）。
- 若視窗根本開不起來，雙擊「啟動（除錯）.bat」可看到即時錯誤訊息。
"""


def render_launcher_version_cs(version: str) -> str:
    """launcher 的組件版本屬性檔（csc 由此自動生成 Win32 版本資源——
    檔案總管「詳細資料」的檔案版本／描述／產品名稱）。版本以 pyproject
    為單一來源、build 時當場產生，不進 repo。"""
    parts = parse_semver(version)
    four = f"{parts[0]}.{parts[1]}.{parts[2]}.0"
    return (
        "using System.Reflection;\n"
        "\n"
        '[assembly: AssemblyTitle("Shioaji 測試精靈")]\n'
        '[assembly: AssemblyProduct("Shioaji 測試精靈（shioaji-wizard）")]\n'
        '[assembly: AssemblyCopyright("Copyright © 2026 Benjamin Teng")]\n'
        f'[assembly: AssemblyVersion("{four}")]\n'
        f'[assembly: AssemblyFileVersion("{four}")]\n'
        f'[assembly: AssemblyInformationalVersion("{version}")]\n'
    )


_KEEP_ENV = (
    "SystemRoot",
    "SystemDrive",
    "COMSPEC",
    "TEMP",
    "TMP",
    "USERPROFILE",
    "APPDATA",
    "LOCALAPPDATA",
    "PROGRAMDATA",
    "USERNAME",
    "NUMBER_OF_PROCESSORS",
    "PROCESSOR_ARCHITECTURE",
)
"""隔離環境保留的 Windows 執行必需變數；其餘（含開發機 Python／uv 相關）
全部剔除。"""


def isolated_env(bundle: Path) -> dict[str, str]:
    """乾淨環境模擬：PATH 僅 bundle python＋系統目錄。``verify_bundle`` 藉此
    證明 bundle 不依賴開發機環境。"""
    env = {k: os.environ[k] for k in _KEEP_ENV if k in os.environ}
    sysroot = env.get("SystemRoot", r"C:\Windows")
    env["PATH"] = os.pathsep.join([str(bundle / "python"), rf"{sysroot}\System32", sysroot])
    env["PYTHONDONTWRITEBYTECODE"] = "1"
    return env


def run_step(
    cmd: list[str],
    *,
    timeout: float,
    what: str,
    cwd: Path | None = None,
    env: dict[str, str] | None = None,
) -> str:
    """跑一步外部指令（list-argv、無 shell、utf-8、顯式 timeout）；失敗
    即 BuildError（帶該步 stderr，供直接定位）。回傳 stdout。"""
    try:
        result = subprocess.run(
            cmd,
            cwd=cwd,
            env=env,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout,
        )
    except FileNotFoundError as e:
        raise BuildError(f"{what}：找不到執行檔 {cmd[0]}（{e}）") from e
    except subprocess.TimeoutExpired as e:
        raise BuildError(f"{what}：逾時（{timeout:.0f}s）") from e
    if result.returncode != 0:
        raise BuildError(f"{what}：失敗（exit={result.returncode}）\n{result.stderr}")
    return result.stdout


def compile_launcher(out_exe: Path, version: str, *, csc: Path = CSC_PATH) -> None:
    """以 Windows 內建 csc.exe 把 launcher.cs 編成 WinExe（無 console）。
    啟動提示視窗用 WinForms，需額外掛 System.Windows.Forms／System.Drawing
    參考。組件版本屬性（``render_launcher_version_cs``）當場產生成臨時 .cs
    一併編譯；編譯完（成功或失敗）即刪除臨時檔，不進 repo。

    icon 目前尚無（``static/favicon.ico`` 未建立）——``/win32icon`` 旗標
    只在該檔存在時才加，未來補上圖示不需改這支腳本。"""
    if not csc.is_file():
        raise BuildError(f"找不到內建 C# 編譯器：{csc}")
    src = Path(__file__).resolve().parent / "launcher.cs"
    ico = REPO_ROOT / "src" / "shioaji_wizard" / "static" / "favicon.ico"
    version_src = out_exe.parent / "launcher_version.cs"
    # utf-8-sig：csc 需 BOM 才能正確讀出 UTF-8 中文組件屬性字串。
    version_src.write_text(render_launcher_version_cs(version), encoding="utf-8-sig")
    cmd = [
        str(csc),
        "/nologo",
        "/target:winexe",
        "/codepage:65001",  # launcher.cs 是 UTF-8（無 BOM），明示避免被當成系統 ANSI（big5）
        "/r:System.Windows.Forms.dll",
        "/r:System.Drawing.dll",
        f"/out:{out_exe}",
    ]
    if ico.is_file():
        cmd.append(f"/win32icon:{ico}")
    cmd += [str(src), str(version_src)]
    try:
        run_step(cmd, timeout=120, what="csc 編譯 launcher")
    finally:
        version_src.unlink(missing_ok=True)


def preflight() -> str:
    """前置檢查：工具存在＋回傳 pyproject 版本。

    不含 fcn-pricing 版的「版本↔GitHub tag 同步」守衛——本專案尚無 release
    tag 慣例（2026-08-22 拍板：省略，日後若建立發版流程再補）。
    """
    for tool in ("uv", "git"):
        if shutil.which(tool) is None:
            raise BuildError(f"PATH 找不到 {tool}")
    if not CSC_PATH.is_file():
        raise BuildError(f"找不到內建 C# 編譯器：{CSC_PATH}")
    return read_project_version(REPO_ROOT / "pyproject.toml")


def stage_runtime(bundle: Path) -> Path:
    """複製 uv 管理的 3.12.13 standalone → bundle/python。``--managed-python``
    強制取 uv 管理的安裝（而非專案 .venv——venv 只是殼、copytree 會壞）；
    需 ``--system`` 避免 uv 的專案內文自動探索返回 repo .venv 直譯器。"""
    run_step(
        ["uv", "python", "install", PYTHON_VERSION_PIN],
        timeout=600,
        what="uv python install（冪等）",
    )
    exe = Path(
        run_step(
            ["uv", "python", "find", "--managed-python", "--system", PYTHON_VERSION_PIN],
            timeout=60,
            what="uv python find",
        ).strip()
    )
    if ".venv" in exe.parts:
        raise BuildError(f"uv python find 回傳 venv 直譯器（{exe}），預期 uv 管理的 standalone")
    shutil.copytree(exe.parent, bundle / "python")
    return bundle / "python" / "python.exe"


def vendor_packages(bundle_python: Path, bundle: Path, aux: Path) -> None:
    """uv.lock 同版 vendored 進 bundle site-packages＋專案 wheel（含
    dist-info → 頁尾版本非 "dev"）＋資產斷言＋原生擴充斷言。"""
    site = bundle / "python" / "Lib" / "site-packages"
    req = aux / "requirements.txt"
    run_step(
        [
            "uv",
            "export",
            "--no-dev",
            "--no-emit-project",
            "--format",
            "requirements-txt",
            "-o",
            str(req),
        ],
        cwd=REPO_ROOT,
        timeout=120,
        what="uv export（鎖定版 requirements）",
    )
    run_step(
        [
            "uv",
            "pip",
            "install",
            "--python",
            str(bundle_python),
            "--target",
            str(site),
            "-r",
            str(req),
        ],
        timeout=900,
        what="vendored 套件安裝",
    )
    run_step(
        ["uv", "build", "--wheel", "-o", str(aux / "wheelhouse")],
        cwd=REPO_ROOT,
        timeout=300,
        what="uv build（專案 wheel）",
    )
    wheels = sorted((aux / "wheelhouse").glob("shioaji_wizard-*.whl"))
    if len(wheels) != 1:
        raise BuildError(f"wheelhouse 應恰有一個 shioaji_wizard wheel，實際：{wheels}")
    run_step(
        [
            "uv",
            "pip",
            "install",
            "--python",
            str(bundle_python),
            "--target",
            str(site),
            "--no-deps",
            str(wheels[0]),
        ],
        timeout=120,
        what="專案 wheel 安裝",
    )
    assert_native_extension_present(site)
    missing = missing_assets(site)
    if missing:
        raise BuildError("wheel 缺資料檔（打包地雷，中止）：\n" + "\n".join(missing))


def write_static_files(bundle: Path, version: str) -> None:
    compile_launcher(bundle / LAUNCHER_EXE_NAME, version)
    (bundle / "啟動（除錯）.bat").write_text(render_debug_bat(), encoding="ascii", newline="")
    (bundle / "README.txt").write_text(render_readme(version), encoding="utf-8")


def hide_bundle_top_level(bundle: Path, *, keep: str) -> list[str]:
    """把 bundle 頂層除 ``keep``（launcher exe 檔名）與 ``*.bat``（啟動（除錯）.bat，
    使用者要看得到、視窗開不起來時雙擊它看錯誤）外的所有項目設為 Windows hidden
    屬性——使用者解壓後只看到 ``wizard.exe`` 與除錯 .bat（之後自己放
    ``.env``／``Sinopac.pfx``，程式跑起來後自己產生 ``*.log``）。

    沿用 ``shioaji_wizard.sjenv.hide_path``（該函式已是本專案 hidden 語意
    的單一來源，``.runtime`` 目錄本身也是它建立時就設好的——不重造輪
    子）。zip 解壓可能不保留 hidden 屬性，故 ``launcher.cs`` 啟動時也會
    best-effort 重設一次（見該檔 ``RehideTopLevel``）。

    回傳被隱藏的項目名稱（供 log）。"""
    sys.path.insert(0, str(REPO_ROOT / "src"))
    from shioaji_wizard.sjenv import hide_path

    hidden: list[str] = []
    for entry in sorted(bundle.iterdir()):
        if entry.name == keep or entry.suffix.lower() == ".bat":
            continue
        hide_path(entry)
        hidden.append(entry.name)
    return hidden


def release_zip_name(version: str) -> str:
    return f"{RELEASE_SLUG}-v{version}.zip"


def finalize(tmp_bundle: Path, final: Path, *, make_zip: bool, version: str = "") -> None:
    """交易式收尾：``verify_bundle``（若有）都在 tmp bundle 上先跑過、全過
    才呼叫本函式。舊 bundle 先改名挪開 → promote 新 bundle → 最後才刪舊。
    任一步失敗時磁碟上仍留有完整可用（或可改名回復）的 bundle，不會出現
    「舊的刪了、新的沒上位」的空窗；``--zip`` 另打 :func:`release_zip_name`
    的 ASCII 檔名 zip，壓縮內容的頂層資料夾仍是 ``final.name``（現為
    ``shioaji_wizard``，本身已是 ASCII）。``version`` 留空時回退成與
    bundle 同名（僅供不帶版本的單元測試用）。"""
    # zip 一律從 tmp bundle 打（還沒有任何使用者檔案），絕不能從 final 打——final 可能已被
    # 開發者拿來實測、放了 .env／Sinopac.pfx（2026-08-22 實際發生）。
    if make_zip:
        # 注意不能用 with_suffix：版本號含點（v0.1.0）會被吃掉最後一節
        zip_path = (
            final.with_name(release_zip_name(version)) if version else final.with_name(final.name + ".zip")
        )
        write_release_zip(tmp_bundle, zip_path, top_name=final.name)
    old = final.with_name(final.name + ".old")
    if old.exists():
        shutil.rmtree(old)
    if final.exists():
        os.replace(final, old)
    os.replace(tmp_bundle, final)
    # 舊 bundle 根層的使用者檔案（.env、*.pfx）搬回新 bundle：重建不該吃掉開發者的測試設定
    kept = preserve_user_files(old, final)
    if kept:
        print("保留使用者檔案：" + "、".join(kept))
    shutil.rmtree(old, ignore_errors=True)
    if old.exists():
        print(f"警告：舊 bundle 未能完全清除（{old}），可手動刪除")


USER_FILE_PATTERNS = (".env", "*.pfx")


def _is_user_file(name: str) -> bool:
    return name == ".env" or name.lower().endswith(".pfx")


def _dos_attributes(path: Path) -> int:
    """回傳 Windows 檔案屬性的低 8 位元（MS-DOS 屬性：hidden=0x02、directory=0x10
    …），非 Windows 或讀不到回 0。zip 規格把這一位元組放在 ``external_attr``
    低位元組，Explorer「解壓縮全部」／7-Zip 解壓時會照它還原 hidden。"""
    if os.name != "nt":
        return 0
    import ctypes

    attrs = ctypes.windll.kernel32.GetFileAttributesW(str(path))  # type: ignore[attr-defined]
    return 0 if attrs == -1 else attrs & 0xFF


def write_release_zip(src_dir: Path, zip_path: Path, *, top_name: str) -> None:
    """把 src_dir 打成 zip，頂層資料夾名固定為 top_name；根層的 .env／*.pfx 一律不收
    （雙重保險：就算 src_dir 不乾淨，出貨 zip 也不會帶金鑰）。

    每個條目的 ``external_attr`` 低位元組寫入磁碟上的 MS-DOS 屬性
    （:func:`_dos_attributes`）——``ZipFile.write`` 只會放 Unix mode 到高
    16 位、低位元組永遠是 0，解出來的 ``python/``／README.txt 就不會是
    hidden。Explorer 與 7-Zip 會照這個位元組還原；``Expand-Archive``／``tar``
    不會，那些情境靠 launcher 的 ``RehideTopLevel`` 補。"""
    import zipfile

    with zipfile.ZipFile(zip_path, "w", compression=zipfile.ZIP_DEFLATED) as zf:
        for f in sorted(src_dir.rglob("*")):
            rel = f.relative_to(src_dir)
            if len(rel.parts) == 1 and _is_user_file(rel.name):
                continue
            arc = Path(top_name, *rel.parts).as_posix()
            info = zipfile.ZipInfo.from_file(f, arc)
            info.external_attr |= _dos_attributes(f)
            if f.is_dir():
                zf.writestr(info, b"")
            else:
                info.compress_type = zipfile.ZIP_DEFLATED
                with f.open("rb") as src, zf.open(info, "w") as dst:
                    shutil.copyfileobj(src, dst)


def preserve_user_files(old: Path, new: Path) -> list[str]:
    """把 old 根層的 .env／*.pfx 複製到 new 根層（new 已有同名檔則不覆蓋）。回傳搬過去的檔名。"""
    if not old.is_dir():
        return []
    kept: list[str] = []
    for pat in USER_FILE_PATTERNS:
        for f in old.glob(pat):
            if f.is_file() and not (new / f.name).exists():
                shutil.copy2(f, new / f.name)
                kept.append(f.name)
    return kept


def dir_size_mb(root: Path) -> float:
    return sum(f.stat().st_size for f in root.rglob("*") if f.is_file()) / 1_048_576


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return int(s.getsockname()[1])


def _http(
    method: str,
    url: str,
    body: bytes | None = None,
    headers: dict[str, str] | None = None,
    timeout: float = 30.0,
) -> tuple[int, str]:
    """回 (狀態碼, body 文字)；非 2xx 不拋（HTTPError 也轉成回傳值），
    診斷訊息才輪得到 status 判斷。連線層失敗（拒連、逾時）仍拋 OSError。"""
    req = urllib.request.Request(url, data=body, headers=headers or {}, method=method)  # noqa: S310
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:  # noqa: S310
            return resp.status, resp.read().decode("utf-8", errors="replace")
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode("utf-8", errors="replace")


def _log_tail(log_path: Path, limit: int = 2000) -> str:
    try:
        return log_path.read_text(encoding="utf-8", errors="replace")[-limit:]
    except OSError:
        return "（讀不到 server log）"


def _is_hidden(path: Path) -> bool:
    """讀 Windows hidden 屬性位元（供 ``verify_bundle`` 驗
    ``hide_bundle_top_level`` 的結果）。"""
    import ctypes

    attrs = ctypes.windll.kernel32.GetFileAttributesW(str(path))  # type: ignore[attr-defined]
    _FILE_ATTRIBUTE_HIDDEN = 0x02
    return attrs != -1 and bool(attrs & _FILE_ATTRIBUTE_HIDDEN)


def clean_runtime(bundle: Path) -> None:
    """刪掉 ``<bundle>/.runtime``（verify 起過一次 app 留下的內部檔）；不存在就略過。"""
    rt = bundle / ".runtime"
    if rt.exists():
        shutil.rmtree(rt, ignore_errors=True)


def verify_bundle(bundle: Path) -> None:
    """乾淨環境 smoke：隔離 PATH（bundle python＋``C:\\Windows\\System32``＋
    ``%SystemRoot%``）起 ``-m shioaji_wizard --no-shell`` → 首頁 200 →
    ``/api/state`` 200（含 JSON、``root`` 鍵）→ idle 逾時優雅自關、零殘留
    → 頂層只多出 ``.runtime``（且已 hidden）。

    刻意不碰任何會連永豐的東西：不送 ``/api/env``、不啟動任何測試 job。
    伺服器輸出落 ``dist/<bundle 名>-verify.log``（DIST_DIR 根，非
    ``bundle.parent``，因 bundle 為 tmp 路徑、成功後會被 cleanup 清掉，
    log 需存活）；失敗訊息附 log（尾段）。"""
    port = _free_port()
    base = f"http://127.0.0.1:{port}"
    log_path = DIST_DIR / f"{bundle.name}-verify.log"
    before = {p.name for p in bundle.iterdir()}
    log_f = open(log_path, "w", encoding="utf-8")
    proc = subprocess.Popen(
        [
            str(bundle / "python" / "python.exe"),
            "-m",
            "shioaji_wizard",
            "--no-shell",
            "--port",
            str(port),
            "--root",
            str(bundle),
            "--idle-timeout",
            "20",
        ],
        env=isolated_env(bundle),
        cwd=bundle,
        stdout=log_f,
        stderr=subprocess.STDOUT,
    )
    try:
        deadline = time.monotonic() + 60
        while True:
            try:
                status, body = _http("GET", f"{base}/", timeout=5)
                break
            except OSError:
                if time.monotonic() > deadline:
                    raise BuildError(
                        "verify：app 60s 內未起來\n--- server log 尾段 ---\n" + _log_tail(log_path)
                    ) from None
                time.sleep(0.5)
        if status != 200 or not body.strip():
            raise BuildError(f"verify：首頁非預期回應（status={status}；log：{log_path}）")
        status, state_body = _http("GET", f"{base}/api/state")
        if status != 200:
            raise BuildError(f"verify：/api/state 回 {status}（log：{log_path}）")
        try:
            data = json.loads(state_body)
        except json.JSONDecodeError:
            raise BuildError(f"verify：/api/state 回非 JSON（{state_body[:300]}）") from None
        if "root" not in data:
            raise BuildError(f"verify：/api/state 缺 root 欄位（{state_body[:300]}）")
        try:
            proc.wait(timeout=60)  # 無心跳 → idle 20s＋watchdog 週期內自關
        except subprocess.TimeoutExpired:
            raise BuildError(f"verify：app 未在 idle 逾時後 60s 內自關（log：{log_path}）") from None
        if proc.returncode != 0:
            raise BuildError(f"verify：app 結束碼非 0（{proc.returncode}；log：{log_path}）")
    finally:
        if proc.poll() is None:
            proc.kill()
        log_f.close()
    after = {p.name for p in bundle.iterdir()}
    new_entries = after - before
    if new_entries != {".runtime"}:
        raise BuildError(
            f"verify：跑完後頂層新增項目非預期（預期僅 .runtime，實際："
            f"{sorted(new_entries)}；log：{log_path}）"
        )
    if not _is_hidden(bundle / ".runtime"):
        raise BuildError("verify：.runtime 未設 hidden 屬性")
    print("verify PASS：首頁／/api/state／自動收攤／.runtime 隱藏 全數通過")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="portable bundle 打包（開發機工具）")
    parser.add_argument("--zip", action="store_true", help="另產 ASCII 檔名 zip（發布用）")
    parser.add_argument("--verify", action="store_true", help="build 後接乾淨環境 smoke 驗收")
    parser.add_argument(
        "--allow-oversize",
        action="store_true",
        help=f"檔案數超過 {MAX_FILES} 時照樣產出（只警告，超額記入下一輪瘦身）",
    )
    args = parser.parse_args(argv)

    t0 = time.monotonic()
    version = preflight()
    name = bundle_name(version)
    tmp_root = DIST_DIR / ".tmp"
    if tmp_root.exists():
        shutil.rmtree(tmp_root)  # 上次失敗的驗屍現場，此刻才清
    bundle = tmp_root / name
    bundle.mkdir(parents=True)
    aux = tmp_root / "aux"
    aux.mkdir()

    bundle_python = stage_runtime(bundle)
    vendor_packages(bundle_python, bundle, aux)
    pruned, n_dead = prune_bundle(bundle)
    print(f"瘦身：裁掉 {len(pruned)} 個目錄（{'、'.join(pruned) or '無'}）")
    print(f"瘦身：另刪 {n_dead:,} 個執行期用不到的檔（型別存根／C 標頭／測試／安裝紀錄）")
    n_pycache = sweep_pycache(bundle)
    print(f"瘦身：另清 __pycache__ {n_pycache} 處（runtime 複製帶入的既有 .pyc）")
    write_static_files(bundle, version)
    if args.verify:
        verify_bundle(bundle)  # tmp bundle 上先驗，全過才 promote
        clean_runtime(bundle)  # verify 會留下 .runtime（.app-ready／.app-status／app.log），出貨要乾淨
    hidden = hide_bundle_top_level(bundle, keep=LAUNCHER_EXE_NAME)
    print(f"隱藏：頂層設 hidden {len(hidden)} 項（{'、'.join(hidden)}）")
    n_files = assert_file_budget(bundle, allow_oversize=args.allow_oversize)
    print(f"檔案數 {n_files:,} / 上限 {MAX_FILES:,}（餘裕 {MAX_FILES - n_files:,}）")
    stray_pyc = [p for p in bundle.rglob("__pycache__") if p.is_dir()]
    if stray_pyc:
        raise BuildError(f"bundle 內仍有 __pycache__（{len(stray_pyc)} 處）——PYTHONDONTWRITEBYTECODE 失效？")
    final = DIST_DIR / name
    finalize(bundle, final, make_zip=args.zip, version=version)
    shutil.rmtree(tmp_root, ignore_errors=True)  # 成功後清 aux
    if tmp_root.exists():
        print(f"警告：暫存目錄未能完全清除（{tmp_root}）")

    size = dir_size_mb(final)
    file_count = count_files(final)
    print(f"bundle 完成：{final}")
    print(
        f"體積 {size:,.0f} MB｜檔案數 {file_count:,}／上限 {MAX_FILES:,}｜"
        f"Python {PYTHON_VERSION_PIN}｜"
        f"耗時 {time.monotonic() - t0:,.0f}s"
    )
    if size >= 1024:
        print("警告：體積超過 1GB 軟目標（不擋，記錄實測）")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except BuildError as e:
        print(f"打包中止：{e}")
        raise SystemExit(1) from None
