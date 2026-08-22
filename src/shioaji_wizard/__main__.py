"""桌面 app 啟動入口：``python -m shioaji_wizard``（bundle 的啟動器以內嵌
Python 呼叫此入口，傳入 exe 所在資料夾）。

使用者看到的根目錄只留 ``wizard.exe``／``.env``／``Sinopac.pfx``／匯出的
``.log``；其餘內部檔案（``app.log``、``.app-status``、``.app-ready``）全放
進隱藏子目錄 ``.runtime``（見 ``sjenv.ensure_runtime_dir``）。
"""

from __future__ import annotations

import argparse
import multiprocessing
import os
import sys
from pathlib import Path


def _redirect_output_to_log(log_path: Path) -> None:
    """pythonw 殼（無 console，``sys.stdout``／``sys.stderr`` 為 None）下把
    輸出導到 log 檔：每次啟動 truncate（單次 session log，KISS）、行緩衝
    讓當機前最後訊息也落地。開檔失敗（權限、磁碟滿等）時放棄重導
    （best-effort）：print 對 None stream 本就靜默略過，app 照常啟動、
    僅少了 log，勝過在無 console 下悶死。"""
    try:
        log_path.parent.mkdir(parents=True, exist_ok=True)
        stream = open(log_path, "w", encoding="utf-8", buffering=1)
    except OSError:
        return
    sys.stdout = stream
    sys.stderr = stream


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="shioaji-wizard",
        description="Shioaji 測試精靈（本機 FastAPI＋Chromium 殼）",
    )
    parser.add_argument(
        "--root",
        type=Path,
        default=Path.cwd(),
        help="資料區根目錄（.env／Sinopac.pfx／.runtime 所在；預設目前工作目錄）",
    )
    parser.add_argument(
        "--idle-timeout",
        type=float,
        default=90.0,
        help="無心跳閒置自動關伺服器的秒數（預設 90；job 執行中不關）",
    )
    parser.add_argument(
        "--no-shell",
        action="store_true",
        help="不自動開瀏覽器殼（僅起服務，供除錯／自動化）",
    )
    parser.add_argument(
        "--port",
        type=int,
        default=None,
        help="固定 port（預設 None＝動態選空閒 port；供除錯／自動化）",
    )
    args = parser.parse_args(argv)

    root = args.root.resolve()
    # SJ_ENV_DIR 必須在任何觸發 sjenv.ROOT 計算的 import 之前設好
    # （sjenv.ROOT 於 import 時就地計算，見該模組頂部）。
    os.environ["SJ_ENV_DIR"] = str(root)

    from shioaji_wizard.sjenv import RUNTIME, ensure_runtime_dir

    ensure_runtime_dir()

    if sys.stdout is None or sys.stderr is None:
        _redirect_output_to_log(RUNTIME / "app.log")

    from shioaji_wizard.status import write_status

    write_status(RUNTIME, "啟動中…")

    from shioaji_wizard.desktop import serve

    serve(
        root,
        idle_timeout=args.idle_timeout,
        open_shell=not args.no_shell,
        port=args.port,
    )
    return 0


if __name__ == "__main__":
    multiprocessing.freeze_support()
    raise SystemExit(main())
