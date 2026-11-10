import { useMemo, useState, type FormEvent } from "react";
import { Link } from "react-router-dom";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { api, downloadFile, newIdempotencyKey } from "../api";
import { useAuth } from "../auth";
import { ErrorBox, Field, Loading, fmtDate, fmtDateTime, fmtInt } from "../components/ui";
import { money } from "./ShowReportPages";

const TZ = "America/New_York";

interface Lines {
  sales: number;
  discounts: number;
  refunds: number;
  net_sales: number;
  commission: number;
  processing: number;
  shipping: number;
  platform_costs: number;
  cogs: number;
  gross_profit: number;
  gross_margin_pct: number | null;
  other: number;
  opex: number;
  opex_by_category: Record<string, number>;
  net_profit: number;
  net_margin_pct: number | null;
}

interface Pnl {
  columns: (Lines & { label: string; from: string; to: string; period_status: string | null })[];
  total: Lines;
  expense_categories: { code: string; name: string }[];
  completeness: {
    paid_orders: number;
    orders_without_fees: number;
    units_without_cogs: number;
    lines_with_issues: number;
    transactions_to_review: number;
    financial_report_until: string | null;
    complete: boolean;
  };
}

const today = () => new Date().toLocaleDateString("en-CA", { timeZone: TZ });
const shift = (iso: string, days: number) => {
  const d = new Date(iso + "T12:00:00Z");
  d.setUTCDate(d.getUTCDate() + days);
  return d.toISOString().slice(0, 10);
};
const monthStart = (iso: string, back = 0) => {
  const d = new Date(iso.slice(0, 7) + "-01T12:00:00Z");
  d.setUTCMonth(d.getUTCMonth() - back);
  return d.toISOString().slice(0, 10);
};
const monthEnd = (iso: string) => shift(monthStart(shift(monthStart(iso), 32)), -1);
const pct = (v: number | null) => (v === null ? "—" : `${v.toFixed(1)}%`);

const PRESETS: { key: string; label: string; range: () => [string, string, string] }[] = [
  { key: "this_month", label: "本月", range: () => [monthStart(today()), today(), "week"] },
  { key: "last_month", label: "上個月", range: () => [monthStart(today(), 1), monthEnd(monthStart(today(), 1)), "week"] },
  { key: "12w", label: "最近 12 週", range: () => { const t = today(); const d = new Date(t + "T12:00:00Z"); const mon = shift(t, -((d.getUTCDay() + 6) % 7)); return [shift(mon, -77), t, "week"]; } },
  { key: "12m", label: "最近 12 個月", range: () => [monthStart(today(), 11), today(), "month"] },
];

function Cell({ v, strong }: { v: number; strong?: boolean }) {
  return <td className="num" style={{ color: v < 0 ? "var(--danger)" : undefined, fontWeight: strong ? 700 : undefined }}>{v === 0 ? "—" : money(v)}</td>;
}

