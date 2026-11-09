// Phase 4 browser test: cost-sheet import wizard + receiving into the Japan warehouse.
// Needs: fresh dev DB, server on :8000, a user with roles tw_manager + jp_inventory
// (E2E_USER / E2E_PASSWORD), the owner's template (E2E_TEMPLATE) and a filled copy (E2E_FILLED).
const { chromium, devices } = require("playwright");
const path = require("path");

const BASE = process.env.E2E_BASE || "http://localhost:8000";
const OUT = process.env.E2E_OUT || require("os").tmpdir();
const USER = process.env.E2E_USER, PW = process.env.E2E_PASSWORD;
const TEMPLATE = process.env.E2E_TEMPLATE, FILLED = process.env.E2E_FILLED;
const step = (m) => console.log("✓", m);
const errors = [];

async function login(page) {
  await page.goto(BASE);
  await page.getByLabel("Email").fill(USER);
  await page.getByLabel("密碼").fill(PW);
  await page.getByRole("button", { name: "登入" }).click();
  await page.getByRole("heading", { name: "庫存總覽" }).waitFor();
}
async function upload(page, file, typeLabel = "採購成本") {
  await page.goto(BASE + "/imports");
  await page.getByLabel("匯入類型").selectOption({ label: typeLabel });
  await page.getByLabel("選擇檔案").setInputFiles(file);
  await page.getByRole("button", { name: "上傳並檢查" }).click();
}

(async () => {
  const browser = await chromium.launch();
  const ctx = await browser.newContext({ viewport: { width: 1360, height: 900 }, deviceScaleFactor: 2 });
  const page = await ctx.newPage();
  page.on("pageerror", (e) => errors.push(e.message));
  page.on("console", (m) => m.type() === "error" && !/status of (401|409|422)/.test(m.text()) && errors.push(m.text()));
  await login(page);

  // 1. The owner's template as delivered: only example rows -> nothing to import
  await upload(page, TEMPLATE);
  await page.waitForURL(/\/imports\/\d+/);
  await page.getByText("工作表「Purchase Cost Batches」第 1 列是標題，已對應 17 個欄位").waitFor();
  await page.getByRole("button", { name: "確認匯入 0 列" }).waitFor();
  if (!(await page.getByRole("button", { name: "確認匯入 0 列" }).isDisabled())) throw new Error("should not allow importing 0 rows");
  await page.getByText("範例列，不會匯入").waitFor();
  step("original template: correct sheet + 17 columns detected, example row skipped, nothing to import");
  page.once("dialog", (d) => d.accept());
  await page.getByRole("button", { name: "取消匯入" }).click();
  await page.getByText("已取消").first().waitFor();

  // 2. Filled copy: 2 new SKUs + 1 row with an inverted exchange rate
  await upload(page, FILLED);
  await page.waitForURL(/\/imports\/\d+/);
  await page.getByText("其中新 SKU 2").waitFor();
  await page.getByRole("button", { name: "錯誤", exact: true }).click();
  await page.getByText(/匯率看起來寫反了/).waitFor();
  await page.getByRole("button", { name: "全部", exact: true }).click();
  await page.waitForTimeout(300);
  await page.screenshot({ path: path.join(OUT, "p4-01-preview.png"), fullPage: true });
  step("filled template: preview shows 2 new SKUs and the inverted-rate error with the correct value");
  await page.getByRole("button", { name: /確認匯入 2 列/ }).click();
  await page.getByText("有錯誤的列；請修正檔案").waitFor();
  await page.getByText(/一併建立 2 個/).click();
  await page.getByText(/略過 1 筆有錯誤的列/).click();
  await page.getByRole("button", { name: /確認匯入 2 列/ }).click();
  await page.getByText(/匯入完成：1 個採購批次、2 筆成本/).waitFor();
  step("commit: blocked until error rows are explicitly skipped and new SKUs confirmed, then writes 1 batch / 2 lines");

  // 3. Same file again is refused
  await upload(page, FILLED);
  await page.getByText("這個檔案已經匯入過了").waitFor();
  step("re-uploading the same file is refused");

  // 4. Importing cost added NO stock
  await page.goto(BASE + "/");
  await page.getByText("PK151-BOX-JP").waitFor();
  let row = (await page.locator("tr", { hasText: "PK151-BOX-JP" }).innerText()).replace(/\s+/g, " ");
  if (!/PK151-BOX-JP .* 0 0 0 0 0/.test(row)) throw new Error("cost import must not create stock: " + row);
  step("no stock created by importing costs");

  // 5. Receive 10 of 24 boxes into Japan (phone)
  const phoneCtx = await browser.newContext({ ...devices["iPhone 13"] });
  const phone = await phoneCtx.newPage();
  phone.on("pageerror", (e) => errors.push(e.message));
  await login(phone);
  await phone.goto(BASE + "/purchasing");
  await phone.getByText("B-2026-1101").click();
  await phone.getByLabel("PK151-BOX-JP 入庫數量").fill("10");
  await phone.getByLabel("到貨日期").first().fill("2026-11-05");
  await phone.getByRole("button", { name: "入庫到日本倉" }).first().click();
  await phone.getByText("已入庫 10 / 24").waitFor();
  await phone.waitForTimeout(300);
  await phone.screenshot({ path: path.join(OUT, "p4-02-batch-phone.png"), fullPage: true });
  const overflow = await phone.evaluate(() => document.documentElement.scrollWidth - innerWidth);
  if (overflow > 1) throw new Error("horizontal overflow on phone: " + overflow);
  step("received 10/24 on phone; no horizontal scrolling");

  // 6. Inventory: JP 10 at landed cost 47.900833 -> $479.01
  await page.goto(BASE + "/");
  await page.reload();
  await page.waitForTimeout(2000);
  await page.getByText("PK151-BOX-JP").waitFor();
  row = (await page.locator("tr", { hasText: "PK151-BOX-JP" }).innerText()).replace(/\s+/g, " ");
  console.log("  inventory row:", row);
  if (!row.includes("$479.01") || !/JP\)? .*10 0 0 0 0/.test(row)) throw new Error("unexpected inventory row");
  await page.screenshot({ path: path.join(OUT, "p4-03-inventory.png") });
  step("Japan stock 10, valued at the landed cost ($479.01)");

  await browser.close();
  if (errors.length) { console.log("PAGE ERRORS:\n" + errors.join("\n")); process.exit(1); }
  console.log("ALL PHASE 4 E2E CHECKS PASSED");
})().catch((e) => { console.error("E2E FAILED:", e.message); if (errors.length) console.log(errors.join("\n")); process.exit(1); });
