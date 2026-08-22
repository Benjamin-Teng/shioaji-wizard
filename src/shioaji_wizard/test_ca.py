"""B：正式環境測試——登入、看各帳戶 signed、啟用 Sinopac.pfx 憑證、查憑證到期日。

只登入、啟用憑證、查詢；不下單。金鑰與憑證密碼從 .env 讀，不印出來。
結束時列出每一項測試通過／未通過與原因。
用法：由精靈／GUI 呼叫；或  uv run python -m shioaji_wizard.test_ca [--futures]
（--futures：也檢查期貨帳戶 signed；沒帶時 B3 標「未要求」）
"""

from __future__ import annotations

import contextlib
import sys
import warnings
from datetime import datetime

from shioaji_wizard.sjenv import ENV_PATH, Report, load_env, pfx_path, print_summary

B_LOGIN = "B1 正式環境登入"
B_STOCK_SIGNED = "B2 證券 API 簽署＋模擬測試審核（signed）"
B_FUT_SIGNED = "B3 期貨 API 簽署＋模擬測試審核（signed）"
B_PFX = "B4 憑證檔存在（SJ_CA_PATH）"
B_ACTIVATE = "B5 憑證啟用（憑證密碼）"
B_EXPIRE = "B6 憑證有效期"
ALL_B = (B_LOGIN, B_STOCK_SIGNED, B_FUT_SIGNED, B_PFX, B_ACTIVATE, B_EXPIRE)

NOT_SIGNED_STOCK = (
    "證券帳戶 signed=False：①證券 API 約定書未簽署（簽署頁 "
    "https://www.sinotrade.com.tw/newweb/signCenter/S_openAPI/ ）；或②模擬下單測試（選項 A）"
    "還沒做／審核未完成（約 1 分鐘）；或③簽署時間晚於測試時間，需重跑 A"
)
NOT_SIGNED_FUT = (
    "期貨帳戶 signed=False：①期貨 API 約定書未簽署（簽署頁 "
    "https://www.sinotrade.com.tw/newweb/signCenter/F_openApi/ ）；或②期貨模擬下單測試"
    "（選項 A 勾期貨）還沒做／審核未完成；只做證券可忽略"
)


def acct_type(acc) -> str:
    """帳戶類型字串：S=證券、F=期貨（account_type 是 enum，取 value）。"""
    t = acc.account_type
    return str(getattr(t, "value", t))


def explain_login_error(e: Exception) -> str:
    msg = f"{type(e).__name__}: {e}"
    low = msg.lower()
    if "not exist" in low or "key:" in low:
        return f"API Key 不存在或打錯 — 伺服器：{msg[:160]}"
    if "secret" in low or "signature" in low:
        return f"Secret Key 錯誤（與 API Key 不成對）— 伺服器：{msg[:160]}"
    if "expire" in low:
        return f"API Key 已過期 — 伺服器：{msg[:160]}"
    if "permission" in low or "forbidden" in low or "403" in low or "production" in low:
        return f"API Key 建立時沒勾「正式環境」（或權限不足）— 伺服器：{msg[:160]}"
    return f"正式環境登入失敗（可能 API Key 沒勾「正式環境」、金鑰錯誤或網路問題）— 伺服器：{msg[:200]}"


def explain_ca_error(e: Exception) -> str:
    name = type(e).__name__
    msg = str(e)
    low = msg.lower()
    if name == "CaPasswordError" or "password" in low:
        return "憑證密碼錯誤（.env 的 SJ_CA_PASSWD）。密碼＝下載憑證時自己設定的；舊版 eLeader 申請的預設為身分證字號"
    if "readfile" in low or "找不到" in low or "no such file" in low or "os error" in low:
        return f"憑證檔讀不到（.env 的 SJ_CA_PATH 指向的檔案不存在或無法讀取）— {msg[:120]}"
    if "expire" in low or "過期" in low:
        return f"憑證已過期，請到 API 管理頁重新下載 — {msg[:120]}"
    return f"憑證啟用失敗（{name}: {msg[:160]}）；可能憑證與此帳號不符、已過期或檔案損毀，請到 API 管理頁重新下載"


