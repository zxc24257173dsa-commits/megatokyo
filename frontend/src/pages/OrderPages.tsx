import { useState } from "react";
import { Link, useNavigate, useParams, useSearchParams } from "react-router-dom";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { api } from "../api";
import { useAuth } from "../auth";
import { ErrorBox, Loading, SkuPicker, fmtDateTime, fmtInt, fmtUsd, useDebounced } from "../components/ui";
import { t } from "../i18n";

interface OrderRow {
  id: number;
  external_order_id: string;
  status: string;
  ordered_at: string;
  total: string;
  item_subtotal: string;
  transaction_type: string | null;
  livestream_title: string | null;
  livestream_id: number | null;
  units: number;
  exceptions: number;
  stock_states: string | null;
}

interface Show {
  id: number;
  title: string | null;
  status: string;
  scheduled_at: string | null;
  orders: number;
  paid_orders: number;
  paid_sales: string;
}

interface UnmappedGroup {
  title: string;
  example_line_id: number;
  lines: number;
  units: number;
  sales: string;
}

export const ORDER_STATUS: Record<string, [string, string]> = {
  pending: [t("未付款"), ""],
  pending_fulfillment: [t("已付款・待出貨"), "info"],
  completed: [t("已完成"), "ok"],
  cancelled: [t("已取消"), ""],
  failed: [t("付款失敗"), "danger"],
};
const STOCK: Record<string, [string, string]> = {
  none: [t("未扣庫存"), ""],
  reserved: [t("已預留"), "info"],
  shipped: [t("已出貨扣庫存"), "ok"],
  released: [t("已釋放"), ""],
};
const EXCEPTION: Record<string, string> = {
  unmapped_sku: t("待對應 SKU"),
  oversold: t("超賣"),
  insufficient_stock: t("出貨時庫存不足"),
  non_usd: t("非 USD 訂單"),
  cancelled_after_shipping: t("出貨後取消"),
};

