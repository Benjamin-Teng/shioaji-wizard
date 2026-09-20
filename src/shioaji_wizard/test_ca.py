"""B：正式環境測試——登入、看各帳戶 signed、啟用 Sinopac.pfx 憑證、查憑證到期日。

只登入、啟用憑證、查詢；不下單。金鑰與憑證密碼從 .env 讀，不印出來。
結束時列出每一項測試通過／未通過與原因。
用法：由精靈／GUI 呼叫；或  uv run python -m shioaji_wizard.test_ca [--futures]
（--futures：也檢查期貨帳戶 signed；沒帶時 B3 標「未要求」）

B4（憑證檔存在）與 B6（憑證到期日）改用 certinfo.read_pfx_info 離線解析，永遠
排在登入之前先做，不受 B1 登入／B5 activate_ca 成不成功影響——使用者實測發現
的落差：activate_ca 失敗時舊流程整段 B4／B6 都被跳過看不到到期日，但永豐官方
Shioaji Pro 憑證管理頁看得到（pfx 本身可離線讀）。B6 只有離線讀取回報
"unavailable"（非 Windows、找不到 powershell.exe、逾時等，不代表憑證有問題）
才退回原本 api.get_ca_expiretime 的線上查法。
"""

from __future__ import annotations

import contextlib
import re
import sys
import warnings
from datetime import datetime
from pathlib import Path

from shioaji_wizard import certinfo
from shioaji_wizard.diagnostics import explain_login_error as _explain_login_error
from shioaji_wizard.diagnostics import missing_account_reason
from shioaji_wizard.sjenv import ENV_PATH, FAIL, PASS, Report, load_env, pfx_path, print_summary

B_LOGIN = "B1 正式環境登入"
B_STOCK_SIGNED = "B2 證券 API 簽署＋模擬測試審核（signed）"
B_FUT_SIGNED = "B3 期貨 API 簽署＋模擬測試審核（signed）"
B_PFX = "B4 憑證檔存在（SJ_CA_PATH）"
B_ACTIVATE = "B5 憑證啟用（憑證密碼）"
B_EXPIRE = "B6 憑證有效期"
ALL_B = (B_LOGIN, B_STOCK_SIGNED, B_FUT_SIGNED, B_PFX, B_ACTIVATE, B_EXPIRE)

# 登入失敗／缺金鑰時才會被跳過的項目——B4／B6 已改成離線先做，不受登入影響。
_SKIP_ON_NO_LOGIN = (B_STOCK_SIGNED, B_FUT_SIGNED, B_ACTIVATE)

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

# 台灣身分證字號樣式（含新式外來人口統一證號）：1 個英文字母 ＋ 1 個英數字 ＋ 8 個數字。
_TAIWAN_ID_RE = re.compile(r"[A-Z][A-Z0-9]\d{8}")


def acct_type(acc) -> str:
    """帳戶類型字串：S=證券、F=期貨（account_type 是 enum，取 value）。"""
    t = acc.account_type
    return str(getattr(t, "value", t))


def explain_login_error(e: Exception) -> str:
    return _explain_login_error(e, production=True)


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


def record_signing_results(rep: Report, accounts: list, *, want_futures: bool) -> None:
    """記錄證券／期貨 signed 狀態；證券是本流程必要帳戶。"""
    stock_accs = [a for a in accounts if acct_type(a) == "S"]
    fut_accs = [a for a in accounts if acct_type(a) == "F"]
    if not stock_accs:
        rep.fail(B_STOCK_SIGNED, missing_account_reason("證券"))
    elif all(a.signed for a in stock_accs):
        rep.ok(B_STOCK_SIGNED)
    else:
        rep.fail(B_STOCK_SIGNED, NOT_SIGNED_STOCK)
    if not want_futures:
        rep.skip(B_FUT_SIGNED, "未要求測期貨（只做證券可忽略；要測請按「也測期貨／選擇權」）")
    elif not fut_accs:
        rep.fail(B_FUT_SIGNED, f"{missing_account_reason('期貨')}；只做證券請不要勾期貨")
    elif all(a.signed for a in fut_accs):
        rep.ok(B_FUT_SIGNED)
    else:
        rep.fail(B_FUT_SIGNED, NOT_SIGNED_FUT)


def classify_ca_expirations(expirations: list[datetime], now: datetime) -> tuple[str, str]:
    """以所有帳戶中最早到期的憑證判定結果。"""
    if not expirations:
        return FAIL, "帳戶沒有 person_id，無法查詢憑證到期日"
    earliest = min(expirations)
    left = (earliest - now).days
    if earliest < now:
        return FAIL, f"憑證已於 {earliest:%Y-%m-%d} 過期，請到 API 管理頁重新下載"
    if left < 30:
        return PASS, f"憑證 {left} 天後（{earliest:%Y-%m-%d}）到期，建議儘早到 API 管理頁重新下載"
    return PASS, ""


