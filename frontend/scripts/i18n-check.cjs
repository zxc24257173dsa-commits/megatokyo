// Lists every t("...") key in src/ and checks that src/locales/en.ts translates each one, keeps the same
// {placeholders}, and has no leftovers. Usage: node scripts/i18n-check.cjs [--keys]  (exit 1 on problems)
const ts = require("typescript");
const fs = require("fs");
const path = require("path");

function files(dir) {
  return fs.readdirSync(dir, { withFileTypes: true }).flatMap((e) =>
    e.isDirectory() ? files(path.join(dir, e.name)) : /\.tsx?$/.test(e.name) ? [path.join(dir, e.name)] : []);
}
const keys = new Map();
for (const f of files("src")) {
  if (f.includes(`${path.sep}locales${path.sep}`)) continue;
  const sf = ts.createSourceFile(f, fs.readFileSync(f, "utf8"), ts.ScriptTarget.Latest, true, ts.ScriptKind.TSX);
  (function walk(n) {
    if (ts.isCallExpression(n) && ts.isIdentifier(n.expression) && n.expression.text === "t" && n.arguments.length) {
      const a = n.arguments[0];
      if (ts.isStringLiteral(a) || ts.isNoSubstitutionTemplateLiteral(a)) keys.set(a.text, (keys.get(a.text) || 0) + 1);
    }
    n.forEachChild(walk);
  })(sf);
}
if (process.argv.includes("--keys")) {
  for (const k of keys.keys()) console.log(JSON.stringify(k));
  process.exit(0);
}
const enSrc = fs.readFileSync("src/locales/en.ts", "utf8");
const enSf = ts.createSourceFile("en.ts", enSrc, ts.ScriptTarget.Latest, true);
const en = new Map();
const server = new Set();
(function walk(n, inServer) {
  if (ts.isCallExpression(n) && n.expression.getText(enSf) === "Object.assign") inServer = true;  // server-sent keys
  if (ts.isPropertyAssignment(n) && (ts.isStringLiteral(n.name)) && ts.isStringLiteral(n.initializer)) {
    en.set(n.name.text, n.initializer.text);
    if (inServer) server.add(n.name.text);
  }
  n.forEachChild((c) => walk(c, inServer));
})(enSf, false);
const ph = (s) => (s.match(/\{\w+\}/g) || []).sort().join(",");
let bad = 0;
for (const k of keys.keys()) {
  if (!en.has(k)) { console.log("MISSING", JSON.stringify(k)); bad++; }
  else if (ph(k) !== ph(en.get(k))) { console.log("PLACEHOLDERS", JSON.stringify(k), "->", JSON.stringify(en.get(k))); bad++; }
}
for (const k of en.keys()) if (!keys.has(k) && !server.has(k)) console.log("UNUSED", JSON.stringify(k));
for (const k of server) if (ph(k) !== ph(en.get(k))) { console.log("PLACEHOLDERS", JSON.stringify(k)); bad++; }
console.log(`${keys.size} keys, ${en.size} translations, ${bad} problems`);
process.exit(bad ? 1 : 0);
