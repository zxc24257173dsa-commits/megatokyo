# Whatnot Global ERP V2 — 系統架構與資料庫設計（Phase 0 / 待確認）

> 狀態：**已確認（2026-11-09）**。業主的決定以 [01-decisions.md](01-decisions.md) 為準，與本文件不同處（例如基準幣別改為 USD）以該文件為準。
> 日期：2026-11-09
> 範圍：架構、資料模型、核心演算法、Whatnot API 可用性與風險。本文件不含程式碼。

---

## 0. 設計原則（所有 Phase 都必須遵守）

| # | 原則 | 實作方式 |
|---|------|----------|
| P1 | 庫存是「帳本」，不是「欄位」 | 所有庫存只能透過 `inventory_movements`（只增不改）異動；`inventory_balances` 只是同一交易內維護的快取，每日對帳 |
| P2 | 金額只增不改 | `financial_entries` 只能新增；更正一律用「沖銷分錄（reversal）+ 新分錄」 |
| P3 | 冪等（Idempotency） | 每筆庫存異動、財務分錄、Webhook 事件都有 `UNIQUE` 冪等鍵；同一事件處理 N 次結果相同 |
| P4 | 資料來源可追溯 | 每筆收入／費用／成本都記錄 `source_type`（api / csv / manual）、`source_ref`、`import_job_id` |
| P5 | 不估算平台費用 | 費用只來自 Whatnot API、Whatnot 匯出報表或人工輸入；缺資料時標示「費用未結算」，不套假費率 |
| P6 | 運送中 ≠ 可銷售 | 「運送中」是獨立 location；只有美國簽收入庫後才變成美國庫存 |
| P7 | 歷史成本不可覆蓋 | 成本以採購批次（lot）保存；已售出的成本分攤結果鎖定；遲到的費用用「成本調整分錄」處理 |
| P8 | 人工確認才採購 | 補貨系統只產生「建議」；採購單需有權限者核准，系統不會自動對外下單 |
| P9 | 金額不用浮點數 | DB 一律 `NUMERIC(18,4)`（匯率 `NUMERIC(18,8)`）；Python 用 `Decimal`；數量用整數 |
| P10 | 時間一律 UTC 儲存 | 顯示時依使用者時區（東京／台北／美國）；報表週期使用一個固定的「營運時區」 |

---

## 1. 系統架構

```
           日本 / 美國 / 台灣 使用者（手機、平板、電腦）
                         │ HTTPS
                ┌────────▼─────────┐
                │  React + TS SPA  │  (Vite, TanStack Query, Recharts, i18n: 繁中/日/英)
                └────────┬─────────┘
                         │ REST /api/v1  (JWT + RBAC)
┌────────────────────────▼──────────────────────────────┐
│                FastAPI 應用服務 (無狀態，可水平擴充)      │
│  auth │ products │ inventory │ imports │ orders │ shows │
│  finance │ logistics │ replenishment │ analytics │ audit │
│  /webhooks/whatnot  (驗證 HMAC → 寫入 webhook_events → 立即回 200)
└───────┬─────────────────────────┬─────────────────────┘
        │ SQLAlchemy 2.0 (交易)    │ enqueue
┌───────▼────────┐        ┌───────▼────────┐      ┌────────────────────┐
│ PostgreSQL 16  │◄───────│ Worker (Celery)│─────►│ Whatnot Seller API │
│ (單一中央資料庫) │        │ + Beat 排程     │      │  GraphQL (Preview) │
└───────┬────────┘        └───────┬────────┘      └────────────────────┘
        │                         │
   PITR 備份 / 每日快照       Redis (佇列) ；物件儲存 (上傳檔案、商品照片)
                               Secret Manager (API Token 加密金鑰)
```

### 1.1 技術選型

| 層 | 選擇 | 理由 |
|----|------|------|
| Backend | Python 3.12, FastAPI, Pydantic v2, SQLAlchemy 2.0 | 依需求；型別完整，非同步 I/O 適合 API 串接 |
| Migration | Alembic | 每個 Phase 的 schema 變更都有版本、可回滾 |
| DB | PostgreSQL 16 | 交易、row lock、CHECK/UNIQUE 約束、JSONB 存原始資料 |
| 背景工作 | Celery + Redis（Beat 排程） | Webhook 處理、輪詢對帳、匯入解析、每日預測 |
| 試算表解析 | openpyxl / pandas（僅解析，不直接寫庫） | 支援 CSV、XLSX、多工作表 |
| Frontend | React 18 + TypeScript + Vite + TanStack Query + React Router + Recharts | 依需求；RWD 手機優先 |
| 驗證 | Argon2id 密碼雜湊、JWT Access（15 分）+ Refresh Token 輪替、管理員強制 TOTP 雙因素 | |
| 部署（建議） | GCP 東京區 `asia-northeast1`：Cloud Run + Cloud SQL (PostgreSQL, PITR) + Memorystore Redis + Cloud Storage + Secret Manager | 日本／台灣延遲最低，美國約 100–150ms 可接受；全託管、備份內建 |
| 本機開發 | Docker Compose（postgres, redis, api, worker, web） | |
| 觀測 | 結構化 JSON log + request_id、Sentry（錯誤）、健康檢查 `/healthz` | |

