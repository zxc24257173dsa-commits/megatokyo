# Phase 4：CSV / Excel 成本匯入

> 依據：業主提供的 `Whatnot_ERP_CSV_Excel_Template_EN.xlsx`（2026-11-09，暫定版）。
> 正確版本提供後，只需調整欄位同義詞或規則，匯入流程本身不變。

## 流程

```
上傳 .xlsx / .csv
  → 自動找到正確的工作表與標題列，依欄位名稱自動對應（英文 / 中文 / 日文）
  → 逐列檢查，產生預覽（將匯入 / 錯誤 / 提醒 / 略過）
  → 使用者可修改對應、勾選「建立新 SKU」「略過錯誤列」
  → 確認匯入：在同一個交易內寫入；同一份檔案不能匯入兩次
  → 之後同樣格式的檔案會自動套用上次儲存的欄位對應
```

**確認前不會寫入任何正式資料。** 確認時會用資料庫的最新狀態再檢查一次。

## 支援的檔案

| 類型 | 說明 |
|---|---|
| .xlsx（多工作表） | 讀取 Excel 最後計算的值；從未用 Excel 開過的公式欄會被辨識出來，不會被當成 0 |
| .csv | 自動辨識 UTF-8、Shift_JIS（日本 Excel）、Big5（台灣 Excel）；逗號、分號、Tab 分隔 |
| 安全 | 10 MB、每張工作表 20,000 列上限；Excel XML 以 defusedxml 解析 |

## 採購成本（工作表「Purchase Cost Batches」）

| 範本欄位 | 系統欄位 | 規則 |
|---|---|---|
| Cost Batch ID | 採購批次編號 | 同編號的列合併為一個批次（採購日、供應商必須相同）；空白則每列自成一批；已存在的編號拒絕匯入 |
| SKU | SKU | 不分大小寫比對；系統沒有時，勾選後一併建立新商品（需有商品名稱） |
| Product Name | 商品名稱 | 建立新 SKU 時使用 |
| Purchase Date | 採購日期 | 必填；接受 Excel 日期、2026-11-01、2026/11/01、2026年11月1日 |
| Purchase Quantity | 數量 | 正整數 |
| Unit Purchase Cost | 單價（原幣） | ≥ 0；可含 ¥、$、千分位 |
| Purchase Currency | 幣別 | USD / JPY / TWD（也接受 円、日幣、US$、台幣…） |
| Exchange Rate to USD | 匯率（1 原幣 = ? USD） | **非 USD 必填，系統不會自行假設匯率**；JPY 需在 0.003–0.02、TWD 0.02–0.06；寫反（如 151.5）會被擋下並提示正確值 |
| Domestic Japan Shipping (USD) | 日本境內運費 | USD，空白 = 0 |
| International Freight Allocation (USD) | 國際運費 | USD，空白 = 0 |
| Duties/Other Costs (USD) | 關稅／其他 | USD，空白 = 0 |
| Purchase Cost / Total Batch Cost / Landed Cost per Unit | （核對用） | **以系統計算為準**；試算表有值且相差超過 0.01 時提醒 |
| Costing Method | — | 系統統一 FIFO；寫 Weighted Average 會提醒 |
| Supplier | 供應商 | 不存在時自動建立 |
| Notes | 備註 | |

**計算方式：**

```
採購成本 USD   = 數量 × 單價 × 匯率
落地成本合計   = 採購成本 + 日本境內運費 + 國際運費 + 關稅/其他
每件落地成本   = 落地成本合計 ÷ 數量
```

範例列（含 EXAMPLE、Sample…Replace、Example row、示例等）自動略過。

### 匯入成本 ≠ 增加庫存

過去的採購批次可能早已賣完，若匯入時直接加到日本倉，會產生不存在的庫存。因此：

1. 匯入只建立 **成本紀錄**（確認後鎖定，不可修改或刪除）。
2. 商品實際到達日本倉時，在「採購批次」對該 SKU 按 **入庫**（可分批、可填到貨日）。
3. 入庫時以該筆的每件落地成本建立 FIFO 批次；入庫總數不能超過採購數量。
4. 系統上線前就已在倉庫的商品，用 Phase 3 的「期初庫存」登記。

## 商品主檔（工作表「Product SKU Master」）

新增或更新：名稱、分類、規格、售價與幣別、安全庫存、狀態、Whatnot Listing ID。

**不會匯入**：Japan / US / In-Transit / Reserved / Available Inventory、Current Standard Cost。庫存只能來自實際的入庫與異動，成本只能來自採購批次。

## 範本中不匯入的工作表

| 工作表 | 原因 / 安排 |
|---|---|
| Order Details、Show Reports、Show Expenses | Phase 5–7（Whatnot 訂單、直播報表、費用）；需要 Whatnot 實際匯出檔確認欄位 |
| Inventory Movements | 庫存異動由系統操作自動產生，不從試算表匯入 |
| Replenishment、Show Profit & Loss | 由系統計算（Phase 7、10），不從試算表匯入 |

## 權限

| 動作 | 權限 |
|---|---|
| 匯入採購成本 | `import.cost`（管理員、日本庫存、台灣管理） |
| 匯入時建立新 SKU、匯入商品主檔 | `catalog.edit` |
| 入庫到日本倉 | `inventory.adjust_jp` |
| 查看成本金額 | `finance.view` 或 `import.cost` |

## 資料庫變更（Migration 0006）

- `purchase_lines`：運費／關稅改為 USD 欄位（`domestic_shipping_base`、`intl_freight_base`、`duty_other_base`），新增 `purchase_cost_base`、`note`
- 匯入類型新增 `sku_master`

## 等正確版本時要確認的事

1. 運費、關稅是否一定以 USD 填寫？還是可能是日幣？（目前依範本：USD）
2. 一個 Cost Batch ID 是否會包含多個 SKU？（目前支援）
3. 需求書提到的「日幣 / 美金 / 台幣」三個金額欄位，正式版本是否會有？
4. 匯率的寫法是「1 JPY = 0.0067 USD」（目前）還是「1 USD = 150 JPY」？