export function PnlPage() {
  const { can } = useAuth();
  const qc = useQueryClient();
  const [preset, setPreset] = useState("12m");
  const init = PRESETS.find((p) => p.key === "12m")!.range();
  const [from, setFrom] = useState(init[0]);
  const [to, setTo] = useState(init[1]);
  const [gran, setGran] = useState(init[2]);
  const q = useQuery({
    queryKey: ["pnl", from, to, gran],
    queryFn: () => api<Pnl>("/reports/pnl", { params: { from, to, granularity: gran } }),
  });
  const period = useMutation({
    mutationFn: ({ p, close }: { p: string; close: boolean }) => api(`/accounting-periods/${p}/${close ? "close" : "reopen"}`, { method: "POST" }),
    onSuccess: () => void qc.invalidateQueries({ queryKey: ["pnl"] }),
  });
  const [dlErr, setDlErr] = useState<unknown>(null);
  const choose = (k: string) => {
    const [a, b, g] = PRESETS.find((p) => p.key === k)!.range();
    setPreset(k);
    setFrom(a);
    setTo(b);
    setGran(g);
  };
  const d = q.data;
  const catNames = useMemo(() => Object.fromEntries((d?.expense_categories ?? []).map((c) => [c.code, c.name])), [d]);
  const cats = useMemo(() => {
    const used = new Set<string>();
    d?.columns.forEach((c) => Object.keys(c.opex_by_category).forEach((k) => used.add(k)));
    return (d?.expense_categories ?? []).map((c) => c.code).filter((c) => used.has(c));
  }, [d]);
  const cols = d ? [...d.columns, { ...d.total, label: "合計", period_status: null, from, to }] : [];
  const lastMonthDone = gran === "month" && can("period.close");

  const ROWS: [keyof Lines, string, boolean?][] = [
    ["sales", "商品銷售額"], ["discounts", "優惠券"], ["refunds", "退款"], ["net_sales", "淨銷售額", true],
    ["commission", "Whatnot 佣金"], ["processing", "金流手續費"], ["shipping", "賣家付的運費"],
    ["cogs", "商品成本"], ["gross_profit", "毛利", true],
  ];

  return (
    <>
      <div className="page-head">
        <h1>損益表</h1>
        <button className="btn" onClick={() => { setDlErr(null); downloadFile("/reports/pnl/entries.csv", { from, to }, `megatokyo-ledger-${from}-${to}.csv`).catch(setDlErr); }}>匯出明細 CSV</button>
      </div>
      <div className="card">
        <div className="row" style={{ marginBottom: 12, alignItems: "end" }}>
          {PRESETS.map((p) => <button key={p.key} className={`btn sm ${preset === p.key ? "primary" : ""}`} onClick={() => choose(p.key)}>{p.label}</button>)}
          <label className="small" style={{ flex: "0 1 150px" }}>從<input type="date" value={from} onChange={(e) => (setPreset(""), setFrom(e.target.value))} /></label>
          <label className="small" style={{ flex: "0 1 150px" }}>到<input type="date" value={to} onChange={(e) => (setPreset(""), setTo(e.target.value))} /></label>
          <select value={gran} onChange={(e) => setGran(e.target.value)} style={{ width: "auto" }} aria-label="分組">
            <option value="week">每週（週一開始）</option>
            <option value="month">每月</option>
            <option value="total">整段合計</option>
          </select>
        </div>
        <ErrorBox error={q.error ?? period.error ?? dlErr} />
        {q.isLoading || !d ? <Loading /> : (
          <>
            <div className="tiles">
              <div className="tile"><div className="label">淨銷售額</div><div className="value">{money(d.total.net_sales)}</div><div className="sub">{fmtInt(d.completeness.paid_orders)} 筆已付款訂單</div></div>
              <div className="tile"><div className="label">毛利</div><div className="value">{money(d.total.gross_profit)}</div><div className="sub">毛利率 {pct(d.total.gross_margin_pct)}</div></div>
              <div className="tile"><div className="label">營運費用</div><div className="value">{money(d.total.opex)}</div><div className="sub"><Link to="/expenses">記錄費用 →</Link></div></div>
              <div className="tile"><div className="label">淨利</div><div className="value" style={{ color: d.total.net_profit < 0 ? "var(--danger)" : undefined }}>{money(d.total.net_profit)}</div><div className="sub">淨利率 {pct(d.total.net_margin_pct)}</div></div>
            </div>
            {!d.completeness.complete && (
              <div className="alert warn" style={{ marginBottom: 12 }}>
                <b>這段期間的數字還沒完整：</b>
                <ul style={{ margin: "6px 0 0", paddingLeft: 20 }}>
                  {d.completeness.orders_without_fees > 0 && <li>{fmtInt(d.completeness.orders_without_fees)} 筆訂單的 Whatnot 費用還沒進財務報表{d.completeness.financial_report_until ? `（財務報表已同步到 ${fmtDateTime(d.completeness.financial_report_until, TZ)}）` : "（尚未同步財務報表）"}。</li>}
                  {d.completeness.units_without_cogs > 0 && <li>{fmtInt(d.completeness.units_without_cogs)} 件已售出但還沒出貨，商品成本會在出貨時認列。</li>}
                  {d.completeness.lines_with_issues > 0 && <li>{fmtInt(d.completeness.lines_with_issues)} 件訂單商品需要處理，請到 <Link to="/orders?tab=exceptions">訂單 › 需要處理</Link>。</li>}
                  {d.completeness.transactions_to_review > 0 && <li>{fmtInt(d.completeness.transactions_to_review)} 筆 Whatnot 交易需要人工確認（Whatnot 串接頁）。</li>}
                </ul>
              </div>
            )}
            <div className="table-wrap">
              <table className="data pnl">
                <thead>
                  <tr>
                    <th></th>
                    {cols.map((c) => (
                      <th key={c.label} className="num" style={{ whiteSpace: "nowrap" }}>
                        {c.label}
                        {c.period_status === "closed" && <div><span className="badge">已結帳</span></div>}
                      </th>
                    ))}
                  </tr>
                </thead>
                <tbody>
                  {ROWS.map(([k, label, strong]) => (
                    <tr key={k} className={strong ? "subtotal" : undefined}>
                      <td style={{ whiteSpace: "nowrap", fontWeight: strong ? 700 : undefined }}>{label}</td>
                      {cols.map((c) => <Cell key={c.label} v={c[k] as number} strong={strong} />)}
                    </tr>
                  ))}
                  <tr><td className="small muted">毛利率</td>{cols.map((c) => <td key={c.label} className="num small muted">{pct(c.gross_margin_pct)}</td>)}</tr>
                  {cols.some((c) => c.other !== 0) && <tr><td>其他收入／支出</td>{cols.map((c) => <Cell key={c.label} v={c.other} />)}</tr>}
                  {cats.map((code) => (
                    <tr key={code}><td style={{ whiteSpace: "nowrap", paddingLeft: 20 }}>{catNames[code] ?? code}</td>{cols.map((c) => <Cell key={c.label} v={c.opex_by_category[code] ?? 0} />)}</tr>
                  ))}
                  <tr><td style={{ whiteSpace: "nowrap" }}>營運費用合計</td>{cols.map((c) => <Cell key={c.label} v={c.opex} />)}</tr>
                  <tr className="subtotal"><td style={{ fontWeight: 700 }}>淨利</td>{cols.map((c) => <Cell key={c.label} v={c.net_profit} strong />)}</tr>
                  <tr><td className="small muted">淨利率</td>{cols.map((c) => <td key={c.label} className="num small muted">{pct(c.net_margin_pct)}</td>)}</tr>
                  {lastMonthDone && (
                    <tr>
                      <td className="small muted">月結</td>
                      {d.columns.map((c) => {
                        const finished = c.label < today().slice(0, 7);
                        return (
                          <td key={c.label} className="num">
                            {finished && (c.period_status === "closed"
                              ? <button className="btn sm" disabled={period.isPending} onClick={() => confirm(`重新開啟 ${c.label}？之後可以再記入這個月的費用。`) && period.mutate({ p: c.label, close: false })}>重新開啟</button>
                              : <button className="btn sm" disabled={period.isPending} onClick={() => confirm(`結帳 ${c.label}？結帳後這個月的數字不會再變動；之後才進來的訂單或費用會記在當月。`) && period.mutate({ p: c.label, close: true })}>結帳</button>)}
                          </td>
                        );
                      })}
                      <td></td>
                    </tr>
                  )}
                </tbody>
              </table>
            </div>
            <p className="small muted">
              美東時間。營收在買家付款（下單）時認列；Whatnot 費用來自財務報表；商品成本依採購批次先進先出，在出貨時認列。買家付的運費與銷售稅不計入營收。
              各場直播的明細見 <Link to="/reports/shows">直播損益</Link>。
            </p>
          </>
        )}
      </div>
    </>
  );
}