### 1.2 專案目錄（Phase 1 建立）

```
whatnot-erp/
├── backend/
│   ├── app/
│   │   ├── core/          # 設定、安全、日誌、錯誤處理、時區/幣別工具
│   │   ├── db/            # session、base、交易工具、鎖
│   │   ├── models/        # SQLAlchemy ORM
│   │   ├── schemas/       # Pydantic
│   │   ├── api/v1/        # 路由
│   │   ├── services/      # 業務邏輯（inventory_ledger, costing, finance ...）
│   │   ├── importers/     # CSV/XLSX 解析、欄位對應、驗證
│   │   ├── integrations/whatnot/  # GraphQL client、webhook 驗證、mapper
│   │   ├── jobs/          # Celery tasks
│   │   └── analytics/     # 趨勢、損益、補貨計算
│   ├── alembic/
│   └── tests/             # pytest：unit / integration（真實 PostgreSQL）
├── frontend/src/{api,features,pages,components,i18n}
├── infra/ (docker-compose.yml, Dockerfiles, deploy/)
└── docs/
```

---

## 2. 核心業務設計決策

### 2.1 庫存位置模型（Location）

| code | 類型 | 說明 |
|------|------|------|
| `JP_MAIN` | 實體 | 日本倉庫 |
| `US_MAIN` | 實體 | 美國倉庫（直播出貨倉） |
| `IN_TRANSIT` | 實體（虛擬位置但計入資產） | 日本已出貨、美國未簽收；每筆異動連結 `shipment_id` |
| `SUPPLIER` | 虛擬 | 採購入庫的來源 |
| `CUSTOMER` | 虛擬 | 銷售出庫的去處 |
| `ADJUSTMENT` / `LOSS` | 虛擬 | 盤點差異、遺失、損壞 |

每筆異動都是 `from_location → to_location`（雙邊記帳），因此任何時間點都能重建任一倉庫的庫存。

**各數量定義（系統唯一標準）：**

```
日本庫存          = balance(JP_MAIN)
運送中            = balance(IN_TRANSIT)          （不得計入美國可銷售）
美國實體庫存      = balance(US_MAIN)
已預留            = SUM(reservations.status='reserved' AND location=US_MAIN)
美國可銷售庫存    = 美國實體庫存 − 已預留
```

### 2.2 成本制度：**FIFO（先進先出）+ 批次（Lot）追蹤** — 建議採用

選擇 FIFO 而非加權平均的理由：
1. 每次採購的**匯率、日本運費、國際運費、關稅**都不同，FIFO 能讓每筆銷售精準對應到「哪一批、哪個匯率」的成本，滿足「每筆銷售可追溯成本來源」。
2. 加權平均在「遲到費用」或「批次錯誤更正」時會重算平均，容易動到已售出商品的成本；FIFO 的已售分攤可以鎖定。
3. 商品會跨倉移動（日→運送中→美），lot 跟著實體移動，美國售出時從**美國倉內最早的 lot** 扣成本，物理上也合理。
4. 若日後需要加權平均報表（例如稅務），可由 lot 資料推導，反之不行。

**落地成本（Landed Cost）計算（基準幣別）：**
```
單位落地成本 = (採購單價 × 數量 × 匯率
              + 日本境內運費 + 分攤國際運費 + 分攤關稅 + 其他費用) ÷ 數量
```
- 國際運費／關稅通常按「每週出貨」結帳，透過 `landed_cost_allocations` 依件數或價值分攤到該 shipment 內的 lot（分攤方式需確認）。
- **遲到的費用**（例如關稅帳單晚兩週到）：
  - 尚未售出的件數 → 提高該 lot 剩餘件數的單位成本（新增成本調整紀錄，舊值保留）。
  - 已售出的件數 → 不改原銷貨成本，改記一筆「成本差異（COGS adjustment）」財務分錄，計入費用發生期間。

### 2.3 訂單 → 庫存 → 收入的狀態機（防重複扣庫存、重複認列）

```
Whatnot 訂單事件              庫存動作（冪等鍵）                       財務動作（冪等鍵）
────────────────────────────────────────────────────────────────────────────────────────
order created / paid    →  建立預留 reservation                  →  認列銷售收入 revenue
                           UNIQUE(order_line_id)                     UNIQUE(order_line, 'revenue')
order shipped/fulfilled →  預留轉出庫：US_MAIN→CUSTOMER          →  認列銷貨成本 COGS (FIFO lot)
                           key = 'ship:'+order_line_id               UNIQUE(order_line, 'cogs', lot)
cancelled（未出貨）     →  釋放預留                              →  收入沖銷 reversal
refund（已出貨）        →  不動庫存；實際退回時人工「退貨入庫」    →  退款分錄 refund（依退款 external_id）
fees（API/CSV 取得）    →  —                                     →  platform_fee / processing_fee
                                                                     UNIQUE(source, external_ref, type)
```

