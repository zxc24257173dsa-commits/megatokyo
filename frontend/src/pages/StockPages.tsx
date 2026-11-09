import { useMemo, useState, type FormEvent } from "react";
import { Link, useNavigate, useParams } from "react-router-dom";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { api, newIdempotencyKey } from "../api";
import { useAuth } from "../auth";
import { ErrorBox, Field, Loading, SkuPicker, fmtDateTime, fmtInt, useBusinessSettings } from "../components/ui";
import { LOCATION_LABEL, type StockCount } from "../types";

type Op = "opening" | "increase" | "decrease" | "damage";
const OP_LABEL: Record<Op, string> = { opening: "期初庫存", increase: "調整增加", decrease: "調整減少", damage: "損壞報廢" };

function useAllowedLocations() {
  const { can } = useAuth();
  const { japanEnabled } = useBusinessSettings();
  return [japanEnabled && can("inventory.adjust_jp") && "JP_MAIN", can("inventory.adjust_us") && "US_MAIN"].filter(Boolean) as string[];
}

export function StockOperationsPage() {
  const qc = useQueryClient();
  const locations = useAllowedLocations();
  const [op, setOp] = useState<Op>("opening");
  const [locChoice, setLoc] = useState("");
  const loc = locations.includes(locChoice) ? locChoice : locations[0] ?? "";
  const [reason, setReason] = useState("");
  const [lines, setLines] = useState<{ sku_id: number | null; qty: string; cost: string }[]>([{ sku_id: null, qty: "", cost: "" }]);
  // One key per form submission attempt: a retry/double tap re-sends the same key and cannot double-post.
  const [idemKey, setIdemKey] = useState(newIdempotencyKey);
  const [done, setDone] = useState<string | null>(null);

  const m = useMutation({
    mutationFn: () => {
      const payload = lines
        .filter((l) => l.sku_id && Number(l.qty) > 0)
        .map((l) => ({ sku_id: l.sku_id, qty: Number(l.qty), unit_cost_usd: l.cost === "" ? null : l.cost }));
      if (op === "opening")
        return api("/inventory/opening-stock", { body: { location_code: loc, note: reason || null, lines: payload }, idempotencyKey: idemKey });
      return api("/inventory/adjustments", { body: { location_code: loc, kind: op, reason, lines: payload }, idempotencyKey: idemKey });
    },
    onSuccess: () => {
      setDone(`${OP_LABEL[op]}已完成：${LOCATION_LABEL[loc]} ${lines.filter((l) => l.sku_id).length} 個 SKU`);
      setLines([{ sku_id: null, qty: "", cost: "" }]);
      setReason("");
      setIdemKey(newIdempotencyKey());
      void qc.invalidateQueries();
    },
  });

  if (!locations.length) return <div className="card">你沒有調整任何倉庫庫存的權限。</div>;
  const needsCost = op === "opening" || op === "increase";
  const set = (i: number, k: "qty" | "cost", v: string) => setLines(lines.map((l, j) => (j === i ? { ...l, [k]: v } : l)));

  return (
    <form onSubmit={(e: FormEvent) => (e.preventDefault(), setDone(null), m.mutate())}>
      <div className="page-head"><h1>入庫 / 庫存調整</h1></div>
      <div className="card">
        <div className="inline-inputs">
          <Field label="作業">
            <select value={op} onChange={(e) => setOp(e.target.value as Op)}>
              {(Object.keys(OP_LABEL) as Op[]).map((k) => <option key={k} value={k}>{OP_LABEL[k]}</option>)}
            </select>
          </Field>
          <Field label="倉庫">
            <select value={loc} onChange={(e) => setLoc(e.target.value)}>
              {locations.map((l) => <option key={l} value={l}>{LOCATION_LABEL[l]}</option>)}
            </select>
          </Field>
        </div>
        <div className="alert info small">
          {op === "opening" && "開始使用系統時登記現有庫存。每一行會建立一個成本批次，需要輸入單位成本（USD）。之後的採購請用「試算表匯入」與「採購批次」的寄出／簽收。"}
          {op === "increase" && "盤點多出、找到的商品。會併入該 SKU 最新批次的成本；若從未有過庫存，需要輸入單位成本。"}
          {op === "decrease" && "盤點短少等原因減少庫存，依先進先出從最早的批次扣除。"}
          {op === "damage" && "損壞、無法銷售的商品，移至「遺失/損壞」，依先進先出扣除。"}
        </div>
        <Field label={op === "opening" ? "備註（選填）" : "原因（必填）"}>
          <input required={op !== "opening"} value={reason} onChange={(e) => setReason(e.target.value)} />
        </Field>
      </div>
      <div className="card">
        <h2>品項</h2>
        {done && <div className="alert ok">{done}</div>}
        <ErrorBox error={m.error} />
        {lines.map((l, i) => (
          <div className="row" key={i} style={{ marginBottom: 10, alignItems: "end" }}>
            <div style={{ flex: "3 1 220px" }}>
              <SkuPicker value={l.sku_id} onChange={(v) => setLines(lines.map((x, j) => (j === i ? { ...x, sku_id: v } : x)))} />
            </div>
            <input style={{ flex: "1 1 80px" }} aria-label="數量" placeholder="數量" inputMode="numeric" value={l.qty} onChange={(e) => set(i, "qty", e.target.value.replace(/\D/g, ""))} />
            {needsCost && (
              <input style={{ flex: "1 1 110px" }} aria-label="單位成本 USD" placeholder={op === "opening" ? "單位成本 USD" : "成本（選填）"} inputMode="decimal" required={op === "opening"} value={l.cost} onChange={(e) => set(i, "cost", e.target.value)} />
            )}
            {lines.length > 1 && <button type="button" className="btn sm danger" onClick={() => setLines(lines.filter((_, j) => j !== i))}>移除</button>}
          </div>
        ))}
        <div className="row">
          <button type="button" className="btn" onClick={() => setLines([...lines, { sku_id: null, qty: "", cost: "" }])}>＋ 新增一行</button>
          <button className="btn primary" disabled={m.isPending}>{m.isPending ? "處理中…" : `確認${OP_LABEL[op]}`}</button>
        </div>
      </div>
    </form>
  );
}

