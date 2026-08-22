"""桌面 app 伺服器生命週期（原樣移植自 fcn-pricing 的 ``fcn_report.app.server``，
接到本專案的 ``shioaji_wizard.server.app``）。

關機決策（與 fcn-pricing OQ4 拍板一致，**視窗存活為主**）：
- **job 執行中一律不關**（鐵則，避免砍出半成品；本專案的 job 是 A／B 測試
  子行程，見 ``shioaji_wizard.server._job``）。
- 追蹤到殼視窗主行程（``shell.resolve_window``；spawn 行程可能只是
  launcher、handoff 後早退不可靠）→ 主行程在即不關（即使無心跳）、主行程
  退出（＝視窗被關）即收攤。
- 追蹤不到視窗（``--no-shell``／預設瀏覽器 fallback／resolve 失敗）→ 退回
  「閒置逾 ``idle_timeout`` 秒（無心跳）才關」，心跳來源固定為
  ``shioaji_wizard.server.heartbeat_age()``（單一時鐘來源，不另開一份）。
"""

from __future__ import annotations

import shutil
import socket
import threading
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any

from shioaji_wizard import shell
from shioaji_wizard.sjenv import ensure_runtime_dir
from shioaji_wizard.status import write_status

_DEFAULT_IDLE_TIMEOUT = 90.0
"""無心跳（且無視窗可追蹤）閒置自動關的秒數。"""

_WATCHDOG_INTERVAL = 5.0
"""watchdog 檢查週期（秒）；遠小於 idle_timeout，關窗後最遲約一個週期內收攤。"""

_WINDOW_SIZE = (1040, 820)
"""殼視窗預設大小（本專案不做視窗大小持久化，恆用此值）。"""


def _free_port() -> int:
    """向 OS 要一個空閒 TCP port（綁 ``127.0.0.1:0`` 後讀回實際 port）。
    單使用者本機場景，綁定與實際啟用間的競態可忽略。"""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return int(s.getsockname()[1])


class AppState:
    """跨路由共享狀態：關機決策 :meth:`should_shutdown` 所需的三個輸入——
    job 是否執行中、殼視窗 tracker、心跳時鐘——皆以注入的 callable 取得，
    不在此另開一份計時器（心跳單一來源＝``server.heartbeat_age()``）。"""

    def __init__(
        self,
        *,
        job_running: Callable[[], bool],
        heartbeat_age: Callable[[], float],
        idle_timeout: float = _DEFAULT_IDLE_TIMEOUT,
    ) -> None:
        self.idle_timeout = idle_timeout
        self.server: Any = None
        # 殼視窗主行程 tracker（duck-typed：alive()／terminate()／close()，
        # 見 shell.WindowTracker）。None＝無殼／末階 fallback／resolve 失敗
        # （改由心跳逾時收攤）。
        self.window: Any = None
        self._job_running = job_running
        self._heartbeat_age = heartbeat_age
        self.cancel_job: Callable[[], bool] = lambda: False
        self.cancel_dialog: Callable[[], bool] = lambda: False

    def should_shutdown(self) -> bool:
        """關機決策（視窗存活為主）：job 執行中不關；但視窗已關而 job 還在跑時，
        先把 job 殺掉（測試是查詢／模擬單，中止無副作用；子行程另有逾時上限），
        下一輪再關。有視窗 tracker → 視窗存活＝伺服器存活；無 tracker → 退回
        「閒置逾 ``idle_timeout``（無心跳）」判斷。"""
        if self._job_running():
            if self.window is not None and not self.window.alive():
                self.cancel_job()
            return False
        if self.window is not None:
            if not self.window.alive():
                self.cancel_dialog()  # 還開著的選檔對話框會卡住關機（in-flight 請求），先殺
                return True
            return False
        return self._heartbeat_age() > self.idle_timeout


def _watchdog_loop(
    state: AppState,
    *,
    interval: float = _WATCHDOG_INTERVAL,
    stop_event: threading.Event,
) -> None:
    """背景 watchdog：每 ``interval`` 秒檢查一次關機決策。成立時令
    ``state.server.should_exit = True`` 並返回；``stop_event`` 被設定
    （伺服器已自行結束）時直接返回、不觸發關機。"""
    while not stop_event.wait(interval):
        if state.should_shutdown():
            if state.server is not None:
                state.server.should_exit = True
            return


def _wait_until_started(server: Any, *, timeout: float = 15.0, poll: float = 0.05) -> None:
    """輪詢 uvicorn ``Server.started`` 直到啟動完成或逾時。用於在殼開窗前
    確保服務已可接受連線（僅啟動一次的短輪詢，非熱迴圈）。"""
    deadline = time.monotonic() + timeout
    while not getattr(server, "started", False):
        if time.monotonic() > deadline:
            raise RuntimeError(f"uvicorn 未能在 {timeout}s 內啟動")
        time.sleep(poll)