def mask_taiwan_id(id_no: str) -> str:
    """身分證字號打碼：前 3 後 2、中間用 * 補（例 A123456789 → A12*****89）。
    不足 5 碼就整串打碼（理論上不會發生，樣式已限制 10 碼，防禦性處理）。"""
    if len(id_no) <= 5:
        return "*" * len(id_no)
    return f"{id_no[:3]}{'*' * (len(id_no) - 5)}{id_no[-2:]}"


def find_taiwan_id(text: str) -> str | None:
    """從憑證 subject 之類的字串裡找身分證字號樣式；找不到回 None。"""
    m = _TAIWAN_ID_RE.search(text)
    return m.group(0) if m else None


def cert_subject_mismatch_reason(subject: str, person_ids: set[str]) -> str:
    """subject 內若含身分證字號且不在登入帳號的 person_id 集合裡，回傳說明；
    不含該樣式、或相符，就回空字串。不連線，供 B5 失敗診斷與測試共用。"""
    cert_id = find_taiwan_id(subject)
    if not cert_id or cert_id in person_ids:
        return ""
    if not person_ids:
        return f"憑證屬於 {mask_taiwan_id(cert_id)}，與登入帳號不同人（登入帳號查無身分證字號）"
    logged_in = "、".join(mask_taiwan_id(p) for p in sorted(person_ids))
    return f"憑證屬於 {mask_taiwan_id(cert_id)}，與登入帳號 {logged_in} 不同人"


def build_offline_diagnostic(cert: certinfo.CertInfo, person_ids: set[str]) -> str:
    """B5（activate_ca）失敗時的補充診斷：離線讀到的到期日，加上身分不符提示
    （偵測得到才附）。不連線，純函式方便測試。"""
    parts = [
        f"憑證檔與密碼本身可正常讀取、到期日 {cert.not_after:%Y-%m-%d}，"
        "請確認這張憑證與這組 API Key 是同一人的"
    ]
    mismatch = cert_subject_mismatch_reason(cert.subject, person_ids)
    if mismatch:
        parts.append(mismatch)
    return "；".join(parts)


def _check_offline_pfx(rep: Report, ca_path: Path, ca_passwd: str) -> tuple[certinfo.CertInfo | None, bool]:
    """B4／B6：離線檢查憑證檔是否存在＋到期日，不靠登入／簽署／activate_ca。

    回傳 (讀到的憑證資訊或 None, B6 是否已有結論)。B6 沒結論（False）只會發生在
    kind="unavailable" 時（非 Windows、找不到 powershell.exe 等）——呼叫端要在
    B5 activate_ca 成功後，退回原本的線上 api.get_ca_expiretime 查一次。
    """
    if not ca_path.is_file():
        rep.fail(
            B_PFX,
            f"找不到 {ca_path}；請把 Sinopac.pfx 放到該處，或把 .env 的 SJ_CA_PATH 改成正確完整路徑",
        )
        rep.skip(B_EXPIRE, "沒有憑證檔")
        return None, True

    rep.ok(B_PFX, str(ca_path))
    if not ca_passwd:
        rep.skip(
            B_EXPIRE,
            "憑證密碼沒填（.env 的 SJ_CA_PASSWD 空白），無法離線讀取到期日；請先在畫面填憑證密碼",
        )
        return None, True

    try:
        cert = certinfo.read_pfx_info(ca_path, ca_passwd)
    except certinfo.CertReadError as e:
        if e.kind == "password":
            rep.fail(B_EXPIRE, "憑證密碼不正確（離線讀取憑證檔失敗）")
            return None, True
        if e.kind == "format":
            rep.fail(B_EXPIRE, f"憑證檔不是有效的 pfx 憑證檔（{e.detail[:120]}）")
            return None, True
        return None, False  # unavailable：不代表憑證有問題，交給後面的線上路徑

    status, reason = classify_ca_expirations([cert.not_after], datetime.now(cert.not_after.tzinfo))
    (rep.fail if status == FAIL else rep.ok)(B_EXPIRE, reason)
    return cert, True


