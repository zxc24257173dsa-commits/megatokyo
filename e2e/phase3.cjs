// Run against a FRESH dev database with an admin created by `python -m app.cli create-admin` (MFA not yet set up).
// Usage: node e2e/phase3.cjs   (env: E2E_BASE, E2E_ADMIN_EMAIL, E2E_ADMIN_PASSWORD, E2E_OUT)
// End-to-end browser test of Phase 3 against the real server (http://localhost:8000).
const { chromium, devices } = require("playwright");
const crypto = require("crypto");
const path = require("path");

const OUT = process.env.E2E_OUT || require("os").tmpdir();
const BASE = process.env.E2E_BASE || "http://localhost:8000";
const EMAIL = process.env.E2E_ADMIN_EMAIL || "owner@megatokyo.test";
const PW = process.env.E2E_ADMIN_PASSWORD || "Kanagawa-Wave-2026!";

function base32(s) {
  const a = "ABCDEFGHIJKLMNOPQRSTUVWXYZ234567";
  let bits = "";
  for (const c of s.replace(/=+$/, "")) bits += a.indexOf(c).toString(2).padStart(5, "0");
  const out = [];
  for (let i = 0; i + 8 <= bits.length; i += 8) out.push(parseInt(bits.slice(i, i + 8), 2));
  return Buffer.from(out);
}
function totp(secret, offset = 0) {
  const step = Math.floor(Date.now() / 1000 / 30) + offset;
  const buf = Buffer.alloc(8);
  buf.writeBigUInt64BE(BigInt(step));
  const h = crypto.createHmac("sha1", base32(secret)).update(buf).digest();
  const o = h[h.length - 1] & 15;
  return String((h.readUInt32BE(o) & 0x7fffffff) % 1e6).padStart(6, "0");
}

const errors = [];
function watch(page, label) {
  page.on("console", (m) => m.type() === "error" && !/status of 401/.test(m.text()) && errors.push(`[${label}] console: ${m.text()}`));
  page.on("pageerror", (e) => errors.push(`[${label}] pageerror: ${e.message}`));
}
const step = (msg) => console.log("✓", msg);
async function pick(select, code) {
  const value = await select.locator("option", { hasText: code + " " }).first().getAttribute("value");
  await select.selectOption(value);
}

