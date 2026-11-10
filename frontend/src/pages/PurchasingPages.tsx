import { useState } from "react";
import { Link, useNavigate, useParams } from "react-router-dom";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { CreateParcelCard } from "./LogisticsPages";
import { api, newIdempotencyKey } from "../api";
import { useAuth } from "../auth";
import { ErrorBox, Loading, fmtDate, fmtDateTime, fmtInt, fmtUsd, useDebounced } from "../components/ui";

interface BatchRow {
  id: number;
  batch_no: string;
  purchase_date: string;
  supplier: string | null;
  line_count: number;
  qty: number;
  shipped: number;
  in_transit: number;
  received_us: number;
  landed_total?: string;
}

interface Line {
  id: number;
  line_no: number;
  sku_id: number;
  sku_code: string;
  name: string;
  qty: number;
  shipped: number;
  in_transit: number;
  received_us: number;
  shipments: { shipped_at: string; qty: number; in_transit: number; eta: string; parcel_id: number | null; parcel_no: string | null }[];
  unparcelled_in_transit: number;
  note: string | null;
  unit_price_orig?: string;
  currency_orig?: string;
  fx_rate_to_base?: string;
  fx_rate_quote?: string | null;
  purchase_cost_base?: string;
  domestic_shipping_jpy?: string;
  intl_freight_jpy?: string;
  domestic_shipping_base?: string;
  intl_freight_base?: string;
  duty_other_base?: string;
  landed_total_base?: string;
  landed_unit_cost_base?: string;
}

function Progress({ qty, shipped, in_transit, received_us }: { qty: number; shipped: number; in_transit: number; received_us: number }) {
  if (received_us >= qty) return <span className="badge ok">美國已全部簽收</span>;
  return (
    <span className="row" style={{ gap: 4, display: "inline-flex" }}>
      <span className={`badge ${shipped >= qty ? "ok" : shipped ? "warn" : ""}`}>已寄出 {fmtInt(shipped)} / {fmtInt(qty)}</span>
      {in_transit > 0 && <span className="badge info">運送中 {fmtInt(in_transit)}</span>}
      {received_us > 0 && <span className="badge ok">美國已收 {fmtInt(received_us)}</span>}
    </span>
  );
}

export function PurchaseBatchesPage() {
  const nav = useNavigate();
  const { can } = useAuth();
  const [search, setSearch] = useState("");
  const [onlyOpen, setOnlyOpen] = useState(false);
  const q = useDebounced(search);
  const list = useQuery({ queryKey: ["batches", q, onlyOpen], queryFn: () => api<BatchRow[]>("/purchase-batches", { params: { search: q, only_open: onlyOpen } }) });
  const showCost = list.data?.some((b) => b.landed_total !== undefined);
  return (
    <>
      <div className="page-head">
        <h1>採購批次</h1>
        {(can("import.cost") || can("catalog.edit")) && <Link className="btn primary" to="/imports">匯入成本試算表</Link>}
      </div>
      <div className="card">
        <div className="row" style={{ marginBottom: 12 }}>
          <input style={{ flex: "1 1 220px" }} placeholder="搜尋批次編號、SKU…" value={search} onChange={(e) => setSearch(e.target.value)} aria-label="搜尋" />
          <label className="row small"><input type="checkbox" checked={onlyOpen} onChange={(e) => setOnlyOpen(e.target.checked)} /> 只看美國尚未全部簽收</label>
        </div>
        <ErrorBox error={list.error} />
        {list.isLoading ? <Loading /> : !list.data?.length ? <div className="empty">沒有採購批次。到「試算表匯入」上傳成本試算表。</div> : (
          <table className="data responsive">
            <thead><tr><th>批次</th><th>採購日</th><th>供應商</th><th className="num">SKU 數</th><th>進度</th>{showCost && <th className="num">落地成本合計</th>}</tr></thead>
            <tbody>
              {list.data.map((b) => (
                <tr key={b.id} className="click" onClick={() => nav(`/purchasing/${b.id}`)}>
                  <td className="lead mono">{b.batch_no}</td>
                  <td data-label="採購日">{fmtDate(b.purchase_date)}</td>
                  <td data-label="供應商">{b.supplier ?? "—"}</td>
                  <td data-label="SKU 數" className="num">{b.line_count}</td>
                  <td data-label="進度"><Progress {...b} /></td>
                  {showCost && <td data-label="落地成本合計" className="num">{fmtUsd(b.landed_total)}</td>}
                </tr>
              ))}
            </tbody>
          </table>
        )}
      </div>
    </>
  );
}

