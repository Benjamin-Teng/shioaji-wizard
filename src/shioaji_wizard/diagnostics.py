"""把 Shioaji 的伺服器拒絕轉成不洩漏敏感資料的使用者提示。"""

from __future__ import annotations


def _message(error: Exception) -> tuple[str, str]:
    msg = f"{type(error).__name__}: {error}"
    return msg, msg.lower()


def _ip_not_allowed(low: str) -> bool:
    return "ip:" in low and ("not allow" in low or "not allowed" in low)


def is_ip_allowlist_error(message: str) -> bool:
    """伺服器文字是否為 API Key IP 白名單拒絕。"""
    return _ip_not_allowed(message.lower())


def missing_account_reason(product: str) -> str:
    """說明帳戶未回傳的兩種可能，避免宣稱 API 能再細分。"""
    return (
        f"API 回傳中沒有{product}帳戶：可能尚未開立{product}帳戶，或建立 API Key 時"
        "未勾選該帳戶；API 本身無法再區分，請到 API 管理頁確認"
    )


def explain_feature_error(error: Exception, *, feature: str) -> str:
    """若可判定為特定功能權限或 IP 白名單錯誤，回傳精確提示。"""
    _msg, low = _message(error)
    if is_ip_allowlist_error(low):
        return "API Key IP 白名單不符：目前對外 IP 不在這把 Key 的允許清單；請到 API 管理頁調整，或改用已允許的網路"
    if "doesn't have permission" in low or "permission" in low or "forbidden" in low or "403" in low:
        return f"API Key 沒勾「{feature}」權限；請到 API 管理頁確認"
    return ""


def explain_login_error(error: Exception, *, production: bool) -> str:
    """依登入環境分類金鑰錯誤；IP 白名單錯誤不回顯實際 IP。"""
    msg, low = _message(error)
    if "not match signature" in low or "invalid secret_key" in low or "secret" in low or "signature" in low:
        return f"Secret Key 錯誤（與 API Key 不成對）— 伺服器：{msg[:160]}"
    if is_ip_allowlist_error(low):
        return "API Key IP 白名單不符：目前對外 IP 不在這把 Key 的允許清單；請到 API 管理頁調整，或改用已允許的網路"
    if "not exist" in low:
        return f"API Key 不存在或打錯（請到 API 管理頁確認／重建）— 伺服器：{msg[:160]}"
    if "token is expired" in low or "token expired" in low:
        return "登入 Token 已逾期；請重新登入（不需要重建 API Key）"
    if "expire" in low:
        return f"API Key 已過期，請到 API 管理頁重新建立 — 伺服器：{msg[:160]}"
    if "doesn't have production permission" in low or "production permission" in low:
        return f"API Key 建立時沒勾「正式環境」— 伺服器：{msg[:160]}"
    if "doesn't have permission" in low or "permission" in low or "forbidden" in low or "403" in low:
        if production:
            return f"API Key 功能權限不足；此錯誤發生在登入階段，無法判定是哪一勾選項，請到 API 管理頁逐項確認 — 伺服器：{msg[:160]}"
        return f"API Key 權限不足（請到 API 管理頁確認「行情/資料」權限）— 伺服器：{msg[:160]}"
    if production:
        return f"正式環境登入失敗（金鑰、網路或正式環境權限問題）— 伺服器：{msg[:200]}"
    return f"登入失敗（網路、金鑰或權限問題）— 伺服器：{msg[:200]}"


def explain_order_error(error: Exception | str, *, product: str) -> str:
    """分類模擬下單拒絕；未知訊息才列出仍需人工排查的原因。"""
    if isinstance(error, Exception):
        msg, low = _message(error)
    else:
        msg = error
        low = msg.lower()
    if is_ip_allowlist_error(low):
        return "API Key IP 白名單不符：目前對外 IP 不在這把 Key 的允許清單；請到 API 管理頁調整，或改用已允許的網路"
    if "account not acceptable" in low:
        return f"{product}帳戶簽署或模擬測試開通未完成（Account Not Acceptable）；請確認約定書已簽且本次測試早於簽署後重新執行"
    if "doesn't have permission" in low or "permission" in low or "forbidden" in low or "403" in low:
        return "API Key 沒勾「交易」權限；請到 API 管理頁確認"
    return (
        f"下單失敗（{msg[:160]}）；可能原因：不在測試時段（週一～五 08:00–20:00；"
        f"18:00–20:00 另限台灣 IP），或{product} API 約定書未簽署"
    )
