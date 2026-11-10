// Phase 11 browser check: home dashboard (Chinese), switching the interface to English (saved on the account),
// main pages in English with no Chinese UI text left, and back to Chinese. Dev server only.
const { chromium, devices } = require("playwright");
const path = require("path");
const BASE = process.env.E2E_BASE || "http://localhost:8000";
const OUT = process.env.E2E_OUT || require("os").tmpdir();
const errors = [];
const step = (m) => console.log("✓", m);
const CJK = /[㐀-鿿]/;

async function login(page, email, pw, labels = { email: "Email", pw: "密碼", go: "登入" }) {
  await page.goto(BASE);
  await page.getByLabel(labels.email).fill(email);
  await page.getByLabel(labels.pw).fill(pw);
  await page.getByRole("button", { name: labels.go, exact: true }).click();
  await page.waitForURL((u) => !u.pathname.includes("login"));
  await page.locator(".shell").waitFor();
}
const watch = (page) => {
  page.on("pageerror", (e) => errors.push(e.message));
  page.on("console", (m) => m.type() === "error" && !/status of (401|409|422)/.test(m.text()) && errors.push(m.text()));
};
// Chinese left on screen, ignoring data that is Chinese by nature (titles, notes, names entered by people)
async function leftoverChinese(page) {
  return page.evaluate((re) => {
    const rx = new RegExp(re);
    const out = new Set();
    const walker = document.createTreeWalker(document.querySelector("main") || document.body, NodeFilter.SHOW_TEXT);
    while (walker.nextNode()) {
      const n = walker.currentNode;
      const el = n.parentElement;
      if (!el || el.closest("td.lead, .mono, [data-user-text], option, .muted.small, td[data-label]")) continue;
      if (rx.test(n.textContent)) out.add(n.textContent.trim().slice(0, 40));
    }
    return [...out];
  }, CJK.source);
}

(async () => {
  const b = await chromium.launch();
  const ctx = await b.newContext({ locale: "zh-TW", viewport: { width: 1360, height: 900 }, deviceScaleFactor: 2 });
  const page = await ctx.newPage();
  watch(page);
  await login(page, "taipei@megatokyo.test", "Formosa-Harbor-2026");
  await page.getByRole("heading", { name: "今天要處理" }).waitFor();
  step("dashboard (zh): " + (await page.locator("ul.todo li").allInnerTexts()).join(" | "));
  await page.screenshot({ path: path.join(OUT, "p11-01-dashboard-zh.png"), fullPage: true });

  await page.goto(BASE + "/account");
  await page.getByLabel("介面語言").selectOption("en-US");
  await page.getByRole("heading", { name: "My account" }).waitFor();
  step("switched to English (saved on the account, page reloaded)");

  const pages = [["/", "To do today"], ["/orders", "Orders"], ["/logistics", "Logistics"], ["/inventory", "Inventory"],
    ["/reports/shows", "Show P&L"], ["/reports/trends", "Sales trends"], ["/reports/pnl", "P&L statement"],
    ["/expenses", "Expenses"], ["/replenishment", "Replenishment"], ["/purchasing", "Purchase batches"],
    ["/stock/operations", "Receive / adjust stock"], ["/stock/counts", "Stock counts"], ["/imports", "Imports"]];
  for (const [url, heading] of pages) {
    await page.goto(BASE + url);
    await page.getByRole("heading", { name: heading }).first().waitFor();
    await page.waitForTimeout(400);
    const left = await leftoverChinese(page);
    if (left.length) throw new Error(`${url}: Chinese UI text left: ${JSON.stringify(left.slice(0, 8))}`);
  }
  step(`${pages.length} pages in English, no Chinese UI text left`);
  await page.goto(BASE + "/");
  await page.getByRole("heading", { name: "To do today" }).waitFor();
  await page.screenshot({ path: path.join(OUT, "p11-02-dashboard-en.png"), fullPage: true });
  await page.goto(BASE + "/replenishment");
  await page.locator("tbody tr").first().getByRole("button", { name: "Why" }).click();
  await page.getByText("Target stock =").waitFor();
  await page.screenshot({ path: path.join(OUT, "p11-03-replenishment-en.png") });
  step("replenishment explanation in English");

  // a new browser picks the account's language after sign-in, even if the browser is Chinese
  const c2 = await b.newContext({ ...devices["iPhone 13"], locale: "zh-TW" });
  const p2 = await c2.newPage();
  watch(p2);
  await login(p2, "taipei@megatokyo.test", "Formosa-Harbor-2026");
  await p2.getByRole("heading", { name: "To do today" }).waitFor();
  const overflow = await p2.evaluate(() => document.documentElement.scrollWidth - window.innerWidth);
  if (overflow > 1) throw new Error(`phone page scrolls sideways by ${overflow}px`);
  await p2.screenshot({ path: path.join(OUT, "p11-04-dashboard-en-phone.png") });
  step("another device follows the account language (English), phone fits");

  await page.goto(BASE + "/account");
  await page.getByLabel("Interface language").selectOption("zh-TW");
  await page.getByRole("heading", { name: "我的帳號" }).waitFor();
  step("switched back to Chinese");

  await b.close();
  if (errors.length) { console.error("browser errors:", errors); process.exit(1); }
  console.log("phase 11 e2e passed");
})().catch((e) => { console.error(e); process.exit(1); });
