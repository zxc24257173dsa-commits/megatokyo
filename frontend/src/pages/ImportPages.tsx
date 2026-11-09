import { useMemo, useState, type FormEvent } from "react";
import { Link, useNavigate, useParams } from "react-router-dom";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { api } from "../api";
import { useAuth } from "../auth";
import { ErrorBox, Loading, fmtDateTime, fmtInt, fmtUsd } from "../components/ui";

interface Job {
  id: number;
  import_type: "purchase_cost" | "sku_master" | "whatnot_orders";
  filename: string;
  status: string;
  created_at: string;
  committed_at: string | null;
  stats: { rows?: number; by_action?: Record<string, number>; errors?: number; warnings?: number; missing_fields?: string[]; result?: Record<string, any> };
  mapping: { sheet?: string; header_row?: number; columns?: Record<string, number>; remembered?: boolean };
  fields: { key: string; label: string; required: boolean }[];
  headers: (string | number | null)[];
  sheets: { name: string; rows: number; preview?: any[][] }[];
}

interface Row {
  row_number: number;
  action: string;
  errors: string[];
  warnings: string[];
  normalized: Record<string, any>;
}

const TYPE_LABEL: Record<string, string> = { purchase_cost: "採購成本", sku_master: "商品主檔", whatnot_orders: "Whatnot 訂單（CSV）" };
const STATUS: Record<string, [string, string]> = {
  uploaded: ["已上傳", ""],
  parsed: ["需要對應欄位", "warn"],
  validated: ["待確認", "info"],
  committed: ["已匯入", "ok"],
  cancelled: ["已取消", ""],
  failed: ["失敗", "danger"],
};
const ACTION: Record<string, [string, string]> = {
  import: ["將匯入", "ok"],
  import_new_sku: ["將匯入＋建立新 SKU", "warn"],
  create: ["新增 SKU", "ok"],
  update: ["更新", "info"],
  unchanged: ["沒有變更", ""],
  skip_example: ["範例列略過", ""],
  import_unmapped: ["將匯入・待對應 SKU", "warn"],
  import_unpaid: ["將匯入（未付款／取消）", ""],
  skip_existing: ["已在系統中", ""],
  error: ["錯誤", "danger"],
};

export function ImportsPage() {
  const { can } = useAuth();
  const nav = useNavigate();
  const PERM = { purchase_cost: "import.cost", sku_master: "catalog.edit", whatnot_orders: "import.sales" } as const;
  const types = (["purchase_cost", "sku_master", "whatnot_orders"] as const).filter((t) => can(PERM[t]));
  const [type, setType] = useState<string>(types[0] ?? "purchase_cost");
  const [file, setFile] = useState<File | null>(null);
  const list = useQuery({ queryKey: ["imports"], queryFn: () => api<Job[]>("/imports") });
  const up = useMutation({
    mutationFn: () => {
      const form = new FormData();
      form.append("import_type", type);
      form.append("file", file!);
      return api<Job>("/imports", { form, method: "POST" });
    },
    onSuccess: (j) => nav(`/imports/${j.id}`),
  });
  return (
    <>
      <div className="page-head"><h1>試算表匯入</h1></div>
      <form className="card" onSubmit={(e: FormEvent) => (e.preventDefault(), up.mutate())}>
        <h2>上傳 CSV / Excel</h2>
        <p className="small muted" style={{ marginTop: 0 }}>
          支援 .xlsx（多個工作表會自動找到正確的那一張）與 .csv（UTF-8、Shift_JIS、Big5）。上傳後會先預覽與檢查，<b>按下確認前不會寫入任何資料</b>。
        </p>
        <ErrorBox error={up.error} />
        <div className="row">
          <select value={type} onChange={(e) => setType(e.target.value)} style={{ width: "auto" }} aria-label="匯入類型">
            {types.map((t) => <option key={t} value={t}>{TYPE_LABEL[t]}</option>)}
          </select>
          <input type="file" accept=".xlsx,.xlsm,.csv" onChange={(e) => setFile(e.target.files?.[0] ?? null)} style={{ flex: "1 1 220px" }} aria-label="選擇檔案" />
          <button className="btn primary" disabled={!file || up.isPending}>{up.isPending ? "讀取中…" : "上傳並檢查"}</button>
        </div>
      </form>
      <div className="card">
        <h2>匯入紀錄</h2>
        {list.isLoading ? <Loading /> : !list.data?.length ? <div className="muted small">還沒有匯入紀錄</div> : (
          <table className="data responsive">
            <thead><tr><th>檔案</th><th>類型</th><th>狀態</th><th>上傳時間</th></tr></thead>
            <tbody>
              {list.data.map((j) => (
                <tr key={j.id} className="click" onClick={() => nav(`/imports/${j.id}`)}>
                  <td className="lead">{j.filename}</td>
                  <td data-label="類型">{TYPE_LABEL[j.import_type]}</td>
                  <td data-label="狀態"><span className={`badge ${STATUS[j.status]?.[1]}`}>{STATUS[j.status]?.[0] ?? j.status}</span></td>
                  <td data-label="上傳時間" className="small">{fmtDateTime(j.created_at)}</td>
                </tr>
              ))}
            </tbody>
          </table>
        )}
      </div>
    </>
  );
}