def main() -> int:
    warnings.simplefilter(
        "ignore", DeprecationWarning
    )  # shioaji 1.7.x 的 api.Contracts 棄用警告，對使用者沒意義
    rep = Report()
    env = load_env()
    want_futures = "--futures" in sys.argv
    if not (env.get("SJ_API_KEY") and env.get("SJ_SEC_KEY")):
        rep.fail(B_LOGIN, f"{ENV_PATH} 缺少 SJ_API_KEY / SJ_SEC_KEY")
        for n in ALL_B[1:]:
            rep.skip(n, "前置（登入）未通過")
        print_summary(rep.items, "B 正式環境測試結果")
        return 2

    try:
        import shioaji as sj
    except Exception as e:  # noqa: BLE001
        rep.fail(
            B_LOGIN,
            f"shioaji 模組載入失敗（{type(e).__name__}: {str(e)[:120]}）；Windows 可能缺 Visual C++ 可轉散發套件，或環境損毀請刪除 .venv 重跑",
        )
        for n in ALL_B[1:]:
            rep.skip(n, "前置未通過")
        print_summary(rep.items, "B 正式環境測試結果")
        return 2

    api = sj.Shioaji(simulation=False)  # 正式環境：CA 與 signed 只有這裡會被檢查
    # shioaji 預設的委託回報 callback 會把整個 OrderState dict 印到輸出（對使用者是雜訊），接管掉
    with contextlib.suppress(Exception):
        api.set_order_callback(lambda *_a, **_k: None)
    logged_in = False
    try:
        try:
            accounts = api.login(env["SJ_API_KEY"], env["SJ_SEC_KEY"])
            logged_in = True
        except Exception as e:  # noqa: BLE001
            rep.fail(B_LOGIN, explain_login_error(e))
            for n in ALL_B[1:]:
                rep.skip(n, "前置（登入）未通過")
            return 1
        rep.ok(B_LOGIN, f"{len(accounts)} 個帳戶")

        # ---- signed：證券／期貨各自判斷 ----
        stock_accs = [a for a in accounts if acct_type(a) == "S"]
        fut_accs = [a for a in accounts if acct_type(a) == "F"]
        for acc in accounts:
            print(f"      {acc.account_type} {acc.broker_id}-{acc.account_id} signed={acc.signed}")
        if not stock_accs:
            rep.skip(B_STOCK_SIGNED, "沒有證券帳戶")
        elif all(a.signed for a in stock_accs):
            rep.ok(B_STOCK_SIGNED)
        else:
            rep.fail(B_STOCK_SIGNED, NOT_SIGNED_STOCK)
        if not want_futures:
            rep.skip(B_FUT_SIGNED, "未要求測期貨（只做證券可忽略；要測請按「也測期貨／選擇權」）")
        elif not fut_accs:
            rep.fail(
                B_FUT_SIGNED,
                "這組 API Key 底下沒有期貨帳戶（未開期貨戶、或建 API Key 時沒勾選期貨帳戶）；只做證券請不要勾期貨",
            )
        elif all(a.signed for a in fut_accs):
            rep.ok(B_FUT_SIGNED)
        else:
            rep.fail(B_FUT_SIGNED, NOT_SIGNED_FUT)

        # ---- 憑證檔 ----
        ca_path = pfx_path(env)
        if not ca_path.is_file():
            rep.fail(
                B_PFX,
                f"找不到 {ca_path}；請把 Sinopac.pfx 放到該處，或把 .env 的 SJ_CA_PATH 改成正確完整路徑",
            )
            rep.skip(B_ACTIVATE, "沒有憑證檔")
            rep.skip(B_EXPIRE, "沒有憑證檔")
            return 1
        rep.ok(B_PFX, str(ca_path))
        if not env.get("SJ_CA_PASSWD"):
            rep.fail(
                B_ACTIVATE,
                "憑證密碼沒填（.env 的 SJ_CA_PASSWD 空白）。密碼＝下載憑證時自己設定的",
            )
            rep.skip(B_EXPIRE, "憑證未啟用")
            return 1

        # ---- 啟用憑證 ----
        try:
            ok = api.activate_ca(ca_path=str(ca_path), ca_passwd=env["SJ_CA_PASSWD"])
        except Exception as e:  # noqa: BLE001
            rep.fail(B_ACTIVATE, explain_ca_error(e))
            rep.skip(B_EXPIRE, "憑證未啟用")
            return 1
        if not ok:
            rep.fail(
                B_ACTIVATE,
                "activate_ca 回傳 False：憑證密碼錯、憑證過期、或憑證與此帳號不符",
            )
            rep.skip(B_EXPIRE, "憑證未啟用")
            return 1
        rep.ok(B_ACTIVATE)

        # ---- 到期日 ----
        pids = sorted({a.person_id for a in api.list_accounts() if a.person_id})
        if not pids:
            rep.skip(B_EXPIRE, "帳戶沒有 person_id，無法查詢")
        else:
            worst = None
            for pid in pids:
                try:
                    exp: datetime = api.get_ca_expiretime(person_id=pid)
                except Exception as e:  # noqa: BLE001
                    rep.fail(
                        B_EXPIRE,
                        f"查詢到期日失敗（{type(e).__name__}: {str(e)[:120]}）",
                    )
                    worst = "err"
                    break
                left = (exp - datetime.now(exp.tzinfo)).days
                print(f"      person_id={pid} 憑證到期 {exp:%Y-%m-%d %H:%M}（剩 {left} 天）")
                if left < 0:
                    worst = f"憑證已於 {exp:%Y-%m-%d} 過期，請到 API 管理頁重新下載"
                elif worst is None and left < 30:
                    worst = f"憑證 {left} 天後（{exp:%Y-%m-%d}）到期，建議儘早到 API 管理頁重新下載"
            if worst is None:
                rep.ok(B_EXPIRE)
            elif worst != "err":
                if "已於" in worst:
                    rep.fail(B_EXPIRE, worst)
                else:
                    rep.ok(B_EXPIRE, worst)
        return 0 if rep.all_passed else 1
    finally:
        if logged_in:
            with contextlib.suppress(Exception):
                api.logout()
        print_summary(rep.items, "B 正式環境測試結果")


if __name__ == "__main__":
    sys.exit(main())