export function PurchaseBatchPage() {
  const id = Number(useParams().id);
  const { can } = useAuth();
  const batch = useQuery({
    queryKey: ["batch", id],
    queryFn: () => api<{ id: number; batch_no: string; purchase_date: string; supplier: string | null; confirmed_at: string; import_job_id: number | null; can_view_cost: boolean; lead_time_days: number; lines: Line[] }>(`/purchase-batches/${id}`),
  });
  if (batch.isLoading) return <Loading />;
  if (!batch.data) return <ErrorBox error={batch.error} />;
  const b = batch.data;
  const cost = b.can_view_cost;
  return (
    <>
      <div className="page-head">
        <div>
          <div className="small muted"><Link to="/purchasing">採購批次</Link></div>
          <h1 className="mono">{b.batch_no}</h1>
          <div className="muted small">採購日 {fmtDate(b.purchase_date)}・{b.supplier ?? "未填供應商"}・確認於 {fmtDateTime(b.confirmed_at)}
            {b.import_job_id && <> ・<Link to={`/imports/${b.import_job_id}`}>來源匯入</Link></>}</div>
        </div>
      </div>
      <div className="alert info small">
        成本紀錄已鎖定，不能修改或刪除。流程：日本按<b>「建立寄出包裹」</b>填追蹤號碼 → 運送中（還不能賣，預計 {b.lead_time_days} 天到）→ 美國在<Link to="/logistics">物流</Link>的包裹頁<b>「簽收」</b>→ 變成美國可銷售庫存。
      </div>
      {can("shipment.manage_jp") && <CreateParcelCard lines={b.lines} batchId={id} />}
      {b.lines.map((l) => (
        <div className="card" key={l.id}>
          <div className="page-head" style={{ marginBottom: 8 }}>
            <div>
              <Link to={`/skus/${l.sku_id}`} className="mono">{l.sku_code}</Link> <span className="muted">{l.name}</span>
            </div>
            <Progress {...l} />
          </div>
          {cost && (
            <dl className="kv" style={{ marginBottom: 12 }}>
              <dt>採購</dt><dd>{fmtInt(l.qty)} × {Number(l.unit_price_orig).toLocaleString()} {l.currency_orig}
                {l.currency_orig !== "USD" && l.fx_rate_quote && `（1 USD = ${Number(l.fx_rate_quote)} ${l.currency_orig}）`} = {fmtUsd(l.purchase_cost_base)}</dd>
              <dt>運費</dt><dd>日本境內 {Number(l.domestic_shipping_jpy) ? `¥${Number(l.domestic_shipping_jpy).toLocaleString()} → ` : ""}{fmtUsd(l.domestic_shipping_base)}・國際 {Number(l.intl_freight_jpy) ? `¥${Number(l.intl_freight_jpy).toLocaleString()} → ` : ""}{fmtUsd(l.intl_freight_base)}</dd>
              <dt>關稅/其他</dt><dd>{fmtUsd(l.duty_other_base)}</dd>
              <dt>落地成本</dt><dd><b>{fmtUsd(l.landed_unit_cost_base)}</b> / 件（合計 {fmtUsd(l.landed_total_base)}）</dd>
            </dl>
          )}
          {l.note && <p className="small muted">{l.note}</p>}
          {l.shipments.some((x) => x.in_transit > 0) && (
            <div className="small" style={{ marginBottom: 10 }}>
              {l.shipments.filter((x) => x.in_transit > 0).map((x, i) => (
                <div key={i} className="muted">運送中 {fmtInt(x.in_transit)} 件：{fmtDate(x.shipped_at)} 寄出，預計 {fmtDate(x.eta)} 到美國
                  {x.parcel_id && <>・包裹 <Link to={`/logistics/parcels/${x.parcel_id}`} className="mono">{x.parcel_no}</Link></>}</div>
              ))}
            </div>
          )}
          <div className="stack">
            {/* goods shipped before parcels existed are still received per line */}
            {can("shipment.receive_us") && l.unparcelled_in_transit > 0 && (
              <LineAction line={l} batchId={id} action="receive" max={l.unparcelled_in_transit} label="美國簽收" hint={`運送中（未建包裹）${fmtInt(l.unparcelled_in_transit)} 件`} />
            )}
          </div>
        </div>
      ))}
    </>
  );
}

function LineAction({ line, batchId, action, max, label, hint }: { line: Line; batchId: number; action: "ship" | "receive"; max: number; label: string; hint: string }) {
  const qc = useQueryClient();
  const [qty, setQty] = useState(String(max));
  const [date, setDate] = useState("");
  const [key, setKey] = useState(newIdempotencyKey);
  const m = useMutation({
    mutationFn: () => api(`/purchase-lines/${line.id}/${action}`, { body: { qty: Number(qty), on_date: date || null }, idempotencyKey: key }),
    onSuccess: () => {
      setKey(newIdempotencyKey());
      setQty(""); // cleared so a second tap can't repeat the same shipment / receipt by accident
      setDate("");
      void qc.invalidateQueries({ queryKey: ["batch", batchId] });
      void qc.invalidateQueries({ queryKey: ["batches"] });
      void qc.invalidateQueries({ queryKey: ["skus"] });
      void qc.invalidateQueries({ queryKey: ["inv-summary"] });
    },
  });
  const word = action === "ship" ? "寄出" : "簽收";
  return (
    <div>
      <ErrorBox error={m.error} />
      <div className="row">
        <input style={{ width: 90 }} inputMode="numeric" value={qty} onChange={(e) => setQty(e.target.value.replace(/\D/g, ""))} aria-label={`${line.sku_code} ${word}數量`} />
        <input style={{ width: 170 }} type="date" value={date} onChange={(e) => setDate(e.target.value)} aria-label={`${word}日期`} title={`${word}日期（空白 = 今天）`} />
        <button className={`btn ${action === "receive" ? "primary" : ""}`} disabled={m.isPending || !Number(qty) || Number(qty) > max} onClick={() => m.mutate()}>
          {label}
        </button>
        <span className="small muted">{hint}；日期空白 = 今天</span>
      </div>
    </div>
  );
}