def _close_browser(result: Any, window: Any = None) -> None:
    """收攤時關掉殼視窗並清理暫時 profile。優先終止 ``window``（真主行程
    tracker；spawn 行程可能只是早退的 launcher），launcher 若仍存活也一併
    終止；無 tracker（fallback 模式）則以 profile 指紋 best-effort 查殺主
    行程。等主行程真的結束（檔案鎖釋放）後帶重試清 profile。行程已結束
    （使用者自行關過視窗）→ 只清 profile；全程容錯，不讓關窗細節卡住收攤。"""
    if result is None:
        return
    if window is not None:
        try:
            window.terminate()
        except Exception:  # noqa: BLE001, S110 - 關窗盡力而為，不阻擋收攤
            pass
    proc = result.process
    if proc is not None and proc.poll() is None:
        try:
            proc.terminate()
            proc.wait(timeout=5)
        except Exception:  # noqa: BLE001, S110 - 關窗盡力而為，不阻擋收攤
            pass
    if window is not None:
        try:
            for _ in range(20):  # 最多等 5s 主行程收攤、釋放 profile 檔案鎖
                if not window.alive():
                    break
                time.sleep(0.25)
            window.close()
        except Exception:  # noqa: BLE001, S110 - 關窗盡力而為，不阻擋收攤
            pass
    else:
        try:
            shell.kill_instance(result)
        except Exception:  # noqa: BLE001, S110 - best-effort，不阻擋收攤
            pass
    profile = result.profile_dir
    if profile is not None:
        for _ in range(5):  # 鎖釋放與行程收尾非同步，帶重試
            shutil.rmtree(profile, ignore_errors=True)
            if not profile.exists():
                break
            time.sleep(0.5)


def _touch_ready_marker(out_root: Path) -> None:
    """啟動就緒訊號：launcher 的啟動提示視窗輪詢 ``<root>/.app-ready``，
    見檔即關閉提示。best-effort（失敗不擋服務）；launcher 每次啟動前會
    先刪舊檔，故不需清理。"""
    try:
        (Path(out_root) / ".app-ready").write_text("", encoding="utf-8")
    except OSError:
        pass