- **收入認列時點**（建議）：付款完成（paid）時認列；取消則沖銷。需業主確認。
- 美國銷售稅（Whatnot 為市場代收代付）**不計入收入**，獨立欄位保存。
- 同一訂單同時來自 API 與 CSV：以 `(platform, external_order_id)` 去重，來源優先序 API > CSV > manual，後到的資料只更新「缺少的欄位」並留下差異紀錄，不產生第二筆收入。
- SKU 對不到的訂單行：進入「待對應佇列」，不扣庫存、收入照記但標記，Dashboard 顯示警示。

### 2.4 併發與一致性
- 庫存異動在**單一 DB 交易**內完成：寫 movement → `SELECT … FOR UPDATE` 鎖 balance 列 → 更新 balance → 寫 audit。
- 鎖順序固定（sku_id, location_id, lot_id 排序）避免死結。
- 實體倉 balance `CHECK (qty >= 0)`：不允許負庫存；若 Whatnot 已賣出但帳上不足（超賣），該訂單行進入「異常佇列」由人工處理（盤點補登或確認超賣），不會悄悄變負數。
- `inventory_movements`、`financial_entries`、`audit_logs` 以 DB trigger 禁止 UPDATE / DELETE。
- 每晚對帳工作：`SUM(movements) == balances`，不一致即告警。

### 2.5 多幣別
- 每筆金額保存：`amount_orig`、`currency_orig`、`fx_rate`（原幣→基準幣）、`amount_base`、`fx_source`、`fx_date`。
- **基準幣別建議：JPY**（公司與採購在日本，成本多為日幣）；畫面可即時切換 USD / TWD 顯示（用報表日匯率換算，並標示匯率日期）。→ 待確認
- 匯率表 `fx_rates` 每日由背景工作抓取（來源待選定，例如央行／商業匯率 API），亦可人工輸入實際換匯匯率；**交易一旦入帳，使用的匯率即鎖定**。

### 2.6 時區與「完整期間」
- 營運報表時區建議：`America/New_York`（直播在美國，日界以美國為準），週一為週起始。→ 待確認
- 趨勢比較只比較**已結束的完整日／週**：「本週」若未結束，標示為「本週迄今」，成長率改用「本週迄今 vs 上週同期（相同天數）」計算。

---

## 3. 資料庫設計（PostgreSQL）

> 主鍵統一 `BIGINT GENERATED ALWAYS AS IDENTITY`（對外顯示用編號另設）。
> 所有表含 `created_at TIMESTAMPTZ DEFAULT now()`；可修改的主檔另有 `updated_at`、`version`（樂觀鎖）。
> 以下標示 ★ 為關鍵約束。

### 3.1 身分、權限、稽核

| 資料表 | 主要欄位 | 約束 / 索引 |
|--------|----------|-------------|
| `users` | email, password_hash, display_name, locale, timezone, is_active, mfa_secret_enc, last_login_at | ★UNIQUE(lower(email)) |
| `roles` | code, name | UNIQUE(code) |
| `permissions` | code（例：`inventory.adjust`, `purchase.approve`, `finance.view`） | UNIQUE(code) |
| `role_permissions` | role_id, permission_id | PK(role_id, permission_id) |
| `user_roles` | user_id, role_id | PK |
| `refresh_tokens` | user_id, token_hash, expires_at, revoked_at, replaced_by_id, ip, user_agent | INDEX(user_id) |
| `audit_logs` | user_id, action, entity_type, entity_id, before JSONB, after JSONB, ip, request_id, created_at | 只增不改；INDEX(entity_type, entity_id), INDEX(created_at) |

**預設角色與權限矩陣：**

| 功能 | 管理員 | 日本庫存 | 美國直播營運 | 台灣管理 | 唯讀 |
|------|:---:|:---:|:---:|:---:|:---:|
| 使用者/權限管理 | ✔ | | | | |
| 商品主檔編輯 | ✔ | ✔ | | ✔ | |
| 日本入庫／盤點 | ✔ | ✔ | | | |
| 建立/出貨 日→美 shipment | ✔ | ✔ | | | |
| 美國簽收入庫／盤點 | ✔ | | ✔ | | |
| 成本匯入（確認寫入） | ✔ | ✔ | | ✔ | |
| 訂單／直播資料匯入 | ✔ | | ✔ | ✔ | |
| 財務／損益檢視 | ✔ | | 限直播報表（可設定） | ✔ | ✔(可設定) |
| 費用輸入 | ✔ | | ✔ | ✔ | |
| 補貨建議修改／核准採購 | ✔(核准) | ✔(修改) | | ✔(核准) | |
| Whatnot 串接設定、Token | ✔ | | | | |

