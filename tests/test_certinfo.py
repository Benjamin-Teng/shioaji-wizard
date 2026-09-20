"""certinfo.read_pfx_info 的邊界測試：只在 Windows 跑。

測試用 pfx 一律當場用 PowerShell 產生自簽憑證（優先走記憶體內的
CertificateRequest.CreateSelfSigned，不落地到 Cert:\\CurrentUser\\My；本機
PowerShell 5.1 若不支援才退回 New-SelfSignedCertificate＋Export-PfxCertificate，
且用完立刻從憑證存放區移除），不碰任何真憑證，也不污染使用者憑證存放區。
"""

from __future__ import annotations

import os
import subprocess
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from shioaji_wizard import certinfo

pytestmark = pytest.mark.skipif(sys.platform != "win32", reason="離線讀取憑證僅支援 Windows")

# 產生測試用 pfx：路徑／密碼一律走環境變數，不上命令列，做法比照 certinfo._PS_SCRIPT。
_MAKE_PFX_SCRIPT = r"""
$ErrorActionPreference = 'Stop'
[Console]::OutputEncoding = [System.Text.Encoding]::UTF8

$outPath = $env:SJ_TEST_PFX_OUT
$password = $env:SJ_TEST_PFX_PWD
$subject = $env:SJ_TEST_PFX_SUBJECT
$notBeforeDays = [int]$env:SJ_TEST_PFX_NOTBEFORE_DAYS
$notAfterDays = [int]$env:SJ_TEST_PFX_NOTAFTER_DAYS

$notBefore = (Get-Date).ToUniversalTime().AddDays($notBeforeDays)
$notAfter = (Get-Date).ToUniversalTime().AddDays($notAfterDays)

$done = $false
try {
    $rsa = [System.Security.Cryptography.RSA]::Create(2048)
    $req = New-Object System.Security.Cryptography.X509Certificates.CertificateRequest(
        $subject, $rsa, [System.Security.Cryptography.HashAlgorithmName]::SHA256,
        [System.Security.Cryptography.RSASignaturePadding]::Pkcs1)
    $cert = $req.CreateSelfSigned([System.DateTimeOffset]$notBefore, [System.DateTimeOffset]$notAfter)
    $bytes = $cert.Export([System.Security.Cryptography.X509Certificates.X509ContentType]::Pfx, $password)
    [System.IO.File]::WriteAllBytes($outPath, $bytes)
    $done = $true
} catch {
    $done = $false
}

if (-not $done) {
    $secPwd = ConvertTo-SecureString -String $password -AsPlainText -Force
    $cert = New-SelfSignedCertificate -Subject $subject -CertStoreLocation Cert:\CurrentUser\My `
        -NotBefore $notBefore -NotAfter $notAfter -KeyExportPolicy Exportable `
        -KeyUsage DigitalSignature, KeyEncipherment -KeySpec Signature
    try {
        Export-PfxCertificate -Cert $cert -FilePath $outPath -Password $secPwd | Out-Null
    } finally {
        Remove-Item -Path ("Cert:\CurrentUser\My\" + $cert.Thumbprint) -Force -ErrorAction SilentlyContinue
    }
}

Write-Output "OK"
"""


def _make_test_pfx(
    path: Path,
    password: str,
    *,
    not_before_days: int = 0,
    not_after_days: int = 3650,
    subject: str = "CN=shioaji-wizard-test",
) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    env = dict(os.environ)
    env["SJ_TEST_PFX_OUT"] = str(path)
    env["SJ_TEST_PFX_PWD"] = password
    env["SJ_TEST_PFX_SUBJECT"] = subject
    env["SJ_TEST_PFX_NOTBEFORE_DAYS"] = str(not_before_days)
    env["SJ_TEST_PFX_NOTAFTER_DAYS"] = str(not_after_days)
    proc = subprocess.run(
        ["powershell.exe", "-NoProfile", "-NonInteractive", "-Command", _MAKE_PFX_SCRIPT],
        env=env,
        capture_output=True,
        timeout=60,
    )
    if proc.returncode != 0 or b"OK" not in proc.stdout:
        stderr = proc.stderr.decode("utf-8", errors="replace")
        pytest.skip(f"本機無法產生測試用自簽 pfx（PowerShell 版本或權限問題）：{stderr[:300]}")


