// Phase 7 browser check: record expenses (yen with rate + receipt, assigned to a show), void one,
// P&L by month and week, CSV export, month close / reopen, phone layout.
// Dev server only (admin MFA disabled via ERP_MFA_REQUIRED_ROLES='[]'); writes test expenses to the dev DB.
const { chromium, devices } = require("playwright");
const fs = require("fs");
const os = require("os");
const path = require("path");
const BASE = process.env.E2E_BASE || "http://localhost:8000";
const OUT = process.env.E2E_OUT || os.tmpdir();
const errors = [];
const step = (m) => console.log("✓", m);

async function login(page, email, pw) {
  await page.goto(BASE);
  await page.getByLabel("Email").fill(email);
  await page.getByLabel("密碼").fill(pw);
  await page.getByRole("button", { name: "登入" }).click();
  await page.waitForURL((u) => !u.pathname.includes("login"));
  await page.locator(".shell").waitFor();
}
const watch = (page) => {
  page.on("pageerror", (e) => errors.push(e.message));
  page.on("console", (m) => m.type() === "error" && !/status of (401|409|422)/.test(m.text()) && errors.push(m.text()));
};

(async () => {
  const receipt = path.join(OUT, "e2e-receipt.png");
  fs.writeFileSync(receipt, Buffer.from("89504e470d0a1a0a0000000d4948445200000001000000010806000000" +
    "1f15c4890000000d49444154789c6360000002000154a24f5d0000000049454e44ae426082", "hex"));
  const b = await chromium.launch();
  const ctx = await b.newContext({ locale: "zh-TW", viewport: { width: 1360, height: 900 }, deviceScaleFactor: 2, acceptDownloads: true });
  const page = await ctx.newPage();
  page.on("dialog", (d) => d.accept(d.type() === "prompt" ? "e2e 測試作廢" : undefined));
  watch(page);
  await login(page, "owner@megatokyo.test", "Kanagawa-Wave-2026!");

  await page.getByRole("link", { name: "營運費用" }).click();
  await page.getByRole("heading", { name: "記錄一筆費用" }).waitFor();
  await page.getByLabel("類別").selectOption({ label: "主播／助理費" });
  await page.locator("input[inputmode=decimal]").first().fill("15000");
  await page.getByLabel("幣別").selectOption("JPY");
  await page.getByLabel("匯率：1 USD = ? JPY").fill("150");
  await page.getByText("約 $100.00").waitFor();
  const show = await page.getByLabel("屬於哪一場直播（選填）").locator("option").nth(1).getAttribute("value");
  await page.getByLabel("屬於哪一場直播（選填）").selectOption(show);
  await page.getByLabel("說明（選填）").fill("e2e 測試：主播費");
  await page.getByLabel("收據（選填）").setInputFiles(receipt);
  await page.getByRole("button", { name: "儲存" }).click();
  await page.getByText("已記錄：主播／助理費 $100.00").waitFor();
  step("expense in JPY at 1 USD = 150 with receipt -> $100.00");

  await page.getByLabel("類別").selectOption({ label: "包材耗材" });
  await page.getByLabel("幣別").selectOption("USD");
  await page.locator("input[inputmode=decimal]").first().fill("12.5");
  const tag = `e2e 測試：要作廢 ${Date.now()}`;
  await page.getByLabel("說明（選填）").fill(tag);
  await page.getByRole("button", { name: "儲存" }).click();
  await page.getByText("已記錄：包材耗材 $12.50").waitFor();
  const row = page.locator("tr", { hasText: tag });
  await row.waitFor();
  await row.getByRole("button", { name: "作廢" }).click();
  await row.getByText("已作廢").waitFor();
  step("void with a reason keeps the row (marked 已作廢)");
  await page.screenshot({ path: path.join(OUT, "p7-01-expenses.png"), fullPage: true });

  await page.goto(BASE + `/reports/shows/${show}`);
  await page.getByText("這場的營運費用").waitFor();
  step("show P&L picks up the show's host fee and shows net profit");

  await page.getByRole("link", { name: "損益表" }).click();
  await page.getByRole("cell", { name: "淨利", exact: true }).waitFor();
  await page.getByRole("cell", { name: "主播／助理費" }).waitFor();
  step("P&L by month with expense categories");
  await page.screenshot({ path: path.join(OUT, "p7-02-pnl-month.png") });

  await page.getByRole("button", { name: "最近 12 週" }).click();
  await page.locator("th", { hasText: "–" }).first().waitFor();
  step("P&L by week");

  const [dl] = await Promise.all([page.waitForEvent("download"), page.getByRole("button", { name: "匯出明細 CSV" }).click()]);
  const csv = fs.readFileSync(await dl.path(), "utf8");
  if (!csv.includes("entry_date,entry_type,amount_usd") || !csv.includes("operating_expense")) throw new Error("bad CSV");
  step(`CSV export: ${csv.trim().split("\n").length - 1} ledger lines`);

  await page.getByRole("button", { name: "最近 12 個月" }).click();
  const closeBtn = page.getByRole("button", { name: "結帳", exact: true }).last();
  await closeBtn.click();
  await page.getByText("已結帳").first().waitFor();
  step("closed the last finished month");
  await page.getByRole("button", { name: "重新開啟" }).last().click();
  await page.waitForFunction(() => !document.body.innerText.includes("已結帳"));
  step("reopened it");

  const phone = await b.newContext({ ...devices["iPhone 13"], locale: "zh-TW" });
  const p2 = await phone.newPage();
  watch(p2);
  await login(p2, "taipei@megatokyo.test", "Formosa-Harbor-2026");
  await p2.goto(BASE + "/reports/pnl");
  await p2.getByRole("cell", { name: "淨利", exact: true }).waitFor();
  const overflow = await p2.evaluate(() => document.documentElement.scrollWidth - window.innerWidth);
  if (overflow > 1) throw new Error(`phone page scrolls sideways by ${overflow}px`);
  await p2.screenshot({ path: path.join(OUT, "p7-03-pnl-phone.png") });
  await p2.goto(BASE + "/expenses");
  await p2.getByRole("heading", { name: "記錄一筆費用" }).waitFor();
  await p2.screenshot({ path: path.join(OUT, "p7-04-expenses-phone.png"), fullPage: true });
  step("phone: P&L (table scrolls inside its card) and expense form");

  await b.close();
  if (errors.length) { console.error("browser errors:", errors); process.exit(1); }
  console.log("phase 7 e2e passed");
})().catch((e) => { console.error(e); process.exit(1); });