### 3.2 商品主檔

| 資料表 | 主要欄位 | 約束 / 索引 |
|--------|----------|-------------|
| `categories` | name, parent_id | |
| `products` | name, category_id, brand, description, status | INDEX(category_id) |
| `skus` | product_id, ★sku_code, variant_name, attributes JSONB（規格：尺寸/顏色/版本…）, barcode, list_price, list_currency, default_purchase_currency, safety_stock_override, status(active/discontinued) | ★UNIQUE(sku_code)；pg_trgm 索引於名稱/sku 供搜尋 |
| `sku_images` | sku_id, storage_key, sort_order, is_primary | |
| `sku_bundle_components` | bundle_sku_id, component_sku_id, qty | 支援福袋/組合商品（待確認是否需要） |
| `external_sku_mappings` | platform, external_product_id, external_variant_id, external_sku, sku_id | ★UNIQUE(platform, external_variant_id) |
| `suppliers` | name, country, currency, default_lead_time_days, contact | |

> 「最近進貨日期」「銷售歷史」「各倉庫存」不存在 skus 表中，由 lots / movements / 銷售彙總查詢產生，避免資料不同步。

### 3.3 庫存帳本

| 資料表 | 主要欄位 | 約束 / 索引 |
|--------|----------|-------------|
| `locations` | code, name, kind(physical/virtual), country, timezone | UNIQUE(code) |
| `inventory_lots` | sku_id, purchase_line_id, lot_no, received_at（FIFO 排序依據）, unit_cost_base（目前有效）, base_currency | INDEX(sku_id, received_at) |
| `lot_cost_history` | lot_id, unit_cost_base, effective_at, reason, ref | 成本變動完整歷史 |
| ★`inventory_movements` | sku_id, lot_id, from_location_id, to_location_id, qty(>0), movement_type（purchase_receipt / transfer_out / transfer_receive / sale / return / adjustment / loss）, ref_type, ref_id, ★idempotency_key, occurred_at, created_by, note | ★UNIQUE(idempotency_key)；CHECK(qty>0)；CHECK(from≠to)；INDEX(sku_id, occurred_at)；禁止 UPDATE/DELETE |
| `inventory_balances` | sku_id, location_id, lot_id, qty, updated_at | PK(sku_id, location_id, lot_id)；實體倉 CHECK(qty>=0) |
| `inventory_reservations` | sku_id, location_id, order_line_id, qty, status(reserved/consumed/released), released_reason | ★UNIQUE(order_line_id)；INDEX(sku_id, status) |
| `stock_counts` / `stock_count_lines` | location_id, status, counted_at；sku_id, expected_qty, counted_qty | 盤點差異經核准才產生 adjustment movement |
| `v_inventory_position`（VIEW） | sku × {jp, in_transit, us_on_hand, reserved, us_available, inventory_value_by_location} | 給畫面與補貨使用 |

### 3.4 採購與成本

| 資料表 | 主要欄位 | 約束 / 索引 |
|--------|----------|-------------|
| `purchase_batches` | batch_no, supplier_id, purchase_date, status(draft/confirmed/locked), import_job_id, note | ★UNIQUE(batch_no) |
| `purchase_lines` | batch_id, sku_id, qty, unit_price_orig, currency_orig, fx_rate, fx_date, domestic_shipping, other_cost, (intl_shipping, duty 可於此或於 shipment 分攤), landed_unit_cost_base, source_row_ref | confirmed 後不可修改（只能沖銷重建） |
| `landed_cost_charges` | charge_type(intl_freight/duty/other), amount_orig, currency, fx_rate, amount_base, shipment_id / batch_id, allocation_method(qty/value), invoice_ref, source | |
| `landed_cost_allocations` | charge_id, lot_id, amount_base, qty_unsold_at_alloc, qty_sold_at_alloc | ★UNIQUE(charge_id, lot_id) |
| `purchase_orders` / `purchase_order_lines` | supplier_id, status(draft/approved/ordered/received/cancelled), approved_by, approved_at, replenishment_run_id；sku_id, qty, expected_unit_cost | 系統只建立 draft，需核准 |

### 3.5 匯入（CSV / XLSX 多工作表）

| 資料表 | 主要欄位 | 約束 / 索引 |
|--------|----------|-------------|
| `import_jobs` | import_type(purchase_cost / whatnot_orders / whatnot_show / whatnot_payout / expenses), filename, ★file_sha256, storage_key, sheets JSONB, column_mapping JSONB, status(uploaded→parsed→validated→committed / failed / cancelled), stats JSONB, created_by, committed_by, committed_at | UNIQUE(import_type, file_sha256) WHERE status='committed'（防同檔重複匯入） |
| `import_rows` | job_id, sheet_name, row_number, raw JSONB, normalized JSONB, matched_sku_id, proposed_action(create_sku/new_batch_line/skip/duplicate), errors JSONB, warnings JSONB, user_decision | INDEX(job_id) |
| `import_mapping_templates` | name, import_type, header_signature, mapping JSONB | 記住你的試算表欄位，下次自動對應 |

