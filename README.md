# Whatnot Global ERP V2

跨國直播電商 ERP（日本採購／美國 Whatnot 直播／台灣管理）。

- 設計文件：[`docs/00-architecture-and-database-design.md`](docs/00-architecture-and-database-design.md)
- 已確認的業務決策：[`docs/01-decisions.md`](docs/01-decisions.md)
- 登入與權限：[`docs/02-auth-and-permissions.md`](docs/02-auth-and-permissions.md)
- 商品與庫存：[`docs/03-catalog-and-inventory.md`](docs/03-catalog-and-inventory.md)
- 成本匯入：[`docs/04-cost-import.md`](docs/04-cost-import.md)
- Whatnot 訂單同步、拆盒：[`docs/05-whatnot-orders.md`](docs/05-whatnot-orders.md)
- 單場直播損益、Whatnot 財務報表：[`docs/06-show-pnl.md`](docs/06-show-pnl.md)

## 目前進度

| Phase | 狀態 |
|---|---|
| 1 系統架構與資料庫 | ✅ 完成（57 張資料表、資料庫層完整性規則、41 項自動測試） |
| 2 登入、權限與多地同步 | ✅ 完成（**Google 帳號一鍵登入**、備用密碼＋雙因素、RBAC、稽核） |
| 3 商品與多倉庫庫存 | ✅ 完成（商品/規格/組合/照片、期初庫存、調整、盤點、FIFO 批次、網頁介面；瀏覽器端對端測試） |
| 4 CSV / Excel 成本匯入 | ✅ 完成（依暫定範本；多工作表、自動欄位對應、預覽、FIFO 入庫；正式範本待業主提供） |
| 5 Whatnot API 與訂單同步 | ✅ 完成（REST API 同步、Webhook、直播訂單 CSV、SKU 名稱規則、更改對應、拆盒平均分攤；以業主帳號只讀驗證） |
| 6 單場直播銷售報表 | 待開始 |

## 本機開發

需求：Python 3.12+、PostgreSQL 16（或 Docker）。

### 方式 A：Docker（推薦）

```bash
docker compose -f infra/docker-compose.yml up -d db
docker compose -f infra/docker-compose.yml run --rm api alembic upgrade head
docker compose -f infra/docker-compose.yml up api
```

開啟 http://localhost:8000/api/v1/readyz 應回傳 `{"status":"ok","database":"ok","schema_version":"0002"}`，
API 文件在 http://localhost:8000/api/docs 。

### 方式 B：直接執行

```bash
cd backend
python3 -m venv .venv && . .venv/bin/activate
pip install -e ".[dev]"
cp .env.example .env          # 依需要修改連線字串
alembic upgrade head          # 建立/升級資料庫
python -m app.cli create-admin --email 你的Gmail --name "Owner" --google-only   # 第一位管理員（Google 登入）
uvicorn app.main:app --reload
```

網頁（另一個終端機）：

```bash
cd frontend
npm install
npm run dev        # http://localhost:5173
```

### 執行測試

測試使用**真實 PostgreSQL**（`ERP_TEST_DATABASE_URL`，預設 `erp_test`），每次執行會清空並重新 migrate 該資料庫。

```bash
cd backend
pytest -q
```

## 資料庫 Migration 規則

- 所有 schema 變更必須透過 Alembic：`alembic revision --autogenerate -m "..."`，人工審查後提交。
- `alembic check` 必須回報 *No new upgrade operations detected*（測試中已自動檢查）。
- 正式環境的 migration 在部署流程中以獨立步驟執行，**禁止**在正式環境執行 `alembic downgrade`。

## 資料庫層保護規則（Phase 1）

以下規則寫在 PostgreSQL trigger / constraint 中，任何程式或手動 SQL 都無法繞過：

| 規則 | 錯誤代碼 |
|---|---|
| 庫存異動、財務分錄、成本分攤、批次、稽核紀錄只能新增，不能修改/刪除 | `ERP_APPEND_ONLY` |
| 庫存餘額只能由庫存異動自動產生，不能直接改數字 | `ERP_BALANCE_DIRECT_WRITE` |
| 任何倉庫庫存不能變負數（超賣會整筆拒絕） | `ck_inventory_balances_qty_nonneg` |
| 同一事件（冪等鍵）不能扣兩次庫存或記兩次收入 | `uq_*_idempotency_key` |
| 只能從美國倉銷售；運送中、日本倉不能直接銷售 | `ERP_MOVEMENT_DIRECTION` |
| 組合商品本身沒有庫存，扣的是組成 SKU；組合不能巢狀 | `ERP_BUNDLE_NO_STOCK` / `ERP_NESTED_BUNDLE` |
| 已確認的採購批次成本不能被覆蓋 | `ERP_BATCH_LOCKED` |
| 費用必須是負數、收入必須是正數（防止把費用當收入） | `ck_financial_entries_sign_by_type` |
| 已關帳月份不能新增分錄 | `ERP_PERIOD_CLOSED` |
| 採購單必須先核准才能下單 | `ck_purchase_orders_approved_before_ordering` |
