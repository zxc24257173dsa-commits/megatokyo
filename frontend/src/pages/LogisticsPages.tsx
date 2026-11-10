import { useState } from "react";
import { Link, useNavigate, useParams, useSearchParams } from "react-router-dom";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { api, newIdempotencyKey } from "../api";
import { useAuth } from "../auth";
import { ErrorBox, Field, Loading, fmtDate, fmtDateTime, fmtInt } from "../components/ui";
import { money } from "./ShowReportPages";
import { ORDER_STATUS } from "./OrderPages";
import { t } from "../i18n";

const TZ = "America/New_York";
const today = () => new Date().toLocaleDateString("en-CA", { timeZone: TZ });

export interface Carrier { id: number; name: string; has_tracking_link: boolean }
interface Parcel {
  id: number; shipment_no: string; status: string; tracking_no: string | null; tracking_url: string | null;
  carrier: string | null; carrier_id: number | null; shipped_at: string; eta: string | null; received_at: string | null;
  packages: number | null; note: string | null; units: number; received: number; lost: number; in_transit: number;
  lines: number; value_in_transit: number | null; overdue_days: number; days_in_transit: number | null;
}
interface ParcelLine {
  id: number; sku_id: number; sku_code: string; name: string; batch_no: string | null; qty_shipped: number;
  qty_received: number; qty_lost: number; in_transit: number; discrepancy_reason: string | null; unit_cost: number | null;
}
interface ParcelDetail extends Omit<Parcel, "lines"> {
  lines: ParcelLine[];
  events: { event: string; occurred_at: string; data: Record<string, unknown>; user_name: string | null }[];
}
interface LeadTime { parcels: number; average_days: number | null; median_days: number | null; max_days: number | null; planned_days: number }
interface Backlog {
  older_than_days: number; orders: number; late_orders: number; units: number;
  items: { id: number; external_order_id: string; ordered_at: string; status: string; livestream_title: string | null; units: number; shipment_status: string | null; has_issue: boolean; age_days: number }[];
}

export function useCarriers() {
  return useQuery({ queryKey: ["carriers"], queryFn: () => api<Carrier[]>("/carriers"), staleTime: 10 * 60_000 });
}

function ParcelStatus({ p }: { p: Pick<Parcel, "status" | "overdue_days" | "received"> }) {
  if (p.status === "received") return <span className="badge ok">{t("已到貨")}</span>;
  if (p.overdue_days > 0) return <span className="badge danger">{t("逾期")} {p.overdue_days} {t("天")}</span>;
  return <span className="badge info">{p.received ? t("部分簽收") : t("運送中")}</span>;
}

function Tracking({ p }: { p: Pick<Parcel, "carrier" | "tracking_no" | "tracking_url"> }) {
  if (!p.tracking_no) return <span className="muted">{p.carrier ? t(p.carrier) : t("未填追蹤號碼")}</span>;
  return (
    <span>
      {p.carrier && <span className="muted">{t(p.carrier)} </span>}
      {p.tracking_url ? <a href={p.tracking_url} target="_blank" rel="noreferrer" className="mono">{p.tracking_no}</a> : <span className="mono">{p.tracking_no}</span>}
    </span>
  );
}

