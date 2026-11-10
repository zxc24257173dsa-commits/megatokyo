import { useState } from "react";
import { Link, useSearchParams } from "react-router-dom";
import { useQuery } from "@tanstack/react-query";
import { api } from "../api";
import { ColumnChart, Sparkline } from "../components/charts";
import { ErrorBox, Loading, fmtInt } from "../components/ui";
import { money } from "./ShowReportPages";

const TZ = "America/New_York";
const LEAD_WEEKS = 11 / 7; // supplier lead time (owner: 9-11 days, plan with 11)

interface Col { label: string; from: string; to: string; units: number; giveaway_units: number; orders: number; shows: number; sales: number | null; sales_per_show: number | null }
interface Item { key: string; label: string; sku_id: number | null; mapped: boolean; units: number[]; sales: number[] | null; total_units: number; total_sales: number | null }
interface Overview { columns: Col[]; items: Item[]; item_count: number; unmapped_items: number }
interface Mover {
  sku_id: number; sku_code: string; name: string; product_name: string; is_bundle: boolean;
  sold_recent: number; sold_prev: number; change_pct: number | null; trend: string; sales_recent: number | null;
  last_sale: string | null; demand_recent: number; weekly_demand: number; us_available: number; us_reserved: number;
  in_transit: number; weeks_of_cover: number | null; weekly_spark: number[];
}
interface Movers { weeks: number; recent_from: string; prev_from: string; to: string; skus: Mover[]; unmapped: { products: number; units: number } }

const TREND: Record<string, [string, string]> = {
  up: ["成長", "ok"], new: ["新開始賣", "info"], down: ["衰退", "warn"], stopped: ["停止銷售", "danger"],
  flat: ["持平", ""], low: ["量少", ""], none: ["沒有銷售", ""],
};

const today = () => new Date().toLocaleDateString("en-CA", { timeZone: TZ });
const shift = (iso: string, days: number) => {
  const d = new Date(iso + "T12:00:00Z");
  d.setUTCDate(d.getUTCDate() + days);
  return d.toISOString().slice(0, 10);
};
const PRESETS: Record<string, { label: string; range: () => [string, string, string] }> = {
  "30d": { label: "最近 30 天（每日）", range: () => [shift(today(), -29), today(), "day"] },
  "12w": { label: "最近 12 週", range: () => { const t = today(); const dow = (new Date(t + "T12:00:00Z").getUTCDay() + 6) % 7; return [shift(t, -dow - 77), t, "week"]; } },
  "12m": { label: "最近 12 個月", range: () => { const t = today(); const d = new Date(t.slice(0, 7) + "-01T12:00:00Z"); d.setUTCMonth(d.getUTCMonth() - 11); return [d.toISOString().slice(0, 10), t, "month"]; } },
};

export function TrendsPage() {
  const [params, setParams] = useSearchParams();
  const tab = params.get("tab") ?? "sales";
  return (
    <>
      <div className="page-head"><h1>銷售趨勢</h1></div>
      <div className="row" style={{ marginBottom: 12 }}>
        {[["sales", "銷售走勢"], ["movers", "商品動向與庫存"]].map(([k, l]) => (
          <button key={k} className={`btn sm ${tab === k ? "primary" : ""}`} onClick={() => setParams(k === "sales" ? {} : { tab: k })}>{l}</button>
        ))}
      </div>
      {tab === "movers" ? <MoversView /> : <SalesView />}
    </>
  );
}

