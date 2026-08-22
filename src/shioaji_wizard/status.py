"""啟動階段狀態訊號（launcher 提示視窗的即時狀態行）。

``<root>/.app-status`` 單行覆寫；launcher 每 100ms 讀取顯示、啟動前先刪
舊檔。best-effort：寫入失敗不擋啟動（無 launcher 場景只是多一個小檔）。"""

from __future__ import annotations

from pathlib import Path


def write_status(out_root: Path, text: str) -> None:
    try:
        (Path(out_root) / ".app-status").write_text(text, encoding="utf-8")
    except OSError:
        pass
