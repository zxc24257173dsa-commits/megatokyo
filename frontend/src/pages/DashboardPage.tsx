import { Link } from "react-router-dom";
import { useQuery } from "@tanstack/react-query";
import { api } from "../api";
import { useAuth } from "../auth";
import { ColumnChart } from "../components/charts";
import { ErrorBox, Loading, fmtDate, fmtDateTime, fmtInt } from "../components/ui";
import { t } from "../i18n";
import { money } from "./ShowReportPages";

const TZ = "America/New_York";

interface Todo { key: string; count: number; link: string; severity: "danger" | "warn" | "info" }
interface Week { label: string; from: string; sales: number | null; units: number; orders: number; shows: number }
interface Dashboard {
  today: string;
  todo: Todo[];
  parcels_in_transit?: number;
  replenishment_as_of?: string;
  last_sync?: { status: string; started_at: string; finished_at: string | null } | null;
  sales_weeks?: Week[];
  this_week?: { sales: number | null; units: number; orders: number; shows: number; last_week_same_days: { sales: number | null; units: number; orders: number } | null };
  month?: { from: string; net_sales: number; gross_profit: number; gross_margin_pct: number | null; opex: number; net_profit: number; net_margin_pct: number | null; complete: boolean };
  recent_shows?: { id: number; title: string | null; start_at: string | null; paid_orders: number; net_sales: number; gross_profit: number; margin_pct: number | null; complete: boolean; stream_minutes: number | null }[];
  stock?: { available: number; transit: number; out_of_stock: number; us_value?: number; transit_value?: number };
}

// What each to-do means, in one line.
const TODO: Record<string, (n: number) => string> = {
  order_exceptions: (n) => t("{0} 件訂單商品需要處理（庫存不足、出貨後取消…）", [fmtInt(n)]),
  unmapped_products: (n) => t("{0} 種 Whatnot 商品還沒對應 SKU", [fmtInt(n)]),
  late_outbound: (n) => t("{0} 筆已付款訂單超過 2 天還沒寄出", [fmtInt(n)]),
  overdue_parcels: (n) => t("{0} 個日本寄來的包裹超過預計到貨日", [fmtInt(n)]),
  stock_counts_to_approve: (n) => t("{0} 份盤點等待核准", [fmtInt(n)]),
  replenish_now: (n) => t("{0} 個 SKU 已經到了最晚下單日", [fmtInt(n)]),
  replenishment_never_run: () => t("還沒有產生過補貨建議"),
  whatnot_review: (n) => t("{0} 筆 Whatnot 財務交易需要人工確認", [fmtInt(n)]),
  sync_stale: () => t("Whatnot 超過 6 小時沒有同步"),
};

function change(now: number | null | undefined, before: number | null | undefined): string | null {
  if (now == null || before == null || before === 0) return null;
  const p = ((now - before) / before) * 100;
  return `${p >= 0 ? "+" : "−"}${Math.abs(p).toFixed(0)}%`;
}

