# Phase 2：登入、權限、雙因素驗證、多地同步

## 登入流程

```
POST /api/v1/auth/login {email, password}
  ├─ 帳號沒有雙因素  → 回傳 access token（JSON）＋ refresh token（httpOnly cookie）
  └─ 帳號有雙因素    → 回傳 mfa_token（5 分鐘有效）
        POST /api/v1/auth/mfa/verify {mfa_token, code} → access token ＋ refresh cookie
POST /api/v1/auth/refresh   用 cookie 換新的 access token（舊 refresh token 立即作廢）
POST /api/v1/auth/logout    作廢這次登入的所有 refresh token
GET  /api/v1/auth/me        目前使用者、角色、權限、待辦限制
POST /api/v1/auth/password  修改密碼（其他裝置全部登出）
POST /api/v1/auth/mfa/setup | /mfa/enable | /mfa/disable
```

## 安全設計

| 項目 | 做法 |
|---|---|
| 密碼儲存 | Argon2id 雜湊，不可還原 |
| 密碼規則 | 至少 12 字元、不可含 email 名稱、字元種類不能太少 |
| 錯誤訊息 | 帳號不存在、密碼錯、帳號停用、帳號鎖定 → 一律同一句話（不洩露帳號是否存在） |
| 鎖定 | 連續 5 次失敗（密碼或驗證碼）鎖定 15 分鐘；管理員可手動解鎖 |
| Access token | JWT，15 分鐘，只存在前端記憶體 |
| Refresh token | 隨機 64 字元，資料庫只存雜湊；httpOnly + Secure + SameSite=Strict cookie，網頁程式讀不到；每次使用即輪替 |
| Token 被盜偵測 | 已作廢的 refresh token 再被使用 → 整個登入工作階段全部作廢並記錄 |
| 立即失效 | 停用帳號、改密碼、改角色、重設雙因素 → 該使用者所有裝置立即登出 |
| 雙因素 | TOTP（Google Authenticator / 1Password 等），密鑰以 AES-256-GCM 加密存放；同一組驗證碼不能用兩次 |
| 強制雙因素 | **管理員角色必須啟用雙因素**，未啟用前只能進行雙因素設定（`ERP_MFA_REQUIRED_ROLES` 可加入其他角色） |
| 首次登入 | 管理員替員工建立的帳號是臨時密碼，員工首次登入必須先改密碼 |
| 最後一位管理員 | 系統拒絕停用或移除最後一位有效管理員；也不能停用自己 |
| 稽核 | 登入成功/失敗、登出、改密碼、雙因素變更、使用者建立/修改（含修改前後）全部寫入只能新增的 `audit_logs`；密碼與密鑰欄位一律遮蔽 |
| 正式環境保護 | `ERP_ENV=production` 時若仍使用開發預設密鑰，程式拒絕啟動 |

## 多地同步（日本／美國／台灣）

- 三地使用同一個 API 與同一個 PostgreSQL，沒有各地副本，因此不存在「同步延遲」或資料分歧。
- 每個請求都重新讀取使用者與權限：在台灣停用某人的帳號，美國那台電腦下一個操作就會被拒絕。
- 兩地同時修改同一筆資料（例如商品、出貨單）時使用樂觀鎖：後存檔的人會收到 `409 modified_by_someone_else`，需重新載入，不會互相覆蓋。
- 時間一律以 UTC 儲存；每位使用者有自己的時區（東京／台北／紐約）供畫面顯示，報表日期一律使用美東時間。

## API 一覽（需 `users.manage`）

| 方法 | 路徑 | 用途 |
|---|---|---|
| GET | /api/v1/users | 使用者列表（`?include_inactive=true`） |
| POST | /api/v1/users | 建立使用者（臨時密碼＋角色） |
| GET / PATCH | /api/v1/users/{id} | 查看／修改（名稱、語系、時區、啟用、角色） |
| POST | /api/v1/users/{id}/reset-password | 重設臨時密碼 |
| POST | /api/v1/users/{id}/reset-mfa | 手機遺失時重設雙因素 |
| POST | /api/v1/users/{id}/unlock | 解除鎖定 |
| GET | /api/v1/roles | 角色與權限 |
| GET | /api/v1/audit-logs | 操作紀錄（需 `audit.view`；可依對象、使用者、動作、時間篩選） |