// ---------------- operating expenses ----------------

interface Expense {
  id: number;
  expense_date: string;
  category: string;
  category_name: string | null;
  amount: number;
  currency: string;
  fx_quote: number | null;
  amount_usd: number;
  vendor: string | null;
  description: string | null;
  livestream_id: number | null;
  livestream_title: string | null;
  receipt_url: string | null;
  voided_at: string | null;
  void_reason: string | null;
}

interface ShowOpt {
  id: number;
  title: string | null;
  scheduled_at: string | null;
}

export function ExpensesPage() {
  const { can } = useAuth();
  const qc = useQueryClient();
  const cats = useQuery({ queryKey: ["expense-categories"], queryFn: () => api<{ code: string; name: string }[]>("/expense-categories") });
  const shows = useQuery({ queryKey: ["livestreams"], queryFn: () => api<ShowOpt[]>("/livestreams"), enabled: can("orders.view") });
  const list = useQuery({ queryKey: ["expenses"], queryFn: () => api<Expense[]>("/expenses") });
  const [f, setF] = useState({ expense_date: today(), category: "", amount: "", currency: "USD", fx_quote: "", vendor: "", description: "", livestream_id: "" });
  const [file, setFile] = useState<File | null>(null);
  const [key, setKey] = useState(newIdempotencyKey);
  const [done, setDone] = useState<Expense | null>(null);
  const set = (k: keyof typeof f, v: string) => setF((x) => ({ ...x, [k]: v }));
  const usd = f.currency === "USD" ? Number(f.amount || 0) : f.fx_quote ? Number(f.amount || 0) / Number(f.fx_quote) : null;

  const save = useMutation({
    mutationFn: () => {
      const form = new FormData();
      Object.entries(f).forEach(([k, v]) => v !== "" && form.append(k, v));
      if (file) form.append("receipt", file);
      return api<Expense>("/expenses", { method: "POST", form, idempotencyKey: key });
    },
    onSuccess: (e) => {
      setDone(e);
      setKey(newIdempotencyKey());
      setF((x) => ({ ...x, amount: "", vendor: "", description: "", livestream_id: "" }));
      setFile(null);
      void qc.invalidateQueries({ queryKey: ["expenses"] });
      void qc.invalidateQueries({ queryKey: ["pnl"] });
    },
  });
  const voidIt = useMutation({
    mutationFn: ({ id, reason }: { id: number; reason: string }) => api(`/expenses/${id}/void`, { body: { reason } }),
    onSuccess: () => {
      void qc.invalidateQueries({ queryKey: ["expenses"] });
      void qc.invalidateQueries({ queryKey: ["pnl"] });
    },
  });

  return (
    <>
      <div className="page-head"><h1>營運費用</h1></div>
      <form className="card" onSubmit={(e: FormEvent) => (e.preventDefault(), setDone(null), save.mutate())}>
        <h2>記錄一筆費用</h2>
        <ErrorBox error={save.error} />
        {done && <div className="alert ok">已記錄：{done.category_name} {money(done.amount_usd)}{done.currency !== "USD" ? `（${done.currency} ${done.amount.toLocaleString()}）` : ""}</div>}
        <div className="grid-2">
          <Field label="日期"><input type="date" required value={f.expense_date} max={today()} onChange={(e) => set("expense_date", e.target.value)} /></Field>
          <Field label="類別">
            <select required value={f.category} onChange={(e) => set("category", e.target.value)}>
              <option value="">請選擇…</option>
              {cats.data?.map((c) => <option key={c.code} value={c.code}>{c.name}</option>)}
            </select>
          </Field>
          <Field label="金額">
            <div className="row" style={{ flexWrap: "nowrap" }}>
              <input inputMode="decimal" required value={f.amount} onChange={(e) => set("amount", e.target.value)} placeholder="0" style={{ flex: 1 }} />
              <select value={f.currency} onChange={(e) => set("currency", e.target.value)} style={{ width: "auto" }} aria-label="幣別">
                <option>USD</option><option>JPY</option><option>TWD</option>
              </select>
            </div>
          </Field>
          {f.currency !== "USD" ? (
            <Field label={`匯率：1 USD = ? ${f.currency}`} hint={usd !== null && Number.isFinite(usd) && usd > 0 ? `約 ${money(usd)}` : f.currency === "JPY" ? "例：150" : "例：32"}>
              <input inputMode="decimal" required value={f.fx_quote} onChange={(e) => set("fx_quote", e.target.value)} />
            </Field>
          ) : <div />}
          <Field label="廠商／付款對象（選填）"><input value={f.vendor} maxLength={120} onChange={(e) => set("vendor", e.target.value)} /></Field>
          <Field label="屬於哪一場直播（選填）" hint="例如主播費；會算進那場直播的淨利">
            <select value={f.livestream_id} onChange={(e) => set("livestream_id", e.target.value)}>
              <option value="">不屬於特定直播</option>
              {shows.data?.map((s) => <option key={s.id} value={s.id}>{fmtDate(s.scheduled_at, TZ)} {s.title ?? "（未命名）"}</option>)}
            </select>
          </Field>
          <Field label="說明（選填）"><input value={f.description} maxLength={2000} onChange={(e) => set("description", e.target.value)} /></Field>
          <Field label="收據（選填）" hint="照片或 PDF，10 MB 以內">
            <input type="file" accept="image/jpeg,image/png,image/webp,application/pdf" onChange={(e) => setFile(e.target.files?.[0] ?? null)} />
          </Field>
        </div>
        <div className="row">
          <button className="btn primary" disabled={save.isPending}>{save.isPending ? "儲存中…" : "儲存"}</button>
          <span className="small muted">費用儲存後不能修改；記錯了請「作廢」再重新記錄，兩筆都會保留紀錄。已結帳的月份不能再記入。</span>
        </div>
      </form>

      <div className="card">
        <h2>最近 90 天</h2>
        <ErrorBox error={list.error ?? voidIt.error} />
        {list.isLoading ? <Loading /> : !list.data?.length ? <div className="empty">還沒有費用紀錄</div> : (
          <div className="table-wrap">
            <table className="data responsive">
              <thead><tr><th>日期</th><th>類別</th><th>說明</th><th className="num">金額</th><th className="num">USD</th><th>收據</th><th></th></tr></thead>
              <tbody>
                {list.data.map((e) => (
                  <tr key={e.id} style={e.voided_at ? { opacity: 0.55 } : undefined}>
                    <td className="lead small">{e.expense_date}</td>
                    <td data-label="類別">{e.category_name ?? e.category}{e.voided_at && <> <span className="badge">已作廢</span></>}</td>
                    <td data-label="說明" className="small">
                      {[e.vendor, e.description].filter(Boolean).join("・") || "—"}
                      {e.livestream_title && <div className="muted">直播：{e.livestream_title}</div>}
                      {e.voided_at && <div className="muted">作廢原因：{e.void_reason}</div>}
                    </td>
                    <td data-label="金額" className="num">{e.currency === "USD" ? money(e.amount) : `${e.currency} ${e.amount.toLocaleString()}`}{e.fx_quote && <div className="small muted">1 USD = {e.fx_quote}</div>}</td>
                    <td data-label="USD" className="num">{money(e.amount_usd)}</td>
                    <td data-label="收據">{e.receipt_url ? <a href={e.receipt_url} target="_blank" rel="noreferrer">查看</a> : "—"}</td>
                    <td>
                      {!e.voided_at && (
                        <button className="btn sm danger" disabled={voidIt.isPending} onClick={() => {
                          const reason = prompt("作廢原因（例如：金額記錯）");
                          if (reason && reason.trim().length >= 2) voidIt.mutate({ id: e.id, reason });
                        }}>作廢</button>
                      )}
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        )}
      </div>
    </>
  );
}