const STATUS_LABEL: Record<string, [string, string]> = {
  draft: ["盤點中", "info"],
  submitted: ["待核准", "warn"],
  approved: ["已核准", "ok"],
  cancelled: ["已取消", ""],
};

export function StockCountsPage() {
  const nav = useNavigate();
  const locations = useAllowedLocations();
  const [locChoice, setLoc] = useState("");
  const loc = locations.includes(locChoice) ? locChoice : locations[0] ?? "";
  const list = useQuery({ queryKey: ["counts"], queryFn: () => api<StockCount[]>("/inventory/stock-counts") });
  const create = useMutation({
    mutationFn: () => api<StockCount>("/inventory/stock-counts", { body: { location_code: loc } }),
    onSuccess: (c) => nav(`/stock/counts/${c.id}`),
  });
  return (
    <>
      <div className="page-head">
        <h1>盤點</h1>
        {locations.length > 0 && (
          <div className="row">
            <select value={loc} onChange={(e) => setLoc(e.target.value)} aria-label="倉庫" style={{ width: "auto" }}>
              {locations.map((l) => <option key={l} value={l}>{LOCATION_LABEL[l]}</option>)}
            </select>
            <button className="btn primary" onClick={() => create.mutate()} disabled={create.isPending}>開始盤點</button>
          </div>
        )}
      </div>
      <ErrorBox error={create.error ?? list.error} />
      <div className="card">
        {list.isLoading ? <Loading /> : !list.data?.length ? <div className="empty">尚無盤點紀錄</div> : (
          <table className="data responsive">
            <thead><tr><th>#</th><th>倉庫</th><th>狀態</th><th>建立時間</th></tr></thead>
            <tbody>
              {list.data.map((c) => (
                <tr key={c.id} className="click" onClick={() => nav(`/stock/counts/${c.id}`)}>
                  <td className="lead">盤點 #{c.id}</td>
                  <td data-label="倉庫">{LOCATION_LABEL[c.location_code]}</td>
                  <td data-label="狀態"><span className={`badge ${STATUS_LABEL[c.status]?.[1]}`}>{STATUS_LABEL[c.status]?.[0]}</span></td>
                  <td data-label="建立時間" className="small">{fmtDateTime(c.created_at)}</td>
                </tr>
              ))}
            </tbody>
          </table>
        )}
      </div>
    </>
  );
}

