# Phase 5：Whatnot API 與訂單同步

## Whatnot Seller API（官方文件確認，2026-11-09）

- REST API：`https://api.whatnot.com/seller-api/rest/v2`，Bearer token（`wn_pat_tk_…`）
- 速率限制：每個賣家每個 App 每秒 10 次。系統自我限制在每秒 8 次，遇到 429 依 `Retry-After` 等待，並以指數退避重試
- 本系統使用的權限：`read:orders`、`read:shows`、`read:shipments`、`read:inventory`、`read:reports`、`write:reports`
- **不申請 `read:customers`**：系統不需要買家姓名、地址
- 已用業主帳號只讀驗證：最近 3 天 612 筆訂單、238 個出貨、2 場直播，0 錯誤

| 需求 | 來源 |
|---|---|
| 訂單、訂單商品 | API `/orders`、`/orders/{id}/items`，Webhook `orders.created/updated` |
| 出貨狀態 | API `/shipments`，Webhook `shipments.*` |
| 直播場次 | API `/shows/{id}`，Webhook `shows.*`；訂單帶 `show_id` |
| 直播數據 | API `/analytics/shows/{id}`（[docs/06](06-show-pnl.md)） |
| 平台佣金、手續費、退款 | 財務報表 `seller_financial_report`（[docs/06](06-show-pnl.md)） |

## 訂單如何影響庫存與帳

| Whatnot 狀態 | ERP 處理 |
|---|---|
| `pending` | 只記錄（尚未付款） |
| `pending_fulfillment`、`completed` | **已付款**：每件商品認列一次營收；預留美國庫存（組合商品預留組成 SKU） |
| 出貨 `dropped_off` / `in_transit` / `delivered`，或訂單 `completed` | **已出貨**：預留的商品依先進先出離開美國倉，認列銷貨成本 |
| `cancelled`、`failed` | 釋放預留、沖銷營收；若已出貨才取消 → 列入「需要處理」 |

- 付款時間：Whatnot 沒有提供，以下單時間（`created_at`）為準
- 銷售稅、買家付的運費：顯示但**不計入營收**
- 贈品（$0）：營收 0，但商品成本照算
- 同一筆訂單可能透過 Webhook、重送、同步、CSV 多次進來，結果永遠只有一份。舊版本晚到時會被忽略，不會倒退

### 需要處理（例外清單）

| 代碼 | 意思 | 怎麼處理 |
|---|---|---|
| `unmapped_sku` | Whatnot 商品還沒對應到 ERP 的 SKU | 訂單 › 待對應商品，對應一次 |
| `oversold` | 已付款但美國可銷售庫存不足 | 補入庫、盤點，或確認超賣 |
| `insufficient_stock` | 已出貨但帳上庫存不足，沒有扣庫存 | 補登入庫後，下次同步自動補扣 |
| `non_usd` | 非 USD 訂單 | 目前只處理 USD |
| `cancelled_after_shipping` | 出貨後被取消 | 確認退貨；退款以財務報表為準 |

## SKU 對應

Whatnot 上同一種商品每次拍賣名稱都不同（`… #1`、`… #2`）。系統依以下順序比對：

1. 已保存的對應（Whatnot 商品編號）
2. Whatnot 商品的 external ID 或 SKU，等於 ERP 的 SKU 編號
3. **名稱規則**：商品名稱去掉最後的 `#編號`

在「訂單 › 待對應商品」對應一次，之前和之後所有同名、同商品的訂單都會套用。

**選錯了可以更改**（訂單明細 › 更改 SKU，需要 `catalog.edit` 加 `inventory.adjust_us`）：

- 已預留的會釋放；已出貨的會用「退回」異動把庫存還給原 SKU，並用沖銷分錄撤回原成本，再依新的 SKU 重新扣庫存、算成本
- 營收不變，因為那筆錢是真的收到了
- 紀錄只能新增不能修改，所以所有更正都留有軌跡

## 拆盒（整盒拆開分售）

業主決定（2026-11-09）：**盒子成本平均分給盒內每一件**。

- 在 SKU 頁設定「盒內內容」，例如 PERONA ×1、補充包 ×2、卡套 ×1，共 4 件
- 按「拆盒 N 盒」：依先進先出扣掉 N 盒，盒內每個 SKU 建立新批次，每件成本 = 盒子成本 ÷ 每盒件數
- 新批次記錄來自哪一個盒子批次（`parent_lot_id`），每張卡都能追溯到原始採購批次
- 防止重複送出；需要該倉庫的調整權限

## CSV 備援：Whatnot 直播訂單匯出檔

檔名 `live-<直播編號>.csv`，以業主的真實匯出檔驗證，內容與 API 完全一致（313 筆訂單、已付款 292 筆、$2,386.00）。

| 欄位 | 用途 |
|---|---|
| `order_numeric_id` | 訂單編號，**與 API 相同**，CSV 與 API 不會重複計算 |
| `product_name`、`product_quantity`、`original_item_price` | 商品、數量、成交價 |
| `placed_at` | 下單時間（UTC） |
| `cancelled_or_failed` | 取消／付款失敗 |
| `shipment_id`、`sku` | 出貨編號、SKU（通常空白） |
| `buyer_username`、`shipping_address`、`postal_code`、`gifted_to` | **不保存**：不寫入資料庫、預覽遮蔽，匯入完成或取消後刪除原始檔 |
| `cost_per_item`、`total_cost` | 不採用，成本以採購批次的 FIFO 為準 |

確認匯入時可以勾選「這些訂單都已寄出」。先匯入 CSV、之後 API 又同步到同一筆訂單時，API 會接手那一筆，不會變成兩筆。

## Webhook

- 依 Standard Webhooks 規格驗證簽章，已用 Whatnot 官方文件的範例驗證：`wn_wh_` secret、HMAC-SHA256，時間誤差超過 5 分鐘即拒絕
- 以 `webhook-id` 去除重複；立即回覆 2xx，之後在背景處理
- 處理時一律向 API 重新讀取最新狀態，不依賴通知的先後順序
- Webhook 網址 `https://<網域>/api/v1/webhooks/whatnot` 要在部署後，到 Whatnot 開發者後台設定
- 定時同步負責補漏：最近 N 天的訂單與出貨、仍未完成的舊訂單、例外重試

## 設定

| 環境變數 | 說明 |
|---|---|
| `ERP_WHATNOT_API_TOKEN` | API 金鑰（正式環境放 Secret Manager）；也可以由管理員在「Whatnot 串接」頁加密保存 |
| `ERP_WHATNOT_WEBHOOK_SECRET` | `wn_wh_…`（同上） |
| `ERP_WHATNOT_SYNC_DAYS` | 每次同步回看的天數，預設 3 |

## 資料庫變更

- **0009**：訂單（交易類型、折扣、取消時間、同步時間）、訂單商品（Whatnot 商品／變體編號、出貨編號、庫存狀態、例外）、`sales_shipments`、`sales_discount` 分錄類型、直播狀態 `stopped`
- **0010**：`sku_box_contents`、`box_openings`、`inventory_lots.parent_lot_id`、異動類型 `box_open` / `box_content`、`sales_order_lines.stock_generation`（更正 SKU 時使用新的防重複編號）
