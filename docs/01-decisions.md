# 業主確認的業務決策（2026-11-09）

| # | 項目 | 決策 | 系統中的位置 |
|---|------|------|--------------|
| 1 | 基準幣別 | **USD** | `settings.base_currency`；所有 `*_base` 金額欄位為 USD；原幣別與匯率另存 |
| 2 | 營運時區 | **美東時間 America/New_York**，週一為週起始 | `settings.business_timezone`；`financial_entries.entry_date`、`daily_sku_sales.biz_date` 以美東日期計 |
| 3 | 成本制度 | **FIFO + 批次（lot）追蹤** | `inventory_lots`、`cogs_allocations` |
| 4 | 收入認列 | **買家付款即認列**；業務上沒有取消訂單 | `sales_orders.paid_at`；仍保留沖銷機制以處理資料更正與退款 |
| 5 | 組合商品 | **需要支援一個商品由多個 SKU 組成** | `skus.is_bundle`、`sku_bundle_components`；組合本身無庫存，銷售時扣組成 SKU |
| 6 | 供應商前置時間 | 下單到日本入庫 **9–11 天**，預設以保守的 **11 天**規劃 | `replenishment_settings.supplier_lead_time_days`，可逐 SKU 覆寫 |
| 7 | 日→美運送 | **商品買了直接從日本寄到美國，不在日本存放**（2026-11-09 更新）；9 天，每週一次，**沒有固定出貨星期** | `lead_time_days = 9`、`review_period_days = 7`、`ship_weekday = NULL`；補貨建議以「建立出貨單當天」為起點計算，不綁定星期 |
| 8 | 國際運費/關稅分攤 | 不另做分攤引擎；直接採用成本試算表上每列的金額 | `purchase_lines.intl_shipping_orig`、`duty_orig`（Phase 4 依實際試算表確認） |
| 9 | 安全庫存 | **依銷量波動計算**：z × σ(日銷量) × √(前置天數)，預設 z = 1.65（約 95% 服務水準） | `replenishment_settings.safety_stock_method = 'volatility'` |
| 10 | Whatnot API | **已取得權限** | Phase 5 串接；Token 只存在加密欄位/Secret Manager，不得貼在對話或程式碼中 |
| 11 | 規模 | SKU 約 50–100；每月訂單量大 | 索引、彙總表已設計；`inventory_movements`/`financial_entries` 未來可依年分區 |
| 12 | 部署 | GCP 東京區 | Phase 12 |
| 13 | 程式碼儲存庫 | GitHub `megatokyo`（私人） | 已推送 |
| 14 | 登入方式 | **Google 帳號一鍵登入**；密碼＋驗證碼為備用 | docs/02 |
| 15 | 成本試算表 | 匯率寫「1 USD = 150 JPY」；運費日幣、關稅 USD；一個批次可含多個 SKU | docs/04 |
| 17 | 拆盒成本 | **平均分攤**：盒子成本 ÷ 盒內件數 | docs/05 |
| 18 | 訂單 SKU 對應 | Whatnot 商品名稱去掉 #編號後對應一次 | docs/05 |
| 19 | 直播費用 | 只用 Whatnot 財務報表的真實數字，不用費率估算；未到的標示為未完整 | docs/06 |
| 20 | 營運費用與月結 | 費用不可修改只能作廢；結帳月份凍結，晚到的自動入帳記在當月 | docs/07 |
| 21 | 物流 | 以包裹（一個追蹤號碼）寄出與簽收；短少需原因並計入存貨損耗 | docs/09 |
| 16 | 日本倉 | 不使用（`ERP_JAPAN_WAREHOUSE_ENABLED=false`），可日後開啟 | docs/04 |

## 尚待提供
- 商品成本試算表（Phase 4 前需要）：決定 `purchase_lines` 最終欄位與欄位對應。
- ~~Whatnot 匯出檔~~：訂單 CSV 已用真實檔驗證（docs/05）；費用改用 API 財務報表（docs/06）。
