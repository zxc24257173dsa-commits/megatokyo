// Phase 10 browser check: generate replenishment suggestions from real dev data, read the explanation,
// override a quantity with a reason, mark as decided, export the purchase list, phone layout.
// Dev server only (writes a replenishment run to the dev DB).
const { chromium, devices } = require("playwright");
const fs = require("fs");
const path = require("path");
const BASE = process.env.E2E_BASE || "http://localhost:8000";
const OUT = process.env.E2E_OUT || require("os").tmpdir();
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
  const b = await chromium.launch();
  const ctx = await b.newContext({ locale: "zh-TW", viewport: { width: 1360, height: 900 }, deviceScaleFactor: 2, acceptDownloads: true });
  const page = await ctx.newPage();
  watch(page);
  await login(page, "owner@megatokyo.test", "Kanagawa-Wave-2026!");

  await page.getByRole("link", { name: "補貨建議" }).click();
  await page.getByText("系統不會自動下單").waitFor();
  await page.getByRole("button", { name: "產生最新建議" }).click();
  await page.getByText("資料到").waitFor();
  const first = page.locator("tbody tr").first();
  await first.waitFor();
  step(`suggestions: ${(await page.locator(".tile").first().innerText()).replace(/\s+/g, " ")}`);

  await first.getByRole("button", { name: "為什麼" }).click();
  await page.getByText("目標庫存 =").waitFor();
  step("explanation: " + (await page.getByText("目標庫存 =").innerText()));
  await page.screenshot({ path: path.join(OUT, "p10-01-replenishment.png"), fullPage: true });

  const buyRow = page.locator("tbody tr", { hasText: "買" }).first();
  if (await buyRow.count()) {
    const code = (await buyRow.locator("td.lead .mono").innerText()).trim();
    await buyRow.getByRole("button", { name: "改" }).click();
    await page.getByLabel(`${code} 採購數量`).fill("240");
    await page.getByLabel(`${code} 修改原因`).fill("e2e：供應商一箱 24 盒");
    await page.getByRole("button", { name: "儲存" }).click();
    await page.locator("tbody tr", { hasText: code }).first().getByText("買 240 件").waitFor();
    step(`override ${code} -> 240 with reason`);
    await page.getByLabel(`選取 ${code}`).check();
    await page.getByRole("button", { name: /標記 1 項為已決定採購/ }).click();
    await page.locator("tbody tr", { hasText: code }).first().getByText("已決定").waitFor();
    step("marked as decided (no order placed)");
  }

  const [dl] = await Promise.all([page.waitForEvent("download"), page.getByRole("button", { name: "匯出採購清單" }).click()]);
  const csv = fs.readFileSync(await dl.path(), "utf8");
  if (!csv.includes("Quantity to buy")) throw new Error("bad purchase list");
  step(`purchase list CSV: ${csv.trim().split("\n").length - 1} rows`);

  const phone = await b.newContext({ ...devices["iPhone 13"], locale: "zh-TW" });
  const p2 = await phone.newPage();
  watch(p2);
  await login(p2, "taipei@megatokyo.test", "Formosa-Harbor-2026");
  await p2.goto(BASE + "/replenishment");
  await p2.getByText("資料到").waitFor();
  const overflow = await p2.evaluate(() => document.documentElement.scrollWidth - window.innerWidth);
  if (overflow > 1) throw new Error(`phone page scrolls sideways by ${overflow}px`);
  await p2.screenshot({ path: path.join(OUT, "p10-02-replenishment-phone.png") });
  step("phone: fits the screen");

  await b.close();
  if (errors.length) { console.error("browser errors:", errors); process.exit(1); }
  console.log("phase 10 e2e passed");
})().catch((e) => { console.error(e); process.exit(1); });
