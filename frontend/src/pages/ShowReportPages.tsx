import { useState } from "react";
import { Link, useNavigate, useParams } from "react-router-dom";
import { useQuery } from "@tanstack/react-query";
import { api } from "../api";
import { ErrorBox, Loading, fmtDateTime, fmtInt } from "../components/ui";
import { t } from "../i18n";

const TZ = "America/New_York"; // business timezone (owner decision)

interface Summary {
  sales: number;
  discounts: number;
  refunds: number;
  commission: number;
  processing: number;
  shipping: number;
  cogs: number;
  other: number;
  net_sales: number;
  platform_costs: number;
  gross_profit: number;
  margin_pct: number | null;
  opex?: number;
  net_profit?: number;
}

interface Coverage {
  paid_orders: number;
  giveaways: number;
  units: number;
  orders_without_fees: number;
  units_without_cogs: number;
  lines_with_issues: number;
  complete: boolean;
}

interface ShowRow extends Summary, Coverage {
  id: number;
  title: string | null;
  status: string;
  start_at: string | null;
  stream_minutes: number | null;
  profit_per_hour: number | null;
  whatnot_sales: number | null;
  whatnot_estimated_earnings: number | null;
}

interface SkuLine {
  sku_id: number | null;
  sku_code: string | null;
  name: string | null;
  mapped: boolean;
  units: number;
  units_without_cogs: number;
  sales: number;
  net_sales: number;
  platform_costs: number;
  cogs: number;
  gross_profit: number;
  margin_pct: number | null;
}

interface ShowDetail {
  livestream: { id: number; external_id: string | null; title: string | null; status: string; analytics_status: string | null; started_at: string | null; scheduled_at: string | null; ended_at: string | null };
  summary: Summary;
  coverage: Coverage;
  stream_minutes: number | null;
  per_hour: { sales: number; gross_profit: number } | null;
  whatnot_metrics: Record<string, number>;
  comparison: { whatnot_sales: number | null; ours_sales: number; whatnot_estimated_earnings: number | null; ours_earnings: number; whatnot_orders: number | null; ours_orders: number } | null;
  skus: SkuLine[];
  unallocated: Record<string, number>;
  refunds: { external_order_id: string | null; transaction_type: string; amount: string; completed_at: string; message: string | null }[];
  needs_review: { external_id: string; transaction_type: string; external_order_id: string | null; amount: string; status: string; detail: string | null; upstream_changed: boolean }[];
  orders_without_fees: { id: number; external_order_id: string; item_subtotal: string; status: string; ordered_at: string }[];
}

/** Signed money: "−$10.40" for costs, so a column of costs reads naturally. */
export function money(v: number | string | null | undefined, opts: { plus?: boolean } = {}) {
  if (v === null || v === undefined) return "—";
  const n = Number(v);
  const s = "$" + Math.abs(n).toLocaleString("en-US", { minimumFractionDigits: 2, maximumFractionDigits: 2 });
  return n < 0 ? "−" + s : opts.plus && n > 0 ? "+" + s : s;
}

function Money({ v, strong }: { v: number | null | undefined; strong?: boolean }) {
  const n = Number(v ?? 0);
  return <span style={{ color: n < 0 ? "var(--danger)" : undefined, fontWeight: strong ? 700 : undefined }}>{money(v)}</span>;
}

const pct = (v: number | null | undefined) => (v === null || v === undefined ? "—" : `${v.toFixed(1)}%`);
const hours = (m: number | null) => (m ? t("{0} 小時 {1} 分", [Math.floor(m / 60), m % 60]) : "—");

function CoverageBadges({ c }: { c: Coverage }) {
  if (c.complete) return <span className="badge ok">{t("數字完整")}</span>;
  return (
    <>
      {c.orders_without_fees > 0 && <span className="badge warn" style={{ marginRight: 4 }}>{fmtInt(c.orders_without_fees)} {t("筆手續費未到")}</span>}
      {c.units_without_cogs > 0 && <span className="badge warn" style={{ marginRight: 4 }}>{fmtInt(c.units_without_cogs)} {t("件成本未認列")}</span>}
      {c.lines_with_issues > 0 && <span className="badge danger">{fmtInt(c.lines_with_issues)} {t("件需處理")}</span>}
    </>
  );
}