export function DashboardPage() {
  const { me, can } = useAuth();
  const q = useQuery({ queryKey: ["dashboard"], queryFn: () => api<Dashboard>("/dashboard"), refetchInterval: 5 * 60_000 });
  if (q.isLoading) return <Loading />;
  if (!q.data) return <ErrorBox error={q.error} />;
  const d = q.data;
  const w = d.this_week;
  const prev = w?.last_week_same_days;
  const money_ = w?.sales !== null && w?.sales !== undefined;

  return (
    <>
      <div className="page-head">
        <div>
          <h1 style={{ marginBottom: 2 }}>{t("總覽")}</h1>
          <div className="small muted">{t("{0}，今天是 {1}（美東）", [me?.display_name ?? "", fmtDate(d.today + "T12:00:00Z", TZ)])}</div>
        </div>
      </div>

      <div className="card">
        <h2>{t("今天要處理")}</h2>
        {!d.todo.length ? <div className="alert ok">{t("沒有需要處理的事項")}</div> : (
          <ul className="todo">
            {d.todo.map((x) => (
              <li key={x.key}>
                <span className={`badge ${x.severity}`}>{x.severity === "danger" ? t("緊急") : x.severity === "warn" ? t("待辦") : t("提醒")}</span>
                <Link to={x.link}>{(TODO[x.key] ?? (() => x.key))(x.count)}</Link>
              </li>
            ))}
          </ul>
        )}
      </div>

      {w && (
        <div className="tiles">
          {money_ && <div className="tile"><div className="label">{t("本週成交額")}</div><div className="value">{money(w.sales)}</div>
            <div className="sub">{prev && change(w.sales, prev.sales) ? t("比上週同期 {0}", [change(w.sales, prev.sales)]) : t("週一開始")}</div></div>}
          <div className="tile"><div className="label">{t("本週售出")}</div><div className="value">{fmtInt(w.units)}</div>
            <div className="sub">{prev && change(w.units, prev.units) ? t("比上週同期 {0}", [change(w.units, prev.units)]) : t("件")}</div></div>
          <div className="tile"><div className="label">{t("本週訂單")}</div><div className="value">{fmtInt(w.orders)}</div><div className="sub">{t("{0} 場直播", [w.shows])}</div></div>
          {d.stock && <div className="tile"><div className="label">{t("美國可售庫存")}</div><div className="value">{fmtInt(d.stock.available)}</div>
            <div className="sub">{d.stock.us_value !== undefined ? t("成本 {0}・運送中 {1} 件", [money(d.stock.us_value), fmtInt(d.stock.transit)]) : t("運送中 {0} 件", [fmtInt(d.stock.transit)])}</div></div>}
        </div>
      )}

      <div className="grid-2">
        {d.sales_weeks && (
          <div className="card">
            <h2>{money_ ? t("最近 8 週成交額") : t("最近 8 週售出件數")}</h2>
            <ColumnChart
              ariaLabel={money_ ? t("最近 8 週成交額") : t("最近 8 週售出件數")}
              height={200}
              format={(v) => (money_ ? money(v) : fmtInt(v))}
              bars={d.sales_weeks.map((x) => ({
                label: x.label.split("–")[0],
                value: money_ ? x.sales ?? 0 : x.units,
                tip: <><b>{x.label}</b>{x.sales !== null && <div>{money(x.sales)}</div>}<div>{t("{0} 件・{1} 筆訂單・{2} 場直播", [fmtInt(x.units), fmtInt(x.orders), x.shows])}</div></>,
              }))}
            />
            <div className="small"><Link to="/reports/trends">{t("銷售趨勢 →")}</Link></div>
          </div>
        )}
        {d.month && (
          <div className="card">
            <h2>{t("本月損益")}</h2>
            <table className="data">
              <tbody>
                <tr><td>{t("淨銷售額")}</td><td className="num">{money(d.month.net_sales)}</td></tr>
                <tr><td>{t("毛利")}</td><td className="num">{money(d.month.gross_profit)} <span className="small muted">{d.month.gross_margin_pct !== null ? `${d.month.gross_margin_pct}%` : ""}</span></td></tr>
                <tr><td>{t("營運費用")}</td><td className="num" style={{ color: d.month.opex < 0 ? "var(--danger)" : undefined }}>{money(d.month.opex)}</td></tr>
                <tr className="subtotal"><td><b>{t("淨利")}</b></td><td className="num"><b>{money(d.month.net_profit)}</b> <span className="small muted">{d.month.net_margin_pct !== null ? `${d.month.net_margin_pct}%` : ""}</span></td></tr>
              </tbody>
            </table>
            {!d.month.complete && <p className="small muted">{t("數字還沒完整（有訂單還沒出貨或手續費還沒到）。")}</p>}
            <div className="small"><Link to="/reports/pnl">{t("損益表 →")}</Link></div>
          </div>
        )}
      </div>

      {d.recent_shows && d.recent_shows.length > 0 && (
        <div className="card">
          <h2>{t("最近的直播")}</h2>
          <div className="table-wrap">
            <table className="data responsive">
              <thead><tr><th>{t("直播")}</th><th className="num">{t("訂單")}</th><th className="num">{t("淨銷售額")}</th><th className="num">{t("毛利")}</th><th className="num">{t("毛利率")}</th></tr></thead>
              <tbody>
                {d.recent_shows.map((s) => (
                  <tr key={s.id}>
                    <td className="lead"><Link to={`/reports/shows/${s.id}`}>{s.title ?? t("（未命名直播）")}</Link><div className="small muted" style={{ fontWeight: 400 }}>{fmtDateTime(s.start_at, TZ)}{!s.complete && <> · {t("未完整")}</>}</div></td>
                    <td data-label={t("訂單")} className="num">{fmtInt(s.paid_orders)}</td>
                    <td data-label={t("淨銷售額")} className="num">{money(s.net_sales)}</td>
                    <td data-label={t("毛利")} className="num">{money(s.gross_profit)}</td>
                    <td data-label={t("毛利率")} className="num">{s.margin_pct !== null ? `${s.margin_pct}%` : "—"}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        </div>
      )}

      {can("integration.manage") && d.last_sync && (
        <p className="small muted">{t("Whatnot 最後同步：{0}（{1}）", [fmtDateTime(d.last_sync.started_at, TZ), d.last_sync.status === "succeeded" ? t("成功") : d.last_sync.status === "running" ? t("進行中") : t("失敗")])}</p>
      )}
    </>
  );
}
