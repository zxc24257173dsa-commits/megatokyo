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