function isoDay(d: Date) {
  return d.toLocaleDateString("en-CA", { timeZone: TZ });
}

export function ShowReportsPage() {
  const nav = useNavigate();
  const today = new Date();
  const [from, setFrom] = useState(isoDay(new Date(today.getTime() - 30 * 86400_000)));
  const [to, setTo] = useState(isoDay(today));
  const q = useQuery({
    queryKey: ["show-reports", from, to],
    queryFn: () => api<ShowRow[]>("/reports/livestreams", { params: { from, to } }),
  });
  const rows = q.data ?? [];
  const total = rows.reduce(
    (a, r) => ({ net: a.net + r.net_sales, fees: a.fees + r.platform_costs, cogs: a.cogs + r.cogs, gp: a.gp + r.gross_profit, orders: a.orders + r.paid_orders }),
    { net: 0, fees: 0, cogs: 0, gp: 0, orders: 0 },
  );
  const incomplete = rows.filter((r) => !r.complete).length;

  return (
    <>
      <div className="page-head"><h1>{t("直播損益")}</h1></div>
      <div className="card">
        <div className="row" style={{ marginBottom: 12, alignItems: "end" }}>
          <label className="small" style={{ flex: "0 1 170px" }}>{t("從")}<input type="date" value={from} onChange={(e) => setFrom(e.target.value)} /></label>
          <label className="small" style={{ flex: "0 1 170px" }}>{t("到")}<input type="date" value={to} onChange={(e) => setTo(e.target.value)} /></label>
          <span className="small muted" style={{ flex: "1 1 240px" }}>{t("美東時間。費用來自 Whatnot 財務報表，成本來自採購批次（先進先出），出貨後才認列。")}</span>
        </div>
        {rows.length > 0 && (
          <div className="tiles">
            <div className="tile"><div className="label">{t("淨銷售額")}</div><div className="value">{money(total.net)}</div><div className="sub">{rows.length} {t("場・")}{fmtInt(total.orders)} {t("筆已付款訂單")}</div></div>
            <div className="tile"><div className="label">{t("Whatnot 費用")}</div><div className="value">{money(total.fees)}</div><div className="sub">{t("佣金、手續費、賣家運費")}</div></div>
            <div className="tile"><div className="label">{t("商品成本")}</div><div className="value">{money(total.cogs)}</div><div className="sub">{t("已出貨的部分")}</div></div>
            <div className="tile"><div className="label">{t("毛利")}</div><div className="value">{money(total.gp)}</div><div className="sub">{incomplete ? t("{0} 場數字尚未完整", [incomplete]) : t("營運費用見「損益表」")}</div></div>
          </div>
        )}
        <ErrorBox error={q.error} />
        {q.isLoading ? <Loading /> : !rows.length ? <div className="empty">{t("這段期間沒有直播訂單。到「Whatnot 串接」同步，或匯入 Whatnot 訂單 CSV。")}</div> : (
          <div className="table-wrap">
            <table className="data responsive">
              <thead>
                <tr><th>{t("直播")}</th><th className="num">{t("訂單")}</th><th className="num">{t("淨銷售額")}</th><th className="num">{t("Whatnot 費用")}</th><th className="num">{t("商品成本")}</th><th className="num">{t("毛利")}</th><th className="num">{t("毛利率")}</th><th className="num">{t("每小時毛利")}</th><th>{t("狀態")}</th></tr>
              </thead>
              <tbody>
                {rows.map((r) => (
                  <tr key={r.id} className="click" onClick={() => nav(`/reports/shows/${r.id}`)}>
                    <td className="lead"><div>{r.title ?? t("（未命名直播）")}</div><div className="small muted" style={{ fontWeight: 400 }}>{fmtDateTime(r.start_at, TZ)}{r.stream_minutes ? `・${hours(r.stream_minutes)}` : ""}</div></td>
                    <td data-label={t("訂單")} className="num">{fmtInt(r.paid_orders)}</td>
                    <td data-label={t("淨銷售額")} className="num">{money(r.net_sales)}</td>
                    <td data-label={t("Whatnot 費用")} className="num"><Money v={r.platform_costs} /></td>
                    <td data-label={t("商品成本")} className="num"><Money v={r.cogs} /></td>
                    <td data-label={t("毛利")} className="num"><Money v={r.gross_profit} strong />{!r.complete && <div className="small muted">{t("未完整")}</div>}</td>
                    <td data-label={t("毛利率")} className="num">{pct(r.margin_pct)}</td>
                    <td data-label={t("每小時毛利")} className="num">{r.profit_per_hour === null ? "—" : money(r.profit_per_hour)}</td>
                    <td data-label={t("狀態")}><CoverageBadges c={r} /></td>
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

const WATERFALL: [keyof Summary, string, string][] = [
  ["sales", t("商品銷售額"), t("訂單成交價（不含買家付的運費、稅）")],
  ["discounts", t("優惠券"), t("賣家發的折扣")],
  ["refunds", t("退款"), t("Whatnot 財務報表")],
  ["commission", t("Whatnot 佣金"), t("含佣金稅")],
  ["processing", t("金流手續費"), t("含手續費稅")],
  ["shipping", t("賣家付的運費"), t("贈品運費、物流商調整")],
  ["cogs", t("商品成本"), t("採購批次先進先出，出貨時認列")],
  ["other", t("其他"), t("小費等")],
];

export function ShowReportPage() {
  const { id } = useParams();
  const q = useQuery({ queryKey: ["show-report", id], queryFn: () => api<ShowDetail>(`/reports/livestreams/${id}`) });
  if (q.isLoading) return <Loading />;
  if (q.error || !q.data) return <ErrorBox error={q.error} />;
  const d = q.data;
  const s = d.summary;
  const c = d.coverage;
  const m = d.whatnot_metrics;
  const ls = d.livestream;

  return (
    <>
      <div className="page-head">
        <div>
          <div className="small"><Link to="/reports/shows">{t("← 直播損益")}</Link></div>
          <h1 style={{ marginBottom: 2 }}>{ls.title ?? t("（未命名直播）")}</h1>
          <div className="small muted">{fmtDateTime(ls.started_at ?? ls.scheduled_at, TZ)}{d.stream_minutes ? `・${hours(d.stream_minutes)}` : ""}・<Link to={`/orders?show=${ls.id}`}>{t("看訂單")}</Link></div>
        </div>
      </div>

      {!c.complete && (
        <div className="alert warn" style={{ marginBottom: 12 }}>
          <b>{t("這場的數字還沒完整，毛利會再變動：")}</b>
          <ul style={{ margin: "6px 0 0", paddingLeft: 20 }}>
            {c.orders_without_fees > 0 && <li>{fmtInt(c.orders_without_fees)} {t("筆訂單的佣金、手續費還沒出現在 Whatnot 財務報表（通常出貨後才會有），系統不會用估計值補。")}</li>}
            {c.units_without_cogs > 0 && <li>{fmtInt(c.units_without_cogs)} {t("件還沒出貨，商品成本尚未認列。")}</li>}
            {c.lines_with_issues > 0 && <li>{fmtInt(c.lines_with_issues)} {t("件商品需要處理（待對應 SKU、庫存不足…），請到")} <Link to={`/orders?tab=exceptions&show=${ls.id}`}>{t("訂單 › 需要處理")}</Link>{t("。")}</li>}
          </ul>
        </div>
      )}

      <div className="tiles">
        <div className="tile"><div className="label">{t("淨銷售額")}</div><div className="value">{money(s.net_sales)}</div><div className="sub">{fmtInt(c.paid_orders)} {t("筆訂單・")}{fmtInt(c.units)} {t("件")}{c.giveaways ? t("・贈品 {0}", [c.giveaways]) : ""}</div></div>
        <div className="tile"><div className="label">{t("Whatnot 費用")}</div><div className="value">{money(s.platform_costs)}</div><div className="sub">{t("佔淨銷售額")} {s.net_sales > 0 ? pct((-s.platform_costs / s.net_sales) * 100) : "—"}</div></div>
        <div className="tile"><div className="label">{t("毛利")}</div><div className="value" style={{ color: s.gross_profit < 0 ? "var(--danger)" : undefined }}>{money(s.gross_profit)}</div><div className="sub">{t("毛利率")} {pct(s.margin_pct)}</div></div>
        <div className="tile"><div className="label">{t("每小時")}</div><div className="value">{d.per_hour ? money(d.per_hour.gross_profit) : "—"}</div><div className="sub">{d.per_hour ? t("毛利；銷售 {0}／小時", [money(d.per_hour.sales)]) : t("Whatnot 直播數據尚未產生")}</div></div>
      </div>

      <div className="grid-2">
        <div className="card">
          <h2>{t("損益明細")}</h2>
          <table className="data">
            <tbody>
              {WATERFALL.filter(([k]) => k === "sales" || k === "cogs" || s[k] !== 0).map(([k, label, hint]) => (
                <tr key={k}>
                  <td><div>{label}</div><div className="small muted">{hint}</div></td>
                  <td className="num"><Money v={s[k] as number} /></td>
                </tr>
              ))}
              <tr style={{ borderTop: "2px solid var(--border)" }}>
                <td><b>{t("毛利")}</b><div className="small muted">{s.opex ? t("扣營運費用前") : t("全公司的營運費用見「損益表」")}</div></td>
                <td className="num"><Money v={s.gross_profit} strong /></td>
              </tr>
              {!!s.opex && (
                <>
                  <tr><td><div>{t("這場的營運費用")}</div><div className="small muted">{t("記錄費用時指定這場直播的（例如主播費）")}</div></td><td className="num"><Money v={s.opex} /></td></tr>
                  <tr style={{ borderTop: "2px solid var(--border)" }}><td><b>{t("淨利")}</b></td><td className="num"><Money v={s.net_profit} strong /></td></tr>
                </>
              )}
            </tbody>
          </table>
        </div>

        <div className="card">
          <h2>{t("和 Whatnot 的數字對照")}</h2>
          {!d.comparison ? (
            <p className="small muted">{t("Whatnot 直播數據")}{ls.analytics_status === "PENDING" ? t("還在計算中") : ls.analytics_status === "NOT_ENDED" ? t("要等直播結束後才有") : t("尚未取得")}{t("，下次同步會再讀取。")}</p>
          ) : (
            <>
              <table className="data">
                <thead><tr><th></th><th className="num">ERP</th><th className="num">Whatnot</th></tr></thead>
                <tbody>
                  <tr><td>{t("銷售額（扣優惠券）")}</td><td className="num">{money(d.comparison.ours_sales)}</td><td className="num">{money(d.comparison.whatnot_sales)}</td></tr>
                  <tr><td>{t("扣平台費用後")}</td><td className="num">{money(d.comparison.ours_earnings)}</td><td className="num">{money(d.comparison.whatnot_estimated_earnings)}</td></tr>
                  <tr><td>{t("訂單數（不含贈品）")}</td><td className="num">{fmtInt(d.comparison.ours_orders)}</td><td className="num">{d.comparison.whatnot_orders === null ? "—" : fmtInt(d.comparison.whatnot_orders)}</td></tr>
                </tbody>
              </table>
              <p className="small muted" style={{ marginBottom: 0 }}>{t("Whatnot 的「預估收入」不扣商品成本，所以和 ERP 的「扣平台費用後」比。差異通常是還沒進財務報表的訂單或退款。")}</p>
            </>
          )}
          {Object.keys(m).length > 0 && (
            <div className="row small" style={{ marginTop: 12, gap: 16 }}>
              {m.max_concurrent_viewers !== undefined && <span>{t("最高同時觀看")} <b>{fmtInt(m.max_concurrent_viewers)}</b></span>}
              {m.distinct_viewers_count !== undefined && <span>{t("觀眾")} <b>{fmtInt(m.distinct_viewers_count)}</b></span>}
              {m.buyers_count !== undefined && <span>{t("買家")} <b>{fmtInt(m.buyers_count)}</b>{m.first_time_buyers_count !== undefined ? t("（新 {0}）", [fmtInt(m.first_time_buyers_count)]) : ""}</span>}
              {m.show_clickthrough_rate !== undefined && <span>{t("點擊率")} <b>{m.show_clickthrough_rate}%</b></span>}
              {m.average_rating !== undefined && <span>{t("評分")} <b>{m.average_rating}</b></span>}
            </div>
          )}
        </div>
      </div>

      <div className="card">
        <h2>{t("各商品")}</h2>
        <p className="small muted" style={{ marginTop: 0 }}>{t("整筆訂單的費用（佣金、手續費、優惠券）依商品金額比例分攤。")}</p>
        <div className="table-wrap">
          <table className="data responsive">
            <thead><tr><th>{t("商品")}</th><th className="num">{t("件數")}</th><th className="num">{t("淨銷售額")}</th><th className="num">{t("Whatnot 費用")}</th><th className="num">{t("商品成本")}</th><th className="num">{t("毛利")}</th><th className="num">{t("毛利率")}</th></tr></thead>
            <tbody>
              {d.skus.map((r, i) => (
                <tr key={r.sku_id ?? `u${i}`}>
                  <td className="lead">
                    {r.sku_id ? <Link to={`/skus/${r.sku_id}`} className="mono">{r.sku_code}</Link> : <span className="badge danger">{t("待對應")}</span>}
                    <div className="small muted" style={{ fontWeight: 400 }}>{r.name}</div>
                  </td>
                  <td data-label={t("件數")} className="num">{fmtInt(r.units)}{r.units_without_cogs > 0 && <div className="small muted">{fmtInt(r.units_without_cogs)} {t("未出貨")}</div>}</td>
                  <td data-label={t("淨銷售額")} className="num">{money(r.net_sales)}</td>
                  <td data-label={t("Whatnot 費用")} className="num"><Money v={r.platform_costs} /></td>
                  <td data-label={t("商品成本")} className="num"><Money v={r.cogs} /></td>
                  <td data-label={t("毛利")} className="num"><Money v={r.gross_profit} strong /></td>
                  <td data-label={t("毛利率")} className="num">{pct(r.margin_pct)}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
        {Object.keys(d.unallocated).length > 0 && (
          <p className="small muted">{t("不屬於任何訂單的項目（小費、物流商調整）：")}{Object.entries(d.unallocated).map(([k, v]) => `${k === "other" ? t("其他") : k === "shipping" ? t("運費") : k} ${money(v)}`).join(t("、"))}</p>
        )}
      </div>

      {(d.refunds.length > 0 || d.needs_review.length > 0) && (
        <div className="card">
          <h2>{t("退款與需要確認的交易")}</h2>
          <div className="table-wrap">
            <table className="data responsive">
              <thead><tr><th>{t("時間")}</th><th>{t("類型")}</th><th>{t("訂單")}</th><th className="num">{t("金額")}</th><th>{t("說明")}</th></tr></thead>
              <tbody>
                {d.refunds.map((r, i) => (
                  <tr key={`r${i}`}>
                    <td className="lead small">{fmtDateTime(r.completed_at, TZ)}</td>
                    <td data-label={t("類型")}><span className="badge warn">{r.transaction_type === "ORDER_REFUND" ? t("退款") : t("申訴退回")}</span></td>
                    <td data-label={t("訂單")} className="mono">{r.external_order_id ?? "—"}</td>
                    <td data-label={t("金額")} className="num"><Money v={Number(r.amount)} /></td>
                    <td data-label={t("說明")} className="small">{r.message ?? ""}<div className="muted">{t("實體商品是否退回，請到「入庫/調整」登記顧客退貨")}</div></td>
                  </tr>
                ))}
                {d.needs_review.map((r) => (
                  <tr key={r.external_id}>
                    <td className="lead small mono">{r.external_id}</td>
                    <td data-label={t("類型")}><span className="badge danger">{r.upstream_changed ? t("金額被更改") : r.status === "order_not_in_erp" ? t("訂單不在系統") : t("需要確認")}</span></td>
                    <td data-label={t("訂單")} className="mono">{r.external_order_id ?? "—"}</td>
                    <td data-label={t("金額")} className="num"><Money v={Number(r.amount)} /></td>
                    <td data-label={t("說明")} className="small">{r.detail}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        </div>
      )}

      {d.orders_without_fees.length > 0 && (
        <details className="card">
          <summary><b>{t("手續費還沒到的訂單（")}{fmtInt(c.orders_without_fees)}{t("）")}</b></summary>
          <div className="row small" style={{ marginTop: 10, gap: 8 }}>
            {d.orders_without_fees.map((o) => <Link key={o.id} to={`/orders/${o.id}`} className="mono">{o.external_order_id}</Link>)}
          </div>
        </details>
      )}
    </>
  );
}
