import { useState } from "react";
import { Link, useNavigate, useParams, useSearchParams } from "react-router-dom";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { api } from "../api";
import { useAuth } from "../auth";
import { ErrorBox, Loading, SkuPicker, fmtDateTime, fmtInt, fmtUsd, useDebounced } from "../components/ui";

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
  pending: ["未付款", ""],
  pending_fulfillment: ["已付款・待出貨", "info"],
  completed: ["已完成", "ok"],
  cancelled: ["已取消", ""],
  failed: ["付款失敗", "danger"],
};
const STOCK: Record<string, [string, string]> = {
  none: ["未扣庫存", ""],
  reserved: ["已預留", "info"],
  shipped: ["已出貨扣庫存", "ok"],
  released: ["已釋放", ""],
};
const EXCEPTION: Record<string, string> = {
  unmapped_sku: "待對應 SKU",
  oversold: "超賣",
  insufficient_stock: "出貨時庫存不足",
  non_usd: "非 USD 訂單",
  cancelled_after_shipping: "出貨後取消",
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
      <div className="page-head"><h1>訂單</h1></div>
      <div className="row" style={{ marginBottom: 12 }}>
        {[["orders", "全部訂單"], ["exceptions", "需要處理"], ["unmapped", `待對應商品${unmapped.data?.length ? `（${unmapped.data.length}）` : ""}`]].map(([k, l]) => (
          <button key={k} className={`btn sm ${tab === k ? "primary" : ""}`} onClick={() => set("tab", k === "orders" ? "" : k)}>{l}</button>
        ))}
      </div>
      {tab === "unmapped" ? <UnmappedProducts groups={unmapped.data} loading={unmapped.isLoading} /> : (
        <div className="card">
          <div className="row" style={{ marginBottom: 12 }}>
            <select style={{ flex: "2 1 240px" }} value={show} onChange={(e) => set("show", e.target.value)} aria-label="直播">
              <option value="">全部直播</option>
              {shows.data?.map((s) => <option key={s.id} value={s.id}>{fmtDateTime(s.scheduled_at)} {s.title ?? "（未命名）"}</option>)}
            </select>
            <input style={{ flex: "1 1 180px" }} placeholder="搜尋訂單編號、商品、SKU…" value={search} onChange={(e) => setSearch(e.target.value)} aria-label="搜尋" />
          </div>
          {currentShow && (
            <div className="tiles" style={{ gridTemplateColumns: "repeat(3, minmax(0, 1fr))" }}>
              <div className="tile"><div className="label">已付款訂單</div><div className="value">{fmtInt(currentShow.paid_orders)}</div><div className="sub">共 {fmtInt(currentShow.orders)} 筆</div></div>
              <div className="tile"><div className="label">商品成交額</div><div className="value">{fmtUsd(currentShow.paid_sales)}</div><div className="sub">不含運費、稅</div></div>
              <div className="tile"><div className="label">直播狀態</div><div className="value" style={{ fontSize: 18 }}>{currentShow.status === "ended" ? "已結束" : currentShow.status}</div><div className="sub">損益分析在 Phase 6–7</div></div>
            </div>
          )}
          <ErrorBox error={orders.error} />
          {orders.isLoading ? <Loading /> : !orders.data?.length ? <div className="empty">{tab === "exceptions" ? "沒有需要處理的訂單" : "沒有訂單。到「Whatnot 串接」同步，或匯入 Whatnot 訂單 CSV。"}</div> : (
            <div className="table-wrap">
              <table className="data responsive">
                <thead><tr><th>下單時間</th><th>訂單</th><th>狀態</th><th className="num">件數</th><th className="num">金額</th><th>庫存</th><th>直播</th></tr></thead>
                <tbody>
                  {orders.data.map((o) => (
                    <tr key={o.id} className="click" onClick={() => nav(`/orders/${o.id}`)}>
                      <td className="lead small">{fmtDateTime(o.ordered_at)}</td>
                      <td data-label="訂單" className="mono">{o.external_order_id}{o.transaction_type === "giveaway" && <> <span className="badge">贈品</span></>}</td>
                      <td data-label="狀態"><span className={`badge ${ORDER_STATUS[o.status]?.[1]}`}>{ORDER_STATUS[o.status]?.[0] ?? o.status}</span>
                        {o.exceptions > 0 && <> <span className="badge danger">需處理</span></>}</td>
                      <td data-label="件數" className="num">{fmtInt(o.units)}</td>
                      <td data-label="金額" className="num">{fmtUsd(o.item_subtotal)}</td>
                      <td data-label="庫存">{(o.stock_states ?? "").split(",").filter(Boolean).map((s) => <span key={s} className={`badge ${STOCK[s]?.[1]}`} style={{ marginRight: 4 }}>{STOCK[s]?.[0] ?? s}</span>)}</td>
                      <td data-label="直播" className="small">{o.livestream_title ?? "—"}</td>
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
  if (!groups?.length) return <div className="card empty">所有訂單商品都已對應到 SKU</div>;
  return (
    <div className="card">
      <p className="small muted" style={{ marginTop: 0 }}>
        Whatnot 上同一種商品每次拍賣名稱都不同（#1、#2…），這裡已經把名稱去掉編號合併。每種商品<b>只要對應一次</b>，之前和之後的訂單都會自動套用，並開始預留／扣除美國庫存。
        也可以在 Whatnot 商品設定 SKU（external ID）等於 ERP 的 SKU 編號，就不用手動對應。
      </p>
      <div className="table-wrap">
        <table className="data responsive">
          <thead><tr><th>Whatnot 商品</th><th className="num">訂單</th><th className="num">件數</th><th className="num">成交額</th>{can("catalog.edit") && <th>對應到 ERP 的 SKU</th>}</tr></thead>
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
      <td data-label="訂單" className="num">{fmtInt(g.lines)}</td>
      <td data-label="件數" className="num">{fmtInt(g.units)}</td>
      <td data-label="成交額" className="num">{fmtUsd(g.sales)}</td>
      {canMap && (
        <td data-label="對應 SKU" style={{ minWidth: 260 }}>
          <ErrorBox error={m.error} />
          <div className="row" style={{ flexWrap: "nowrap" }}>
            <div style={{ flex: 1 }}><SkuPicker value={sku} onChange={setSku} excludeBundles={false} /></div>
            <button className="btn sm primary" disabled={!sku || m.isPending} onClick={() => m.mutate()}>對應</button>
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
          <div className="small muted"><Link to="/orders">訂單</Link>{d.livestream && <> / <Link to={`/orders?show=${d.livestream.id}`}>{d.livestream.title ?? "直播"}</Link></>}</div>
          <h1 className="mono">#{d.external_order_id} <span className={`badge ${ORDER_STATUS[d.status]?.[1]}`}>{ORDER_STATUS[d.status]?.[0] ?? d.status}</span></h1>
          <div className="small muted">下單 {fmtDateTime(d.ordered_at)}・來源 {d.source === "csv" ? "CSV 匯入" : d.source === "webhook" ? "Whatnot 即時通知" : "Whatnot API"}・最後同步 {fmtDateTime(d.last_synced_at)}</div>
        </div>
        {hasException && can("inventory.adjust_us") && <button className="btn" onClick={() => reprocess.mutate()} disabled={reprocess.isPending}>重新處理</button>}
      </div>
      <ErrorBox error={map.error ?? reprocess.error ?? remap.error} />
      <div className="grid-2">
        <div className="card">
          <h2>金額</h2>
          <dl className="kv">
            <dt>商品金額</dt><dd>{fmtUsd(d.item_subtotal)}</dd>
            {Number(d.discount_total) > 0 && <><dt>賣家折扣</dt><dd>−{fmtUsd(d.discount_total)}</dd></>}
            <dt>買家付的運費</dt><dd>{fmtUsd(d.shipping_charged)}<span className="small muted">（不計營收）</span></dd>
            <dt>銷售稅</dt><dd>{fmtUsd(d.sales_tax_collected)}<span className="small muted">（Whatnot 代收，不計營收）</span></dd>
            <dt>買家付款合計</dt><dd>{fmtUsd(d.total)}</dd>
          </dl>
          <p className="small muted" style={{ marginBottom: 0 }}>平台佣金與手續費以 Whatnot 財務報表為準（Phase 6）。</p>
        </div>
        <div className="card">
          <h2>商品</h2>
          {d.lines.map((l) => (
            <div key={l.id} style={{ borderTop: "1px solid var(--border)", padding: "10px 0" }}>
              <div className="small">{l.title_snapshot}</div>
              <div className="row small" style={{ gap: 6, marginTop: 4 }}>
                <span>{l.qty} × {fmtUsd(l.unit_price)}</span>
                {l.sku_code ? <Link className="mono" to={`/skus/${l.sku_id}`}>{l.sku_code}</Link> : <span className="badge warn">未對應 SKU</span>}
                <span className={`badge ${STOCK[l.stock_state]?.[1]}`}>{STOCK[l.stock_state]?.[0]}</span>
                {l.shipment_status && <span className="badge">出貨：{l.shipment_status}</span>}
                {l.cogs && <span className="muted">成本 {fmtUsd(l.cogs)}</span>}
              </div>
              {l.exception_code && <div className="alert error small" style={{ marginTop: 6, marginBottom: 0 }}><b>{EXCEPTION[l.exception_code] ?? l.exception_code}</b>：{l.exception_detail}</div>}
              {l.sku_id && can("catalog.edit") && can("inventory.adjust_us") && (changing !== l.id ? (
                <button className="btn sm" style={{ marginTop: 6 }} onClick={() => setChanging(l.id)}>更改 SKU</button>
              ) : (
                <div style={{ marginTop: 6 }}>
                  <div className="alert info small" style={{ marginBottom: 6 }}>會套用到這個 Whatnot 商品的所有訂單。已出貨的會把庫存退回原 SKU、沖銷成本，再用新的 SKU 重新扣庫存；營收不變。</div>
                  <div className="row" style={{ flexWrap: "nowrap" }}>
                    <div style={{ flex: 1 }}><SkuPicker value={sku[l.id] ?? null} onChange={(v) => setSku({ ...sku, [l.id]: v })} excludeBundles={false} excludeIds={[l.sku_id]} /></div>
                    <button className="btn sm primary" disabled={!sku[l.id] || remap.isPending} onClick={() => remap.mutate({ lineId: l.id, skuId: sku[l.id] ?? null })}>改為這個 SKU</button>
                  </div>
                  <div className="row" style={{ marginTop: 6 }}>
                    <button className="btn sm danger" disabled={remap.isPending} onClick={() => confirm("取消這個商品的 SKU 對應？已扣的庫存與成本會退回。") && remap.mutate({ lineId: l.id, skuId: null })}>取消對應</button>
                    <button className="btn sm" onClick={() => setChanging(null)}>取消</button>
                  </div>
                </div>
              ))}
              {!l.sku_id && can("catalog.edit") && (
                <div className="row" style={{ marginTop: 6, flexWrap: "nowrap" }}>
                  <div style={{ flex: 1 }}><SkuPicker value={sku[l.id] ?? null} onChange={(v) => setSku({ ...sku, [l.id]: v })} excludeBundles={false} /></div>
                  <button className="btn sm primary" disabled={!sku[l.id] || map.isPending} onClick={() => map.mutate(l.id)}>對應</button>
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
    onSuccess: () => (setToken(""), setSecret(""), done("已加密儲存。請按「測試連線」確認。")),
  });
  const test = useMutation({
    mutationFn: () => api<{ seller: { name: string } }>("/integrations/whatnot/test", { method: "POST" }),
    onSuccess: (r) => done(`連線成功：${r.seller.name}`),
  });
  const sync = useMutation({
    mutationFn: () => api("/integrations/whatnot/sync", { body: { days: Number(days) } }),
    onSuccess: () => done("同步已開始，完成後下方會出現結果。"),
  });
  if (st.isLoading) return <Loading />;
  if (!st.data) return <ErrorBox error={st.error} />;
  const s = st.data;
  return (
    <>
      <div className="page-head"><h1>Whatnot 串接</h1></div>
      {notice && <div className="alert ok">{notice}</div>}
      <ErrorBox error={save.error ?? test.error ?? sync.error} />
      <div className="tiles" style={{ gridTemplateColumns: "repeat(3, minmax(0, 1fr))" }}>
        <div className="tile"><div className="label">連線</div><div className="value" style={{ fontSize: 18 }}>{s.connected ? "已連線" : s.token_source ? "待測試" : "未設定"}</div><div className="sub">{s.seller?.name ?? "—"}</div></div>
        <div className="tile"><div className="label">即時通知（Webhook）</div><div className="value" style={{ fontSize: 18 }}>{s.webhook_secret_configured ? "已設定" : "未設定"}</div>
          <div className="sub">已處理 {fmtInt(s.webhook_events.processed ?? 0)}・失敗 {fmtInt(s.webhook_events.failed ?? 0)}</div></div>
        <div className="tile"><div className="label">需要處理的訂單商品</div><div className="value">{fmtInt(s.open_exceptions)}</div><div className="sub"><Link to="/orders?tab=exceptions">查看</Link></div></div>
      </div>
      <div className="grid-2">
        <form className="card" onSubmit={(e) => (e.preventDefault(), save.mutate())}>
          <h2>API 金鑰</h2>
          <p className="small muted" style={{ marginTop: 0 }}>
            在 Whatnot 開發者後台 › Personal access tokens 產生（權限：read:orders、read:shows、read:shipments、read:inventory、read:reports、write:reports）。
            金鑰加密保存，儲存後任何人（包含管理員）都無法再看到內容。{s.token_source === "environment" && " 目前使用伺服器環境設定的金鑰。"}
          </p>
          <label className="field"><span>API 金鑰（wn_pat_tk_…）</span><input type="password" autoComplete="off" value={token} onChange={(e) => setToken(e.target.value)} /></label>
          <label className="field"><span>Webhook secret（wn_wh_…）</span><input type="password" autoComplete="off" value={secret} onChange={(e) => setSecret(e.target.value)} /></label>
          <div className="row">
            <button className="btn primary" disabled={(!token && !secret) || save.isPending}>儲存</button>
            <button type="button" className="btn" disabled={!s.token_source || test.isPending} onClick={() => test.mutate()}>測試連線</button>
          </div>
          <p className="small muted">在 Whatnot 開發者後台設定 Webhook 網址為：<br /><span className="mono">{s.webhook_url}</span><br />主題選 orders.*、shipments.*、shows.*。</p>
        </form>
        <div className="card">
          <h2>同步</h2>
          <p className="small muted" style={{ marginTop: 0 }}>即時通知會自動更新；同步用來補漏（例如伺服器維護期間的訂單）。正式上線後會每小時自動執行。</p>
          <div className="row">
            <select value={days} onChange={(e) => setDays(e.target.value)} style={{ width: "auto" }} aria-label="同步天數">
              {["1", "3", "7", "30"].map((d) => <option key={d} value={d}>最近 {d} 天</option>)}
            </select>
            <button className="btn primary" disabled={!s.token_source || sync.isPending} onClick={() => sync.mutate()}>立即同步</button>
          </div>
          <table className="data" style={{ marginTop: 12 }}>
            <thead><tr><th>開始</th><th>結果</th><th>內容</th></tr></thead>
            <tbody>
              {s.sync_runs.map((r) => (
                <tr key={r.id}>
                  <td className="small">{fmtDateTime(r.started_at)}</td>
                  <td><span className={`badge ${r.status === "succeeded" ? "ok" : r.status === "running" ? "info" : "danger"}`}>{r.status === "succeeded" ? "成功" : r.status === "running" ? "進行中" : "失敗"}</span></td>
                  <td className="small">{r.error ?? `訂單 ${r.stats.orders ?? 0}・出貨 ${r.stats.shipments ?? 0}・直播 ${(r.stats.shows ?? 0) + (r.stats.show_details ?? 0)}${r.stats.failed ? `・失敗 ${r.stats.failed}` : ""}`}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      </div>
    </>
  );
}