def main() -> int:
    warnings.simplefilter(
        "ignore", DeprecationWarning
    )  # shioaji 1.7.x 的 api.Contracts 棄用警告，對使用者沒意義
    rep = Report()
    env = load_env()
    want_futures = "--futures" in sys.argv

    # ---- B4／B6：離線先做，永遠不受登入／B5 activate_ca 影響 ----
    ca_path = pfx_path(env)
    ca_passwd = env.get("SJ_CA_PASSWD") or ""
    offline_cert, expire_settled = _check_offline_pfx(rep, ca_path, ca_passwd)

    if not (env.get("SJ_API_KEY") and env.get("SJ_SEC_KEY")):
        rep.fail(B_LOGIN, f"{ENV_PATH} 缺少 SJ_API_KEY / SJ_SEC_KEY")
        for n in _SKIP_ON_NO_LOGIN:
            rep.skip(n, "前置（登入）未通過")
        if not expire_settled:
            rep.skip(B_EXPIRE, "前置（登入）未通過，且無法離線讀取到期日")
        print_summary(rep.items, "B 正式環境測試結果")
        return 2

    try:
        import shioaji as sj
    except Exception as e:  # noqa: BLE001
        rep.fail(
            B_LOGIN,
            f"shioaji 模組載入失敗（{type(e).__name__}: {str(e)[:120]}）；Windows 可能缺 Visual C++ 可轉散發套件，或環境損毀請刪除 .venv 重跑",
        )
        for n in _SKIP_ON_NO_LOGIN:
            rep.skip(n, "前置未通過")
        if not expire_settled:
            rep.skip(B_EXPIRE, "前置未通過，且無法離線讀取到期日")
        print_summary(rep.items, "B 正式環境測試結果")
        return 2

    try:
        api = sj.Shioaji(simulation=False)  # 正式環境：signed 只有這裡會被檢查
    except Exception as e:  # noqa: BLE001
        rep.fail(B_LOGIN, f"shioaji 初始化失敗（{type(e).__name__}: {str(e)[:120]}）")
        for n in _SKIP_ON_NO_LOGIN:
            rep.skip(n, "前置未通過")
        if not expire_settled:
            rep.skip(B_EXPIRE, "前置未通過，且無法離線讀取到期日")
        print_summary(rep.items, "B 正式環境測試結果")
        return 2
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
            for n in _SKIP_ON_NO_LOGIN:
                rep.skip(n, "前置（登入）未通過")
            if not expire_settled:
                rep.skip(B_EXPIRE, "登入未通過，且無法離線讀取到期日（非 Windows 或 PowerShell 無法使用）")
            return 1
        rep.ok(B_LOGIN, f"{len(accounts)} 個帳戶")

        # ---- signed：證券／期貨各自判斷 ----
        for acc in accounts:
            print(f"      {acc.account_type} {acc.broker_id}-{acc.account_id} signed={acc.signed}")
        record_signing_results(rep, accounts, want_futures=want_futures)

        # ---- 啟用憑證（B4／B6 已離線做完，這裡只管 B5） ----
        if not ca_path.is_file():
            rep.skip(B_ACTIVATE, "沒有憑證檔")
            return 1
        if not ca_passwd:
            rep.skip(
                B_ACTIVATE,
                "憑證密碼沒填（.env 的 SJ_CA_PASSWD 空白）。密碼＝下載憑證時自己設定的",
            )
            return 1

        person_ids = {a.person_id for a in accounts if a.person_id}
        try:
            ok = api.activate_ca(ca_path=str(ca_path), ca_passwd=ca_passwd)
        except Exception as e:  # noqa: BLE001
            reason = explain_ca_error(e)
            if offline_cert is not None:
                reason += "；" + build_offline_diagnostic(offline_cert, person_ids)
            rep.fail(B_ACTIVATE, reason)
            if not expire_settled:  # 離線讀取不可用又啟用失敗：B6 不能整列消失
                rep.skip(B_EXPIRE, "憑證未啟用")
            return 1
        if not ok:
            reason = "activate_ca 回傳 False：憑證密碼錯、憑證過期、或憑證與此帳號不符"
            if offline_cert is not None:
                reason += "；" + build_offline_diagnostic(offline_cert, person_ids)
            rep.fail(B_ACTIVATE, reason)
            if not expire_settled:  # 離線讀取不可用又啟用失敗：B6 不能整列消失
                rep.skip(B_EXPIRE, "憑證未啟用")
            return 1
        rep.ok(B_ACTIVATE)

        # ---- 到期日線上 fallback：只有離線讀取回報 unavailable 才會走到這 ----
        if not expire_settled:
            pids = sorted({a.person_id for a in api.list_accounts() if a.person_id})
            if not pids:
                _status, reason = classify_ca_expirations([], datetime.now())
                rep.fail(B_EXPIRE, reason)
            else:
                expirations = []
                failed = False
                for pid in pids:
                    try:
                        exp: datetime = api.get_ca_expiretime(person_id=pid)
                    except Exception as e:  # noqa: BLE001
                        rep.fail(
                            B_EXPIRE,
                            f"查詢到期日失敗（{type(e).__name__}: {str(e)[:120]}）",
                        )
                        failed = True
                        break
                    left = (exp - datetime.now(exp.tzinfo)).days
                    print(
                        f"      person_id={mask_taiwan_id(pid)} 憑證到期 {exp:%Y-%m-%d %H:%M}（剩 {left} 天）"
                    )
                    expirations.append(exp)
                if not failed and expirations:
                    status, reason = classify_ca_expirations(expirations, datetime.now(expirations[0].tzinfo))
                    (rep.fail if status == FAIL else rep.ok)(B_EXPIRE, reason)
        return 0 if rep.all_passed else 1
    finally:
        if logged_in:
            with contextlib.suppress(Exception):
                api.logout()
        print_summary(rep.items, "B 正式環境測試結果")


if __name__ == "__main__":
    sys.exit(main())