export function ImportDetailPage() {
  const id = Number(useParams().id);
  const qc = useQueryClient();
  const job = useQuery({ queryKey: ["import", id], queryFn: () => api<Job>(`/imports/${id}`) });
  const [filter, setFilter] = useState("all");
  const rows = useQuery({
    queryKey: ["import-rows", id, filter],
    queryFn: () => api<Row[]>(`/imports/${id}/rows`, { params: { filter } }),
    enabled: job.data?.status === "validated" || job.data?.status === "committed",
  });
  const [createSkus, setCreateSkus] = useState(false);
  const [skipErrors, setSkipErrors] = useState(false);
  const [markShipped, setMarkShipped] = useState(false);
  const refresh = (j: Job) => {
    qc.setQueryData(["import", id], j);
    void qc.invalidateQueries({ queryKey: ["import-rows", id] });
    void qc.invalidateQueries({ queryKey: ["imports"] });
  };
  const commit = useMutation({
    mutationFn: () => api<{ job: Job; summary: Record<string, any> }>(`/imports/${id}/commit`, { body: { create_missing_skus: createSkus, skip_error_rows: skipErrors, mark_shipped: markShipped } }),
    onSuccess: (r) => {
      refresh({ ...job.data!, ...r.job });
      void qc.invalidateQueries({ queryKey: ["skus"] });
    },
  });
  const cancel = useMutation({ mutationFn: () => api<Job>(`/imports/${id}/cancel`, { method: "POST" }), onSuccess: (j) => refresh({ ...job.data!, ...j }) });

  if (job.isLoading) return <Loading />;
  if (!job.data) return <ErrorBox error={job.error} />;
  const j = job.data;
  const by = j.stats.by_action ?? {};
  const isCost = j.import_type === "purchase_cost";
  const isOrders = j.import_type === "whatnot_orders";
  const willImport = isOrders
    ? (by.import ?? 0) + (by.import_unmapped ?? 0) + (by.import_unpaid ?? 0)
    : isCost ? (by.import ?? 0) + (by.import_new_sku ?? 0) : (by.create ?? 0) + (by.update ?? 0);
  const open = j.status === "validated" || j.status === "parsed";

  return (
    <>
      <div className="page-head">
        <div>
          <div className="small muted"><Link to="/imports">試算表匯入</Link> / {TYPE_LABEL[j.import_type]}</div>
          <h1 style={{ wordBreak: "break-all" }}>{j.filename} <span className={`badge ${STATUS[j.status]?.[1]}`}>{STATUS[j.status]?.[0]}</span></h1>
        </div>
        {open && <button className="btn danger" onClick={() => confirm("取消這次匯入？不會寫入任何資料。") && cancel.mutate()}>取消匯入</button>}
      </div>

      <MappingCard job={j} onSaved={refresh} />

      {j.status !== "parsed" && (
        <div className="tiles">
          <div className="tile"><div className="label">{j.status === "committed" ? "已匯入" : "將匯入"}</div><div className="value">{fmtInt(willImport)}</div>
            <div className="sub">{isOrders ? `待對應 SKU ${fmtInt(by.import_unmapped ?? 0)}・未付款 ${fmtInt(by.import_unpaid ?? 0)}` : isCost ? `其中新 SKU ${fmtInt(by.import_new_sku ?? 0)}` : `新增 ${fmtInt(by.create ?? 0)}・更新 ${fmtInt(by.update ?? 0)}`}</div></div>
          <div className="tile"><div className="label">錯誤</div><div className="value" style={{ color: j.stats.errors ? "var(--danger)" : undefined }}>{fmtInt(j.stats.errors)}</div><div className="sub">需修正或略過</div></div>
          <div className="tile"><div className="label">提醒</div><div className="value">{fmtInt(j.stats.warnings)}</div><div className="sub">可匯入，請確認</div></div>
          <div className="tile"><div className="label">略過</div><div className="value">{fmtInt((by.skip_example ?? 0) + (by.unchanged ?? 0) + (by.skip_existing ?? 0))}</div><div className="sub">{isOrders ? "已在系統中的訂單" : `範例列${isCost ? "" : "・沒有變更"}`}</div></div>
        </div>
      )}

      {j.status === "committed" && j.stats.result && (
        <div className="alert ok">
          匯入完成：{isOrders
            ? `${j.stats.result.orders} 筆訂單（${j.stats.result.lines} 件商品），已付款成交額 ${fmtUsd(j.stats.result.revenue_usd)}${j.stats.result.unmapped_lines ? `；${j.stats.result.unmapped_lines} 件待對應 SKU` : ""}。`
            : isCost
            ? `${j.stats.result.batches} 個採購批次、${j.stats.result.lines} 筆成本，落地成本合計 ${fmtUsd(j.stats.result.landed_total_usd)}${j.stats.result.new_skus ? `，新增 ${j.stats.result.new_skus} 個 SKU` : ""}。成本已入帳；商品從日本寄出時，到「採購批次」按「寄出」，美國收到後按「簽收」。`
            : `新增 ${j.stats.result.created} 個 SKU、更新 ${j.stats.result.updated} 個 SKU。`}
          {isCost && <> <Link to="/purchasing">前往採購批次 →</Link></>}
          {isOrders && <> <Link to="/orders?tab=unmapped">前往對應商品 →</Link></>}
        </div>
      )}

      {j.status === "validated" && (
        <div className="card">
          <h2>確認匯入</h2>
          <ErrorBox error={commit.error} />
          {isCost && (by.import_new_sku ?? 0) > 0 && (
            <label className="row small" style={{ marginBottom: 8 }}><input type="checkbox" checked={createSkus} onChange={(e) => setCreateSkus(e.target.checked)} />
              一併建立 {by.import_new_sku} 個系統中還沒有的 SKU</label>
          )}
          {isOrders && (
            <label className="row small" style={{ marginBottom: 8 }}><input type="checkbox" checked={markShipped} onChange={(e) => setMarkShipped(e.target.checked)} />
              這些訂單都已經寄出（扣除美國庫存）。不勾選時只預留庫存，之後由 API 同步出貨狀態。</label>
          )}
          {(j.stats.errors ?? 0) > 0 && (
            <label className="row small" style={{ marginBottom: 8 }}><input type="checkbox" checked={skipErrors} onChange={(e) => setSkipErrors(e.target.checked)} />
              略過 {j.stats.errors} 筆有錯誤的列，只匯入正確的列</label>
          )}
          <div className="row">
            <button className="btn primary" disabled={commit.isPending || willImport === 0} onClick={() => commit.mutate()}>
              {commit.isPending ? "匯入中…" : `確認匯入 ${willImport} 列`}
            </button>
            <span className="small muted">{isOrders ? "買家帳號、地址不會保存；匯入完成後原始檔案會刪除。" : isCost ? "匯入的是成本紀錄；確認後不可修改，也不會直接增加庫存。" : "庫存與成本欄位不會從商品主檔匯入。"}</span>
          </div>
        </div>
      )}

      {(j.status === "validated" || j.status === "committed") && (
        <div className="card">
          <div className="row" style={{ marginBottom: 10 }}>
            {[["all", "全部"], ["errors", "錯誤"], ["warnings", "提醒"], ["import", "將匯入"], ["skip", "略過"]].map(([k, l]) => (
              <button key={k} className={`btn sm ${filter === k ? "primary" : ""}`} onClick={() => setFilter(k)}>{l}</button>
            ))}
          </div>
          {rows.isLoading ? <Loading /> : !rows.data?.length ? <div className="muted small">沒有資料列</div> : isOrders ? <OrderRowsTable rows={rows.data} /> : <RowsTable rows={rows.data} isCost={isCost} />}
        </div>
      )}
    </>
  );
}