export function LogisticsPage() {
  const { can } = useAuth();
  const nav = useNavigate();
  const [params, setParams] = useSearchParams();
  const tab = params.get("tab") ?? "inbound";
  const parcels = useQuery({ queryKey: ["parcels", tab], queryFn: () => api<Parcel[]>("/parcels", { params: { status: tab === "arrived" ? "received" : "open" } }), enabled: tab !== "outbound" });
  const lead = useQuery({ queryKey: ["lead-time"], queryFn: () => api<LeadTime>("/logistics/lead-time") });
  const backlog = useQuery({ queryKey: ["backlog"], queryFn: () => api<Backlog>("/logistics/outbound-backlog"), enabled: can("orders.view") });
  const open = tab === "inbound" ? parcels.data ?? [] : [];
  const overdue = open.filter((p) => p.overdue_days > 0).length;
  const valueTransit = open.reduce((a, p) => a + (p.value_in_transit ?? 0), 0);

  return (
    <>
      <div className="page-head"><h1>{t("物流")}</h1></div>
      <div className="tiles">
        <div className="tile"><div className="label">{t("運送中包裹")}</div><div className="value">{tab === "inbound" && parcels.data ? fmtInt(open.length) : "—"}</div>
          <div className="sub">{overdue ? <span style={{ color: "var(--danger)" }}>{overdue} {t("個逾期")}</span> : t("沒有逾期")}</div></div>
        <div className="tile"><div className="label">{t("運送中商品價值")}</div><div className="value">{tab === "inbound" && parcels.data?.some((p) => p.value_in_transit !== null) ? money(valueTransit) : "—"}</div><div className="sub">{t("落地成本；還不能賣")}</div></div>
        <div className="tile"><div className="label">{t("實際交期")}</div><div className="value">{lead.data?.average_days != null ? t("{0} 天", [lead.data.average_days]) : "—"}</div>
          <div className="sub">{lead.data?.parcels ? t("{0} 個包裹，最長 {1} 天", [lead.data.parcels, lead.data.max_days]) : t("尚無到貨紀錄")}{t("・規劃")} {lead.data?.planned_days ?? "—"} {t("天")}</div></div>
        {can("orders.view") && (
          <div className="tile"><div className="label">{t("待出貨訂單")}</div><div className="value">{backlog.data ? fmtInt(backlog.data.orders) : "—"}</div>
            <div className="sub">{backlog.data?.late_orders ? <span style={{ color: "var(--danger)" }}>{backlog.data.late_orders} {t("筆超過")} {backlog.data.older_than_days} {t("天")}</span> : t("已付款、還沒寄出")}</div></div>
        )}
      </div>
      <div className="row" style={{ marginBottom: 12 }}>
        {[["inbound", t("日本 → 美國 運送中")], ["arrived", t("已到貨")], ...(can("orders.view") ? [["outbound", t("美國待出貨")]] : [])].map(([k, l]) => (
          <button key={k} className={`btn sm ${tab === k ? "primary" : ""}`} onClick={() => setParams(k === "inbound" ? {} : { tab: k })}>{l}</button>
        ))}
      </div>

      {tab === "outbound" ? <BacklogView q={backlog} /> : (
        <div className="card">
          {tab === "inbound" && can("shipment.manage_jp") && (
            <p className="small muted" style={{ marginTop: 0 }}>{t("要寄新的包裹：到")} <Link to="/purchasing">{t("採購批次")}</Link> {t("打開批次，按「建立寄出包裹」。")}</p>
          )}
          <ErrorBox error={parcels.error} />
          {parcels.isLoading ? <Loading /> : !parcels.data?.length ? <div className="empty">{tab === "inbound" ? t("沒有運送中的包裹") : t("還沒有到貨的包裹")}</div> : (
            <div className="table-wrap">
              <table className="data responsive">
                <thead><tr><th>{t("包裹")}</th><th>{t("狀態")}</th><th>{t("追蹤")}</th><th>{t("寄出")}</th><th>{t("預計到貨")}</th><th className="num">{t("件數")}</th><th className="num">{tab === "inbound" ? t("運送中") : t("短少")}</th></tr></thead>
                <tbody>
                  {parcels.data.map((p) => (
                    <tr key={p.id} className="click" onClick={() => nav(`/logistics/parcels/${p.id}`)}>
                      <td className="lead mono">{p.shipment_no}{p.packages ? <span className="small muted">{t("（")}{p.packages} {t("箱）")}</span> : null}</td>
                      <td data-label={t("狀態")}><ParcelStatus p={p} /></td>
                      <td data-label={t("追蹤")} onClick={(e) => e.stopPropagation()}><Tracking p={p} /></td>
                      <td data-label={t("寄出")} className="small">{fmtDate(p.shipped_at, TZ)}</td>
                      <td data-label={t("預計到貨")} className="small">{p.status === "received" ? t("已到 {0}（{1} 天）", [fmtDate(p.received_at, TZ), p.days_in_transit]) : fmtDate(p.eta)}</td>
                      <td data-label={t("件數")} className="num">{fmtInt(p.units)}</td>
                      <td data-label={tab === "inbound" ? t("運送中") : t("短少")} className="num">{tab === "inbound" ? fmtInt(p.in_transit) : p.lost ? <span className="badge danger">{fmtInt(p.lost)}</span> : "—"}</td>
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

function BacklogView({ q }: { q: { data?: Backlog; isLoading: boolean; error: unknown } }) {
  const nav = useNavigate();
  return (
    <div className="card">
      <p className="small muted" style={{ marginTop: 0 }}>{t("已付款、商品還沒離開美國倉的 Whatnot 訂單（出貨狀態由 Whatnot 同步）。下單超過")} {q.data?.older_than_days ?? 2} {t("天標紅。")}</p>
      <ErrorBox error={q.error} />
      {q.isLoading ? <Loading /> : !q.data?.items.length ? <div className="empty">{t("沒有待出貨的訂單")}</div> : (
        <div className="table-wrap">
          <table className="data responsive">
            <thead><tr><th>{t("下單")}</th><th>{t("訂單")}</th><th>{t("狀態")}</th><th className="num">{t("件數")}</th><th className="num">{t("等了")}</th><th>{t("直播")}</th></tr></thead>
            <tbody>
              {q.data.items.map((o) => (
                <tr key={o.id} className="click" onClick={() => nav(`/orders/${o.id}`)}>
                  <td className="lead small">{fmtDateTime(o.ordered_at, TZ)}</td>
                  <td data-label={t("訂單")} className="mono">{o.external_order_id}</td>
                  <td data-label={t("狀態")}><span className={`badge ${ORDER_STATUS[o.status]?.[1]}`}>{ORDER_STATUS[o.status]?.[0] ?? o.status}</span>{o.has_issue && <> <span className="badge danger">{t("需處理")}</span></>}</td>
                  <td data-label={t("件數")} className="num">{fmtInt(o.units)}</td>
                  <td data-label={t("等了")} className="num">{o.age_days >= q.data!.older_than_days ? <span className="badge danger">{o.age_days.toFixed(1)} {t("天")}</span> : t("{0} 天", [o.age_days.toFixed(1)])}</td>
                  <td data-label={t("直播")} className="small">{o.livestream_title ?? "—"}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
    </div>
  );
}

const EVENT_LABEL: Record<string, string> = { shipped: t("從日本寄出"), updated: t("更新追蹤資訊"), partially_received: t("部分簽收"), received: t("全部到貨") };

export function ParcelPage() {
  const id = Number(useParams().id);
  const { can } = useAuth();
  const qc = useQueryClient();
  const carriers = useCarriers();
  const q = useQuery({ queryKey: ["parcel", id], queryFn: () => api<ParcelDetail>(`/parcels/${id}`) });
  const [rows, setRows] = useState<Record<number, { received: string; lost: string; reason: string }>>({});
  const [day, setDay] = useState(today());
  const [key, setKey] = useState(newIdempotencyKey);
  const [edit, setEdit] = useState<{ carrier_id: string; tracking_no: string; eta: string } | null>(null);
  const refresh = (d: ParcelDetail) => {
    qc.setQueryData(["parcel", id], d);
    void qc.invalidateQueries({ queryKey: ["parcels"] });
    void qc.invalidateQueries({ queryKey: ["lead-time"] });
    void qc.invalidateQueries({ queryKey: ["skus"] });
  };
  const recv = useMutation({
    mutationFn: () => api<ParcelDetail>(`/parcels/${id}/receive`, {
      idempotencyKey: key,
      body: { received_on: day, lines: Object.entries(rows).map(([lid, r]) => ({ shipment_line_id: Number(lid), received: Number(r.received || 0), lost: Number(r.lost || 0), lost_reason: r.reason || null })) },
    }),
    onSuccess: (d) => { refresh(d); setRows({}); setKey(newIdempotencyKey()); },
  });
  const upd = useMutation({
    mutationFn: () => api<ParcelDetail>(`/parcels/${id}`, { method: "PATCH", body: { carrier_id: edit!.carrier_id ? Number(edit!.carrier_id) : null, tracking_no: edit!.tracking_no || null, eta: edit!.eta || null, packages: q.data?.packages ?? null, note: q.data?.note ?? null } }),
    onSuccess: (d) => { refresh(d); setEdit(null); },
  });
  if (q.isLoading) return <Loading />;
  if (!q.data) return <ErrorBox error={q.error} />;
  const p = q.data;
  const canReceive = can("shipment.receive_us") && p.in_transit > 0;
  const set = (lid: number, k: "received" | "lost" | "reason", v: string) => setRows((x) => ({ ...x, [lid]: { ...(x[lid] ?? { received: "", lost: "", reason: "" }), [k]: v } }));
  const fillAll = () => setRows(Object.fromEntries(p.lines.filter((l) => l.in_transit > 0).map((l) => [l.id, { received: String(l.in_transit), lost: "", reason: "" }])));

  return (
    <>
      <div className="page-head">
        <div>
          <div className="small"><Link to="/logistics">{t("← 物流")}</Link></div>
          <h1 className="mono" style={{ marginBottom: 4 }}>{p.shipment_no} <ParcelStatus p={p} /></h1>
          <div className="small muted">
            {fmtDate(p.shipped_at, TZ)} {t("從日本寄出・")}<Tracking p={p} />・{p.status === "received" ? t("{0} 全部到貨（{1} 天）", [fmtDate(p.received_at, TZ), p.days_in_transit]) : t("預計 {0} 到美國", [fmtDate(p.eta)])}
            {p.packages ? t("・{0} 箱", [p.packages]) : ""}
          </div>
        </div>
        {(can("shipment.manage_jp") || can("shipment.receive_us")) && !edit && (
          <button className="btn" onClick={() => setEdit({ carrier_id: p.carrier_id ? String(p.carrier_id) : "", tracking_no: p.tracking_no ?? "", eta: p.eta ?? "" })}>{t("更新追蹤資訊")}</button>
        )}
      </div>
      {p.note && <div className="alert info small">{p.note}</div>}
      {edit && (
        <form className="card" onSubmit={(e) => (e.preventDefault(), upd.mutate())}>
          <ErrorBox error={upd.error} />
          <div className="grid-2">
            <Field label={t("物流商")}><select value={edit.carrier_id} onChange={(e) => setEdit({ ...edit, carrier_id: e.target.value })}><option value="">{t("未指定")}</option>{carriers.data?.map((c) => <option key={c.id} value={c.id}>{t(c.name)}</option>)}</select></Field>
            <Field label={t("追蹤號碼")}><input value={edit.tracking_no} onChange={(e) => setEdit({ ...edit, tracking_no: e.target.value })} /></Field>
            <Field label={t("預計到貨日")}><input type="date" value={edit.eta} onChange={(e) => setEdit({ ...edit, eta: e.target.value })} /></Field>
          </div>
          <div className="row"><button className="btn primary" disabled={upd.isPending}>{t("儲存")}</button><button type="button" className="btn" onClick={() => setEdit(null)}>{t("取消")}</button></div>
        </form>
      )}
      {p.overdue_days > 0 && <div className="alert error">{t("已超過預計到貨日")} {p.overdue_days} {t("天。請用追蹤號碼確認位置，必要時更新預計到貨日。")}</div>}

      <div className="card">
        <h2>{t("內容")}</h2>
        <ErrorBox error={recv.error} />
        <div className="table-wrap">
          <table className="data responsive">
            <thead><tr><th>SKU</th><th>{t("採購批次")}</th><th className="num">{t("寄出")}</th><th className="num">{t("已簽收")}</th><th className="num">{t("短少")}</th><th className="num">{t("運送中")}</th>{canReceive && <th>{t("這次簽收")}</th>}</tr></thead>
            <tbody>
              {p.lines.map((l) => {
                const r = rows[l.id] ?? { received: "", lost: "", reason: "" };
                return (
                  <tr key={l.id}>
                    <td className="lead"><Link to={`/skus/${l.sku_id}`} className="mono">{l.sku_code}</Link><div className="small muted" style={{ fontWeight: 400 }}>{l.name}{l.unit_cost !== null ? t("・{0}/件", [money(l.unit_cost)]) : ""}</div></td>
                    <td data-label={t("採購批次")} className="small mono">{l.batch_no ?? "—"}</td>
                    <td data-label={t("寄出")} className="num">{fmtInt(l.qty_shipped)}</td>
                    <td data-label={t("已簽收")} className="num">{fmtInt(l.qty_received)}</td>
                    <td data-label={t("短少")} className="num">{l.qty_lost ? <span className="badge danger" title={l.discrepancy_reason ?? ""}>{fmtInt(l.qty_lost)}</span> : "—"}{l.discrepancy_reason && <div className="small muted">{l.discrepancy_reason}</div>}</td>
                    <td data-label={t("運送中")} className="num">{fmtInt(l.in_transit)}</td>
                    {canReceive && (
                      <td data-label={t("這次簽收")} style={{ minWidth: 230 }}>
                        {l.in_transit > 0 && (
                          <div className="row" style={{ flexWrap: "wrap", gap: 6 }}>
                            <input aria-label={t("{0} 收到", [l.sku_code])} inputMode="numeric" placeholder={t("收到")} value={r.received} onChange={(e) => set(l.id, "received", e.target.value)} style={{ width: 72 }} />
                            <input aria-label={t("{0} 短少或損壞", [l.sku_code])} inputMode="numeric" placeholder={t("短少/損壞")} value={r.lost} onChange={(e) => set(l.id, "lost", e.target.value)} style={{ width: 92 }} />
                            {Number(r.lost) > 0 && <input aria-label={t("{0} 原因", [l.sku_code])} placeholder={t("原因（必填）")} value={r.reason} onChange={(e) => set(l.id, "reason", e.target.value)} style={{ flex: "1 1 140px" }} />}
                          </div>
                        )}
                      </td>
                    )}
                  </tr>
                );
              })}
            </tbody>
          </table>
        </div>
        {canReceive && (
          <div className="row" style={{ marginTop: 12, alignItems: "end" }}>
            <button type="button" className="btn" onClick={fillAll}>{t("全部收到")}</button>
            <label className="small" style={{ flex: "0 1 170px" }}>{t("簽收日期")}<input type="date" value={day} max={today()} onChange={(e) => setDay(e.target.value)} /></label>
            <button className="btn primary" disabled={recv.isPending || !Object.values(rows).some((r) => Number(r.received) > 0 || Number(r.lost) > 0)} onClick={() => recv.mutate()}>
              {recv.isPending ? t("處理中…") : t("確認簽收")}
            </button>
            <span className="small muted">{t("收到的件數立即變成美國可售庫存；沒填的留在運送中，之後可以再簽收。短少／損壞會記為存貨損耗（扣毛利）。")}</span>
          </div>
        )}
      </div>

      <div className="card">
        <h2>{t("紀錄")}</h2>
        <ul className="small" style={{ margin: 0, paddingLeft: 18 }}>
          {p.events.map((e, i) => (
            <li key={i}>{fmtDateTime(e.occurred_at, TZ)} · {EVENT_LABEL[e.event] ?? e.event}{e.user_name ? t("（{0}）", [e.user_name]) : ""}
              {e.event === "partially_received" && typeof e.data.still_in_transit === "number" ? t("，還有 {0} 件在途中", [e.data.still_in_transit]) : ""}</li>
          ))}
        </ul>
      </div>
    </>
  );
}

export function CreateParcelCard({ lines, batchId }: { lines: { id: number; sku_code: string; name: string; qty: number; shipped: number }[]; batchId: number }) {
  const nav = useNavigate();
  const qc = useQueryClient();
  const carriers = useCarriers();
  const open = lines.filter((l) => l.shipped < l.qty);
  const [qty, setQty] = useState<Record<number, string>>(() => Object.fromEntries(open.map((l) => [l.id, String(l.qty - l.shipped)])));
  const [f, setF] = useState({ shipped_on: today(), carrier_id: "", tracking_no: "", packages: "", note: "" });
  const [key] = useState(newIdempotencyKey);
  const [show, setShow] = useState(false);
  const m = useMutation({
    mutationFn: () => api<ParcelDetail>("/parcels", {
      idempotencyKey: key,
      body: {
        lines: open.filter((l) => Number(qty[l.id]) > 0).map((l) => ({ purchase_line_id: l.id, qty: Number(qty[l.id]) })),
        shipped_on: f.shipped_on, carrier_id: f.carrier_id ? Number(f.carrier_id) : null, tracking_no: f.tracking_no || null,
        packages: f.packages ? Number(f.packages) : null, note: f.note || null,
      },
    }),
    onSuccess: (d) => {
      void qc.invalidateQueries({ queryKey: ["batch", batchId] });
      void qc.invalidateQueries({ queryKey: ["parcels"] });
      nav(`/logistics/parcels/${d.id}`);
    },
  });
  if (!open.length) return null;
  if (!show) return <div className="row" style={{ marginBottom: 12 }}><button className="btn primary" onClick={() => setShow(true)}>{t("建立寄出包裹")}</button><span className="small muted">{t("把這個批次（或其中一部分）一起寄到美國，填一個追蹤號碼。")}</span></div>;
  const units = open.reduce((a, l) => a + Number(qty[l.id] || 0), 0);
  return (
    <form className="card" onSubmit={(e) => (e.preventDefault(), m.mutate())}>
      <h2>{t("建立寄出包裹")}</h2>
      <ErrorBox error={m.error} />
      <div className="table-wrap">
        <table className="data responsive">
          <thead><tr><th>SKU</th><th className="num">{t("尚未寄出")}</th><th>{t("這次寄出")}</th></tr></thead>
          <tbody>
            {open.map((l) => (
              <tr key={l.id}>
                <td className="lead"><span className="mono">{l.sku_code}</span> <span className="small muted">{l.name}</span></td>
                <td data-label={t("尚未寄出")} className="num">{fmtInt(l.qty - l.shipped)}</td>
                <td data-label={t("這次寄出")}><input aria-label={t("{0} 寄出數量", [l.sku_code])} inputMode="numeric" value={qty[l.id] ?? ""} onChange={(e) => setQty({ ...qty, [l.id]: e.target.value })} style={{ width: 90 }} /></td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
      <div className="grid-2">
        <Field label={t("寄出日期")}><input type="date" required value={f.shipped_on} max={today()} onChange={(e) => setF({ ...f, shipped_on: e.target.value })} /></Field>
        <Field label={t("物流商")}><select value={f.carrier_id} onChange={(e) => setF({ ...f, carrier_id: e.target.value })}><option value="">{t("之後再填")}</option>{carriers.data?.map((c) => <option key={c.id} value={c.id}>{t(c.name)}</option>)}</select></Field>
        <Field label={t("追蹤號碼（選填，之後也可以補）")}><input value={f.tracking_no} onChange={(e) => setF({ ...f, tracking_no: e.target.value })} /></Field>
        <Field label={t("箱數（選填）")}><input inputMode="numeric" value={f.packages} onChange={(e) => setF({ ...f, packages: e.target.value })} /></Field>
      </div>
      <div className="row">
        <button className="btn primary" disabled={m.isPending || units === 0}>{m.isPending ? t("建立中…") : t("寄出 {0} 件", [fmtInt(units)])}</button>
        <button type="button" className="btn" onClick={() => setShow(false)}>{t("取消")}</button>
        <span className="small muted">{t("寄出後變成「運送中」，還不能賣；美國收到後在包裹頁簽收。")}</span>
      </div>
    </form>
  );
}