## 建立第一位管理員

```bash
cd backend
python -m app.cli create-admin --email you@example.com --name "Owner"
```

密碼以互動方式輸入，不會留在指令紀錄中。第一次登入後系統會要求設定雙因素驗證。

## Google 帳號登入（2026-11-09 新增，主要登入方式）

業主決定以 **Google 帳號一鍵登入** 作為主要登入方式：不用記密碼，也不用輸入驗證碼。

### 運作方式

1. 管理員在「使用者」頁面加入員工的 **Google 帳號 Email**（臨時密碼留空 = 只用 Google 登入）。
2. 員工打開 ERP → 按「使用 Google 帳號登入」→ 在 Google 選帳號 → 自動回到 ERP。
3. 沒有被加入的 Google 帳號一律拒絕；停用的帳號立即無法登入。
4. 第一次登入時系統會綁定該 Google 帳號的唯一編號；之後即使有人拿到同一個 Email 地址的新 Google 帳號也無法冒用。

### 安全設計

| 項目 | 做法 |
|---|---|
| 協定 | OpenID Connect 授權碼流程（Google 官方建議的伺服器端方式） |
| 防偽造登入 | state（綁定 10 分鐘有效的簽章 cookie）、nonce、PKCE (S256) |
| 驗證 Google 回傳 | 以 Google 公開金鑰驗簽 ID Token，檢查發行者、受眾（我們的 Client ID）、到期時間、nonce、Email 已驗證 |
| 公司網域限制（選用） | 設定 `ERP_GOOGLE_HOSTED_DOMAIN` 後，只接受該 Google Workspace 網域的帳號 |
| 不會跳到其他網站 | 登入後只導回本站路徑 |
| Client Secret | 只存在伺服器（Secret Manager），不會送到瀏覽器 |
| 雙因素 | Google 登入不再要求 ERP 的驗證碼；**建議每位員工在 Google 帳號開啟「兩步驟驗證」** |
| 備用登入 | Email＋密碼仍可用（例如 Google 故障時）；管理員以密碼登入時仍需驗證碼。設定 `ERP_PASSWORD_LOGIN_ENABLED=false` 可完全關閉密碼登入 |

### 設定步驟（部署時需要業主操作一次）

1. 打開 [Google Cloud Console](https://console.cloud.google.com/) → 選擇或建立專案。
2. 「API 和服務」→「OAuth 同意畫面」：
   - 有 Google Workspace（公司網域信箱）：選「內部」。
   - 一般 Gmail：選「外部」，並在「測試使用者」加入員工的 Gmail，或發布應用程式。
3. 「API 和服務」→「憑證」→「建立憑證」→「OAuth 用戶端 ID」→ 類型「網頁應用程式」。
4. 「已授權的重新導向 URI」加入：
   - 正式環境：`https://<你的網域>/api/v1/auth/google/callback`
   - 本機測試：`http://localhost:8000/api/v1/auth/google/callback`
5. 把產生的 **用戶端 ID** 與 **用戶端密鑰** 設為環境變數 `ERP_GOOGLE_CLIENT_ID`、`ERP_GOOGLE_CLIENT_SECRET`（正式環境放在 Secret Manager；**不要貼在聊天或程式碼中**），並設定 `ERP_PUBLIC_BASE_URL=https://<你的網域>`。

### 第一位管理員

```bash
python -m app.cli create-admin --email 你的Gmail --name "Owner" --google-only
```

### 資料庫變更（Migration 0005）

- `users.password_hash` 可為空（只用 Google 登入的帳號）
- `users.google_sub`：綁定的 Google 帳號編號（唯一）
- `refresh_tokens.auth_method`：這次登入的方式（password / password+totp / google）