function RowsTable({ rows, isCost }: { rows: Row[]; isCost: boolean }) {
  return (
    <div className="table-wrap">
      <table className="data responsive">
        <thead>
          {isCost ? (
            <tr><th>列</th><th>狀態</th><th>批次 / SKU</th><th className="num">數量</th><th className="num">單價</th><th className="num">匯率（1 USD =）</th><th className="num">落地成本/件</th><th>說明</th></tr>
          ) : (
            <tr><th>列</th><th>狀態</th><th>SKU</th><th>名稱</th><th>分類</th><th className="num">售價</th><th className="num">安全庫存</th><th>說明</th></tr>
          )}
        </thead>
        <tbody>
          {rows.map((r) => {
            const n = r.normalized;
            const a = ACTION[r.action] ?? [r.action, ""];
            const notes = (
              <td data-label="說明" className="small" style={{ minWidth: 220 }}>
                {r.errors.map((e, i) => <div key={i} style={{ color: "var(--danger)" }}>✖ {e}</div>)}
                {r.warnings.map((w, i) => <div key={i} style={{ color: r.action === "skip_example" ? "var(--muted)" : "var(--warn)" }}>! {w}</div>)}
              </td>
            );
            return isCost ? (
              <tr key={r.row_number}>
                <td className="lead">第 {r.row_number} 列</td>
                <td data-label="狀態"><span className={`badge ${a[1]}`}>{a[0]}</span></td>
                <td data-label="批次 / SKU"><div className="mono">{n.sku_code ?? "—"}</div><div className="small muted">{n.batch_no ?? n.batch_ref ?? ""}{n.purchase_date ? ` · ${n.purchase_date}` : ""}</div></td>
                <td data-label="數量" className="num">{n.qty ?? "—"}</td>
                <td data-label="單價" className="num">{n.unit_cost ? `${Number(n.unit_cost).toLocaleString()} ${n.currency ?? ""}` : "—"}</td>
                <td data-label="匯率（1 USD =）" className="num">{n.fx_quote ? `${Number(n.fx_quote)} ${n.currency ?? ""}` : "—"}</td>
                <td data-label="落地成本/件" className="num">{n.landed_unit_usd ? fmtUsd(n.landed_unit_usd) : "—"}</td>
                {notes}
              </tr>
            ) : (
              <tr key={r.row_number}>
                <td className="lead">第 {r.row_number} 列</td>
                <td data-label="狀態"><span className={`badge ${a[1]}`}>{a[0]}</span></td>
                <td data-label="SKU" className="mono">{n.sku_code ?? "—"}</td>
                <td data-label="名稱">{n.product_name ?? "—"}</td>
                <td data-label="分類">{n.category ?? "—"}</td>
                <td data-label="售價" className="num">{n.list_price ?? "—"}</td>
                <td data-label="安全庫存" className="num">{n.safety_stock ?? "—"}</td>
                {notes}
              </tr>
            );
          })}
        </tbody>
      </table>
    </div>
  );
}

