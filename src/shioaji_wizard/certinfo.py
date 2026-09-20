"""離線解析 Sinopac.pfx：不連線、不呼叫 shioaji，直接用 Windows 內建 .NET
X509Certificate2 讀出憑證到期日與 subject。

存在理由：使用者在客戶端實測，activate_ca（B5）失敗時舊流程整段查不到到期日，
但永豐官方 Shioaji Pro 憑證管理頁看得到——因為 pfx 本身可以離線讀、不需要登入
或啟用成功。這裡只做「讀 pfx」，判定與報表邏輯留在 test_ca.py。

零第三方依賴；只在 Windows 可用（用子行程呼叫 PowerShell 5.1，客戶機器不保證
有 pwsh）。pfx 路徑與密碼一律用子行程環境變數傳入，絕不出現在命令列或任何
print／log。
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Literal

_DEFAULT_TIMEOUT = 30.0

# Windows 內建 PowerShell（5.1）用 .NET X509Certificate2 解析 pfx。
# - 路徑／密碼只經環境變數 SJ_CERT_PATH／SJ_CERT_PWD，腳本裡不 Write-Output 它們。
# - [Console]::OutputEncoding 開頭就設 UTF8：專案已知坑，管線導向預設 big5，
#   中文路徑／subject 會壞。
# - EphemeralKeySet 在 .NET Framework < 4.7.2 沒有這個旗標成員，抓不到就退回
#   預設旗標，不能讓整個讀取因此失敗。
# - 密碼一律傳明文字串，不要轉 SecureString：X509Certificate2Collection.Import
#   只有 (string, string, flags) 多載，PowerShell 找不到相符多載時會默默把
#   SecureString ToString() 成字面字串 "System.Security.SecureString" 當密碼，
#   結果對「正確密碼」也回 0x80070056（密碼錯誤）。
# - 成功／失敗都輸出單行 JSON（-Compress），失敗時帶 hresult 與例外型別讓呼叫端
#   分類：只有「密碼學類 HRESULT（0x8009xxxx：ASN.1／CRYPT_E_*）的
#   CryptographicException」或 pfx 內沒有憑證才算憑證檔本身的問題；其餘——存取被拒
#   （0x80070005）、key storage／provider 問題、公司電腦的 PowerShell 受限語言模式
#   擋掉 New-Object——都算環境問題，退回線上查法，不能錯報成「憑證檔損毀」。
_PS_SCRIPT = r"""
$ErrorActionPreference = 'Stop'
$ProgressPreference = 'SilentlyContinue'
$WarningPreference = 'SilentlyContinue'
[Console]::OutputEncoding = [System.Text.Encoding]::UTF8

$path = $env:SJ_CERT_PATH
$plainPwd = $env:SJ_CERT_PWD