export function StockCountPage() {
  const id = Number(useParams().id);
  const { can } = useAuth();
  const qc = useQueryClient();
  const sc = useQuery({ queryKey: ["count", id], queryFn: () => api<StockCount>(`/inventory/stock-counts/${id}`) });
  const [entries, setEntries] = useState<Record<number, string>>({});
  const [filter, setFilter] = useState("");
  const merged = useMemo(() => {
    const m: Record<number, string> = {};
    sc.data?.lines.forEach((l) => (m[l.sku_id] = entries[l.sku_id] ?? (l.counted_qty?.toString() ?? "")));
    return m;
  }, [sc.data, entries]);
  const onDone = (data: StockCount) => {
    qc.setQueryData(["count", id], data);
    setEntries({});
    void qc.invalidateQueries({ queryKey: ["counts"] });
    void qc.invalidateQueries({ queryKey: ["skus"] });
  };
  const save = useMutation({
    mutationFn: () =>
      api<StockCount>(`/inventory/stock-counts/${id}/counts`, {
        method: "PUT",
        body: { counts: Object.entries(entries).map(([k, v]) => ({ sku_id: Number(k), counted_qty: v === "" ? null : Number(v) })) },
      }),
    onSuccess: onDone,
  });
  const action = useMutation({ mutationFn: (a: string) => api<StockCount>(`/inventory/stock-counts/${id}/${a}`, { method: "POST" }), onSuccess: onDone });

  if (sc.isLoading) return <Loading />;
  if (!sc.data) return <ErrorBox error={sc.error} />;
  const c = sc.data;
  const editable = c.status === "draft";
  const dirty = Object.keys(entries).length > 0;
  const lines = c.lines.filter((l) => !filter || (l.sku_code + l.name).toLowerCase().includes(filter.toLowerCase()));
  const counted = c.lines.filter((l) => merged[l.sku_id] !== "").length;

  return (
    <>
      <div className="page-head">
        <div>
          <div className="small muted"><Link to="/stock/counts">盤點</Link> / #{c.id}</div>
          <h1>{LOCATION_LABEL[c.location_code]} 盤點 #{c.id} <span className={`badge ${STATUS_LABEL[c.status]?.[1]}`}>{STATUS_LABEL[c.status]?.[0]}</span></h1>
        </div>
        <div className="row">
          {editable && <button className="btn" disabled={!dirty || save.isPending} onClick={() => save.mutate()}>儲存</button>}
          {editable && <button className="btn primary" disabled={dirty || action.isPending} onClick={() => action.mutate("submit")} title={dirty ? "請先儲存" : undefined}>送出待核准</button>}
          {c.status === "submitted" && can("stock_count.approve") && (
            <button className="btn primary" disabled={action.isPending} onClick={() => confirm("核准後會依差異調整庫存，確定？") && action.mutate("approve")}>核准並調整庫存</button>
          )}
          {(c.status === "draft" || c.status === "submitted") && <button className="btn danger" onClick={() => confirm("取消這次盤點？") && action.mutate("cancel")}>取消</button>}
        </div>
      </div>
      <ErrorBox error={save.error ?? action.error} />
      {c.status === "submitted" && !can("stock_count.approve") && <div className="alert info">已送出，等待台灣管理人員或管理員核准。</div>}
      <div className="card">
        <div className="row" style={{ marginBottom: 10 }}>
          <input placeholder="篩選 SKU…" value={filter} onChange={(e) => setFilter(e.target.value)} style={{ flex: 1 }} />
          <span className="small muted">已填 {counted} / {c.lines.length}</span>
        </div>
        <table className="data responsive">
          <thead><tr><th>SKU</th><th className="num">系統數量</th><th className="num">實際盤點</th><th className="num">差異</th></tr></thead>
          <tbody>
            {lines.map((l) => {
              const v = merged[l.sku_id];
              const diff = l.difference_qty ?? (v === "" ? null : Number(v) - l.expected_qty);
              return (
                <tr key={l.sku_id}>
                  <td className="lead"><span className="mono">{l.sku_code}</span> <span className="small muted">{l.name}</span></td>
                  <td className="num" data-label="系統數量">{fmtInt(l.expected_qty)}</td>
                  <td className="num" data-label="實際盤點">
                    {editable ? (
                      <input style={{ width: 90, textAlign: "right" }} inputMode="numeric" value={v} onChange={(e) => setEntries({ ...entries, [l.sku_id]: e.target.value.replace(/\D/g, "") })} aria-label={`${l.sku_code} 盤點數量`} />
                    ) : fmtInt(l.counted_qty)}
                  </td>
                  <td className="num" data-label="差異">
                    {diff === null ? "—" : <span className={`badge ${diff === 0 ? "ok" : diff < 0 ? "danger" : "warn"}`}>{diff > 0 ? "+" : ""}{diff}</span>}
                  </td>
                </tr>
              );
            })}
          </tbody>
        </table>
        <p className="small muted" style={{ marginBottom: 0 }}>系統數量會在核准當下重新讀取；請在倉庫沒有出入貨時盤點。</p>
      </div>
    </>
  );
}
