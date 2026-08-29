"""A：永豐 API 開通必經的「模擬登入＋模擬下單測試」（simulation=True，不需憑證、不碰真錢）。

通過後約 1 分鐘，正式環境各帳戶的 signed 才會變 True；沒過之前正式下單會被拒
（account not acceptable）。限制：週一～五 08:00–20:00 台灣時間（18:00 後限台灣 IP）；
簽署時間必須早於測試時間；證券與期貨分開測、相隔 ≥ 1 秒。

結束時列出每一項測試通過／未通過與原因。
用法：由精靈／GUI 呼叫；或  uv run python -m shioaji_wizard.test_sim_order [--futures]
"""

from __future__ import annotations

import contextlib
import sys
import time
import warnings

from shioaji_wizard.diagnostics import (
    explain_feature_error,
    explain_order_error,
    is_ip_allowlist_error,
    missing_account_reason,
)
from shioaji_wizard.diagnostics import explain_login_error as _explain_login_error
from shioaji_wizard.sjenv import ENV_PATH, Report, load_env, print_summary

A_LOGIN = "A1 模擬環境登入"
A_STOCK_ACC = "A2 證券帳戶"
A_STOCK_ORDER = "A3 證券模擬下單"
A_FUT_ACC = "A4 期貨帳戶"
A_FUT_ORDER = "A5 期貨模擬下單"


def explain_login_error(e: Exception) -> str:
    return _explain_login_error(e, production=False)


def get_contract(api, kind: str, code: str):
    """取合約；合約還沒載完就補抓一次。回 (contract, 原因)。"""
    book = (
        api.Contracts.Stocks if kind == "stock" else api.Contracts.Futures
    )  # v2 的 api.contracts 沒有 Stocks，沿用舊介面
    try:
        c = book[code]
    except (KeyError, IndexError, TypeError):
        c = None
    if c is None:
        try:
            api.fetch_contracts(contract_download=True)
            c = book[code]
        except Exception as e:  # noqa: BLE001 — 小白工具：任何失敗都要化成可讀原因
            diagnostic = explain_feature_error(e, feature="行情/資料")
            if diagnostic:
                return None, diagnostic
            return (
                None,
                f"合約資料載入失敗（{type(e).__name__}: {str(e)[:120]}），請確認網路後重試",
            )
    if c is None:
        return None, f"找不到合約 {code}（合約資料未載入或代碼異動）"
    if getattr(c, "reference", None) in (None, 0):
        return None, f"合約 {code} 沒有平盤參考價（合約資料不完整，稍後再試）"
    return c, ""


def order_reason(trade, sj, *, product: str = "該商品") -> str:
    status = trade.status.status
    msg = getattr(trade.status, "msg", "") or ""
    if status in (sj.OrderStatus.PendingSubmit, sj.OrderStatus.Submitted):
        return ""
    reason = explain_order_error(msg or f"狀態 {status}", product=product)
    detail = "" if is_ip_allowlist_error(msg) else (f"，伺服器訊息：{msg}" if msg else "")
    return f"狀態 {status}{detail}；{reason}"


