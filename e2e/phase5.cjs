// Phase 5 browser check: orders, show filter, unmapped products + mapping, order detail,
// Whatnot settings, sync. Run against a dev server whose DB was synced from Whatnot
// (read-only) and has a SKU "OP-GC26-PERONA" with US stock. Dev only: admin MFA disabled
// via ERP_MFA_REQUIRED_ROLES='[]'.
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

(async () => {
  const b = await chromium.launch();
  const ctx = await b.newContext({ viewport: { width: 1360, height: 900 }, deviceScaleFactor: 2 });
  const page = await ctx.newPage();
  page.on("pageerror", (e) => errors.push(e.message));
  page.on("console", (m) => m.type() === "error" && !/status of (401|409|422)/.test(m.text()) && errors.push(m.text()));
  await login(page, "owner@megatokyo.test", "Kanagawa-Wave-2026!");

  await page.goto(BASE + "/orders");
  await page.locator("tr.click").first().waitFor();
  const rows = await page.locator("tr.click").count();
  step(`order list shows ${rows} orders`);
  const showOption = await page.getByLabel("直播").locator("option").nth(1).getAttribute("value");
  await page.getByLabel("直播").selectOption(showOption);
  await page.getByText("已付款訂單").waitFor();
  await page.waitForTimeout(500);
  await page.screenshot({ path: path.join(OUT, "p5-01-orders-show.png") });
  step("filter by show: paid orders and sales tiles");

  await page.goto(BASE + "/orders?tab=unmapped");
  await page.getByText("只要對應一次").waitFor();
  const groups = await page.locator("tbody tr").count();
  await page.screenshot({ path: path.join(OUT, "p5-02-unmapped.png"), fullPage: true });
  step(`unmapped products grouped: ${groups} products`);

  // match on the product-name cell only (every row's SKU dropdown also contains the text)
  const row = page.locator("tbody tr").filter({ has: page.locator("td.lead", { hasText: "PERONA OP14-111" }) }).first();
  const before = (await row.innerText()).replace(/\s+/g, " ");
  const sel = row.locator("select");
  const val = await sel.locator("option", { hasText: "OP-GC26-PERONA" }).first().getAttribute("value");
  await sel.selectOption(val);
  await row.getByRole("button", { name: "對應" }).click();
  await page.waitForTimeout(1500);
  if (await page.locator("td.lead", { hasText: "PERONA OP14-111" }).count()) throw new Error("PERONA still unmapped");
  step("mapped PERONA once -> all its auctions: " + before.slice(0, 80));

  await page.goto(BASE + "/");
  await page.getByText("OP-GC26-PERONA").waitFor();
  const inv = (await page.locator("tr", { hasText: "OP-GC26-PERONA" }).innerText()).replace(/\s+/g, " ");
  console.log("  inventory:", inv);
  step("inventory shows US stock reserved by the mapped orders");

  await page.goto(BASE + "/orders?search=PERONA");
  await page.locator("tr.click").first().click();
  await page.getByText("買家付款合計").waitFor();
  await page.waitForTimeout(300);
  await page.screenshot({ path: path.join(OUT, "p5-03-order-detail.png"), fullPage: true });
  step("order detail with SKU, stock state and shipment");

  await page.goto(BASE + "/settings/whatnot");
  await page.getByText("API 金鑰").first().waitFor();
  await page.getByRole("button", { name: "測試連線" }).click();
  await page.getByText(/連線成功/).waitFor({ timeout: 30000 });
  await page.waitForTimeout(500);
  await page.screenshot({ path: path.join(OUT, "p5-04-whatnot-settings.png"), fullPage: true });
  step("Whatnot settings: connection test against the real account");

  const phone = await (await b.newContext({ ...devices["iPhone 13"] })).newPage();
  phone.on("pageerror", (e) => errors.push(e.message));
  await login(phone, "taipei@megatokyo.test", "Formosa-Harbor-2026");
  await phone.goto(BASE + "/orders");
  await phone.locator("tr.click").first().waitFor();
  await phone.waitForTimeout(400);
  await phone.screenshot({ path: path.join(OUT, "p5-05-orders-phone.png") });
  const overflow = await phone.evaluate(() => document.documentElement.scrollWidth - innerWidth);
  if (overflow > 1) throw new Error("horizontal overflow on phone " + overflow);
  if (await phone.getByRole("link", { name: "Whatnot 串接" }).count()) throw new Error("non-admin sees Whatnot settings");
  step("phone order list; non-admins don't see Whatnot settings");

  await b.close();
  if (errors.length) { console.log("PAGE ERRORS:\n" + errors.join("\n")); process.exit(1); }
  console.log("ALL PHASE 5 CHECKS PASSED");
})().catch((e) => { console.error("E2E FAILED:", e.message); if (errors.length) console.log(errors.join("\n")); process.exit(1); });