try {
    try {
        $flags = [System.Security.Cryptography.X509Certificates.X509KeyStorageFlags]::EphemeralKeySet
    } catch {
        $flags = [System.Security.Cryptography.X509Certificates.X509KeyStorageFlags]::DefaultKeySet
    }
    $coll = New-Object System.Security.Cryptography.X509Certificates.X509Certificate2Collection
    $coll.Import($path, $plainPwd, $flags)
    $cert = $null
    foreach ($c in $coll) {
        if ($c.HasPrivateKey) { $cert = $c; break }
    }
    if (-not $cert -and $coll.Count -gt 0) { $cert = $coll[0] }
    if (-not $cert) {
        Write-Output (@{ ok = $false; hresult = 0; crypto = $false; empty = $true; message = "pfx 內沒有任何憑證" } | ConvertTo-Json -Compress)
        return
    }
    $notAfter = $cert.NotAfter.ToUniversalTime().ToString("o")
    $result = @{ ok = $true; not_after = $notAfter; subject = $cert.Subject }
    $coll.Reset()
    Write-Output ($result | ConvertTo-Json -Compress)
} catch {
    $ex = $_.Exception
    if ($ex.InnerException) { $ex = $ex.InnerException }
    $hresult = 0
    try { $hresult = $ex.HResult } catch { $hresult = 0 }
    $crypto = $ex -is [System.Security.Cryptography.CryptographicException]
    $result = @{ ok = $false; hresult = $hresult; crypto = $crypto; message = $ex.Message }
    Write-Output ($result | ConvertTo-Json -Compress)
}
"""

_ERROR_INVALID_PASSWORD = 0x80070056  # ERROR_INVALID_PASSWORD
_FACILITY_SECURITY = 0x9  # 0x8009xxxx：CRYPT_E_*／ASN.1 解析錯誤＝檔案內容不是有效的 PKCS#12


def _is_crypt_facility(hresult: int) -> bool:
    return ((hresult & 0xFFFFFFFF) >> 16) & 0x1FFF == _FACILITY_SECURITY


class CertReadError(Exception):
    """離線讀取 pfx 失敗；kind 給呼叫端分流訊息，訊息本身不含金鑰內容。

    kind：
    - "password"：密碼錯（HRESULT 0x80070056）。
    - "format"：不是有效的 pfx，或檔案損毀。
    - "unavailable"：非 Windows、找不到 powershell.exe、逾時、或輸出無法解析——
      不代表密碼或檔案有問題，呼叫端應退回原本的線上查法。
    """

    def __init__(self, kind: Literal["password", "format", "unavailable"], detail: str = "") -> None:
        super().__init__(f"{kind}: {detail}" if detail else kind)
        self.kind = kind
        self.detail = detail


@dataclass(frozen=True)
class CertInfo:
    not_after: datetime  # aware，UTC
    subject: str


def read_pfx_info(path: Path, password: str, timeout: float = _DEFAULT_TIMEOUT) -> CertInfo:
    """離線解析 pfx，回傳到期日與 subject；不連線、不需要登入或 activate_ca 成功。

    只在 Windows 可用。其餘平台、找不到 powershell.exe、逾時、或輸出無法解析，
    一律 raise CertReadError("unavailable")——呼叫端應退回原本的線上查法，
    不能當成「憑證有問題」。
    """
    if sys.platform != "win32":
        raise CertReadError("unavailable", "離線讀取憑證僅支援 Windows")

    env = dict(os.environ)
    env["SJ_CERT_PATH"] = str(path)
    env["SJ_CERT_PWD"] = password

    try:
        proc = subprocess.run(
            ["powershell.exe", "-NoProfile", "-NonInteractive", "-Command", _PS_SCRIPT],
            env=env,
            capture_output=True,
            timeout=timeout,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
    except FileNotFoundError as e:
        raise CertReadError("unavailable", "找不到 powershell.exe") from e
    except subprocess.TimeoutExpired as e:
        raise CertReadError("unavailable", f"讀取逾時（{timeout} 秒）") from e
    except OSError as e:  # AppLocker／ACL 擋掉程序建立等：環境問題，不是憑證問題
        raise CertReadError("unavailable", f"無法啟動 PowerShell（{type(e).__name__}）") from e

    stdout = proc.stdout.decode("utf-8", errors="replace").strip()
    if not stdout:
        stderr = proc.stderr.decode("utf-8", errors="replace").strip()
        detail = f"PowerShell 沒有輸出（{stderr[:160]}）" if stderr else "PowerShell 沒有輸出"
        raise CertReadError("unavailable", detail)

    try:
        data = json.loads(stdout.splitlines()[-1])
    except (json.JSONDecodeError, IndexError) as e:
        raise CertReadError("unavailable", f"PowerShell 輸出無法解析：{stdout[:160]}") from e

    if not data.get("ok"):
        hresult = int(data.get("hresult") or 0)
        message = str(data.get("message") or "")
        if (hresult & 0xFFFFFFFF) == _ERROR_INVALID_PASSWORD:
            raise CertReadError("password", message)
        if data.get("empty") or (data.get("crypto") and _is_crypt_facility(hresult)):
            raise CertReadError("format", message)
        raise CertReadError("unavailable", message)

    try:
        not_after = datetime.fromisoformat(str(data["not_after"]))
        subject = str(data["subject"])
    except (KeyError, ValueError) as e:
        raise CertReadError("unavailable", f"PowerShell 輸出欄位不完整：{stdout[:160]}") from e

    return CertInfo(not_after=not_after, subject=subject)
