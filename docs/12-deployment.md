# Phase 12：上線

## 目前選擇：裝在業主自己的 Windows 電腦（免費）

業主決定（2026-11-10）：先不租主機，把系統裝在一台一直開著的 Windows 電腦上；跑得不順再改租主機，資料可以直接搬過去。

操作步驟（中文）在 [`selfhost/使用說明.txt`](../selfhost/使用說明.txt)。安裝包在 `selfhost/`：

| 檔案 | 用途 |
|---|---|
| `啟動.bat`（`install.ps1`） | 第一次：檢查 Docker Desktop、產生這台電腦專用的密鑰、建置系統、更新資料庫、建立管理員帳號、取得同事用網址；之後：啟動或更新到新版本（資料保留） |
| `停止.bat` | 暫停系統，資料保留 |
| `顯示網址.bat` | 查看同事用的網址 |
| `設定固定網址.bat`（`fixed-url.ps1`） | 用 Tailscale Funnel（免費）取得固定的 https 網址（`https://<電腦>.<tailnet>.ts.net` → `localhost:8080`），寫入 `ERP_PUBLIC_BASE_URL`，關掉 Cloudflare 臨時網址（`COMPOSE_PROFILES`） |
| `做搬家包.bat`（`pack.ps1`） | 把資料庫、照片收據、鑰匙打包成 `backups/move-<日期>`（搬到別台電腦或換電腦用） |
| `還原搬家包.bat`（`restore.ps1`） | 在新電腦還原搬家包：使用搬家包的鑰匙（加密資料才打得開），沿用這台已有資料庫的密碼；還原資料庫、照片收據，更新到最新版本後啟動 |
| `docker-compose.yml` | 五個服務：資料庫、ERP、排程、備份、對外網址 |

### 五個服務

| 服務 | 內容 |
|---|---|
| `db` | PostgreSQL 16，資料在 Docker volume |
| `app` | ERP（網頁＋API），只開在 `http://localhost:8080`（這台電腦） |
| `scheduler` | 每分鐘處理 Whatnot 通知、每小時同步訂單／出貨／直播／財務報表（`python -m app.scheduler`，呼叫 `/api/v1/internal/cron/*`，以密鑰保護） |
| `backup` | 每天備份資料庫（`pg_dump`）與上傳的照片、收據到 `selfhost/backups`，保留 30 天 |
| `tunnel` | Cloudflare 免費通道，提供 https 網址給任何地方的同事；網址在重新開機後會換 |

### 安全

- **密鑰**：只存在這台電腦的 `selfhost/.env`，第一次啟動時隨機產生，不進 git；`backups` 裡另存一份供還原。
- **連線**：ERP 只開在本機，外部只能透過 https 通道連入。
- **登入**：與正式環境相同。管理員用密碼登入時必須啟用雙因素驗證；登入 token 存在 Secure Cookie 中。
- **Google 一鍵登入**：需要固定網址才能設定，所以目前先用「密碼＋驗證碼」。

### 已在這裡驗證

| 項目 | 結果 |
|---|---|
| 建置 | 映像檔建置完成（342 MB） |
| 資料庫 | 更新到最新版本 |
| 管理員帳號 | 用安裝流程的方式建立成功 |
| 健康檢查 | `/readyz`：資料庫 ok |
| 登入 | 密碼登入成功 |
| 排程 | 每分鐘呼叫成功（尚未連 Whatnot 時正確略過） |
| 備份 | 產生資料庫與檔案備份，並實際還原到另一個資料庫成功 |
| 重新啟動 | 停止後再啟動，資料保留 |
| 搬家 | 在第二套環境（已有自己的鑰匙與資料庫）還原搬家包：帳號登入、照片收據、之後新增檔案都正常 |

**未能在這裡驗證的**：
- **對外 https 通道**：測試環境不允許對外開通道，要在業主的電腦上確認。
- **PowerShell 腳本**：要在 Windows 上實際執行。

### 已知限制

- 預設的 Cloudflare 免費網址在電腦重開後會改變；執行「設定固定網址.bat」改用 Tailscale Funnel 後網址固定（需在這台電腦安裝並登入 Tailscale）。Tailscale 部分無法在這裡驗證，要在業主電腦上確認。
- 這台電腦關機、睡眠、斷網時，大家都無法使用。

## 之後改租主機

程式已經準備好兩種方式：

### Google Cloud（東京）

- **開通與部署**：`deploy/gcp/bootstrap.sh`（在 Cloud Shell 執行一次）＋ `.github/workflows/deploy.yml`（手動執行部署）。
- **架構**：
  - 主機：Cloud Run，request-based billing，最少 0 台、最多 2 台
  - 資料庫：Cloud SQL PostgreSQL 16（db-f1-micro），每日備份＋時間點還原
  - 照片、收據、匯入檔：私人 Cloud Storage
  - 排程：Cloud Scheduler，每小時同步、每分鐘處理通知
  - 部署權限：Workload Identity Federation，不使用任何金鑰檔
- **費用**：約每月 15～25 美元。
- 業主目前不採用。

### 其他主機（Zeabur、Railway 等）

- 使用同一個 `Dockerfile` 和同樣的環境變數；檔案可以用 `ERP_SINGLE_SERVER=true` 加上永久磁碟。
- **搬家**：把 `backups` 裡最新的資料庫備份還原到新主機，再帶上 `.env` 裡的密鑰即可。

## 程式變更

| 項目 | 內容 |
|---|---|
| 檔案儲存 | 本機硬碟或私人 Cloud Storage；由 API 讀出後送給瀏覽器（`nosniff`、PDF 直接顯示） |
| 排程端點 | `/api/v1/internal/cron/whatnot-sync`、`/webhooks`（`X-Cron-Secret`，未設定時不存在） |
| `ERP_INLINE_JOBS` | 在會暫停背景工作的主機上，「立即同步」改在請求內完成 |
| `ERP_SINGLE_SERVER` | 單機安裝可以使用本機檔案儲存 |
| 正式環境檢查 | 拒絕：開發用密鑰、非 https 網址、太短的排程密鑰、雲端主機用本機儲存 |
| 管理員建立指令 | `create-admin --if-missing`：重複執行不出錯 |

## 資料庫變更

無。
