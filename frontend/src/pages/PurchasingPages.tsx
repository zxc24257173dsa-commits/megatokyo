import { useState } from "react";
import { Link, useNavigate, useParams } from "react-router-dom";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
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
  received: number;
  landed_total?: string;
}

interface Line {
  id: number;
  line_no: number;
  sku_id: number;
  sku_code: string;
  name: string;
  qty: number;
  received: number;
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

function Progress({ received, qty }: { received: number; qty: number }) {
  const done = received >= qty;
  return <span className={`badge ${done ? "ok" : received > 0 ? "warn" : "info"}`}>{done ? "已全部入庫" : `已入庫 ${fmtInt(received)} / ${fmtInt(qty)}`}</span>;
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
          <label className="row small"><input type="checkbox" checked={onlyOpen} onChange={(e) => setOnlyOpen(e.target.checked)} /> 只看尚未全部入庫</label>
        </div>
        <ErrorBox error={list.error} />
        {list.isLoading ? <Loading /> : !list.data?.length ? <div className="empty">沒有採購批次。到「試算表匯入」上傳成本試算表。</div> : (
          <table className="data responsive">
            <thead><tr><th>批次</th><th>採購日</th><th>供應商</th><th className="num">SKU 數</th><th>入庫</th>{showCost && <th className="num">落地成本合計</th>}</tr></thead>
            <tbody>
              {list.data.map((b) => (
                <tr key={b.id} className="click" onClick={() => nav(`/purchasing/${b.id}`)}>
                  <td className="lead mono">{b.batch_no}</td>
                  <td data-label="採購日">{fmtDate(b.purchase_date)}</td>
                  <td data-label="供應商">{b.supplier ?? "—"}</td>
                  <td data-label="SKU 數" className="num">{b.line_count}</td>
                  <td data-label="入庫"><Progress received={b.received} qty={b.qty} /></td>
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
    queryFn: () => api<{ id: number; batch_no: string; purchase_date: string; supplier: string | null; confirmed_at: string; import_job_id: number | null; can_view_cost: boolean; lines: Line[] }>(`/purchase-batches/${id}`),
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
      <div className="alert info small">成本紀錄已鎖定，不能修改或刪除。商品實際到達日本倉時按「入庫」，系統會用這筆的落地成本建立先進先出批次。</div>
      {b.lines.map((l) => (
        <div className="card" key={l.id}>
          <div className="page-head" style={{ marginBottom: 8 }}>
            <div>
              <Link to={`/skus/${l.sku_id}`} className="mono">{l.sku_code}</Link> <span className="muted">{l.name}</span>
            </div>
            <Progress received={l.received} qty={l.qty} />
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
          {can("inventory.adjust_jp") && l.received < l.qty && <ReceiveForm line={l} batchId={id} />}
        </div>
      ))}
    </>
  );
}

function ReceiveForm({ line, batchId }: { line: Line; batchId: number }) {
  const qc = useQueryClient();
  const remaining = line.qty - line.received;
  const [qty, setQty] = useState(String(remaining));
  const [date, setDate] = useState("");
  const [key, setKey] = useState(newIdempotencyKey);
  const m = useMutation({
    mutationFn: () => api(`/purchase-lines/${line.id}/receive`, { body: { qty: Number(qty), received_on: date || null }, idempotencyKey: key }),
    onSuccess: () => {
      setKey(newIdempotencyKey());
      void qc.invalidateQueries({ queryKey: ["batch", batchId] });
      void qc.invalidateQueries({ queryKey: ["batches"] });
      void qc.invalidateQueries({ queryKey: ["skus"] });
    },
  });
  return (
    <div>
      <ErrorBox error={m.error} />
      <div className="row">
        <input style={{ width: 100 }} inputMode="numeric" value={qty} onChange={(e) => setQty(e.target.value.replace(/\D/g, ""))} aria-label={`${line.sku_code} 入庫數量`} />
        <input style={{ width: 170 }} type="date" value={date} onChange={(e) => setDate(e.target.value)} aria-label="到貨日期" title="到貨日期（空白 = 今天）" />
        <button className="btn primary" disabled={m.isPending || !Number(qty) || Number(qty) > remaining} onClick={() => m.mutate()}>
          入庫到日本倉
        </button>
        <span className="small muted">尚未入庫 {fmtInt(remaining)} 件；日期空白 = 今天</span>
      </div>
    </div>
  );
}