function SalesView() {
  const [preset, setPreset] = useState("12w");
  const [group, setGroup] = useState("sku");
  const [metric, setMetric] = useState<"sales" | "units">("sales");
  const [from, to, gran] = PRESETS[preset].range();
  const q = useQuery({
    queryKey: ["trends", from, to, gran, group],
    queryFn: () => api<Overview>("/reports/trends", { params: { from, to, granularity: gran, group } }),
  });
  const d = q.data;
  const hasMoney = d ? d.columns.every((c) => c.sales !== null) : true;
  const m = hasMoney ? metric : "units";
  const fmt = (v: number) => (m === "sales" ? money(v) : fmtInt(v));
  const total = d?.columns.reduce((a, c) => ({ sales: a.sales + (c.sales ?? 0), units: a.units + c.units, orders: a.orders + c.orders, shows: a.shows + c.shows, give: a.give + c.giveaway_units }), { sales: 0, units: 0, orders: 0, shows: 0, give: 0 });

  return (
    <div className="card">
      <div className="row" style={{ marginBottom: 12 }}>
        {Object.entries(PRESETS).map(([k, p]) => <button key={k} className={`btn sm ${preset === k ? "primary" : ""}`} onClick={() => setPreset(k)}>{p.label}</button>)}
        {hasMoney && (
          <select value={metric} onChange={(e) => setMetric(e.target.value as "sales" | "units")} style={{ width: "auto" }} aria-label="指標">
            <option value="sales">成交額</option>
            <option value="units">件數</option>
          </select>
        )}
      </div>
      <ErrorBox error={q.error} />
      {q.isLoading || !d || !total ? <Loading /> : (
        <>
          <div className="tiles">
            {hasMoney && <div className="tile"><div className="label">商品成交額</div><div className="value">{money(total.sales)}</div><div className="sub">已付款訂單，不含運費、稅</div></div>}
            <div className="tile"><div className="label">售出件數</div><div className="value">{fmtInt(total.units)}</div><div className="sub">含贈品 {fmtInt(total.give)} 件</div></div>
            <div className="tile"><div className="label">訂單</div><div className="value">{fmtInt(total.orders)}</div><div className="sub">{fmtInt(total.shows)} 場直播</div></div>
            {hasMoney && <div className="tile"><div className="label">每場平均成交額</div><div className="value">{total.shows ? money(total.sales / total.shows) : "—"}</div><div className="sub">成交額 ÷ 直播場數</div></div>}
          </div>
          <h2>{m === "sales" ? "商品成交額" : "售出件數"}（{gran === "day" ? "每日" : gran === "week" ? "每週，週一開始" : "每月"}）</h2>
          <ColumnChart
            ariaLabel={m === "sales" ? "商品成交額長條圖" : "售出件數長條圖"}
            format={fmt}
            bars={d.columns.map((c) => ({
              label: gran === "month" ? c.label.slice(5) + "月" : gran === "week" ? c.label.split("–")[0] : c.label,
              value: m === "sales" ? (c.sales ?? 0) : c.units,
              tip: (
                <>
                  <b>{c.label}</b>
                  {c.sales !== null && <div>成交額 {money(c.sales)}</div>}
                  <div>{fmtInt(c.units)} 件・{fmtInt(c.orders)} 筆訂單</div>
                  <div>{fmtInt(c.shows)} 場直播{c.sales_per_show !== null ? `・每場 ${money(c.sales_per_show)}` : ""}</div>
                </>
              ),
            }))}
          />

          <div className="row" style={{ margin: "18px 0 8px", alignItems: "center" }}>
            <h2 style={{ margin: 0, flex: 1 }}>{group === "sku" ? "各商品" : group === "product" ? "各產品" : "各分類"}</h2>
            <select value={group} onChange={(e) => setGroup(e.target.value)} style={{ width: "auto" }} aria-label="分組">
              <option value="sku">依 SKU</option>
              <option value="product">依產品</option>
              <option value="category">依分類</option>
            </select>
          </div>
          {d.unmapped_items > 0 && (
            <p className="small muted" style={{ marginTop: 0 }}>{fmtInt(d.unmapped_items)} 種商品還沒對應 SKU（以 Whatnot 商品名稱顯示）。<Link to="/orders?tab=unmapped">去對應 →</Link></p>
          )}
          {!d.items.length ? <div className="empty">這段期間沒有銷售</div> : (
            <div className="table-wrap">
              <table className="data responsive">
                <thead><tr><th>商品</th><th className="num">件數</th>{hasMoney && <th className="num">成交額</th>}{hasMoney && <th className="num">佔比</th>}<th>走勢（件數）</th></tr></thead>
                <tbody>
                  {d.items.map((it) => (
                    <tr key={it.key}>
                      <td className="lead">
                        {it.sku_id ? <Link to={`/skus/${it.sku_id}`}>{it.label}</Link> : it.label}
                        {!it.mapped && <> <span className="badge warn">待對應</span></>}
                      </td>
                      <td data-label="件數" className="num">{fmtInt(it.total_units)}</td>
                      {hasMoney && <td data-label="成交額" className="num">{money(it.total_sales)}</td>}
                      {hasMoney && <td data-label="佔比" className="num">{total.sales ? `${(((it.total_sales ?? 0) / total.sales) * 100).toFixed(1)}%` : "—"}</td>}
                      <td data-label="走勢"><Sparkline values={it.units} /></td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          )}
          <p className="small muted">只計已付款訂單，依付款（下單）日期、美東時間。退款不從這裡扣除（見損益表）。</p>
        </>
      )}
    </div>
  );
}

function MoversView() {
  const [weeks, setWeeks] = useState(4);
  const [onlySelling, setOnlySelling] = useState(true);
  const q = useQuery({ queryKey: ["movers", weeks], queryFn: () => api<Movers>("/reports/trends/movers", { params: { weeks } }) });
  const d = q.data;
  const rows = (d?.skus ?? []).filter((r) => !onlySelling || r.trend !== "none" || r.demand_recent > 0);
  const short = rows.filter((r) => r.weeks_of_cover !== null && r.weeks_of_cover < LEAD_WEEKS);

  return (
    <div className="card">
      <div className="row" style={{ marginBottom: 12, alignItems: "center" }}>
        <span className="small">比較最近</span>
        <select value={weeks} onChange={(e) => setWeeks(Number(e.target.value))} style={{ width: "auto" }} aria-label="比較週數">
          {[1, 2, 4, 8, 12].map((w) => <option key={w} value={w}>{w} 週</option>)}
        </select>
        <span className="small">和再之前同樣長的期間</span>
        <label className="row small" style={{ marginLeft: "auto" }}><input type="checkbox" checked={onlySelling} onChange={(e) => setOnlySelling(e.target.checked)} />只看有銷售的</label>
      </div>
      <ErrorBox error={q.error} />
      {q.isLoading || !d ? <Loading /> : (
        <>
          <p className="small muted" style={{ marginTop: 0 }}>
            最近 {d.weeks} 週：{d.recent_from} – {d.to}；之前：{d.prev_from} 起。「每週需求」是實際從庫存拿走的量（組合商品拆成組成 SKU、拆盒算盒子），補貨建議（Phase 10）會用這個數字。
            運送中的庫存<b>不算</b>美國可售。
          </p>
          {short.length > 0 && (
            <div className="alert warn" style={{ marginBottom: 12 }}>
              {short.length} 個 SKU 的美國可售庫存撐不到 11 天（供應商交期）：{short.slice(0, 6).map((r) => r.sku_code).join("、")}{short.length > 6 ? "…" : ""}
            </div>
          )}
          {d.unmapped.units > 0 && (
            <p className="small muted">另有 {fmtInt(d.unmapped.products)} 種、{fmtInt(d.unmapped.units)} 件商品還沒對應 SKU，不在下表；<Link to="/orders?tab=unmapped">對應後</Link>就會計入。</p>
          )}
          {!rows.length ? <div className="empty">沒有資料</div> : (
            <div className="table-wrap">
              <table className="data responsive">
                <thead>
                  <tr><th>SKU</th><th>動向</th><th className="num">最近 {d.weeks} 週售出</th><th className="num">之前</th><th className="num">變化</th><th className="num">每週需求</th><th className="num">美國可售</th><th className="num">運送中</th><th className="num">可撐</th><th>12 週需求</th></tr>
                </thead>
                <tbody>
                  {rows.map((r) => {
                    const lowCover = r.weeks_of_cover !== null && r.weeks_of_cover < LEAD_WEEKS;
                    return (
                      <tr key={r.sku_id}>
                        <td className="lead"><Link to={`/skus/${r.sku_id}`} className="mono">{r.sku_code}</Link><div className="small muted" style={{ fontWeight: 400 }}>{r.name}{r.is_bundle ? "（組合）" : ""}</div></td>
                        <td data-label="動向"><span className={`badge ${TREND[r.trend][1]}`}>{TREND[r.trend][0]}</span></td>
                        <td data-label={`最近 ${d.weeks} 週售出`} className="num">{fmtInt(r.sold_recent)}{r.sales_recent !== null && r.sales_recent > 0 && <div className="small muted">{money(r.sales_recent)}</div>}</td>
                        <td data-label="之前" className="num">{fmtInt(r.sold_prev)}</td>
                        <td data-label="變化" className="num">{r.change_pct === null ? "—" : `${r.change_pct > 0 ? "+" : ""}${r.change_pct.toFixed(0)}%`}</td>
                        <td data-label="每週需求" className="num">{r.weekly_demand ? r.weekly_demand.toFixed(1) : "—"}</td>
                        <td data-label="美國可售" className="num">{r.is_bundle ? "—" : fmtInt(r.us_available)}</td>
                        <td data-label="運送中" className="num">{r.in_transit ? fmtInt(r.in_transit) : "—"}</td>
                        <td data-label="可撐" className="num">
                          {r.weeks_of_cover === null ? "—" : <span className={lowCover ? "badge danger" : undefined}>{r.weeks_of_cover < 1 ? `${Math.round(r.weeks_of_cover * 7)} 天` : `${r.weeks_of_cover.toFixed(1)} 週`}</span>}
                        </td>
                        <td data-label="12 週需求"><Sparkline values={r.weekly_spark} /></td>
                      </tr>
                    );
                  })}
                </tbody>
              </table>
            </div>
          )}
        </>
      )}
    </div>
  );
}
