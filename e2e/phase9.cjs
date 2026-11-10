// Phase 9 browser check: import a small cost batch, ship it as a parcel with a tracking number, receive part
// of it in the US with one damaged item (reason required), see the loss in the P&L, logistics overview and
// outbound backlog, phone layout. Dev server only; writes a new batch / parcel to the dev DB each run.
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
  const batch = `E2E-PARCEL-${Date.now().toString().slice(-6)}`;
  const csv = path.join(OUT, `${batch}.csv`);
  const H = ["Cost Batch ID", "SKU", "Product Name", "Purchase Date", "Purchase Quantity", "Unit Purchase Cost", "Purchase Currency",
    "Exchange Rate (1 USD =)", "Domestic Japan Shipping (JPY)", "International Freight Allocation (JPY)", "Duties/Other Costs (USD)", "Supplier"];
  fs.writeFileSync(csv, [H.join(","), [batch, "OP-GC26-PERONA", "PERONA promo", "2026-11-05", 10, 1200, "JPY", 150, 0, 300, 0, "Tokyo"].join(",")].join("\n"));

  const b = await chromium.launch();
  const ctx = await b.newContext({ viewport: { width: 1360, height: 900 }, deviceScaleFactor: 2 });
  const page = await ctx.newPage();
  watch(page);
  page.on("dialog", (d) => d.accept());
  await login(page, "owner@megatokyo.test", "Kanagawa-Wave-2026!");

  await page.goto(BASE + "/imports");
  await page.getByLabel("匯入類型").selectOption({ label: "採購成本" });
  await page.getByLabel("選擇檔案").setInputFiles(csv);
  await page.getByRole("button", { name: /上傳/ }).click();
  await page.getByRole("button", { name: /確認匯入 1 列/ }).click();
  await page.getByText("匯入完成").waitFor();
  step(`imported cost batch ${batch}`);

  await page.goto(BASE + "/purchasing");
  await page.getByText(batch).click();
  await page.getByRole("button", { name: "建立寄出包裹" }).click();
  await page.getByLabel("OP-GC26-PERONA 寄出數量").fill("10");
  await page.getByLabel("物流商").selectOption({ label: "日本郵便 EMS" });
  await page.getByLabel("追蹤號碼（選填，之後也可以補）").fill("EM987654321JP");
  await page.getByLabel("箱數（選填）").fill("1");
  await page.getByRole("button", { name: "寄出 10 件" }).click();
  await page.getByRole("heading", { name: "內容" }).waitFor();
  const href = await page.getByRole("link", { name: "EM987654321JP" }).getAttribute("href");
  if (!href.includes("EM987654321JP")) throw new Error("tracking link missing");
  step("parcel created with EMS tracking link");
  await page.screenshot({ path: path.join(OUT, "p9-01-parcel-shipped.png") });

  await page.getByLabel("OP-GC26-PERONA 收到").fill("8");
  await page.getByLabel("OP-GC26-PERONA 短少或損壞").fill("1");
  await page.getByRole("button", { name: "確認簽收" }).click();
  await page.getByText("請說明短少或損壞的原因").or(page.locator(".alert.error")).first().waitFor();
  step("loss without a reason is refused");
  await page.getByLabel("OP-GC26-PERONA 原因").fill("e2e：卡片折到");
  await page.getByRole("button", { name: "確認簽收" }).click();
  await page.getByText("部分簽收").first().waitFor();
  step("received 8, 1 damaged, 1 still in transit");
  await page.screenshot({ path: path.join(OUT, "p9-02-parcel-partial.png"), fullPage: true });

  await page.goto(BASE + "/logistics");
  await page.getByText("運送中包裹").waitFor();
  await page.locator("tr", { hasText: "EM987654321JP" }).first().waitFor();
  step("logistics overview lists the open parcel");
  await page.screenshot({ path: path.join(OUT, "p9-03-logistics.png") });
  await page.getByRole("button", { name: "美國待出貨" }).click();
  await page.getByText("已付款、商品還沒離開美國倉").waitFor();
  step("outbound backlog: " + (await page.locator(".tile").last().innerText()).replace(/\s+/g, " "));

  await page.goto(BASE + "/reports/pnl");
  await page.getByRole("button", { name: "本月" }).click();
  await page.getByRole("cell", { name: "存貨損耗／盤盈" }).waitFor();
  step("P&L shows the inventory loss line");

  const phone = await b.newContext({ ...devices["iPhone 13"] });
  const p2 = await phone.newPage();
  watch(p2);
  await login(p2, "taipei@megatokyo.test", "Formosa-Harbor-2026");
  await p2.locator(".bottom-nav").getByText("物流").click();
  await p2.getByText("運送中包裹").waitFor();
  const overflow = await p2.evaluate(() => document.documentElement.scrollWidth - window.innerWidth);
  if (overflow > 1) throw new Error(`phone page scrolls sideways by ${overflow}px`);
  await p2.screenshot({ path: path.join(OUT, "p9-04-logistics-phone.png") });
  step("phone: logistics page fits");

  await b.close();
  fs.unlinkSync(csv);
  if (errors.length) { console.error("browser errors:", errors); process.exit(1); }
  console.log("phase 9 e2e passed");
})().catch((e) => { console.error(e); process.exit(1); });