function MappingCard({ job, onSaved }: { job: Job; onSaved: (j: Job) => void }) {
  const editable = job.status === "validated" || job.status === "parsed";
  const [open, setOpen] = useState(job.status === "parsed");
  const [sheet, setSheet] = useState(job.mapping.sheet ?? job.sheets[0]?.name ?? "");
  const [headerRow, setHeaderRow] = useState(job.mapping.header_row ?? 0);
  const [cols, setCols] = useState<Record<string, number | null>>(job.mapping.columns ?? {});
  const preview = job.sheets.find((s) => s.name === sheet)?.preview ?? [];
  const headers = useMemo(() => preview[headerRow] ?? [], [preview, headerRow]);
  const save = useMutation({
    mutationFn: () => api<Job>(`/imports/${job.id}/mapping`, { method: "PUT", body: { sheet, header_row: headerRow, columns: cols } }),
    onSuccess: (j) => (onSaved(j), setOpen(false)),
  });
  const mappedCount = Object.keys(job.mapping.columns ?? {}).length;

  return (
    <div className="card">
      <div className="page-head" style={{ marginBottom: open ? 12 : 0 }}>
        <div>
          <h2 style={{ margin: 0 }}>欄位對應</h2>
          <div className="small muted">
            {job.mapping.sheet ? `工作表「${job.mapping.sheet}」第 ${(job.mapping.header_row ?? 0) + 1} 列是標題，已對應 ${mappedCount} 個欄位` : "找不到可辨識的標題列，請手動指定"}
            {job.mapping.remembered && "（使用上次儲存的對應）"}
          </div>
        </div>
        {editable && <button className="btn sm" onClick={() => setOpen(!open)}>{open ? "收合" : "檢查 / 修改"}</button>}
      </div>
      {job.status === "parsed" && job.stats.missing_fields?.length ? (
        <div className="alert error">還沒對應的必填欄位：{job.stats.missing_fields.join("、")}</div>
      ) : null}
      {open && editable && (
        <>
          <ErrorBox error={save.error} />
          <div className="inline-inputs">
            <label className="field"><span>工作表</span>
              <select value={sheet} onChange={(e) => (setSheet(e.target.value), setHeaderRow(0), setCols({}))}>
                {job.sheets.map((s) => <option key={s.name} value={s.name}>{s.name}（{s.rows} 列）</option>)}
              </select>
            </label>
            <label className="field"><span>標題在第幾列</span>
              <select value={headerRow} onChange={(e) => setHeaderRow(Number(e.target.value))}>
                {preview.map((r, i) => <option key={i} value={i}>第 {i + 1} 列：{r.filter((c) => c !== null).slice(0, 3).join(" | ").slice(0, 50)}</option>)}
              </select>
            </label>
          </div>
          <div className="inline-inputs">
            {job.fields.map((f) => (
              <label className="field" key={f.key}>
                <span>{f.label}{f.required && <b style={{ color: "var(--danger)" }}> *</b>}</span>
                <select value={cols[f.key] ?? ""} onChange={(e) => setCols({ ...cols, [f.key]: e.target.value === "" ? null : Number(e.target.value) })} aria-label={f.label}>
                  <option value="">（不使用）</option>
                  {headers.map((h, i) => (h === null || h === "" ? null : <option key={i} value={i}>{String(h)}</option>))}
                </select>
              </label>
            ))}
          </div>
          <button className="btn primary" disabled={save.isPending} onClick={() => save.mutate()}>{save.isPending ? "檢查中…" : "套用並重新檢查"}</button>
        </>
      )}
    </div>
  );
}

