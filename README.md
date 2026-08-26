# shioaji-wizard

> **一般使用者請到下載頁：<https://benjamin-teng.github.io/shioaji-wizard/>**
> （免裝 Python，解壓後雙擊 `wizard.exe`）。以下為開發文件。

永豐 Shioaji 開通測試精靈：本機 FastAPI 後端＋系統 Chromium `--app` 殼的
一頁式桌面工具，給完全不懂技術的使用者完成永豐 API 開通必經的「模擬下單
測試」（A）與「正式環境憑證測試」（B），逐項列出通過／未通過與原因。設定存成
程式同層的 `.env`（`SJ_API_KEY`／`SJ_SEC_KEY`／`SJ_CA_PASSWD`／`SJ_CA_PATH`），
與 Shioaji Pro／`shioaji server` 同名、可直接沿用。憑證檔欄預設留空（＝同資料夾的
`Sinopac.pfx`），並提示 eLeader 預設位置 `C:\ekey\551\(身分證字號)\S`（只有裝過
eLeader 的電腦才有；偵測到會給「使用這個」一鍵帶入）。

## 畫面行為（v0.1.0）

- **金鑰與憑證**：四欄（API Key、Secret Key、憑證密碼、憑證檔＋「瀏覽…」）。
  輸入框顏色＝儲存狀態（綠＝已存、黃＝缺；一開始打字就還原預設、清空又回
  綠／黃）。標籤前的圓圈＝最近一次 A／B 對該欄的驗證結果（✓ 通過／✗ 卡在
  這欄；未驗或改過尚未再驗就空白）。更新過的欄位在涵蓋它的測試跑完前一律
  不標（A 只涵蓋金鑰，B 才涵蓋憑證密碼／憑證檔；伺服器端 `stale` 記錄，重整
  不會倒回）。
- **金鑰鎖定**：A1／B1 通過且之後沒改 → API Key／Secret Key 鎖定（Secret
  Key 只會生成一次，不給誤觸機會），要換按「解鎖修改」；鎖定中伺服器拒絕改
  金鑰（409）。按下解鎖後兩欄的 ✓／✗ 先拿掉；開始測試或測試跑完就結束解鎖
  狀態。
- **瀏覽…**：Windows 原生檔案對話框，以 TopMost＋工作列可見的隱形 owner 視窗
  開啟（否則會壓在 app 後面、工作列沒項目）；PowerShell 輸出強制 UTF-8
  （中文路徑不會亂碼）。
- **A／B 按鈕**各附用途說明；「也測期貨／選擇權」是整顆可按的切換鈕（按下變
  綠打勾）。A 執行前檢查台灣時間（週一～五 08:00–20:00）與 18–20 點的台灣
  IP。
- **執行輸出**：子行程逐行輸出（即時遮罩金鑰值與身分證字號），末尾是
  `═══` 框線的文字總覽；**檢核單**表格列每一項 ✓／✗／－ 與原因，原因裡的
  永豐簽署頁網址可點（走 `/api/open` 白名單、系統瀏覽器開）。
- **匯出除錯紀錄**：在程式同層產生 `shioaji_wizard-debug-<時間>.log`
  （版本、遮罩後 .env、檢核單、最近一次輸出、app.log／shioaji.log 尾段；金鑰
  值全文遮罩、身分證字號只留頭尾），並在檔案總管選取它。
- **使用說明**是畫面內彈窗。
- 殼：一律 `--disable-extensions --disable-sync`；關窗即收攤（job 執行中
  會先殺子行程；子行程另有 120 秒無輸出／10 分鐘總逾時）。

## 安全與免責聲明

- 本軟體為個人開發的開源工具，**非永豐金證券官方軟體**，與永豐金證券無任何
  隸屬或合作關係。
- 金鑰只在使用者電腦與永豐官方 API 之間傳輸：後端只綁 127.0.0.1（Host／
  Origin 守衛擋 DNS rebinding 與 CSRF、無 CORS），金鑰不進 argv／環境變數／
  URL／瀏覽器儲存，API 只回遮罩值；子行程從 `.env` 讀金鑰後交給永豐官方
  shioaji SDK（Rust 編譯，reqwest＋rustls，未發現關閉憑證驗證）連永豐伺服器。
  本軟體不會把金鑰、憑證或帳號資料送到永豐以外的伺服器。唯一的第三方連線：
  週一～五 18:00–20:00 呼叫 `/api/window` 時向 `ipinfo.io` 查公網 IP 所屬國家
  （永豐該時段限台灣 IP）：ipinfo.io 會收到使用者公網 IP 並回傳 IP／國家，
  不會送出金鑰、憑證、密碼或帳號資料。
