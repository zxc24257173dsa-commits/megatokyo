// Phase 8 browser check: sales trend chart (hover tooltip), grouping, movers with stock cover,
// phone layout (money hidden for non-finance roles is covered by the API tests). Dev server only.
const { chromium, devices } = require("playwright");
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
  const ctx = await b.newContext({ locale: "zh-TW", viewport: { width: 1360, height: 900 }, deviceScaleFactor: 2 });
  const page = await ctx.newPage();
  watch(page);
  await login(page, "owner@megatokyo.test", "Kanagawa-Wave-2026!");

  await page.getByRole("link", { name: "銷售趨勢" }).click();
  const chart = page.getByRole("img", { name: "商品成交額長條圖" });
  await chart.waitFor();
  const bars = await page.locator(".chart-bar").count();
  step(`weekly chart drawn with ${bars} non-empty weeks`);
  await page.locator(".chart-bar").last().hover();
  await page.locator(".chart-tip").getByText("筆訂單").waitFor();
  step("hover tooltip: " + (await page.locator(".chart-tip").innerText()).replace(/\s+/g, " "));
  await page.mouse.move(5, 5);
  await page.screenshot({ path: path.join(OUT, "p8-01-trends-week.png"), fullPage: true });

  await page.getByRole("button", { name: "最近 30 天（每日）" }).click();
  await page.waitForTimeout(400);
  await page.getByLabel("分組").selectOption("product");
  await page.getByRole("heading", { name: "各產品" }).waitFor();
  step("daily view, grouped by product");
  await page.getByLabel("指標").selectOption("units");
  await page.getByRole("img", { name: "售出件數長條圖" }).waitFor();
  await page.screenshot({ path: path.join(OUT, "p8-02-trends-day-units.png") });
  step("switch metric to units");

  await page.getByRole("button", { name: "商品動向與庫存" }).click();
  await page.getByRole("columnheader", { name: "每週需求" }).waitFor();
  step(`movers: ${await page.locator("tbody tr").count()} SKUs`);
  await page.screenshot({ path: path.join(OUT, "p8-03-movers.png"), fullPage: true });

  const phone = await b.newContext({ ...devices["iPhone 13"], locale: "zh-TW" });
  const p2 = await phone.newPage();
  watch(p2);
  await login(p2, "taipei@megatokyo.test", "Formosa-Harbor-2026");
  await p2.goto(BASE + "/reports/trends");
  await p2.getByRole("img", { name: "商品成交額長條圖" }).waitFor();
  const overflow = await p2.evaluate(() => document.documentElement.scrollWidth - window.innerWidth);
  if (overflow > 1) throw new Error(`phone page scrolls sideways by ${overflow}px`);
  await p2.screenshot({ path: path.join(OUT, "p8-04-trends-phone.png"), fullPage: true });
  step("phone: chart fits the screen");
  await b.close();
  if (errors.length) { console.error("browser errors:", errors); process.exit(1); }
  console.log("phase 8 e2e passed");
})().catch((e) => { console.error(e); process.exit(1); });
