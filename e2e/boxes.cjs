// Box opening + SKU correction UI check (dev server; demo box GC-HEROINES-BOX with contents and stock).
const { chromium } = require("playwright");
const path = require("path");
const BASE = process.env.E2E_BASE || "http://localhost:8000";
const OUT = process.env.E2E_OUT || require("os").tmpdir();
const BOX_ID = process.env.E2E_BOX_ID;
(async () => {
  const b = await chromium.launch();
  const p = await (await b.newContext({ viewport: { width: 1360, height: 900 }, deviceScaleFactor: 2 })).newPage();
  const errors = [];
  p.on("pageerror", (e) => errors.push(e.message));
  await p.goto(BASE);
  await p.getByLabel("Email").fill("taipei@megatokyo.test");
  await p.getByLabel("密碼").fill("Formosa-Harbor-2026");
  await p.getByRole("button", { name: "登入" }).click();
  await p.locator(".shell").waitFor();
  await p.goto(`${BASE}/skus/${BOX_ID}`);
  await p.getByText("盒內內容（拆盒）").waitFor();
  await p.getByLabel("拆幾盒").fill("1");
  p.once("dialog", (d) => d.accept());
  await p.getByRole("button", { name: "拆盒", exact: true }).click();
  await p.getByText(/已拆 1 盒，取出 4 件/).waitFor();
  await p.waitForTimeout(500);
  await p.screenshot({ path: path.join(OUT, "p5-06-box-open.png"), fullPage: true });
  console.log("✓ opened 1 box -> 4 units (cost split equally)");
  await p.goto(`${BASE}/orders?search=PERONA`);
  await p.locator("tr.click").first().click();
  await p.getByRole("button", { name: "更改 SKU" }).first().click();
  await p.getByText("會套用到這個 Whatnot 商品的所有訂單").waitFor();
  await p.screenshot({ path: path.join(OUT, "p5-07-change-sku.png"), fullPage: true });
  console.log("✓ change-SKU panel shown on a mapped line");
  await b.close();
  if (errors.length) { console.log(errors.join("\n")); process.exit(1); }
  console.log("BOX/REMAP UI CHECK PASSED");
})().catch((e) => { console.error("FAILED:", e.message); process.exit(1); });