**匯入流程（兩段式，確認前不寫入正式表）：**
1. 上傳 → 存檔（物件儲存）→ 計算 SHA-256（同檔已匯入則阻擋）。
2. 解析所有工作表 → 自動偵測表頭列 → 依「同義詞字典」（SKU/品番/商品コード/貨號…）與既有模板建議欄位對應。
3. 正規化：去空白、全形→半形、數字千分位、日期多格式、幣別代碼。
4. 驗證：必填、數量>0、單價≥0、匯率合理範圍、同檔重複 SKU+日期、與既有批次疑似重複、落地成本與各項加總不一致、未知 SKU。
5. 預覽畫面：每列顯示 ✔/⚠/✖ 與原因；未知 SKU 可一鍵「建立新商品」或「對應到既有 SKU」。
6. 使用者確認 → **單一交易**寫入 purchase_batches / lines / lots（＋入庫 movement 若勾選「已入日本倉」）→ job 狀態 committed。任何一列失敗整批回滾。

### 3.6 Whatnot 整合

| 資料表 | 主要欄位 | 約束 / 索引 |
|--------|----------|-------------|
| `integration_accounts` | platform, seller_id, ★access_token_enc, ★refresh_token_enc, scopes, expires_at, webhook_secret_enc, status, environment(staging/production) | Token 以 Secret Manager 金鑰做 envelope encryption（AES-GCM），永不回傳前端 |
| `webhook_events` | platform, topic, seller_id, ★dedupe_key（事件 ID；無則 sha256(body)）, headers JSONB, payload JSONB, signature_valid, received_at, status(pending/processed/failed/ignored), attempts, last_error, processed_at | ★UNIQUE(platform, dedupe_key)；INDEX(status, received_at) |
| `sync_runs` | kind(orders/listings/livestreams), cursor, window_start, window_end, status, stats JSONB, error | |
| `external_raw_records` | platform, entity_type, external_id, payload_hash, payload JSONB, fetched_at | UNIQUE(platform, entity_type, external_id, payload_hash)；完整保留原始資料版本 |

### 3.7 直播與銷售

| 資料表 | 主要欄位 | 約束 / 索引 |
|--------|----------|-------------|
| `livestreams` | platform, ★external_id, title, scheduled_at, started_at, ended_at, status, host_user_id, source | UNIQUE(platform, external_id) |
| `livestream_metrics` | livestream_id, metric_code（viewers, peak_viewers, show_gmv_reported…）, value, source, import_job_id | 只存來源有提供的指標 |
| `sales_orders` | platform, ★external_order_id, livestream_id, buyer_ref_hash, status, ordered_at, paid_at, currency, item_subtotal, shipping_charged, sales_tax_collected, total, source, external_updated_at | ★UNIQUE(platform, external_order_id)；INDEX(livestream_id)、INDEX(ordered_at) |
| `sales_order_lines` | order_id, ★external_line_id, sku_id(可空=待對應), qty, unit_price, line_amount, discount, status, fulfillment_status, shipped_at | ★UNIQUE(order_id, external_line_id) |
| `order_status_history` | order_id, from_status, to_status, source_event_id, occurred_at | |
| `refunds` | order_id, order_line_id, ★external_refund_id, amount_orig, currency, reason, refunded_at, restocked, source | UNIQUE(platform, external_refund_id) |
| `cogs_allocations` | order_line_id, lot_id, movement_id, qty, unit_cost_base, total_cost_base | ★UNIQUE(order_line_id, lot_id, movement_id)；分攤後鎖定 |

> 不儲存買家姓名／地址（不申請 `read_customers` 權限），只存不可逆雜湊作為回購分析用 → 降低個資風險。若美國端需要用 ERP 列印出貨標籤，再另行評估。

### 3.8 財務

| 資料表 | 主要欄位 | 約束 / 索引 |
|--------|----------|-------------|
| ★`financial_entries` | entry_date, entry_type(revenue / revenue_reversal / cogs / cogs_adjustment / platform_fee / payment_processing_fee / shipping_cost / refund / operating_expense / other), amount_orig, currency_orig, fx_rate, amount_base, sales_order_id, order_line_id, livestream_id, shipment_id, sku_id, ★source_type, ★source_ref, import_job_id, ★idempotency_key, reversal_of_id, created_by | ★UNIQUE(idempotency_key)；INDEX(livestream_id)、INDEX(entry_date, entry_type)、INDEX(sku_id)；禁止 UPDATE/DELETE |
| `payouts` | platform, external_payout_id, amount, currency, period_start, period_end, paid_at, source | UNIQUE(platform, external_payout_id) |
| `operating_expenses` | expense_date, category, amount_orig, currency, fx_rate, livestream_id(可空), description, receipt_storage_key, created_by | 寫入時同步產生 financial_entry |
| `fx_rates` | rate_date, base_ccy, quote_ccy, rate, source | ★UNIQUE(rate_date, base_ccy, quote_ccy, source) |
| `accounting_periods` | period(YYYY-MM), status(open/closed), closed_by, closed_at | 已關帳期間禁止新增分錄（更正入下一期） |

