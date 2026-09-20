"""Shioaji 登入／下單拒絕原因的使用者訊息分類。"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest

from shioaji_wizard import certinfo, test_ca, test_sim_order
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


def test_mask_taiwan_id_keeps_front_three_and_back_two():
    assert test_ca.mask_taiwan_id("A123456789") == "A12*****89"


def test_cert_subject_matches_logged_in_person_id_reports_no_mismatch():
    reason = test_ca.cert_subject_mismatch_reason("CN=A123456789, O=Sinopac", {"A123456789", "B234567890"})

    assert reason == ""


def test_cert_subject_mismatches_logged_in_person_id_names_both_masked():
    reason = test_ca.cert_subject_mismatch_reason("CN=A123456789, O=Sinopac", {"B234567890"})

    assert "憑證屬於 A12*****89" in reason
    assert "與登入帳號" in reason
    assert "B23*****90" in reason
    assert "不同人" in reason
    # 完整字號不應該外洩
    assert "A123456789" not in reason
    assert "B234567890" not in reason


def test_cert_subject_without_id_pattern_reports_nothing():
    reason = test_ca.cert_subject_mismatch_reason("CN=shioaji-wizard-test", {"B234567890"})

    assert reason == ""


def test_build_offline_diagnostic_reports_expiry_date_and_mismatch():
    cert = certinfo.CertInfo(not_after=datetime(2028, 8, 22, tzinfo=UTC), subject="CN=A123456789, O=Sinopac")

    reason = test_ca.build_offline_diagnostic(cert, {"B234567890"})

    assert "2028-08-22" in reason
    assert "是同一人的" in reason
    assert "憑證屬於 A12*****89" in reason


def test_offline_pfx_check_settles_b4_and_b6_without_login_or_shioaji(tmp_path):
    """B4／B6 是純離線檢查：不需要 shioaji、不需要登入，缺檔案也能直接得到結論。"""
    rep = Report()
    missing_pfx = tmp_path / "Sinopac.pfx"

    cert, settled = test_ca._check_offline_pfx(rep, missing_pfx, "somepass")

    assert cert is None
    assert settled is True
    pfx_item = next(item for item in rep.items if item["name"] == test_ca.B_PFX)
    expire_item = next(item for item in rep.items if item["name"] == test_ca.B_EXPIRE)
    assert pfx_item["status"] == "FAIL"
    assert expire_item["status"] == "SKIP"


def test_build_offline_diagnostic_without_mismatch_only_reports_expiry():
    cert = certinfo.CertInfo(not_after=datetime(2028, 8, 22, tzinfo=UTC), subject="CN=shioaji-wizard-test")

    reason = test_ca.build_offline_diagnostic(cert, {"B234567890"})

    assert "2028-08-22" in reason
    assert "憑證屬於" not in reason


@pytest.mark.parametrize("activate", ["raises", "false"])
def test_b_main_records_all_six_items_once_when_offline_unavailable_and_activate_fails(
    monkeypatch, tmp_path, activate
):
    """離線讀取不可用＋B5 啟用失敗：B6 要記成 SKIP，不能整列從檢核單消失。"""
    import sys

    pfx = tmp_path / "Sinopac.pfx"
    pfx.write_bytes(b"x")
    recorded: list[Report] = []

    class SpyReport(Report):
        def __init__(self) -> None:
            super().__init__()
            recorded.append(self)

    def unavailable(*_a, **_k):
        raise certinfo.CertReadError("unavailable", "no powershell")

    class FakeApi:
        def __init__(self, simulation: bool) -> None:
            assert simulation is False

        def set_order_callback(self, _cb) -> None:
            pass

        def login(self, _k, _s):
            return [
                SimpleNamespace(
                    account_type="S", broker_id="9A95", account_id="1", signed=True, person_id="A123456789"
                )
            ]

        def activate_ca(self, **_k):
            if activate == "raises":
                raise RuntimeError("boom")
            return False

        def logout(self) -> None:
            pass

    monkeypatch.setitem(sys.modules, "shioaji", SimpleNamespace(Shioaji=FakeApi))
    monkeypatch.setattr(test_ca, "Report", SpyReport)
    monkeypatch.setattr(
        test_ca, "load_env", lambda: {"SJ_API_KEY": "k", "SJ_SEC_KEY": "s", "SJ_CA_PASSWD": "pw"}
    )
    monkeypatch.setattr(test_ca, "pfx_path", lambda _env: pfx)
    monkeypatch.setattr(test_ca.certinfo, "read_pfx_info", unavailable)
    monkeypatch.setattr(sys, "argv", ["test_ca"])

    assert test_ca.main() == 1

    names = [item["name"] for item in recorded[0].items]
    expected = [
        test_ca.B_LOGIN,
        test_ca.B_STOCK_SIGNED,
        test_ca.B_FUT_SIGNED,
        test_ca.B_PFX,
        test_ca.B_ACTIVATE,
        test_ca.B_EXPIRE,
    ]
    assert sorted(names) == sorted(expected)
    status = {item["name"]: item["status"] for item in recorded[0].items}
    assert status[test_ca.B_ACTIVATE] == "FAIL"
    assert status[test_ca.B_EXPIRE] == "SKIP"


def test_b_main_records_all_six_items_when_shioaji_constructor_fails(monkeypatch, tmp_path):
    """Shioaji() 建構就失敗（native runtime 壞掉）：六項仍各記一次，不是程式直接炸掉。"""
    import sys

    recorded: list[Report] = []

    class SpyReport(Report):
        def __init__(self) -> None:
            super().__init__()
            recorded.append(self)

    def broken(**_k):
        raise RuntimeError("native runtime missing")

    def unavailable(*_a, **_k):
        raise certinfo.CertReadError("unavailable", "no powershell")

    pfx = tmp_path / "Sinopac.pfx"
    pfx.write_bytes(b"x")
    monkeypatch.setitem(sys.modules, "shioaji", SimpleNamespace(Shioaji=broken))
    monkeypatch.setattr(test_ca, "Report", SpyReport)
    monkeypatch.setattr(
        test_ca, "load_env", lambda: {"SJ_API_KEY": "k", "SJ_SEC_KEY": "s", "SJ_CA_PASSWD": "pw"}
    )
    monkeypatch.setattr(test_ca, "pfx_path", lambda _env: pfx)
    monkeypatch.setattr(test_ca.certinfo, "read_pfx_info", unavailable)
    monkeypatch.setattr(sys, "argv", ["test_ca"])

    assert test_ca.main() == 2

    names = sorted(item["name"] for item in recorded[0].items)
    assert names == sorted(
        [
            test_ca.B_LOGIN,
            test_ca.B_STOCK_SIGNED,
            test_ca.B_FUT_SIGNED,
            test_ca.B_PFX,
            test_ca.B_ACTIVATE,
            test_ca.B_EXPIRE,
        ]
    )


_ALL_A = [
    test_sim_order.A_LOGIN,
    test_sim_order.A_STOCK_ACC,
    test_sim_order.A_STOCK_ORDER,
    test_sim_order.A_FUT_ACC,
    test_sim_order.A_FUT_ORDER,
]


@pytest.mark.parametrize("failure", ["no_keys", "import", "constructor", "login"])
def test_a_main_records_all_five_items_once_on_every_early_exit(monkeypatch, failure):
    """A1 之前／當下任何失敗，檢核單仍固定五列（A2–A5 補 SKIP），不是只剩 A1。"""
    import sys

    recorded: list[Report] = []

    class SpyReport(Report):
        def __init__(self) -> None:
            super().__init__()
            recorded.append(self)

    class FakeApi:
        def __init__(self, simulation: bool) -> None:
            assert simulation is True
            if failure == "constructor":
                raise RuntimeError("native runtime missing")

        def set_order_callback(self, _cb) -> None:
            pass

        def login(self, _k, _s):
            raise RuntimeError("login refused")

        def logout(self) -> None:
            pass

    env = {} if failure == "no_keys" else {"SJ_API_KEY": "k", "SJ_SEC_KEY": "s"}
    # sys.modules 的值為 None 時 import 會丟 ImportError：模擬 shioaji 載入失敗
    monkeypatch.setitem(
        sys.modules, "shioaji", None if failure == "import" else SimpleNamespace(Shioaji=FakeApi)
    )
    monkeypatch.setattr(test_sim_order, "Report", SpyReport)
    monkeypatch.setattr(test_sim_order, "load_env", lambda: env)
    monkeypatch.setattr(sys, "argv", ["test_sim_order"])

    assert test_sim_order.main() != 0

    assert sorted(item["name"] for item in recorded[0].items) == sorted(_ALL_A)
    status = {item["name"]: item["status"] for item in recorded[0].items}
    assert status[test_sim_order.A_LOGIN] == "FAIL"
    assert all(status[n] == "SKIP" for n in _ALL_A[1:])
