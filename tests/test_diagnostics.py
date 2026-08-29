"""Shioaji 登入／下單拒絕原因的使用者訊息分類。"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest

from shioaji_wizard import test_ca, test_sim_order
from shioaji_wizard.diagnostics import missing_account_reason
from shioaji_wizard.sjenv import Report


class FakeShioajiError(Exception):
    """只提供伺服器錯誤文字，不模擬 Shioaji 行為。"""


class FakeOrderStatus:
    PendingSubmit = "PendingSubmit"
    Submitted = "Submitted"


FAKE_SJ = SimpleNamespace(OrderStatus=FakeOrderStatus)


def trade_with(status: str, msg: str):
    return SimpleNamespace(status=SimpleNamespace(status=status, msg=msg))


@pytest.mark.parametrize("explain", [test_sim_order.explain_login_error, test_ca.explain_login_error])
def test_login_reports_api_key_ip_allowlist_without_echoing_address(explain):
    result = explain(FakeShioajiError("ip: 203.0.113.17 not allow."))

    assert "API Key IP 白名單不符" in result
    assert "203.0.113.17" not in result
    assert "18:00" not in result


@pytest.mark.parametrize("explain", [test_sim_order.explain_login_error, test_ca.explain_login_error])
def test_login_checks_signature_mismatch_before_broad_key_match(explain):
    result = explain(FakeShioajiError("key: abc not match signature."))

    assert "Secret Key" in result
    assert "API Key 不存在" not in result


def test_production_login_reports_missing_production_permission():
    result = test_ca.explain_login_error(FakeShioajiError("Token doesn't have production permission."))

    assert "沒勾「正式環境」" in result


def test_simulation_login_reports_market_data_permission_context():
    result = test_sim_order.explain_login_error(FakeShioajiError("Token doesn't have permission."))

    assert "行情/資料" in result


def test_production_login_does_not_guess_permission_scope_without_operation():
    result = test_ca.explain_login_error(FakeShioajiError("Token doesn't have permission."))

    assert "無法判定是哪一勾選項" in result
    assert "帳務/帳戶" not in result


@pytest.mark.parametrize("explain", [test_sim_order.explain_login_error, test_ca.explain_login_error])
def test_login_token_expiry_requires_relogin_not_api_key_rebuild(explain):
    result = explain(FakeShioajiError("Token is expired"))

    assert "登入 Token 已逾期" in result
    assert "重新登入" in result
    assert "重新建立" not in result


@pytest.mark.parametrize("explain", [test_sim_order.explain_login_error, test_ca.explain_login_error])
def test_api_key_expiry_still_requires_rebuild(explain):
    result = explain(FakeShioajiError("key_id is expired."))

    assert "API Key 已過期" in result
    assert "重新建立" in result


def test_order_reports_account_onboarding_incomplete_as_distinct_reason():
    trade = trade_with("Failed", "Account Not Acceptable.")

    result = test_sim_order.order_reason(trade, FAKE_SJ)

    assert "簽署或模擬測試開通未完成" in result
    assert "API Key IP 白名單" not in result


def test_order_reports_trading_permission_as_distinct_reason():
    trade = trade_with("Failed", "Token doesn't have permission.")

    result = test_sim_order.order_reason(trade, FAKE_SJ)

    assert "沒勾「交易」" in result
    assert "常見原因" not in result


def test_order_exception_reports_api_key_ip_allowlist_not_test_window():
    result = test_sim_order.explain_order_error(
        FakeShioajiError("ip: 203.0.113.17 not allow."), product="證券"
    )

    assert "API Key IP 白名單不符" in result
    assert "203.0.113.17" not in result
    assert "18:00" not in result


def test_order_status_ip_allowlist_does_not_reintroduce_address_in_raw_detail():
    trade = trade_with("Failed", "ip: 203.0.113.17 not allow.")

    result = test_sim_order.order_reason(trade, FAKE_SJ, product="證券")

    assert "API Key IP 白名單不符" in result
    assert "203.0.113.17" not in result


def test_contract_permission_failure_is_not_reported_as_network_failure():
    class MissingBook:
        def __getitem__(self, _code):
            raise KeyError

    class FakeApi:
        Contracts = SimpleNamespace(Stocks=MissingBook(), Futures=MissingBook())

        def fetch_contracts(self, *, contract_download):
            assert contract_download is True
            raise FakeShioajiError("Token doesn't have permission.")

    contract, reason = test_sim_order.get_contract(FakeApi(), "stock", "2890")

    assert contract is None
    assert "沒勾「行情/資料」" in reason
    assert "網路" not in reason


@pytest.mark.parametrize("product", ["證券", "期貨"])
def test_missing_account_reason_states_api_cannot_distinguish_causes(product):
    reason = missing_account_reason(product)

    assert f"沒有{product}帳戶" in reason
    assert "未開" in reason
    assert "API Key" in reason
    assert "API 本身無法再區分" in reason


def test_b_missing_stock_account_is_failure_not_skip():
    rep = Report()

    test_ca.record_signing_results(rep, [], want_futures=False)

    stock = next(item for item in rep.items if item["name"] == test_ca.B_STOCK_SIGNED)
    assert stock["status"] == "FAIL"
    assert "API 本身無法再區分" in stock["reason"]
    assert rep.all_passed is False


def test_ca_expiry_classification_uses_earliest_account_and_warns_below_30_days():
    now = datetime(2026, 8, 29, 12, tzinfo=UTC)

    status, reason = test_ca.classify_ca_expirations(
        [now + timedelta(days=90), now + timedelta(days=29, hours=23)], now
    )

    assert status == "PASS"
    assert "29 天後" in reason


def test_ca_expiry_classification_does_not_warn_at_exactly_30_days():
    now = datetime(2026, 8, 29, 12, tzinfo=UTC)

    status, reason = test_ca.classify_ca_expirations([now + timedelta(days=30)], now)

    assert status == "PASS"
    assert reason == ""


def test_ca_expiry_classification_fails_when_already_expired():
    now = datetime(2026, 8, 29, 12, tzinfo=UTC)

    status, reason = test_ca.classify_ca_expirations([now - timedelta(seconds=1)], now)

    assert status == "FAIL"
    assert "已於" in reason
    assert "過期" in reason


def test_ca_expiry_classification_fails_when_person_id_is_unavailable():
    now = datetime(2026, 8, 29, 12, tzinfo=UTC)

    status, reason = test_ca.classify_ca_expirations([], now)

    assert status == "FAIL"
    assert "person_id" in reason
    assert "無法查詢" in reason
