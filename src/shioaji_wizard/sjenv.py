"""共用：定位根目錄（放 .env、Sinopac.pfx 的地方）、讀取 .env、測試結果回報。

根目錄 = 環境變數 SJ_ENV_DIR；沒有就取目前工作目錄。
"""

from __future__ import annotations

import json
import os
from pathlib import Path

SRC_DIR = Path(__file__).resolve().parent
# 根目錄（放 .env／Sinopac.pfx）：一律由啟動端用 SJ_ENV_DIR 指定（桌面殼＝程式所在資料夾）；
# 沒設就用目前工作目錄（開發時在專案根目錄跑）。
ROOT = Path(os.environ.get("SJ_ENV_DIR") or Path.cwd()).resolve()
ENV_PATH = ROOT / ".env"
DEFAULT_PFX = ROOT / "Sinopac.pfx"
# 內部檔案（app.log、shioaji.log、.app-ready、.app-status）全放這個隱藏子資料夾，
# 使用者看到的根層只有 wizard.exe／.env／Sinopac.pfx／匯出的 .log。
RUNTIME = ROOT / ".runtime"


def ensure_runtime_dir() -> Path:
    """建立 .runtime 並（Windows）設隱藏屬性；失敗不上拋。"""
    try:
        RUNTIME.mkdir(parents=True, exist_ok=True)
    except OSError:
        return RUNTIME
    hide_path(RUNTIME)
    return RUNTIME


def hide_path(path: Path) -> None:
    """Windows 設 hidden 屬性（best-effort）；其他平台不動（點開頭的名字本來就隱藏）。"""
    if os.name != "nt":
        return
    try:
        import ctypes

        FILE_ATTRIBUTE_HIDDEN = 0x02
        attrs = ctypes.windll.kernel32.GetFileAttributesW(str(path))  # type: ignore[attr-defined]
        if attrs != -1 and not attrs & FILE_ATTRIBUTE_HIDDEN:
            ctypes.windll.kernel32.SetFileAttributesW(str(path), attrs | FILE_ATTRIBUTE_HIDDEN)  # type: ignore[attr-defined]
    except (OSError, AttributeError):
        pass


REQUIRED_KEYS = ("SJ_API_KEY", "SJ_SEC_KEY")
OPTIONAL_KEYS = ("SJ_CA_PASSWD", "SJ_CA_PATH")

PASS, FAIL, SKIP = "PASS", "FAIL", "SKIP"
_MARK = {PASS: "✓ 通過", FAIL: "✗ 未通過", SKIP: "－ 未測"}


# ---------------------------------------------------------------- .env
def parse_env_text(text: str) -> tuple[dict[str, str], list[str]]:
    """回傳 (鍵值, 格式問題清單)。值會去掉首尾空白與成對引號。"""
    values: dict[str, str] = {}
    problems: list[str] = []
    for no, raw in enumerate(text.splitlines(), 1):
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if "=" not in line:
            problems.append(f"第 {no} 行沒有「=」：{raw.strip()[:40]}")
            continue
        k, v = line.split("=", 1)
        k = k.strip()
        v = v.strip()
        if k.lower().startswith("export "):
            k = k[7:].strip()
        if not k or " " in k:
            problems.append(f"第 {no} 行的名稱不合法：{k!r}")
            continue
        if len(v) >= 2 and v[0] == v[-1] and v[0] in "\"'":
            v = v[1:-1]
        if k in values:
            problems.append(f"第 {no} 行重複設定 {k}，以後者為準")
        values[k] = v
    return values, problems


def load_env() -> dict[str, str]:
    if not ENV_PATH.is_file():
        return {}
    values, _ = parse_env_text(ENV_PATH.read_text(encoding="utf-8-sig"))
    return values


def pfx_path(env: dict[str, str]) -> Path:
    """SJ_CA_PATH 有設就尊重（相對路徑以根目錄為基準）；沒設才用同資料夾的 Sinopac.pfx。"""
    p = env.get("SJ_CA_PATH")
    if not p:
        return DEFAULT_PFX
    path = Path(p).expanduser()
    return path if path.is_absolute() else (ROOT / path)


def ensure_env_keys(path: Path, defaults: dict[str, str]) -> list[str]:
    """.env 缺哪些鍵就在檔尾補上「鍵=預設值」；回傳補上的鍵名。已存在的（含空值）一律不動。"""
    text = path.read_text(encoding="utf-8-sig") if path.is_file() else ""
    present, _ = parse_env_text(text)
    added: list[str] = []
    lines: list[str] = []
    for k, v in defaults.items():
        if k not in present:
            lines.append(f"{k}={v}")
            added.append(k)
    if added:
        sep = "" if (not text or text.endswith("\n")) else "\n"
        path.write_text(text + sep + "\n".join(lines) + "\n", encoding="utf-8")
    return added


# ---------------------------------------------------------------- 測試結果
class Report:
    """收集個別測試的 通過／未通過／未測 與原因；可印總覽，也可寫 JSON 給精靈彙整。

    環境變數 SJ_REPORT_FILE 有設時，每次 add 都會把整份結果重寫到該檔（JSON），
    精靈（wizard.py）用它把 A／B 兩支腳本的結果合併成一張總表。
    """

    def __init__(self) -> None:
        self.items: list[dict[str, str]] = []
        self._file = os.environ.get("SJ_REPORT_FILE") or ""

    def add(self, name: str, status: str, reason: str = "") -> None:
        self.items.append({"name": name, "status": status, "reason": reason})
        mark = _MARK[status]
        print(f"  [{name}] {mark}" + (f" — {reason}" if reason else ""))
        if self._file:
            Path(self._file).write_text(
                json.dumps(self.items, ensure_ascii=False, indent=1), encoding="utf-8"
            )

    def ok(self, name: str, reason: str = "") -> None:
        self.add(name, PASS, reason)

    def fail(self, name: str, reason: str) -> None:
        self.add(name, FAIL, reason)

    def skip(self, name: str, reason: str) -> None:
        self.add(name, SKIP, reason)

    @property
    def all_passed(self) -> bool:
        return all(i["status"] != FAIL for i in self.items)


def print_summary(items: list[dict[str, str]], title: str = "測試結果總覽") -> None:
    """列出每一項測試：通過／未通過／未測，未通過的把原因寫清楚。
    版面：═══ 框線；每列「結果 ＋ 項目」，原因另起一行固定縮排——不靠欄寬對齊，
    窄視窗（GUI 的輸出區）折行也不會跑版。SJ_NO_TEXT_SUMMARY=1 可關掉。"""
    if os.environ.get("SJ_NO_TEXT_SUMMARY"):
        return
    line = "═" * 48
    thin = "─" * 48
    print()
    print(line)
    print(f" {title}")
    print(line)
    if not items:
        print("  （本次沒有執行任何測試）")
    for i in items:
        mark = _MARK[i["status"]]
        print(f" {mark:<6} {i['name']}")
        if i["reason"] and i["status"] != PASS:
            print(f"        原因：{i['reason']}")
    n_fail = sum(1 for i in items if i["status"] == FAIL)
    n_pass = sum(1 for i in items if i["status"] == PASS)
    n_skip = sum(1 for i in items if i["status"] == SKIP)
    print(thin)
    print(f" 通過 {n_pass}　未通過 {n_fail}　未測 {n_skip}")
    print(line)
