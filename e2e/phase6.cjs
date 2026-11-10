// Phase 6 browser check: per-show P&L list + detail (desktop and phone), financial report import
// type, Whatnot page reconciliation tile. Run against a dev server whose DB was synced from Whatnot
// (read-only) including the financial report. Dev only: admin MFA disabled via ERP_MFA_REQUIRED_ROLES='[]'.
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
  const ctx = await b.newContext({ viewport: { width: 1360, height: 900 }, deviceScaleFactor: 2 });
  const page = await ctx.newPage();
  watch(page);
  await login(page, "owner@megatokyo.test", "Kanagawa-Wave-2026!");

  await page.getByRole("link", { name: "直播損益" }).first().click();
  await page.locator("tr.click").first().waitFor();
  const n = await page.locator("tr.click").count();
  step(`show list: ${n} shows`);
  if (!(await page.getByText("淨銷售額").first().isVisible())) throw new Error("summary tiles missing");
  await page.screenshot({ path: path.join(OUT, "p6-01-shows.png") });

  await page.locator("tr.click").first().click();
  await page.getByRole("heading", { name: "損益明細" }).waitFor();
  await page.getByRole("heading", { name: "和 Whatnot 的數字對照" }).waitFor();
  await page.getByRole("heading", { name: "各商品" }).waitFor();
  const wn = await page.getByRole("row", { name: /^扣平台費用後/ }).innerText();
  step(`show detail; comparison row: ${wn.replace(/\s+/g, " ")}`);
  await page.screenshot({ path: path.join(OUT, "p6-02-show-detail.png"), fullPage: true });

  await page.getByRole("link", { name: "看訂單" }).click();
  await page.getByText("看這場的損益").waitFor();
  step("orders page links back to the show P&L");

  await page.goto(BASE + "/imports");
  await page.getByLabel("匯入類型").waitFor();
  const opts = await page.getByLabel("匯入類型").locator("option").allTextContents();
  if (!opts.includes("Whatnot 財務報表（CSV）")) throw new Error("financial import type missing: " + opts);
  step("import page offers the Whatnot financial report");

  await page.goto(BASE + "/settings/whatnot");
  await page.getByText("財務報表交易").waitFor();
  step("Whatnot page shows financial reconciliation");

  // phone
  const phone = await b.newContext({ ...devices["iPhone 13"] });
  const p2 = await phone.newPage();
  watch(p2);
  await login(p2, "taipei@megatokyo.test", "Formosa-Harbor-2026");
  await p2.locator(".bottom-nav").getByText("直播損益").click();
  await p2.locator("tr.click").first().waitFor();
  await p2.screenshot({ path: path.join(OUT, "p6-03-shows-phone.png") });
  await p2.locator("tr.click").first().click();
  await p2.getByRole("heading", { name: "損益明細" }).waitFor();
  const overflow = await p2.evaluate(() => document.documentElement.scrollWidth - window.innerWidth);
  if (overflow > 1) throw new Error(`phone page scrolls sideways by ${overflow}px`);
  await p2.screenshot({ path: path.join(OUT, "p6-04-detail-phone.png"), fullPage: true });
  step("phone: list + detail, no sideways scroll");

  await b.close();
  if (errors.length) { console.error("browser errors:", errors); process.exit(1); }
  console.log("phase 6 e2e passed");
})().catch((e) => { console.error(e); process.exit(1); });