def serve(
    root: Path,
    *,
    idle_timeout: float = _DEFAULT_IDLE_TIMEOUT,
    open_shell: bool = True,
    host: str = "127.0.0.1",
    port: int | None = None,
) -> None:
    """啟動桌面 app：起 uvicorn（僅綁 ``host``，動態空閒 port）→ 等啟動完成
    → 開系統 Chromium ``--app`` 視窗（或末階 fallback）→ 追蹤視窗主行程 →
    起 watchdog → block 到伺服器 ``should_exit``（視窗被關；或 fallback 模式
    心跳逾時，且皆須無 job）後收攤。

    ``root``（同 ``sjenv.ROOT``：呼叫端須在 import 本模組前先設好
    ``SJ_ENV_DIR``）只用來確認／建立 ``.runtime`` 隱藏子目錄——使用者看到
    的根層只留 ``wizard.exe``／``.env``／``Sinopac.pfx``／匯出的
    ``.log``，``.app-status``／``.app-ready`` 一律寫進
    ``root/.runtime``（見 ``sjenv.ensure_runtime_dir``）。

    在此終端機按 Ctrl+C（或 Ctrl+Break）即優雅結束、不噴 traceback。

    重依賴（uvicorn／guards／shell 的 COM／ctypes 使用）於函式內延遲匯入或
    僅在需要時觸發，讓本模組的匯入（含單元測試）不必付這些成本。lifecycle
    組裝以 localhost 親眼 QA 驗收。
    """
    import signal

    import uvicorn

    from shioaji_wizard import server as server_module
    from shioaji_wizard.guards import install_guards

    _ = root  # 僅供呼叫端表明「已設好 SJ_ENV_DIR」；實際路徑一律經 sjenv 取得
    runtime_dir = ensure_runtime_dir()
    write_status(runtime_dir, "啟動本機服務…")

    app = server_module.app
    install_guards(app)
    _cancel_job = server_module.cancel_job

    def _job_running() -> bool:
        job = server_module._job
        return bool(job is not None and job.running)

    state = AppState(
        job_running=_job_running,
        heartbeat_age=server_module.heartbeat_age,
        idle_timeout=idle_timeout,
    )
    state.cancel_job = _cancel_job
    state.cancel_dialog = server_module.cancel_dialog

    port = _free_port() if port is None else port
    url = f"http://{host}:{port}/"
    config = uvicorn.Config(
        app, host=host, port=port, log_level="warning", timeout_graceful_shutdown=5
    )  # 卡住的請求最多等 5 秒
    server = uvicorn.Server(config)
    state.server = server

    stop_event = threading.Event()

    def _request_stop(signum: object = None, frame: object = None) -> None:
        """令伺服器優雅結束（設 ``should_exit`` → uvicorn serve 迴圈退出）。
        兼作 SIGINT／SIGBREAK 處理器：安裝後 Ctrl+C 不再拋 KeyboardInterrupt，
        改為乾淨關閉。"""
        stop_event.set()
        server.should_exit = True

    # uvicorn 跑在背景執行緒時它「不會」自行安裝訊號處理器（只在 main thread
    # 安裝），若主執行緒又卡在無 timeout 的 join，Windows 上 Ctrl+C 送不進來、
    # 甚至最終以未捕捉的 KeyboardInterrupt 噴 traceback。故在主執行緒統一安裝
    # SIGINT／SIGBREAK 處理器，並改用「有 timeout 的 join 輪詢」讓處理器有機會
    # 執行——兩者缺一，Ctrl+C 都無法乾淨結束。
    if threading.current_thread() is threading.main_thread():
        signal.signal(signal.SIGINT, _request_stop)
        if hasattr(signal, "SIGBREAK"):
            signal.signal(signal.SIGBREAK, _request_stop)

    launch_result = None
    server_thread = threading.Thread(target=server.run, name="sjw-uvicorn")
    server_thread.start()
    try:
        _wait_until_started(server)
        write_status(runtime_dir, "開啟應用視窗…")
        if open_shell:
            # 先開窗、再找出真正持有視窗的主行程記進 state.window，watchdog
            # 才能在視窗被關時即時收攤（spawn 行程可能只是 launcher、早退不可
            # 靠）。開窗／追蹤失敗都不應弄垮伺服器（仍可手動開 URL）。
            try:
                launch_result = shell.launch(url, window_size=_WINDOW_SIZE)
            except Exception as e:  # noqa: BLE001 - 殼失敗不擋伺服器
                print(f"殼視窗啟動失敗（伺服器仍在 {url}）：{e}")
            # 視窗已送出啟動（畫面馬上會出來）→ 立刻讓 launcher 的啟動提示消失，
            # 不等下面 resolve_window（最長可等 10 秒）與 brand_window。
            _touch_ready_marker(runtime_dir)
            if launch_result is not None and launch_result.mode == "app":
                try:
                    state.window = shell.resolve_window(launch_result, abort=stop_event.is_set)
                except Exception as e:  # noqa: BLE001 - 追蹤失敗不擋伺服器
                    state.window = None
                    print(f"殼視窗主行程追蹤失敗（{e}）。")
                if state.window is None and not stop_event.is_set():
                    print("追蹤不到殼視窗主行程，改用心跳閒置逾時判斷收攤。")
                if state.window is not None:
                    # 工作列身份：獨立 AUMID＋icon（Chromium --app 視窗預設
                    # 掛在瀏覽器分組、工作列 icon 恆為瀏覽器 icon）。背景
                    # 執行緒＋長 timeout：新 profile 冷啟動的視窗首繪／標題
                    # 可能晚於 resolve_window 認列主行程的時點。best-effort：
                    # 失敗僅退回瀏覽器 icon，不影響功能。
                    window_pid = state.window.pid

                    def _brand() -> None:
                        try:
                            ok = shell.brand_window(window_pid, timeout=20.0)
                        except Exception:  # noqa: BLE001 - 純外觀，不擋伺服器
                            ok = False
                        print(
                            "工作列身份（AUMID＋icon）已設定。"
                            if ok
                            else "工作列身份設定失敗，退回瀏覽器 icon。"
                        )

                    threading.Thread(target=_brand, name="sjw-brand", daemon=True).start()
        _touch_ready_marker(runtime_dir)  # launcher 啟動提示視窗輪詢此訊號關閉
        watchdog = threading.Thread(
            target=_watchdog_loop,
            args=(state,),
            kwargs={"stop_event": stop_event},
            name="sjw-watchdog",
            daemon=True,
        )
        watchdog.start()
        print(f"Shioaji 測試精靈已啟動：{url}（關窗即自動結束；於此終端機按 Ctrl+C 亦可立即結束）")
        # 可被訊號中斷的等待：無 timeout 的 join 在 Windows 主執行緒不可被
        # Ctrl+C 中斷，改短 timeout 輪詢，讓上面的訊號處理器有機會執行。
        while server_thread.is_alive():
            server_thread.join(timeout=0.5)
    except KeyboardInterrupt:
        # 極少數情形（處理器未生效等）仍可能拋出——一律吞下、走乾淨關閉，
        # 不讓 traceback 外洩到終端機。
        _request_stop()
    finally:
        _request_stop()
        server_thread.join(timeout=10.0)
        _close_browser(launch_result, state.window)  # 收攤時連帶關掉殼視窗
        print("Shioaji 測試精靈已關閉。")