**直播損益公式（全部由 financial_entries 彙總，可逐筆追溯）：**
```
商品銷售收入   = Σ revenue − Σ revenue_reversal              （不含運費收入與銷售稅）
商品銷售成本   = Σ cogs + Σ cogs_adjustment
商品毛利       = 商品銷售收入 − 商品銷售成本
退款           = Σ refund（單獨列示；屬收入減項）
直播淨利       = 商品毛利 − 退款 − 平台佣金 − 支付手續費 − 物流費 − 其他營運費用
毛利率         = 商品毛利 ÷ 商品銷售收入
淨利率         = 直播淨利 ÷ 商品銷售收入
實際結算金額   = Whatnot payout（另表對照；不等於淨利）
```
每個數字都顯示「資料完整度」：例如「平台費用：已取得 38/40 筆訂單」；未完整前標示「暫定」。

### 3.9 跨國物流

| 資料表 | 主要欄位 | 約束 / 索引 |
|--------|----------|-------------|
| `carriers` | name, tracking_url_template | |
| `shipments` | ★shipment_no, from_location(JP_MAIN), to_location(US_MAIN), carrier_id, tracking_no, status(draft→packed→shipped→delivered→received→closed / cancelled), planned_ship_date, shipped_at, eta, delivered_at, received_at, replenishment_run_id | UNIQUE(shipment_no)；INDEX(status, eta) |
| `shipment_lines` | shipment_id, sku_id, qty_planned, qty_shipped, qty_received, jp_on_hand_snapshot, discrepancy_reason | |
| `shipment_events` | shipment_id, event, occurred_at, user_id, data | 每個步驟的紀錄 |

**狀態轉換與庫存：**
- `shipped`：JP_MAIN → IN_TRANSIT（依 FIFO 從日本取 lot），key=`ship:{shipment_line}:{lot}`
- `received`：IN_TRANSIT → US_MAIN（實收數量）；短少部分 IN_TRANSIT → LOSS（需填原因與核准）
- 支援部分簽收（多次 received 事件，每次有自己的冪等鍵）

### 3.10 補貨預測

| 資料表 | 主要欄位 |
|--------|----------|
| `replenishment_settings` | lead_time_days(9), review_period_days(7), ship_weekday, velocity_weights(7d/14d/30d), service_level_z, default_safety_days |
| `sku_replenishment_params` | sku_id, safety_stock_override, min_ship_qty, case_pack, supplier_lead_time_days, exclude_flag |
| `livestream_schedule` | planned_at, expected_scale（規劃中的直播場次，用於需求調整） |
| `replenishment_runs` | run_at, data_as_of, params_snapshot JSONB, status |
| `replenishment_suggestions` | run_id, sku_id, v7, v14, v30, v_blended, trend_factor, horizon_days, forecast_demand, safety_stock, committed_demand, us_available, in_transit_in_time, in_transit_late, jp_available, days_to_stockout, stockout_date, latest_ship_date, suggested_ship_qty, suggested_purchase_qty, status_color, override_ship_qty, override_purchase_qty, override_reason, overridden_by, confirmed_at, confirmed_by | UNIQUE(run_id, sku_id) |

### 3.11 分析彙總（效能用，可重建）
- `daily_sku_sales`：(biz_date, sku_id, livestream_count, qty_sold, revenue_base, cogs_base, refund_base) — 由財務分錄與訂單每晚／即時增量重建，PK(biz_date, sku_id)。
- 趨勢、排行、Dashboard 皆查此表，原始帳本為唯一真實來源。

---

## 4. 補貨演算法（第一版：透明的統計模型）

> 「AI」第一版刻意採用**可解釋**的統計預測：每個建議數字都能展開看計算過程。累積 3–6 個月真實資料後，再評估加入機器學習模型（例如考慮直播場次、季節性），並用回測證明比現有方法準確才上線。

**輸入（只用完整日資料）：**
```
v7, v14, v30   = 最近 7/14/30 個完整營運日的日均淨銷量（扣除取消）
v_blended      = 0.5·v7 + 0.3·v14 + 0.2·v30（權重可調）
直播調整       = 若未來期間規劃的直播場次 ≠ 過去平均場次，按 (規劃場次/歷史場次) 比例調整
L              = 9 天（日本→美國前置時間）
R              = 7 天（每週寄一次的檢視週期）
H = L + R      = 16 天（本次出貨要撐到下一批到貨）
安全庫存 SS    = 每 SKU 可覆寫；預設 = z·σ_daily·√(L) 或 N 天銷量（擇一，待確認）
```