def test_reads_expiry_close_to_generation_parameter(tmp_path: Path) -> None:
    pfx = tmp_path / "test.pfx"
    before = datetime.now(UTC)
    _make_test_pfx(pfx, "correct-pw-1234", not_after_days=400)
    after = datetime.now(UTC)

    info = certinfo.read_pfx_info(pfx, "correct-pw-1234")

    expected_low = before + timedelta(days=400) - timedelta(seconds=2)
    expected_high = after + timedelta(days=400) + timedelta(seconds=2)
    assert expected_low <= info.not_after <= expected_high
    assert info.not_after.tzinfo is not None


def test_wrong_password_is_classified_as_password_error(tmp_path: Path) -> None:
    pfx = tmp_path / "test.pfx"
    _make_test_pfx(pfx, "correct-pw-1234")

    with pytest.raises(certinfo.CertReadError) as exc_info:
        certinfo.read_pfx_info(pfx, "wrong-pw-9999")

    assert exc_info.value.kind == "password"


def test_non_pfx_file_is_classified_as_format_error(tmp_path: Path) -> None:
    bogus = tmp_path / "not-a-cert.pfx"
    bogus.write_bytes(os.urandom(256))

    with pytest.raises(certinfo.CertReadError) as exc_info:
        certinfo.read_pfx_info(bogus, "whatever")

    assert exc_info.value.kind == "format"


def test_expired_certificate_is_still_readable_and_classified_as_fail(tmp_path: Path) -> None:
    from shioaji_wizard import test_ca

    pfx = tmp_path / "expired.pfx"
    _make_test_pfx(pfx, "correct-pw-1234", not_before_days=-400, not_after_days=-30)

    info = certinfo.read_pfx_info(pfx, "correct-pw-1234")

    assert info.not_after < datetime.now(UTC)
    status, reason = test_ca.classify_ca_expirations([info.not_after], datetime.now(info.not_after.tzinfo))
    assert status == "FAIL"
    assert "過期" in reason


def test_chinese_path_is_readable(tmp_path: Path) -> None:
    pfx = tmp_path / "憑證" / "測試.pfx"
    _make_test_pfx(pfx, "correct-pw-1234")

    info = certinfo.read_pfx_info(pfx, "correct-pw-1234")

    assert info.not_after.tzinfo is not None
    assert info.subject


@pytest.mark.parametrize(
    ("payload", "kind"),
    [
        ({"ok": False, "hresult": -2147024810, "crypto": True}, "password"),  # 0x80070056
        ({"ok": False, "hresult": -2146885623, "crypto": True}, "format"),  # 0x80092009 CRYPT_E_NO_MATCH
        ({"ok": False, "hresult": -2146881269, "crypto": True}, "format"),  # 0x8009310B ASN.1 bad tag
        ({"ok": False, "hresult": -2147024891, "crypto": True}, "unavailable"),  # 0x80070005 存取被拒
        ({"ok": False, "hresult": -2146233087, "crypto": False}, "unavailable"),  # 受限語言模式等非密碼學例外
        ({"ok": False, "hresult": 0, "crypto": False, "empty": True}, "format"),  # pfx 內沒有憑證
    ],
)
def test_error_classification_only_blames_the_file_for_crypt_facility_errors(
    monkeypatch, tmp_path, payload, kind
):
    import json
    import subprocess

    def fake_run(*_a, **_k):
        return subprocess.CompletedProcess([], 0, stdout=json.dumps(payload).encode(), stderr=b"")

    monkeypatch.setattr(certinfo.subprocess, "run", fake_run)
    with pytest.raises(certinfo.CertReadError) as ei:
        certinfo.read_pfx_info(tmp_path / "x.pfx", "pw")
    assert ei.value.kind == kind


def test_process_creation_blocked_is_environment_problem_not_certificate_problem(monkeypatch, tmp_path):
    """AppLocker／ACL 擋掉 powershell.exe（PermissionError）→ unavailable，讓呼叫端退回線上查法。"""

    def blocked(*_a, **_k):
        raise PermissionError("blocked by policy")

    monkeypatch.setattr(certinfo.subprocess, "run", blocked)
    with pytest.raises(certinfo.CertReadError) as ei:
        certinfo.read_pfx_info(tmp_path / "x.pfx", "pw")
    assert ei.value.kind == "unavailable"