function OrderRowsTable({ rows }: { rows: Row[] }) {
  return (
    <div className="table-wrap">
      <table className="data responsive">
        <thead><tr><th>列</th><th>狀態</th><th>訂單</th><th>商品</th><th className="num">數量</th><th className="num">成交價</th><th>說明</th></tr></thead>
        <tbody>
          {rows.map((r) => {
            const n = r.normalized;
            const a = ACTION[r.action] ?? [r.action, ""];
            return (
              <tr key={r.row_number}>
                <td className="lead">第 {r.row_number} 列</td>
                <td data-label="狀態"><span className={`badge ${a[1]}`}>{a[0]}</span></td>
                <td data-label="訂單" className="mono">{n.order_id ?? "—"}</td>
                <td data-label="商品" className="small">{n.title ?? "—"}</td>
                <td data-label="數量" className="num">{n.qty ?? "—"}</td>
                <td data-label="成交價" className="num">{n.price !== undefined ? fmtUsd(n.price) : "—"}</td>
                <td data-label="說明" className="small" style={{ minWidth: 200 }}>
                  {r.errors.map((e, i) => <div key={i} style={{ color: "var(--danger)" }}>✖ {e}</div>)}
                  {r.warnings.map((w, i) => <div key={i} style={{ color: "var(--muted)" }}>! {w}</div>)}
                </td>
              </tr>
            );
          })}
        </tbody>
      </table>
    </div>
  );
}