**計算：**
```
預估需求 D_H              = v_blended × 直播調整 × H
可及時到貨運送中 T_in     = Σ 運送中數量 WHERE eta ≤ today + L
                           （晚於 L 才到的不計入；且 ETA 已逾期未簽收者標示異常）
預計缺貨天數              = (美國可銷售 + 依 ETA 逐日加入的在途) 逐日模擬扣除日需求，第一個 <0 的日子
建議 日→美 出貨量         = MAX(0, D_H + SS + 已承諾需求 − 美國可銷售 − T_in)
                           → 依 case_pack 進位、上限 = 日本可用庫存
建議 向供應商採購量       = MAX(0, 建議出貨量 − 日本可用庫存)
                           + 下一週期（H + 供應商前置天數）的日本端需求缺口
最晚出貨日                = 預計缺貨日 − L
```

> ⚠ **公式釐清**：需求書中的公式同時「加上已承諾需求」又「減去美國可用庫存」。由於「美國可銷售 = 實體 − 已預留」已經扣掉已預留訂單，若「已承諾需求」也包含這些預留，就會**重複計算**。本設計定義：
> - 已預留（已成立訂單）→ 只在「美國可銷售」中扣除一次；
> - 已承諾需求 → 只代表**尚未成為訂單**的承諾（例如已安排的直播專場、預購），兩者不重疊。

**顏色標示：**
| 顏色 | 條件 |
|------|------|
| 🔴 紅 | 預計缺貨天數 ≤ L（9 天）：就算今天寄出也來不及，需考慮緊急空運或下架/限量 |
| 🟠 橘 | L < 缺貨天數 ≤ L + R（16 天）：**本週出貨必須包含** |
| 🟡 黃 | L + R < 缺貨天數 ≤ L + 2R（23 天），或銷量變動大（變異係數高）、資料不足 14 天 |
| 🟢 綠 | 其餘 |

所有建議可人工修改（需填原因），確認後才能轉成 shipment 草稿或採購單草稿。

---

## 5. Whatnot Seller API 可用性評估

> 調查依據：Whatnot 官方開發者網站 developers.whatnot.com（搜尋結果摘要）與第三方 Hookdeck 整合指南。**本次雲端環境無法直接開啟官方文件頁面**，以下狀態在 Phase 5 開始前必須以官方文件與實際帳號權限逐項驗證。

| 需求功能 | 目前判斷 | 依據 | 備援方案 |
|----------|----------|------|----------|
| API 本身 | 🟡 **Developer Preview（開發預覽）**，GraphQL，可能變動 | 官方文件說明 | 所有串接包在 adapter 層；schema 變動只改 mapper |
| 取得權限 | 🟡 需向 Whatnot 申請 App；Webhook 訂閱需寫信給 Whatnot 提供 App ID、接收 URL、topics，由 Whatnot 提供 secret | 第三方指南 | 未核准前完全以 CSV 運作 |
| 訂單同步 | 🟢 有 `order/created`、`order/updated` Webhook，API 可管理訂單 | 官方 webhook 列表 | Seller Hub 訂單匯出 CSV |
| 訂單商品明細 / SKU | 🟡 有 Product / ProductVariant / Listing 物件；訂單行是否帶 SKU、帶哪個 ID 需驗證 | 官方 inventory 文件 | 以 listing/variant ID 建立 `external_sku_mappings`；CSV 依標題比對＋人工對應 |
| 單場直播資料 | 🟢/🟡 有 `livestream/created`、`livestream/stopped`、`livestream/product_added`、`livestream/product_removed` Webhook；訂單是否帶 livestream ID 需驗證 | 官方 webhook 列表 | 以直播起訖時間歸屬訂單 + CSV |
| Show Analytics（觀看數等） | 🔴 **未發現**公開 API | — | CSV / 手動輸入 |
| 銷售報表 / 平台佣金 / 支付手續費 / Payout | 🔴 **未確認** API 提供 | — | Seller Hub 報表／Payout 匯出 CSV（**這是損益正確性的關鍵來源**） |
| 退款與取消 | 🟡 預期反映在 `order/updated` 狀態；退款金額明細需驗證 | 推測 | CSV |
| 庫存同步 | 🟡 Listing / Product 有建立、更新事件（`listing/created`、`listing/updated`、`product/sold`）；更新數量的 mutation 需驗證 | 官方 webhook 列表 | 第一版**只讀不寫**：ERP 不自動改 Whatnot 數量，僅顯示差異，避免雙向同步造成超賣 |
| 出貨標籤 | 🟡 `shipment/label_created`（Beta） | 官方 webhook 列表 | 非必要 |
| 買家地址 | 需 `read_customers` scope，否則 shipping_address、tracking_info 為 null | 官方 order 文件 | 本系統不需要，**不申請** |
| Webhook 安全 | 🟢 `X-Whatnot-Webhook-Signature` = 原始 body 的 HMAC-SHA256（hex）；`X-Whatnot-Seller-Id` 標示賣家 | 第三方指南（需官方確認） | — |

