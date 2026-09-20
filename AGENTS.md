# AGENTS.md — shioaji-wizard 開發指引

回應一律台灣慣用繁體中文。本專案獨立於 fcn-pricing（殼與打包流程是從那邊移植
來的，**不要反向修改 fcn-pricing**）。

## 一句話

給完全不懂技術的永豐使用者的「Shioaji 開通測試精靈」：FastAPI 後端＋系統
Chromium `--app` 殼，一頁式 GUI 做 A（模擬登入＋模擬下單測試）、B（正式環境
憑證測試），逐項列出 ✓／✗／－ 與原因；`.env`（SJ_API_KEY／SJ_SEC_KEY／
SJ_CA_PASSWD／SJ_CA_PATH）讓 Shioaji Pro／`shioaji server` 直接沿用——支援多
帳號，`.env` 實際位置跟著選用的憑證走（程式同層 or eLeader 憑證資料夾），見
「結構」`sjenv.py`／`server.py` 條目。打包成 portable 零依賴資料夾
（`dist/shioaji_wizard/`：使用者只看到 `wizard.exe`、`.env`、`Sinopac.pfx`、
匯出的 `.log`，其餘 hidden）。畫面行為細節見 `README.md`「畫面行為」。

## 鐵則

1. Python 一律 uv（`uv sync`／`uv add`／`uv run`），禁 pip；裝新套件先問
   （Pillow 只在 `tools/make_icon.py` 用 `uv run --with pillow`，不進依賴）。
2. 改 .py 必跑 `uv run ruff check src tools tests`＋`uv run ruff format`＋
   `uv run ty check src tools`；改 .md 必跑 `npx markdownlint-cli2`。
3. 測試：`uv run pytest -q`（不連永豐；A／B 真測試只能由使用者在 GUI 觸發）。
4. web／app 改動 QA 要親眼看畫面：
   `uv run python -m shioaji_wizard --no-shell --port 8765 --root <資料夾>`
   後用瀏覽器或 `msedge --headless=new --screenshot` 看；有殼模式
   `uv run python -m shioaji_wizard --root <資料夾>`。`index.html` 每次請求
   重讀、測試腳本走子行程 → 改這些不用重啟；改 `server.py` 要重啟。
5. 絕不要求使用者把 API Key／Secret Key／憑證密碼貼進對話；絕不 print／log
   金鑰（子行程輸出與匯出紀錄都走 `_redact_text`）。**QA 用的工作資料夾
   （如 `.qa/`）放了真金鑰，用完必刪**；`dist/shioaji_wizard/.env` 是開發者
   實測留下的，重建會保留、zip 永遠不含。
6. 不主動 commit／push，等使用者說。

## 結構

- `src/shioaji_wizard/sjenv.py` — ROOT（`SJ_ENV_DIR`，否則 cwd；`.runtime`
  隱藏目錄、`Sinopac.pfx` 預設位置都綁這裡，不隨帳號切換）、`PROFILE_DIR`
  （`SJ_PROFILE_DIR`，否則＝ROOT；`.env` 實際所在，供 `test_sim_order`／
  `test_ca` 子行程沿用）、`.env` 解析／補鍵、`Report`／`print_summary`
  （`SJ_NO_TEXT_SUMMARY` 可關）。
- `certinfo.py` — 離線解析 `.pfx`（不連線、不用 shioaji）：子行程呼叫 Windows
  內建 PowerShell 5.1 的 .NET `X509Certificate2` 讀到期日與 subject；路徑／
  密碼經環境變數傳入。非 Windows、找不到 `powershell.exe`、逾時、輸出無法
  解析一律 `CertReadError("unavailable")`，呼叫端（`test_ca.py`）退回原本的
  `api.get_ca_expiretime` 線上查法；密碼錯／格式錯分開分類。
- `server.py` — FastAPI：`/api/state`（含 `stale`、`keys_locked`、
  `profiles`、`root_env_available`）、`/api/env`（鎖定中改金鑰 409，
  `unlock_keys` 解鎖；憑證路徑指到別的 profile 會先切換）、`/api/profile`
  （切換目前帳號：`pfx` 為 `find_eleader_pfx()` 白名單內路徑，空字串＝切回
  ROOT；切換一律兩段式：`_enter_profile_dir` 只改記憶體並清 SESSION／
  `_STALE_FIELDS` → 寫目標 `.env` → 最後才 `_persist_active_profile` 提交，
  任何 OSError 還原記憶體與目標 `.env` 原位元組）、`/api/browse`
  （`_browse_script`：UTF-8 輸出＋TopMost owner）、`/api/window`、`/api/run`
  （先清空 SESSION 再跑）＋`/api/job`（子行程跑 `test_sim_order`／
  `test_ca`，帶 `SJ_PROFILE_DIR`，逐行遮罩、120s 無輸出／600s 總逾時、
  `Job.proc`／`cancel_job()`）、`/api/summary`、`/api/heartbeat`、`/api/open`
  （白名單）、`/api/export-log`（一律寫 ROOT，不隨帳號切換）、
  `/favicon.ico`；guard 在建 app 時掛。目前帳號＝模組層可變狀態
  `_profile_dir`（`env_path()` 取代舊常數 `ENV_PATH`），持久化在
  `.runtime/active-profile`（只採信 ROOT 或現存 eLeader 候選的父目錄）；
  `find_eleader_pfx()` 掃 `EKEY_BASE`（`C:\ekey\551`，QA 用 `SJ_EKEY_BASE`
  覆寫，不動真的 `C:\ekey`）。