def main() -> int:
    warnings.simplefilter(
        "ignore", DeprecationWarning
    )  # shioaji 1.7.x 的 api.Contracts 棄用警告，對使用者沒意義
    rep = Report()
    env = load_env()
    want_futures = "--futures" in sys.argv
    if not (env.get("SJ_API_KEY") and env.get("SJ_SEC_KEY")):
        rep.fail(A_LOGIN, f"{ENV_PATH} 缺少 SJ_API_KEY / SJ_SEC_KEY")
        print_summary(rep.items, "A 模擬測試結果")
        return 2

    try:
        import shioaji as sj
    except Exception as e:  # noqa: BLE001
        rep.fail(
            A_LOGIN,
            f"shioaji 模組載入失敗（{type(e).__name__}: {str(e)[:120]}）；Windows 可能缺 Visual C++ 可轉散發套件，或環境損毀請刪除 .venv 重跑",
        )
        print_summary(rep.items, "A 模擬測試結果")
        return 2

    api = sj.Shioaji(simulation=True)  # 模擬模式：這是硬性保險，不要改成 False
    # shioaji 預設的委託回報 callback 會把整個 OrderState dict 印到輸出（對使用者是雜訊），接管掉
    with contextlib.suppress(Exception):
        api.set_order_callback(lambda *_a, **_k: None)
    logged_in = False
    try:
        try:
            accounts = api.login(env["SJ_API_KEY"], env["SJ_SEC_KEY"])
            logged_in = True
        except Exception as e:  # noqa: BLE001
            rep.fail(A_LOGIN, explain_login_error(e))
            for n in (A_STOCK_ACC, A_STOCK_ORDER):
                rep.skip(n, "前置（登入）未通過")
            for n in (A_FUT_ACC, A_FUT_ORDER):
                rep.skip(n, "前置（登入）未通過" if want_futures else "未要求測期貨")
            return 1
        rep.ok(A_LOGIN, f"{len(accounts)} 個帳戶")
        for acc in accounts:
            print(f"      {acc.account_type} {acc.broker_id}-{acc.account_id}")

        # ---- 證券：2890（永豐金）平盤價買 1 張，ROD 限價 ----
        if api.stock_account is None:
            rep.fail(
                A_STOCK_ACC,
                missing_account_reason("證券"),
            )
            rep.skip(A_STOCK_ORDER, "沒有證券帳戶")
        else:
            rep.ok(
                A_STOCK_ACC,
                f"{api.stock_account.broker_id}-{api.stock_account.account_id}",
            )
            sc, why = get_contract(api, "stock", "2890")
            if sc is None:
                rep.fail(A_STOCK_ORDER, why)
            else:
                try:
                    st = api.place_order(
                        sc,
                        sj.StockOrder(
                            price=sc.reference,
                            quantity=1,
                            action=sj.Action.Buy,
                            price_type=sj.StockPriceType.LMT,
                            order_type=sj.OrderType.ROD,
                            order_lot=sj.StockOrderLot.Common,
                            account=api.stock_account,
                        ),
                    )
                    why = order_reason(st, sj, product="證券")
                    if why:
                        rep.fail(A_STOCK_ORDER, why)
                    else:
                        rep.ok(A_STOCK_ORDER, f"2890@{sc.reference} → {st.status.status}")
                except Exception as e:  # noqa: BLE001
                    rep.fail(
                        A_STOCK_ORDER,
                        explain_order_error(e, product="證券"),
                    )

        # ---- 期貨：近月台指 TXFR1，僅在明示 --futures 時才測 ----
        if want_futures:
            time.sleep(1.1)  # 證券／期貨測試需相隔 ≥ 1 秒
            if api.futopt_account is None:
                rep.fail(
                    A_FUT_ACC,
                    f"{missing_account_reason('期貨')}；只做證券可忽略",
                )
                rep.skip(A_FUT_ORDER, "沒有期貨帳戶")
            else:
                rep.ok(
                    A_FUT_ACC,
                    f"{api.futopt_account.broker_id}-{api.futopt_account.account_id}",
                )
                fc, why = get_contract(api, "futures", "TXFR1")
                if fc is None:
                    rep.fail(A_FUT_ORDER, why)
                else:
                    try:
                        ft = api.place_order(
                            fc,
                            sj.FuturesOrder(
                                price=fc.reference,
                                quantity=1,
                                action=sj.Action.Buy,
                                price_type=sj.FuturesPriceType.LMT,
                                order_type=sj.OrderType.ROD,
                                octype=sj.FuturesOCType.Auto,
                                account=api.futopt_account,
                            ),
                        )
                        why = order_reason(ft, sj, product="期貨")
                        if why:
                            rep.fail(A_FUT_ORDER, why)
                        else:
                            rep.ok(
                                A_FUT_ORDER,
                                f"TXFR1@{fc.reference} → {ft.status.status}",
                            )
                    except Exception as e:  # noqa: BLE001
                        rep.fail(
                            A_FUT_ORDER,
                            explain_order_error(e, product="期貨"),
                        )
        else:
            rep.skip(A_FUT_ACC, "未要求測期貨（只做證券可忽略；要測請按「也測期貨／選擇權」）")
            rep.skip(A_FUT_ORDER, "未要求測期貨")
        return 0 if rep.all_passed else 1
    finally:
        if logged_in:
            with contextlib.suppress(Exception):
                api.logout()
        print_summary(rep.items, "A 模擬測試結果")


if __name__ == "__main__":
    sys.exit(main())
