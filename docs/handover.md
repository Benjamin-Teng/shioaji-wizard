# Shioaji 測試精靈交接

## 操作流程

桌面版沿用「跟著做」的四步驟。每次啟動從「設定金鑰」開始。使用者可開啟現有
`.env` 或選擇新檔位置。偵測到 eKey 時，新建預設位置是憑證資料夾，否則為
`%USERPROFILE%\.shioaji-pro`。設定金鑰後可用左側橘色按鈕複製路徑。

Shioaji 1.8.0 的美股複委託可在模擬環境加測委託，在正式環境加查帳戶簽署狀態。
自動測試使用模擬回應。對永豐環境送出的模擬委託由使用者從 App 觸發。

## 打包與宣傳頁

Windows 下載檔由 NSIS 產出。GitHub Pages 從 `main` 的 `docs/` 部署，下載按鈕
連到最新版的 `shioaji-wizard-setup.exe`。截圖使用隔離設定的實際桌面介面，
畫面中的 Windows 使用者名稱以「使用者」呈現。

NSIS 依出貨清單解除安裝。啟動器需將執行資料放進安裝目錄的 `data`，並避免 Python
在程式目錄產生 bytecode，否則從開始選單啟動後，解除安裝會留下快取。發版驗收
包含 `--verify` 及啟動實際 Windows 開始選單捷徑後的解除安裝。

版本、安裝檔與校驗碼以 GitHub Release 為準。待辦與驗收證據記在 `kata list`。