- `test_sim_order.py`（A1–A5）、`test_ca.py`（B1–B6；`--futures` 決定 B3；
  B4／B6 用 `certinfo` 離線先做、不受登入／B5 影響，B5 失敗會附離線診斷
  含身分證字號比對）— 也可獨立 `-m` 執行；都接管 shioaji 的委託回報
  callback。
- `desktop.py`／`shell.py`／`chromium.py`／`status.py`／`guards.py`／
  `__main__.py` — 殼與生命週期（移植自 fcn-pricing `app/`：視窗存活為主、
  job 執行中不關但關窗會殺 job 與選檔對話框、心跳 idle fallback、
  `timeout_graceful_shutdown=5`、`--disable-extensions --disable-sync` 一律帶、
  `.app-ready` 在 `shell.launch` 後立刻寫）。
- `static/index.html` — 單頁前端（含使用說明彈窗、標示語義、金鑰鎖定、
  帳號清單與切換）。
- `tools/build_bundle.py`、`tools/launcher.cs`、`tools/make_icon.py` —
  portable 打包（`uv run python tools/build_bundle.py --zip --verify`；zip 從
  tmp bundle 打且排除 .env／pfx；重建保留舊 bundle 的 .env／pfx；verify 後清
  `.runtime`；csc `/codepage:65001`）。

## 已知坑

- shioaji 1.7.3：`api.contracts`（v2）沒有 `.Stocks`，測試用 `api.Contracts`＋
  壓 DeprecationWarning；`account_type` 是 enum 要取 `.value`；`import shioaji`
  會在 cwd 寫 `shioaji.log`（子行程 cwd＝`.runtime`，伺服器行程本身不
  import shioaji）；連線訊息「Session up」沒換行會黏住下一行，reader 要切。
- 永豐錯誤型別：`CaPasswordError`＝憑證密碼錯、`CaError ReadFile`＝路徑錯、
  登入 `BadRequestError key … not exist`、`ShioajiValueError invalid secret_key`。
- Windows PowerShell stdout 導向管線預設 big5 → 選檔路徑必先設
  `[Console]::OutputEncoding=UTF8`；對話框沒 owner 會被壓在 app 後面。
- 換 exe icon 後 Explorer 仍顯示舊圖＝磁碟 iconcache_*.db（停 Explorer 刪
  cache 再重啟；`ie4uinit -show` 不夠）。
- console 是 big5 → 跑 Python 帶 `PYTHONUTF8=1`；.bat 一律 CRLF 無 BOM。
- `classList.toggle(x, force)` 的 force 傳 undefined 會變「翻轉」；
  `Path.with_suffix` 會吃掉 `v0.1.0` 最後一節。
- TestClient 預設 Host=testserver 會被 origin guard 擋 → fixture 用
  `base_url="http://127.0.0.1"`。
- `.env` 是金鑰檔：所有寫入走 `sjenv.atomic_write_text`（暫存檔＋`os.replace`）；
  既有檔「讀不到」（OSError）一律上拋，**不可當空檔重建**；要切換／搬家時，目標檔
  快照在改任何狀態之前拍、`active-profile` 最後才提交。改 `.env` 存放規則必須同時
  給既有使用者遷移路徑（「空白欄位＝保留原值」換了檔就變成空值）。
- 檢核單列數固定（A1–A5／B1–B6）：每一條早退路徑都要把沒跑到的項目補 SKIP，
  調整檢查順序後用「每項恰好記一次」的測試對帳（見 `tests/test_diagnostics.py`）。
- PowerShell 呼叫 .NET 方法找不到相符多載時會默默把參數 `ToString()`：
  `X509Certificate2Collection.Import` 沒有 `SecureString` 多載，傳
  `SecureString` 會變成字面字串當密碼，對正確密碼也回密碼錯——`.NET` 互通一律
  傳明文字串，不要圖「安全」轉 `SecureString`。

## Review 時的驗證指令（唯讀沙箱可跑）

review 沙箱是唯讀的，`uv run` 會因為要寫快取而失敗；要實跑驗證請直接用 venv
執行檔、關掉所有快取寫入：

```text
.venv/Scripts/python.exe -B -m pytest tests/ -q -p no:cacheprovider
.venv/Scripts/ruff.exe check src tools tests --no-cache
```
