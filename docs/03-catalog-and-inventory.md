# Phase 3：商品與多倉庫庫存

## 功能

| 功能 | 說明 |
|---|---|
| 商品 / SKU | 一個商品可有多個規格（SKU）。SKU 編號不分大小寫唯一，建立後不可修改；不刪除，改為「已停售」 |
| 組合商品 | 由多個 SKU 組成（例：Box ×1 + 單包 ×5），本身沒有庫存；可銷售數 = 組成 SKU 能湊出的完整組數；不可巢狀 |
| 商品照片 | JPEG / PNG / WebP，5 MB 以內，以檔案內容判斷格式（不信任副檔名）；透過 1 小時有效的簽章網址顯示 |
| 期初庫存 | 開始使用時登記現有庫存，每行建立一個成本批次（USD 單位成本） |
| 調整 | 增加（併入最新批次成本）、減少與損壞報廢（先進先出），都需要原因 |
| 盤點 | 建立 → 輸入實盤數 → 送出 → 由有核准權限者核准 → 系統依差異自動調整；核准時重新讀取系統數量 |
| 庫存總覽 | 日本倉、運送中、美國倉、已預留、可銷售、庫存成本、最近入庫 |
| 批次 | 每個 SKU 的 FIFO 批次、各倉剩餘數量、單位成本 |
| 異動紀錄 | 每筆異動：時間、類型、從哪到哪、數量、批次、操作人、原因 |

## 權限

| 動作 | 需要的權限 |
|---|---|
| 查看商品 / 庫存 | `catalog.view` / `inventory.view` |
| 新增 / 修改商品、組合、照片 | `catalog.edit` |
| 日本倉入庫、調整、盤點 | `inventory.adjust_jp` |
| 美國倉入庫、調整、盤點 | `inventory.adjust_us` |
| 核准盤點 | `stock_count.approve`（管理員、台灣管理人員） |
| 看到成本 | `finance.view` 或 `import.cost`；美國直播營運與唯讀使用者看不到成本 |

運送中的庫存不能直接調整，只能透過 Phase 9 的出貨／簽收流程改變。

## 防止重複操作

所有入庫、調整的請求都帶 `Idempotency-Key`（網頁每次送出表單自動產生）。手機網路不穩、重複點擊時，同一個 Key 只會執行一次，並回傳第一次的結果。

## 資料庫變更（Migration 0004）

- `sku_images`：檔案類型、大小、上傳者；每個 SKU 只能有一張主圖（部分唯一索引）
- `stock_counts`：送出人、送出時間；`stock_count_lines.difference_qty`
- 檢視表 `v_lot_cost`：每個批次目前的單位成本 = 原始落地成本 + 之後成本調整的每單位分攤
- 檢視表 `v_inventory_valuation`：每個 SKU 在每個倉庫的數量與成本

## API

| 方法 | 路徑 |
|---|---|
| GET / POST | /api/v1/categories |
| POST | /api/v1/products、PATCH /api/v1/products/{id}、POST /api/v1/products/{id}/skus |
| GET | /api/v1/skus（search、category_id、status、include_bundles） |
| GET / PATCH | /api/v1/skus/{id}（PATCH 需帶 version） |
| POST | /api/v1/bundles、PUT /api/v1/skus/{id}/components |
| POST / DELETE | /api/v1/skus/{id}/images、POST …/images/{image_id}/primary |
| GET | /api/v1/inventory/summary、/api/v1/inventory/movements |
| POST | /api/v1/inventory/opening-stock、/api/v1/inventory/adjustments（需 Idempotency-Key） |
| GET / POST | /api/v1/inventory/stock-counts，PUT …/{id}/counts，POST …/{id}/submit、approve、cancel |

## 網頁

```bash
cd frontend && npm install && npm run build   # 產生 frontend/dist，由後端在同一網址提供
```

開發時可用 `npm run dev`（http://localhost:5173，API 自動轉到 :8000）。