**整合架構重點：**
1. Webhook 只做：驗簽 → 存 `webhook_events`（去重）→ 回 200 → 交給背景工作。
2. Webhook 可能漏送、亂序、重送 → 每 15 分鐘以 API **輪詢對帳**（依 updated_at 視窗），以 API 回傳的最新狀態為準，不依賴事件順序。
3. 原始資料一律保存在 `external_raw_records`，任何數字都能回溯到原始 payload。
4. 開發／測試使用 Whatnot staging 環境與真實 schema 的**錄製回應（標示為 fixture）**，fixture 只存在測試中，絕不進正式資料庫或畫面。

---

## 6. 風險清單

| # | 風險 | 影響 | 對策 |
|---|------|------|------|
| R1 | Whatnot API 為預覽版，欄位可能變動或下架 | 同步中斷 | adapter 隔離、schema 版本檢查、失敗告警、CSV 永久備援 |
| R2 | API 權限申請未核准或時程不確定 | Phase 5 延後 | Phase 4–8 全部可以 CSV 運作；API 為加分項 |
| R3 | 平台費／手續費無 API | 損益不完整 | 匯入 Payout/Sales 報表；畫面顯示資料完整度，不估算 |
| R4 | 拍賣、福袋、盲盒、拆卡（break）等商品無固定 SKU | 庫存無法扣、成本無法對應 | 待對應佇列、組合商品（BOM）、「通用 SKU」規則；需你提供實際商品型態 |
| R5 | 同一訂單同時從 API 與 CSV 進來 | 重複收入 | 外部訂單 ID 唯一鍵 + 來源優先序 + 冪等分錄 |
| R6 | 超賣（Whatnot 已售但美國帳上沒貨） | 負庫存 | 禁止負數、異常佇列、建議盤點 |
| R7 | 你的成本試算表格式不一、合併儲存格、公式錯誤 | 匯入錯誤 | 預覽驗證、模板記憶、整批交易回滾；需要你提供 1–2 份真實檔案 |
| R8 | 國際運費、關稅晚到 | 成本失真 | landed cost 分攤 + 成本差異分錄（不改歷史） |
| R9 | 匯率波動、換匯日不同 | 利潤失真 | 每筆鎖定匯率；可輸入實際換匯匯率 |
| R10 | 三地時區造成「今天」「本週」定義不一 | 報表不一致 | 單一營運時區（待確認），畫面註明 |
| R11 | Token 外洩 | 帳號被濫用 | 加密儲存、最小權限 scope、只有管理員可設定、操作稽核 |
| R12 | 資料量增加 | 報表變慢 | 彙總表、索引、必要時資料表分區（movements、entries 依年分區） |
| R13 | ERP 非正式會計／報稅系統 | 稅務誤用 | 提供匯出給會計師；日本消費稅出口退稅、美國銷售稅不在第一版範圍 |
| R14 | 初期銷售資料少 | 補貨預測不穩 | 資料不足 14 天標黃、允許人工設定預估日銷量 |

---

## 7. 開發階段對應（不變，僅補充驗收標準）

| Phase | 內容 | 驗收重點 |
|-------|------|----------|
| 1 | 專案骨架、Docker、DB、Alembic 初始 migration（本文件 §3 全部表）、日誌、錯誤處理 | `alembic upgrade head` 成功；約束/trigger 測試通過 |
| 2 | 登入、Refresh、TOTP、RBAC、稽核 | 權限矩陣自動化測試 |
| 3 | 商品、SKU、多倉庫帳本、預留、盤點 | 併發扣庫存測試、不可負庫存、冪等測試、帳本對帳 |
| 4 | CSV/XLSX 成本匯入、FIFO lot | 多工作表、重複檔、錯誤列、整批回滾測試 |
| 5 | Whatnot API + Webhook + CSV 訂單匯入 | 重送/亂序/重複事件測試；不重複扣庫存、不重複收入 |
| 6 | 單場直播報表 | |
| 7 | 直播損益、COGS 追溯 | 每個數字可展開至分錄與原始資料 |
| 8 | 銷售趨勢（完整期間） | 未完成週不誤判 |
| 9 | 日→美物流 | 部分簽收、短少、在途不計入可售 |
| 10 | 補貨建議 | 以歷史資料回測、公式逐項可見 |
| 11 | Dashboard、RWD、多語系 | 手機實測 |
| 12 | E2E 測試、雲端部署、備份還原演練 | 實際還原一次備份 |