(async () => {
  const browser = await chromium.launch();

  // Product photo: render a simple card and screenshot it to PNG bytes.
  const art = await browser.newPage({ viewport: { width: 400, height: 400 } });
  await art.setContent(`<div style="width:400px;height:400px;display:grid;place-items:center;background:linear-gradient(135deg,#fb923c,#c2410c);font:700 72px system-ui;color:white">151</div>`);
  const photo = path.join(OUT, "photo.png");
  await art.screenshot({ path: photo });
  await art.close();

  const ctx = await browser.newContext({ locale: "zh-TW", viewport: { width: 1360, height: 860 }, deviceScaleFactor: 2 });
  const page = await ctx.newPage();
  watch(page, "desktop");

  // 1. Login -> admin must enrol MFA
  await page.goto(BASE);
  await page.getByLabel("Email").fill(EMAIL);
  await page.getByLabel("密碼").fill(PW);
  await page.getByRole("button", { name: "登入" }).click();
  await page.getByText("管理員帳號必須啟用雙因素驗證").waitFor();
  step("admin is forced to set up 2FA");
  await page.getByRole("button", { name: "開始設定" }).click();
  await page.locator("img.qr").waitFor();
  await page.screenshot({ path: path.join(OUT, "01-mfa-setup.png") });
  const secret = (await page.locator(".mono").first().textContent()).trim();
  await page.getByLabel("2. 輸入 App 顯示的 6 位數字").fill(totp(secret));
  await page.getByRole("button", { name: "啟用雙因素驗證" }).click();
  await page.locator(".shell").waitFor();
  step("2FA enabled, inventory page shown");

  // 2. Logout + login again with code
  await page.goto(BASE + "/account");
  await page.getByRole("button", { name: "登出" }).click();
  await page.getByRole("heading", { name: "登入" }).waitFor();
  await page.waitForTimeout(1000); // like a person: start typing after the login screen settles
  await page.getByLabel("Email").fill(EMAIL);
  await page.getByLabel("密碼").fill(PW);
  await page.getByRole("button", { name: "登入" }).click();
  await page.getByLabel("驗證器 App 上的 6 位數字").fill(totp(secret, 1));
  await page.getByRole("button", { name: "驗證" }).click();
  await page.getByRole("heading", { name: "我的帳號" }).waitFor();  // returns to the page you were on
  step("login with password + TOTP works");

  // 3. Reload keeps session (refresh cookie)
  await page.reload();
  await page.getByRole("heading", { name: "我的帳號" }).waitFor();
  step("session survives page reload (httpOnly refresh cookie)");

  // 4. Create products
  async function createProduct(name, category, variants) {
    await page.goto(BASE + "/products/new");
    await page.getByLabel("商品名稱").fill(name);
    if (category) {
      page.once("dialog", (d) => d.accept(category));
      await page.getByRole("button", { name: "＋ 分類" }).click();
      await page.waitForTimeout(300);
    }
    for (let i = 0; i < variants.length; i++) {
      if (i > 0) await page.getByRole("button", { name: "＋ 新增規格" }).click();
      await page.getByLabel("SKU 編號").nth(i).fill(variants[i][0]);
      await page.getByLabel("規格", { exact: true }).nth(i).fill(variants[i][1]);
      await page.getByLabel("售價 (USD)").nth(i).fill(variants[i][2]);
    }
    await page.getByRole("button", { name: "建立商品" }).click();
    await page.waitForURL(/\/skus\/\d+/);
  }
  await createProduct("Pokémon Card 151 Booster Box", "Trading Cards", [["PK151-BOX-JP", "日版 Box", "89.99"], ["PK151-PACK-JP", "日版 單包", "6.99"]]);
  await createProduct("One Piece OP-09 Booster Box", null, [["OP09-BOX-JP", "日版 Box", "79.99"]]);
  step("products with variants created");

  // 5. Photo upload on current SKU (OP09)
  await page.locator('input[type="file"]').setInputFiles(photo);
  await page.locator(".gallery img").first().waitFor();
  step("product photo uploaded and displayed via signed URL");

  // 6. Opening stock: JP and US
  async function stockOp(op, loc, lines, reason) {
    await page.goto(BASE + "/stock/operations");
    await page.getByLabel("作業").selectOption({ label: op });
    await page.getByLabel("倉庫").selectOption({ label: loc });
    if (reason) await page.locator("label.field input").first().fill(reason);
    for (let i = 0; i < lines.length; i++) {
      if (i > 0) await page.getByRole("button", { name: "＋ 新增一行" }).click();
      await pick(page.locator("select").nth(2 + i), lines[i][0]);
      await page.getByLabel("數量").nth(i).fill(String(lines[i][1]));
      if (lines[i][2]) await page.getByLabel("單位成本 USD").nth(i).fill(lines[i][2]);
    }
    await page.getByRole("button", { name: /^確認/ }).click();
    await page.locator(".alert.ok").waitFor();
  }
  await stockOp("期初庫存", "日本倉", [["PK151-BOX-JP", 24, "52.40"], ["PK151-PACK-JP", 120, "3.10"], ["OP09-BOX-JP", 18, "48.00"]], "go-live stock take");
  await stockOp("期初庫存", "美國倉", [["PK151-BOX-JP", 6, "55.00"], ["OP09-BOX-JP", 4, "49.50"]], "");
  await stockOp("損壞報廢", "美國倉", [["OP09-BOX-JP", 1]], "box crushed in storage");
  step("opening stock JP/US and damage write-off posted");

  // 7. Bundle
  await page.goto(BASE + "/bundles/new");
  await page.getByLabel("組合 SKU 編號").fill("PK151-STARTER");
  await page.getByLabel("名稱").fill("151 Starter Set (Box + 5 packs)");
  await page.getByLabel("售價 (USD)").fill("119.00");
  await pick(page.locator("select").nth(1), "PK151-BOX-JP");
  await page.getByRole("button", { name: "＋ 加入 SKU" }).click();
  await pick(page.locator("select").nth(2), "PK151-PACK-JP");
  await page.getByLabel("數量").nth(1).fill("5");
  await page.getByRole("button", { name: "建立組合商品" }).click();
  await page.waitForURL(/\/skus\/\d+/);
  await page.getByText("組成 SKU").waitFor();
  step("bundle created");

  // 8. Stock count with a difference, approved
  await page.goto(BASE + "/stock/counts");
  await page.getByLabel("倉庫").selectOption({ label: "日本倉" });
  await page.getByRole("button", { name: "開始盤點" }).click();
  await page.waitForURL(/\/stock\/counts\/\d+/);
  await page.getByLabel("OP09-BOX-JP 盤點數量").fill("17");
  await page.getByLabel("PK151-BOX-JP 盤點數量").fill("24");
  await page.getByLabel("PK151-PACK-JP 盤點數量").fill("120");
  await page.getByRole("button", { name: "儲存" }).click();
  await page.getByText("已填 3 / 3").waitFor();
  await page.getByRole("button", { name: "送出待核准" }).click();
  await page.getByText("待核准").first().waitFor();
  page.once("dialog", (d) => d.accept());
  await page.getByRole("button", { name: "核准並調整庫存" }).click();
  await page.getByText("已核准").first().waitFor();
  await page.screenshot({ path: path.join(OUT, "04-stock-count.png"), fullPage: true });
  step("stock count submitted and approved (OP09 -1)");

  // 9. Inventory overview
  await page.goto(BASE + "/inventory");
  await page.getByText("PK151-STARTER").waitFor();
  await page.waitForTimeout(300);
  await page.screenshot({ path: path.join(OUT, "02-inventory-desktop.png"), fullPage: true });
  const rowText = await page.locator("tr", { hasText: "OP09-BOX-JP" }).innerText();
  const bundleRow = await page.locator("tr", { hasText: "PK151-STARTER" }).innerText();
  console.log("  OP09 row:", rowText.replace(/\s+/g, " "));
  console.log("  bundle row:", bundleRow.replace(/\s+/g, " "));

  // 10. SKU detail
  await page.locator("tr", { hasText: "OP09-BOX-JP" }).click();
  await page.getByText("庫存異動紀錄").waitFor();
  await page.waitForTimeout(300);
  await page.screenshot({ path: path.join(OUT, "03-sku-detail.png"), fullPage: true });
  step("SKU detail with lots, photo and movement history");

  // 11. Users: create JP staff
  await page.goto(BASE + "/users");
  await page.getByRole("button", { name: "＋ 新增使用者" }).click();
  await page.getByLabel("Email").fill("tokyo@megatokyo.test");
  await page.getByLabel("顯示名稱").fill("東京倉庫");
  await page.getByLabel("臨時密碼").fill("Harbor-Lights-2026");
  await page.getByRole("button", { name: "建立" }).click();
  await page.getByText("已建立 tokyo@megatokyo.test").waitFor();
  step("JP staff user created");

  // 12. Staff first login: forced password change, then limited menu
  const ctx2 = await browser.newContext({ ...devices["iPhone 13"], locale: "zh-TW" });
  const phone = await ctx2.newPage();
  watch(phone, "phone");
  await phone.goto(BASE);
  await phone.getByLabel("Email").fill("tokyo@megatokyo.test");
  await phone.getByLabel("密碼").fill("Harbor-Lights-2026");
  await phone.getByRole("button", { name: "登入" }).click();
  await phone.getByText("這是管理員設定的臨時密碼").waitFor();
  await phone.getByLabel("目前密碼").fill("Harbor-Lights-2026");
  await phone.getByLabel(/^新密碼/).fill("Shinjuku-Night-88");
  await phone.getByLabel("再輸入一次新密碼").fill("Shinjuku-Night-88");
  await phone.getByRole("button", { name: "更新密碼" }).click();
  await phone.locator(".shell").waitFor();
  const navText = await phone.locator(".bottom-nav").innerText();
  if (navText.includes("使用者")) throw new Error("JP staff should not see the users menu");
  await phone.waitForTimeout(300);
  await phone.screenshot({ path: path.join(OUT, "05-inventory-phone.png"), fullPage: true });
  step("JP staff: forced password change on phone, no admin menu");

  // JP staff cannot adjust the US warehouse: only JP in the location list
  await phone.goto(BASE + "/stock/operations");
  await phone.waitForTimeout(800);
  const locs = await phone.getByLabel("倉庫").locator("option").allTextContents();
  if (locs.join() !== "日本倉") throw new Error("JP staff should only see 日本倉, got " + locs);
  step("JP staff can only operate the Japan warehouse");

  // horizontal overflow check on phone
  for (const url of ["/inventory", "/stock/operations", "/stock/counts", "/account"]) {
    await phone.goto(BASE + url);
    await phone.waitForTimeout(400);
    const overflow = await phone.evaluate(() => document.documentElement.scrollWidth - window.innerWidth);
    if (overflow > 1) throw new Error(`horizontal overflow ${overflow}px on ${url}`);
  }
  step("no horizontal scrolling on phone pages");

  await browser.close();
  if (errors.length) {
    console.log("CONSOLE ERRORS:\n" + errors.join("\n"));
    process.exit(1);
  }
  console.log("ALL E2E CHECKS PASSED");
})().catch(async (e) => {
  console.error("E2E FAILED:", e.message);
  if (errors.length) console.log(errors.join("\n"));
  process.exit(1);
});