- 金鑰用途僅限「模擬環境連線測試（A：`simulation=True`，只在模擬環境下單）」
  與「正式環境憑證連線測試（B：只登入與查詢，無任何下單呼叫）」。
- 金鑰與憑證由使用者自行保管（`.env`／`Sinopac.pfx` 為明文檔案）；本軟體與
  開發者不負任何金鑰／憑證保管責任。
- 本軟體按「現狀」提供，不提供任何明示或默示之擔保；因使用本軟體、金鑰或
  憑證外洩、帳戶操作、永豐服務或規則變更所造成之任何損失（含交易損失），
  開發者概不負責。使用即表示同意以上條款。完整條文同時放在程式內「使用說明」、
  bundle 的 `README.txt` 與下載頁。

## 開發

```bash
uv sync
uv run python -m shioaji_wizard --no-shell --port 8765 --root <資料夾>   # 起服務，不開殼
uv run python -m shioaji_wizard --root <資料夾>                          # 有殼（開 Edge/Chrome --app）
uv run pytest -q
uv run ruff check src tools tests && uv run ruff format src tools tests
uv run ty check src tools
uv run --with pillow python tools/make_icon.py   # 重新產 static/favicon.ico（自製印章圖）
```

`--root` 是工作資料夾（放 `.env`／`Sinopac.pfx`；內部檔案 `app.log`／
`shioaji.log`／`.app-ready`／`.app-status` 放其下的隱藏子目錄 `.runtime`，見
`src/shioaji_wizard/sjenv.py`）。正式打包後的桌面 app 用 exe 所在資料夾。
**QA 用的工作資料夾若放了真金鑰，用完要刪**（例如 `.qa/`，已 gitignore）。

## 打包（portable bundle）

```bash
uv run python tools/build_bundle.py [--zip] [--verify] [--allow-oversize]
```

移植自姊妹專案 fcn-pricing 的同名打包腳本（複製 uv 管理的 CPython 3.12.13
standalone、套件依 `uv.lock` 同版 vendored、launcher 以 Windows 內建 `csc.exe`
現編、`/codepage:65001`）；差異與設計取捨見 `tools/build_bundle.py` 模組頂部
docstring。

- `--zip`：另打一份 ASCII 檔名 zip（`dist/shioaji-wizard-v<版本>.zip`）。zip
  一律從乾淨的 tmp bundle 打、且永遠排除根層 `.env`／`*.pfx`。
- `--verify`：build 完成後在隔離 PATH 下起一次服務，確認首頁與 `/api/state`
  正常、閒置逾時自動關閉、`.runtime` 為 hidden；verify 後清掉 `.runtime`。
- 重建時會把舊 bundle 根層的 `.env`／`*.pfx` 搬回新 bundle（開發者實測用的
  設定不會被吃掉）。
- `--allow-oversize`：檔案數超過 `MAX_FILES`（5000）時仍產出（只警告）。

輸出在 `dist/shioaji_wizard/`（bundle 目錄名固定不帶版本；版本號在 zip 檔名
與 exe 版本資源）。

### bundle 內容結構

```text
dist/shioaji_wizard/
├── wizard.exe              # 雙擊即開啟（與除錯 .bat 一起是僅有的可見項目）
├── python/                 # 內嵌 CPython 3.12.13 + vendored 套件（隱藏）
├── 啟動（除錯）.bat          # 可見；視窗開不起來時雙擊它看即時錯誤
├── README.txt              # 使用說明＋SmartScreen 解除封鎖教學（隱藏）
├── .env                    # 使用者自己產生；API 金鑰等設定
├── Sinopac.pfx              # 使用者自己放；永豐憑證檔
├── .runtime/                # 程式跑起來後才出現（隱藏）
└── shioaji_wizard-debug-*.log  # 使用者按「匯出除錯紀錄」才會出現
```

除 `wizard.exe`／`啟動（除錯）.bat`／`.env`／`*.pfx`／`*.log` 外，其餘項目在打包時與每次啟動時都
會被 best-effort 設為隱藏（打包端 `hide_bundle_top_level`；啟動端
`tools/launcher.cs` 的 `RehideTopLevel`）。zip 條目另把隱藏屬性寫進
`external_attr`，Explorer「解壓縮全部」／7-Zip 會照著還原；`Expand-Archive`／
`tar` 不會，那種情況第一次啟動 `wizard.exe` 後才會隱藏。啟動提示（splash）在瀏覽器視窗送出啟動後約 1.5 秒消失。
