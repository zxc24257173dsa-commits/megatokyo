# Phase 4：CSV / Excel 成本匯入

> 依據：業主提供的 `Whatnot_ERP_CSV_Excel_Template_EN.xlsx`（2026-11-09，暫定版），
> 以及業主確認的規則：**匯率寫成「1 USD = 150 JPY」、運費是日幣、關稅是 USD**。
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
| Exchange Rate | 匯率（**1 USD = ? 原幣**，例：150） | **非 USD 必填，系統不會自行假設匯率**；JPY 需在 70–300、TWD 20–50；USD 為 1 或空白；寫成另一種方向（如 0.0067）會被擋下並提示正確值（約 149.25） |
| Domestic Japan Shipping | 日本境內運費 | **日幣**，以該列匯率換算成 USD；空白 = 0 |
| International Freight Allocation | 國際運費 | **日幣**，以該列匯率換算成 USD；空白 = 0 |
| Duties/Other Costs (USD) | 關稅／其他 | **USD**，空白 = 0 |
| Purchase Cost / Total Batch Cost / Landed Cost per Unit | （核對用） | **以系統計算為準**；試算表有值且相差超過 0.01 時提醒 |
| Costing Method | — | 系統統一 FIFO；寫 Weighted Average 會提醒 |
| Supplier | 供應商 | 不存在時自動建立 |
| Notes | 備註 | |

**計算方式：**

```
採購成本 USD   = 數量 × 單價 ÷ 匯率                    （例：24 × ¥6,800 ÷ 150 = $1,088.00）
運費 USD       = (日本境內運費 + 國際運費)（日幣）÷ 匯率   （例：(¥2,775 + ¥6,300) ÷ 150 = $60.50）
落地成本合計   = 採購成本 + 運費 + 關稅/其他               （例：1,088.00 + 60.50 + 12.00 = $1,160.50）
每件落地成本   = 落地成本合計 ÷ 數量                      （例：$48.354167）
```

**運費欄的幣別依標題判斷**：標題寫 JPY 或沒寫幣別 → 日幣；標題明確寫 USD（例如第一版範本的
「Domestic Japan Shipping (USD)」）→ 美金。運費換算後比商品採購成本還高時會提醒，避免幣別填錯。
日幣運費只能在採購幣別為 JPY 的列使用（需要該列的日幣匯率換算）。

系統保存原始填寫的匯率（`fx_rate_quote`）與日幣運費（`domestic_shipping_jpy`、`intl_freight_jpy`），可隨時對帳。

範例列（含 EXAMPLE、Sample…Replace、Example row、示例等）自動略過。

### 匯入成本 ≠ 增加庫存；商品從日本直接寄到美國

業主確認（2026-11-09）：**商品買了直接從日本寄到美國，不在日本存放。**

1. 匯入只建立 **成本紀錄**（確認後鎖定，不可修改或刪除），不會增加任何庫存。
2. 日本寄出時，在「採購批次」對該 SKU 按 **「從日本寄出」**（可分批、可填寄出日）：
   - 庫存變成 **運送中**（不可銷售），以這筆的每件落地成本建立 FIFO 批次
   - 預計到貨日 = 寄出日 + 9 天（補貨設定的前置天數）
   - 需要權限 `shipment.manage_jp`（日本庫存管理員）
3. 美國收到時按 **「美國簽收」**：運送中 → 美國倉，這時才變成可銷售；先寄出的先簽收。
   - 需要權限 `shipment.receive_us`（美國直播營運）
4. 寄出總數不能超過採購數量；簽收數量不能超過該筆運送中的數量；重複點擊不會重複寄出／簽收。
5. 系統上線前就已在美國的商品，用 Phase 3 的「期初庫存」登記到美國倉。

**日本倉**：預設關閉（`ERP_JAPAN_WAREHOUSE_ENABLED=false`），畫面不顯示日本倉，日本倉的入庫／調整／盤點會被拒絕。日後若開始在日本存貨，打開這個設定即可。

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
| 從日本寄出 | `shipment.manage_jp` |
| 美國簽收 | `shipment.receive_us` |
| 查看成本金額 | `finance.view` 或 `import.cost` |

## 資料庫變更（Migration 0006）

- `purchase_lines`：運費／關稅改為 USD 欄位（`domestic_shipping_base`、`intl_freight_base`、`duty_other_base`），新增 `purchase_cost_base`、`note`
- 匯入類型新增 `sku_master`

## 資料庫變更（Migration 0007）

- `purchase_lines.fx_rate_quote`：試算表上填的匯率（1 USD = ? 原幣）
- `purchase_lines.domestic_shipping_jpy`、`intl_freight_jpy`：原始日幣運費

## 資料庫變更（Migration 0008）

- 庫存異動規則新增：供應商 → 運送中、供應商 → 美國倉（採購直接寄美國）

## 業主已確認（2026-11-09）

- 運費（日本境內、國際）是日幣；關稅是 USD
- 匯率寫法：1 USD = 150 JPY
- 一個批次編號會包含多個 SKU（同一批次的多列合併成一張採購單）
- 商品直接從日本寄到美國，不在日本存放

## 等正確版本時要確認的事

1. 需求書提到的「日幣 / 美金 / 台幣」三個金額欄位，正式版本是否會有？
2. 運費欄的標題請寫 (JPY) 或不寫幣別；若寫 (USD)，系統會當成美金