export function OrdersPage() {
  const nav = useNavigate();
  const [params, setParams] = useSearchParams();
  const tab = params.get("tab") ?? "orders";
  const show = params.get("show") ?? "";
  const [search, setSearch] = useState(params.get("search") ?? ""); // links can pre-fill the search
  const q = useDebounced(search);
  const shows = useQuery({ queryKey: ["livestreams"], queryFn: () => api<Show[]>("/livestreams") });
  const orders = useQuery({
    queryKey: ["orders", tab, show, q],
    queryFn: () => api<OrderRow[]>("/orders", { params: { exceptions_only: tab === "exceptions", livestream_id: show || undefined, search: q, limit: 300 } }),
    enabled: tab !== "unmapped",
  });
  const unmapped = useQuery({ queryKey: ["unmapped"], queryFn: () => api<UnmappedGroup[]>("/orders/unmapped-products") });
  const set = (k: string, v: string) => {
    const p = new URLSearchParams(params);
    if (v) p.set(k, v);
    else p.delete(k);
    setParams(p);
  };
  const currentShow = shows.data?.find((s) => String(s.id) === show);

  return (
    <>
      <div className="page-head"><h1>{t("訂單")}</h1></div>
      <div className="row" style={{ marginBottom: 12 }}>
        {[["orders", t("全部訂單")], ["exceptions", t("需要處理")], ["unmapped", t("待對應商品{0}", [unmapped.data?.length ? t("（{0}）", [unmapped.data.length]) : ""])]].map(([k, l]) => (
          <button key={k} className={`btn sm ${tab === k ? "primary" : ""}`} onClick={() => set("tab", k === "orders" ? "" : k)}>{l}</button>
        ))}
      </div>
      {tab === "unmapped" ? <UnmappedProducts groups={unmapped.data} loading={unmapped.isLoading} /> : (
        <div className="card">
          <div className="row" style={{ marginBottom: 12 }}>
            <select style={{ flex: "2 1 240px" }} value={show} onChange={(e) => set("show", e.target.value)} aria-label={t("直播")}>
              <option value="">{t("全部直播")}</option>
              {shows.data?.map((s) => <option key={s.id} value={s.id}>{fmtDateTime(s.scheduled_at)} {s.title ?? t("（未命名）")}</option>)}
            </select>
            <input style={{ flex: "1 1 180px" }} placeholder={t("搜尋訂單編號、商品、SKU…")} value={search} onChange={(e) => setSearch(e.target.value)} aria-label={t("搜尋")} />
          </div>
          {currentShow && (
            <div className="tiles" style={{ gridTemplateColumns: "repeat(3, minmax(0, 1fr))" }}>
              <div className="tile"><div className="label">{t("已付款訂單")}</div><div className="value">{fmtInt(currentShow.paid_orders)}</div><div className="sub">{t("共")} {fmtInt(currentShow.orders)} {t("筆")}</div></div>
              <div className="tile"><div className="label">{t("商品成交額")}</div><div className="value">{fmtUsd(currentShow.paid_sales)}</div><div className="sub">{t("不含運費、稅")}</div></div>
              <div className="tile"><div className="label">{t("直播狀態")}</div><div className="value" style={{ fontSize: 18 }}>{currentShow.status === "ended" ? t("已結束") : currentShow.status}</div><div className="sub"><Link to={`/reports/shows/${currentShow.id}`}>{t("看這場的損益 →")}</Link></div></div>
            </div>
          )}
          <ErrorBox error={orders.error} />
          {orders.isLoading ? <Loading /> : !orders.data?.length ? <div className="empty">{tab === "exceptions" ? t("沒有需要處理的訂單") : t("沒有訂單。到「Whatnot 串接」同步，或匯入 Whatnot 訂單 CSV。")}</div> : (
            <div className="table-wrap">
              <table className="data responsive">
                <thead><tr><th>{t("下單時間")}</th><th>{t("訂單")}</th><th>{t("狀態")}</th><th className="num">{t("件數")}</th><th className="num">{t("金額")}</th><th>{t("庫存")}</th><th>{t("直播")}</th></tr></thead>
                <tbody>
                  {orders.data.map((o) => (
                    <tr key={o.id} className="click" onClick={() => nav(`/orders/${o.id}`)}>
                      <td className="lead small">{fmtDateTime(o.ordered_at)}</td>
                      <td data-label={t("訂單")} className="mono">{o.external_order_id}{o.transaction_type === "giveaway" && <> <span className="badge">{t("贈品")}</span></>}</td>
                      <td data-label={t("狀態")}><span className={`badge ${ORDER_STATUS[o.status]?.[1]}`}>{ORDER_STATUS[o.status]?.[0] ?? o.status}</span>
                        {o.exceptions > 0 && <> <span className="badge danger">{t("需處理")}</span></>}</td>
                      <td data-label={t("件數")} className="num">{fmtInt(o.units)}</td>
                      <td data-label={t("金額")} className="num">{fmtUsd(o.item_subtotal)}</td>
                      <td data-label={t("庫存")}>{(o.stock_states ?? "").split(",").filter(Boolean).map((s) => <span key={s} className={`badge ${STOCK[s]?.[1]}`} style={{ marginRight: 4 }}>{STOCK[s]?.[0] ?? s}</span>)}</td>
                      <td data-label={t("直播")} className="small">{o.livestream_title ?? "—"}</td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          )}
        </div>
      )}
    </>
  );
}

function UnmappedProducts({ groups, loading }: { groups?: UnmappedGroup[]; loading: boolean }) {
  const { can } = useAuth();
  if (loading) return <Loading />;
  if (!groups?.length) return <div className="card empty">{t("所有訂單商品都已對應到 SKU")}</div>;
  return (
    <div className="card">
      <p className="small muted" style={{ marginTop: 0 }}>
        {t("Whatnot 上同一種商品每次拍賣名稱都不同（#1、#2…），這裡已經把名稱去掉編號合併。每種商品")}<b>{t("只要對應一次")}</b>{t("，之前和之後的訂單都會自動套用，並開始預留／扣除美國庫存。 也可以在 Whatnot 商品設定 SKU（external ID）等於 ERP 的 SKU 編號，就不用手動對應。")}
      </p>
      <div className="table-wrap">
        <table className="data responsive">
          <thead><tr><th>{t("Whatnot 商品")}</th><th className="num">{t("訂單")}</th><th className="num">{t("件數")}</th><th className="num">{t("成交額")}</th>{can("catalog.edit") && <th>{t("對應到 ERP 的 SKU")}</th>}</tr></thead>
          <tbody>{groups.map((g) => <UnmappedRow key={g.example_line_id} g={g} canMap={can("catalog.edit")} />)}</tbody>
        </table>
      </div>
    </div>
  );
}

function UnmappedRow({ g, canMap }: { g: UnmappedGroup; canMap: boolean }) {
  const qc = useQueryClient();
  const [sku, setSku] = useState<number | null>(null);
  const m = useMutation({
    mutationFn: () => api(`/orders/lines/${g.example_line_id}/map`, { body: { sku_id: sku } }),
    onSuccess: () => {
      void qc.invalidateQueries({ queryKey: ["unmapped"] });
      void qc.invalidateQueries({ queryKey: ["orders"] });
      void qc.invalidateQueries({ queryKey: ["skus"] });
    },
  });
  return (
    <tr>
      <td className="lead small">{g.title}</td>
      <td data-label={t("訂單")} className="num">{fmtInt(g.lines)}</td>
      <td data-label={t("件數")} className="num">{fmtInt(g.units)}</td>
      <td data-label={t("成交額")} className="num">{fmtUsd(g.sales)}</td>
      {canMap && (
        <td data-label={t("對應 SKU")} style={{ minWidth: 260 }}>
          <ErrorBox error={m.error} />
          <div className="row" style={{ flexWrap: "nowrap" }}>
            <div style={{ flex: 1 }}><SkuPicker value={sku} onChange={setSku} excludeBundles={false} /></div>
            <button className="btn sm primary" disabled={!sku || m.isPending} onClick={() => m.mutate()}>{t("對應")}</button>
          </div>
        </td>
      )}
    </tr>
  );
}

interface OrderDetail {
  id: number;
  external_order_id: string;
  status: string;
  ordered_at: string;
  paid_at: string | null;
  cancelled_at: string | null;
  item_subtotal: string;
  shipping_charged: string;
  sales_tax_collected: string;
  discount_total: string;
  total: string;
  transaction_type: string | null;
  source: string;
  last_synced_at: string | null;
  livestream: { id: number; title: string | null } | null;
  lines: {
    id: number;
    title_snapshot: string | null;
    purchased_sku: string | null;
    qty: number;
    unit_price: string;
    line_amount: string;
    stock_state: string;
    exception_code: string | null;
    exception_detail: string | null;
    sku_id: number | null;
    sku_code: string | null;
    sku_name: string | null;
    shipment_status: string | null;
    tracking_code: string | null;
    cogs: string | null;
  }[];
}

export function OrderDetailPage() {
  const id = Number(useParams().id);
  const { can } = useAuth();
  const qc = useQueryClient();
  const o = useQuery({ queryKey: ["order", id], queryFn: () => api<OrderDetail>(`/orders/${id}`) });
  const [sku, setSku] = useState<Record<number, number | null>>({});
  const map = useMutation({
    mutationFn: (lineId: number) => api<OrderDetail>(`/orders/lines/${lineId}/map`, { body: { sku_id: sku[lineId] } }),
    onSuccess: (d) => {
      qc.setQueryData(["order", id], d);
      void qc.invalidateQueries({ queryKey: ["unmapped"] });
      void qc.invalidateQueries({ queryKey: ["orders"] });
    },
  });
  const [changing, setChanging] = useState<number | null>(null);
  const remap = useMutation({
    mutationFn: ({ lineId, skuId }: { lineId: number; skuId: number | null }) => api<OrderDetail>(`/orders/lines/${lineId}/remap`, { body: { sku_id: skuId } }),
    onSuccess: (d) => {
      qc.setQueryData(["order", id], d);
      setChanging(null);
      void qc.invalidateQueries({ queryKey: ["unmapped"] });
      void qc.invalidateQueries({ queryKey: ["orders"] });
      void qc.invalidateQueries({ queryKey: ["skus"] });
    },
  });
  const reprocess = useMutation({
    mutationFn: () => api<OrderDetail>(`/orders/${id}/reprocess`, { method: "POST" }),
    onSuccess: (d) => qc.setQueryData(["order", id], d),
  });
  if (o.isLoading) return <Loading />;
  if (!o.data) return <ErrorBox error={o.error} />;
  const d = o.data;
  const hasException = d.lines.some((l) => l.exception_code);
  return (
    <>
      <div className="page-head">
        <div>
          <div className="small muted"><Link to="/orders">{t("訂單")}</Link>{d.livestream && <> / <Link to={`/orders?show=${d.livestream.id}`}>{d.livestream.title ?? t("直播")}</Link></>}</div>
          <h1 className="mono">#{d.external_order_id} <span className={`badge ${ORDER_STATUS[d.status]?.[1]}`}>{ORDER_STATUS[d.status]?.[0] ?? d.status}</span></h1>
          <div className="small muted">{t("下單")} {fmtDateTime(d.ordered_at)}{t("・來源")} {d.source === "csv" ? t("CSV 匯入") : d.source === "webhook" ? t("Whatnot 即時通知") : "Whatnot API"}{t("・最後同步")} {fmtDateTime(d.last_synced_at)}</div>
        </div>
        {hasException && can("inventory.adjust_us") && <button className="btn" onClick={() => reprocess.mutate()} disabled={reprocess.isPending}>{t("重新處理")}</button>}
      </div>
      <ErrorBox error={map.error ?? reprocess.error ?? remap.error} />
      <div className="grid-2">
        <div className="card">
          <h2>{t("金額")}</h2>
          <dl className="kv">
            <dt>{t("商品金額")}</dt><dd>{fmtUsd(d.item_subtotal)}</dd>
            {Number(d.discount_total) > 0 && <><dt>{t("賣家折扣")}</dt><dd>−{fmtUsd(d.discount_total)}</dd></>}
            <dt>{t("買家付的運費")}</dt><dd>{fmtUsd(d.shipping_charged)}<span className="small muted">{t("（不計營收）")}</span></dd>
            <dt>{t("銷售稅")}</dt><dd>{fmtUsd(d.sales_tax_collected)}<span className="small muted">{t("（Whatnot 代收，不計營收）")}</span></dd>
            <dt>{t("買家付款合計")}</dt><dd>{fmtUsd(d.total)}</dd>
          </dl>
          <p className="small muted" style={{ marginBottom: 0 }}>{t("平台佣金與手續費以 Whatnot 財務報表為準（Phase 6）。")}</p>
        </div>
        <div className="card">
          <h2>{t("商品")}</h2>
          {d.lines.map((l) => (
            <div key={l.id} style={{ borderTop: "1px solid var(--border)", padding: "10px 0" }}>
              <div className="small">{l.title_snapshot}</div>
              <div className="row small" style={{ gap: 6, marginTop: 4 }}>
                <span>{l.qty} × {fmtUsd(l.unit_price)}</span>
                {l.sku_code ? <Link className="mono" to={`/skus/${l.sku_id}`}>{l.sku_code}</Link> : <span className="badge warn">{t("未對應 SKU")}</span>}
                <span className={`badge ${STOCK[l.stock_state]?.[1]}`}>{STOCK[l.stock_state]?.[0]}</span>
                {l.shipment_status && <span className="badge">{t("出貨：")}{l.shipment_status}</span>}
                {l.cogs && <span className="muted">{t("成本")} {fmtUsd(l.cogs)}</span>}
              </div>
              {l.exception_code && <div className="alert error small" style={{ marginTop: 6, marginBottom: 0 }}><b>{EXCEPTION[l.exception_code] ?? l.exception_code}</b>{t("：")}{l.exception_detail}</div>}
              {l.sku_id && can("catalog.edit") && can("inventory.adjust_us") && (changing !== l.id ? (
                <button className="btn sm" style={{ marginTop: 6 }} onClick={() => setChanging(l.id)}>{t("更改 SKU")}</button>
              ) : (
                <div style={{ marginTop: 6 }}>
                  <div className="alert info small" style={{ marginBottom: 6 }}>{t("會套用到這個 Whatnot 商品的所有訂單。已出貨的會把庫存退回原 SKU、沖銷成本，再用新的 SKU 重新扣庫存；營收不變。")}</div>
                  <div className="row" style={{ flexWrap: "nowrap" }}>
                    <div style={{ flex: 1 }}><SkuPicker value={sku[l.id] ?? null} onChange={(v) => setSku({ ...sku, [l.id]: v })} excludeBundles={false} excludeIds={[l.sku_id]} /></div>
                    <button className="btn sm primary" disabled={!sku[l.id] || remap.isPending} onClick={() => remap.mutate({ lineId: l.id, skuId: sku[l.id] ?? null })}>{t("改為這個 SKU")}</button>
                  </div>
                  <div className="row" style={{ marginTop: 6 }}>
                    <button className="btn sm danger" disabled={remap.isPending} onClick={() => confirm(t("取消這個商品的 SKU 對應？已扣的庫存與成本會退回。")) && remap.mutate({ lineId: l.id, skuId: null })}>{t("取消對應")}</button>
                    <button className="btn sm" onClick={() => setChanging(null)}>{t("取消")}</button>
                  </div>
                </div>
              ))}
              {!l.sku_id && can("catalog.edit") && (
                <div className="row" style={{ marginTop: 6, flexWrap: "nowrap" }}>
                  <div style={{ flex: 1 }}><SkuPicker value={sku[l.id] ?? null} onChange={(v) => setSku({ ...sku, [l.id]: v })} excludeBundles={false} /></div>
                  <button className="btn sm primary" disabled={!sku[l.id] || map.isPending} onClick={() => map.mutate(l.id)}>{t("對應")}</button>
                </div>
              )}
            </div>
          ))}
        </div>
      </div>
    </>
  );
}

interface WhatnotStatus {
  token_source: string | null;
  webhook_secret_configured: boolean;
  connected: boolean;
  seller: { id: string; name: string } | null;
  webhook_url: string;
  webhook_events: Record<string, number>;
  open_exceptions: number;
  financial: { transactions: Record<string, { count: number; amount: number }>; upstream_changed: number };
  sync_runs: { id: number; kind: string; status: string; started_at: string; finished_at: string | null; stats: Record<string, any>; error: string | null }[];
}

export function WhatnotSettingsPage() {
  const qc = useQueryClient();
  const st = useQuery({ queryKey: ["whatnot-status"], queryFn: () => api<WhatnotStatus>("/integrations/whatnot"), refetchInterval: 10_000 });
  const [token, setToken] = useState("");
  const [secret, setSecret] = useState("");
  const [days, setDays] = useState("3");
  const [notice, setNotice] = useState<string | null>(null);
  const done = (msg: string) => {
    setNotice(msg);
    void qc.invalidateQueries({ queryKey: ["whatnot-status"] });
  };
  const save = useMutation({
    mutationFn: () => api("/integrations/whatnot/credentials", { method: "PUT", body: { token: token || null, webhook_secret: secret || null } }),
    onSuccess: () => (setToken(""), setSecret(""), done(t("已加密儲存。請按「測試連線」確認。"))),
  });
  const test = useMutation({
    mutationFn: () => api<{ seller: { name: string } }>("/integrations/whatnot/test", { method: "POST" }),
    onSuccess: (r) => done(t("連線成功：{0}", [r.seller.name])),
  });
  const sync = useMutation({
    mutationFn: () => api<{ finished: boolean }>("/integrations/whatnot/sync", { body: { days: Number(days) } }),
    onSuccess: (r) => done(r.finished ? t("同步完成，結果在下方。") : t("同步已開始，完成後下方會出現結果。")),
  });
  if (st.isLoading) return <Loading />;
  if (!st.data) return <ErrorBox error={st.error} />;
  const s = st.data;
  return (
    <>
      <div className="page-head"><h1>{t("Whatnot 串接")}</h1></div>
      {notice && <div className="alert ok">{notice}</div>}
      <ErrorBox error={save.error ?? test.error ?? sync.error} />
      <div className="tiles">
        <div className="tile"><div className="label">{t("連線")}</div><div className="value" style={{ fontSize: 18 }}>{s.connected ? t("已連線") : s.token_source ? t("待測試") : t("未設定")}</div><div className="sub">{s.seller?.name ?? "—"}</div></div>
        <div className="tile"><div className="label">{t("即時通知（Webhook）")}</div><div className="value" style={{ fontSize: 18 }}>{s.webhook_secret_configured ? t("已設定") : t("未設定")}</div>
          <div className="sub">{t("已處理")} {fmtInt(s.webhook_events.processed ?? 0)}{t("・失敗")} {fmtInt(s.webhook_events.failed ?? 0)}</div></div>
        <div className="tile"><div className="label">{t("需要處理的訂單商品")}</div><div className="value">{fmtInt(s.open_exceptions)}</div><div className="sub"><Link to="/orders?tab=exceptions">{t("查看")}</Link></div></div>
        <div className="tile"><div className="label">{t("財務報表交易")}</div><div className="value">{fmtInt(s.financial.transactions.booked?.count ?? 0)}</div>
          <div className="sub">{t("已入帳")}{s.financial.transactions.order_not_in_erp ? t("・訂單不在系統 {0}", [fmtInt(s.financial.transactions.order_not_in_erp.count)]) : ""}{(s.financial.transactions.needs_review?.count ?? 0) + s.financial.upstream_changed > 0 ? t("・需確認 {0}", [fmtInt((s.financial.transactions.needs_review?.count ?? 0) + s.financial.upstream_changed)]) : ""}</div></div>
      </div>
      <div className="grid-2">
        <form className="card" onSubmit={(e) => (e.preventDefault(), save.mutate())}>
          <h2>{t("API 金鑰")}</h2>
          <p className="small muted" style={{ marginTop: 0 }}>
            {t("在 Whatnot 開發者後台 › Personal access tokens 產生（權限：read:orders、read:shows、read:shipments、read:inventory、read:reports、write:reports）。 金鑰加密保存，儲存後任何人（包含管理員）都無法再看到內容。")}{s.token_source === "environment" && t(" 目前使用伺服器環境設定的金鑰。")}
          </p>
          <label className="field"><span>{t("API 金鑰（wn_pat_tk_…）")}</span><input type="password" autoComplete="off" value={token} onChange={(e) => setToken(e.target.value)} /></label>
          <label className="field"><span>{t("Webhook secret（wn_wh_…）")}</span><input type="password" autoComplete="off" value={secret} onChange={(e) => setSecret(e.target.value)} /></label>
          <div className="row">
            <button className="btn primary" disabled={(!token && !secret) || save.isPending}>{t("儲存")}</button>
            <button type="button" className="btn" disabled={!s.token_source || test.isPending} onClick={() => test.mutate()}>{t("測試連線")}</button>
          </div>
          <p className="small muted">{t("在 Whatnot 開發者後台設定 Webhook 網址為：")}<br /><span className="mono">{s.webhook_url}</span><br />{t("主題選 orders.*、shipments.*、shows.*。")}</p>
        </form>
        <div className="card">
          <h2>{t("同步")}</h2>
          <p className="small muted" style={{ marginTop: 0 }}>{t("即時通知會自動更新；同步用來補漏（例如伺服器維護期間的訂單），並向 Whatnot 申請最近 14 天的財務報表（佣金、手續費、退款）。正式上線後會每小時自動執行。")}</p>
          <div className="row">
            <select value={days} onChange={(e) => setDays(e.target.value)} style={{ width: "auto" }} aria-label={t("同步天數")}>
              {["1", "3", "7", "30"].map((d) => <option key={d} value={d}>{t("最近")} {d} {t("天")}</option>)}
            </select>
            <button className="btn primary" disabled={!s.token_source || sync.isPending} onClick={() => sync.mutate()}>{sync.isPending ? t("同步中，請稍候…") : t("立即同步")}</button>
          </div>
          <table className="data" style={{ marginTop: 12 }}>
            <thead><tr><th>{t("開始")}</th><th>{t("結果")}</th><th>{t("內容")}</th></tr></thead>
            <tbody>
              {s.sync_runs.map((r) => (
                <tr key={r.id}>
                  <td className="small">{fmtDateTime(r.started_at)}</td>
                  <td><span className={`badge ${r.status === "succeeded" ? "ok" : r.status === "running" ? "info" : "danger"}`}>{r.status === "succeeded" ? t("成功") : r.status === "running" ? t("進行中") : t("失敗")}</span></td>
                  <td className="small">{r.error ?? t("訂單 {0}・出貨 {1}・直播 {2}{3}{4}", [r.stats.orders ?? 0, r.stats.shipments ?? 0, (r.stats.shows ?? 0) + (r.stats.show_details ?? 0), r.stats.financial ? t("・財務交易新增 {0}{1}", [r.stats.financial.new ?? 0, r.stats.financial.still_generating ? t("（報表產生中，下次同步完成）") : ""]) : "", r.stats.failed ? t("・失敗 {0}", [r.stats.failed]) : ""])}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      </div>
    </>
  );
}
