import { useMemo, useState, type FormEvent } from "react";
import { Link, useNavigate, useParams } from "react-router-dom";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { api } from "../api";
import { useAuth } from "../auth";
import { ErrorBox, Loading, fmtDateTime, fmtInt, fmtUsd } from "../components/ui";
import { t } from "../i18n";

interface Job {
  id: number;
  import_type: "purchase_cost" | "sku_master" | "whatnot_orders" | "whatnot_financial";
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

const TYPE_LABEL: Record<string, string> = { purchase_cost: t("採購成本"), sku_master: t("商品主檔"), whatnot_orders: t("Whatnot 訂單（CSV）"), whatnot_financial: t("Whatnot 財務報表（CSV）") };
const STATUS: Record<string, [string, string]> = {
  uploaded: [t("已上傳"), ""],
  parsed: [t("需要對應欄位"), "warn"],
  validated: [t("待確認"), "info"],
  committed: [t("已匯入"), "ok"],
  cancelled: [t("已取消"), ""],
  failed: [t("失敗"), "danger"],
};
const ACTION: Record<string, [string, string]> = {
  import: [t("將匯入"), "ok"],
  import_new_sku: [t("將匯入＋建立新 SKU"), "warn"],
  create: [t("新增 SKU"), "ok"],
  update: [t("更新"), "info"],
  unchanged: [t("沒有變更"), ""],
  skip_example: [t("範例列略過"), ""],
  import_unmapped: [t("將匯入・待對應 SKU"), "warn"],
  import_unpaid: [t("將匯入（未付款／取消）"), ""],
  skip_existing: [t("已在系統中"), ""],
  import_order_missing: [t("將匯入・訂單不在系統"), "warn"],
  error: [t("錯誤"), "danger"],
};

export function ImportsPage() {
  const { can } = useAuth();
  const nav = useNavigate();
  const PERM = { purchase_cost: "import.cost", sku_master: "catalog.edit", whatnot_orders: "import.sales", whatnot_financial: "import.sales" } as const;
  const types = (["purchase_cost", "sku_master", "whatnot_orders", "whatnot_financial"] as const).filter((t) => can(PERM[t]));
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
      <div className="page-head"><h1>{t("試算表匯入")}</h1></div>
      <form className="card" onSubmit={(e: FormEvent) => (e.preventDefault(), up.mutate())}>
        <h2>{t("上傳 CSV / Excel")}</h2>
        <p className="small muted" style={{ marginTop: 0 }}>
          {t("支援 .xlsx（多個工作表會自動找到正確的那一張）與 .csv（UTF-8、Shift_JIS、Big5）。上傳後會先預覽與檢查，")}<b>{t("按下確認前不會寫入任何資料")}</b>{t("。")}
        </p>
        <ErrorBox error={up.error} />
        <div className="row">
          <select value={type} onChange={(e) => setType(e.target.value)} style={{ width: "auto" }} aria-label={t("匯入類型")}>
            {types.map((t) => <option key={t} value={t}>{TYPE_LABEL[t]}</option>)}
          </select>
          <input type="file" accept=".xlsx,.xlsm,.csv" onChange={(e) => setFile(e.target.files?.[0] ?? null)} style={{ flex: "1 1 220px" }} aria-label={t("選擇檔案")} />
          <button className="btn primary" disabled={!file || up.isPending}>{up.isPending ? t("讀取中…") : t("上傳並檢查")}</button>
        </div>
      </form>
      <div className="card">
        <h2>{t("匯入紀錄")}</h2>
        {list.isLoading ? <Loading /> : !list.data?.length ? <div className="muted small">{t("還沒有匯入紀錄")}</div> : (
          <table className="data responsive">
            <thead><tr><th>{t("檔案")}</th><th>{t("類型")}</th><th>{t("狀態")}</th><th>{t("上傳時間")}</th></tr></thead>
            <tbody>
              {list.data.map((j) => (
                <tr key={j.id} className="click" onClick={() => nav(`/imports/${j.id}`)}>
                  <td className="lead">{j.filename}</td>
                  <td data-label={t("類型")}>{TYPE_LABEL[j.import_type]}</td>
                  <td data-label={t("狀態")}><span className={`badge ${STATUS[j.status]?.[1]}`}>{STATUS[j.status]?.[0] ?? j.status}</span></td>
                  <td data-label={t("上傳時間")} className="small">{fmtDateTime(j.created_at)}</td>
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
  const isFin = j.import_type === "whatnot_financial";
  const willImport = isFin
    ? (by.import ?? 0) + (by.import_order_missing ?? 0)
    : isOrders
    ? (by.import ?? 0) + (by.import_unmapped ?? 0) + (by.import_unpaid ?? 0)
    : isCost ? (by.import ?? 0) + (by.import_new_sku ?? 0) : (by.create ?? 0) + (by.update ?? 0);
  const open = j.status === "validated" || j.status === "parsed";

  return (
    <>
      <div className="page-head">
        <div>
          <div className="small muted"><Link to="/imports">{t("試算表匯入")}</Link> / {TYPE_LABEL[j.import_type]}</div>
          <h1 style={{ wordBreak: "break-all" }}>{j.filename} <span className={`badge ${STATUS[j.status]?.[1]}`}>{STATUS[j.status]?.[0]}</span></h1>
        </div>
        {open && <button className="btn danger" onClick={() => confirm(t("取消這次匯入？不會寫入任何資料。")) && cancel.mutate()}>{t("取消匯入")}</button>}
      </div>

      <MappingCard job={j} onSaved={refresh} />

      {j.status !== "parsed" && (
        <div className="tiles">
          <div className="tile"><div className="label">{j.status === "committed" ? t("已匯入") : t("將匯入")}</div><div className="value">{fmtInt(willImport)}</div>
            <div className="sub">{isFin ? t("訂單不在系統 {0}", [fmtInt(by.import_order_missing ?? 0)]) : isOrders ? t("待對應 SKU {0}・未付款 {1}", [fmtInt(by.import_unmapped ?? 0), fmtInt(by.import_unpaid ?? 0)]) : isCost ? t("其中新 SKU {0}", [fmtInt(by.import_new_sku ?? 0)]) : t("新增 {0}・更新 {1}", [fmtInt(by.create ?? 0), fmtInt(by.update ?? 0)])}</div></div>
          <div className="tile"><div className="label">{t("錯誤")}</div><div className="value" style={{ color: j.stats.errors ? "var(--danger)" : undefined }}>{fmtInt(j.stats.errors)}</div><div className="sub">{t("需修正或略過")}</div></div>
          <div className="tile"><div className="label">{t("提醒")}</div><div className="value">{fmtInt(j.stats.warnings)}</div><div className="sub">{t("可匯入，請確認")}</div></div>
          <div className="tile"><div className="label">{t("略過")}</div><div className="value">{fmtInt((by.skip_example ?? 0) + (by.unchanged ?? 0) + (by.skip_existing ?? 0))}</div><div className="sub">{isFin ? t("已入帳的交易") : isOrders ? t("已在系統中的訂單") : t("範例列{0}", [isCost ? "" : t("・沒有變更")])}</div></div>
        </div>
      )}

      {j.status === "committed" && j.stats.result && (
        <div className="alert ok">
          {t("匯入完成：")}{isFin
            ? t("{0} 筆交易，入帳 {1} 筆（費用 {2}、退款 {3}）{4}{5}。", [j.stats.result.transactions, j.stats.result.booked, fmtUsd(j.stats.result.fees_usd), fmtUsd(j.stats.result.refunds_usd), j.stats.result.order_not_in_erp ? t("；{0} 筆訂單不在系統，訂單進來後自動補上", [j.stats.result.order_not_in_erp]) : "", j.stats.result.needs_review ? t("；{0} 筆需要確認", [j.stats.result.needs_review]) : ""])
            : isOrders
            ? t("{0} 筆訂單（{1} 件商品），已付款成交額 {2}{3}。", [j.stats.result.orders, j.stats.result.lines, fmtUsd(j.stats.result.revenue_usd), j.stats.result.unmapped_lines ? t("；{0} 件待對應 SKU", [j.stats.result.unmapped_lines]) : ""])
            : isCost
            ? t("{0} 個採購批次、{1} 筆成本，落地成本合計 {2}{3}。成本已入帳；商品從日本寄出時，到「採購批次」按「寄出」，美國收到後按「簽收」。", [j.stats.result.batches, j.stats.result.lines, fmtUsd(j.stats.result.landed_total_usd), j.stats.result.new_skus ? t("，新增 {0} 個 SKU", [j.stats.result.new_skus]) : ""])
            : t("新增 {0} 個 SKU、更新 {1} 個 SKU。", [j.stats.result.created, j.stats.result.updated])}
          {isCost && <> <Link to="/purchasing">{t("前往採購批次 →")}</Link></>}
          {isOrders && <> <Link to="/orders?tab=unmapped">{t("前往對應商品 →")}</Link></>}
          {isFin && <> <Link to="/reports/shows">{t("前往直播損益 →")}</Link></>}
        </div>
      )}

      {j.status === "validated" && (
        <div className="card">
          <h2>{t("確認匯入")}</h2>
          <ErrorBox error={commit.error} />
          {isCost && (by.import_new_sku ?? 0) > 0 && (
            <label className="row small" style={{ marginBottom: 8 }}><input type="checkbox" checked={createSkus} onChange={(e) => setCreateSkus(e.target.checked)} />
              {t("一併建立")} {by.import_new_sku} {t("個系統中還沒有的 SKU")}</label>
          )}
          {isOrders && (
            <label className="row small" style={{ marginBottom: 8 }}><input type="checkbox" checked={markShipped} onChange={(e) => setMarkShipped(e.target.checked)} />
              {t("這些訂單都已經寄出（扣除美國庫存）。不勾選時只預留庫存，之後由 API 同步出貨狀態。")}</label>
          )}
          {(j.stats.errors ?? 0) > 0 && (
            <label className="row small" style={{ marginBottom: 8 }}><input type="checkbox" checked={skipErrors} onChange={(e) => setSkipErrors(e.target.checked)} />
              {t("略過")} {j.stats.errors} {t("筆有錯誤的列，只匯入正確的列")}</label>
          )}
          <div className="row">
            <button className="btn primary" disabled={commit.isPending || willImport === 0} onClick={() => commit.mutate()}>
              {commit.isPending ? t("匯入中…") : t("確認匯入 {0} 列", [willImport])}
            </button>
            <span className="small muted">{isFin ? t("每筆交易只入帳一次，和 API 同步重疊也不會重複；買家名稱不會保存，匯入完成後原始檔案會刪除。") : isOrders ? t("買家帳號、地址不會保存；匯入完成後原始檔案會刪除。") : isCost ? t("匯入的是成本紀錄；確認後不可修改，也不會直接增加庫存。") : t("庫存與成本欄位不會從商品主檔匯入。")}</span>
          </div>
        </div>
      )}

      {(j.status === "validated" || j.status === "committed") && (
        <div className="card">
          <div className="row" style={{ marginBottom: 10 }}>
            {[["all", t("全部")], ["errors", t("錯誤")], ["warnings", t("提醒")], ["import", t("將匯入")], ["skip", t("略過")]].map(([k, l]) => (
              <button key={k} className={`btn sm ${filter === k ? "primary" : ""}`} onClick={() => setFilter(k)}>{l}</button>
            ))}
          </div>
          {rows.isLoading ? <Loading /> : !rows.data?.length ? <div className="muted small">{t("沒有資料列")}</div> : isOrders ? <OrderRowsTable rows={rows.data} /> : <RowsTable rows={rows.data} isCost={isCost} />}
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
            <tr><th>{t("列")}</th><th>{t("狀態")}</th><th>{t("批次 / SKU")}</th><th className="num">{t("數量")}</th><th className="num">{t("單價")}</th><th className="num">{t("匯率（1 USD =）")}</th><th className="num">{t("落地成本/件")}</th><th>{t("說明")}</th></tr>
          ) : (
            <tr><th>{t("列")}</th><th>{t("狀態")}</th><th>SKU</th><th>{t("名稱")}</th><th>{t("分類")}</th><th className="num">{t("售價")}</th><th className="num">{t("安全庫存")}</th><th>{t("說明")}</th></tr>
          )}
        </thead>
        <tbody>
          {rows.map((r) => {
            const n = r.normalized;
            const a = ACTION[r.action] ?? [r.action, ""];
            const notes = (
              <td data-label={t("說明")} className="small" style={{ minWidth: 220 }}>
                {r.errors.map((e, i) => <div key={i} style={{ color: "var(--danger)" }}>✖ {e}</div>)}
                {r.warnings.map((w, i) => <div key={i} style={{ color: r.action === "skip_example" ? "var(--muted)" : "var(--warn)" }}>! {w}</div>)}
              </td>
            );
            return isCost ? (
              <tr key={r.row_number}>
                <td className="lead">{t("第")} {r.row_number} {t("列")}</td>
                <td data-label={t("狀態")}><span className={`badge ${a[1]}`}>{a[0]}</span></td>
                <td data-label={t("批次 / SKU")}><div className="mono">{n.sku_code ?? "—"}</div><div className="small muted">{n.batch_no ?? n.batch_ref ?? ""}{n.purchase_date ? ` · ${n.purchase_date}` : ""}</div></td>
                <td data-label={t("數量")} className="num">{n.qty ?? "—"}</td>
                <td data-label={t("單價")} className="num">{n.unit_cost ? `${Number(n.unit_cost).toLocaleString()} ${n.currency ?? ""}` : "—"}</td>
                <td data-label={t("匯率（1 USD =）")} className="num">{n.fx_quote ? `${Number(n.fx_quote)} ${n.currency ?? ""}` : "—"}</td>
                <td data-label={t("落地成本/件")} className="num">{n.landed_unit_usd ? fmtUsd(n.landed_unit_usd) : "—"}</td>
                {notes}
              </tr>
            ) : (
              <tr key={r.row_number}>
                <td className="lead">{t("第")} {r.row_number} {t("列")}</td>
                <td data-label={t("狀態")}><span className={`badge ${a[1]}`}>{a[0]}</span></td>
                <td data-label="SKU" className="mono">{n.sku_code ?? "—"}</td>
                <td data-label={t("名稱")}>{n.product_name ?? "—"}</td>
                <td data-label={t("分類")}>{n.category ?? "—"}</td>
                <td data-label={t("售價")} className="num">{n.list_price ?? "—"}</td>
                <td data-label={t("安全庫存")} className="num">{n.safety_stock ?? "—"}</td>
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
          <h2 style={{ margin: 0 }}>{t("欄位對應")}</h2>
          <div className="small muted">
            {job.mapping.sheet ? t("工作表「{0}」第 {1} 列是標題，已對應 {2} 個欄位", [job.mapping.sheet, (job.mapping.header_row ?? 0) + 1, mappedCount]) : t("找不到可辨識的標題列，請手動指定")}
            {job.mapping.remembered && t("（使用上次儲存的對應）")}
          </div>
        </div>
        {editable && <button className="btn sm" onClick={() => setOpen(!open)}>{open ? t("收合") : t("檢查 / 修改")}</button>}
      </div>
      {job.status === "parsed" && job.stats.missing_fields?.length ? (
        <div className="alert error">{t("還沒對應的必填欄位：")}{job.stats.missing_fields.join(t("、"))}</div>
      ) : null}
      {open && editable && (
        <>
          <ErrorBox error={save.error} />
          <div className="inline-inputs">
            <label className="field"><span>{t("工作表")}</span>
              <select value={sheet} onChange={(e) => (setSheet(e.target.value), setHeaderRow(0), setCols({}))}>
                {job.sheets.map((s) => <option key={s.name} value={s.name}>{s.name}{t("（")}{s.rows} {t("列）")}</option>)}
              </select>
            </label>
            <label className="field"><span>{t("標題在第幾列")}</span>
              <select value={headerRow} onChange={(e) => setHeaderRow(Number(e.target.value))}>
                {preview.map((r, i) => <option key={i} value={i}>{t("第")} {i + 1} {t("列：")}{r.filter((c) => c !== null).slice(0, 3).join(" | ").slice(0, 50)}</option>)}
              </select>
            </label>
          </div>
          <div className="inline-inputs">
            {job.fields.map((f) => (
              <label className="field" key={f.key}>
                <span>{f.label}{f.required && <b style={{ color: "var(--danger)" }}> *</b>}</span>
                <select value={cols[f.key] ?? ""} onChange={(e) => setCols({ ...cols, [f.key]: e.target.value === "" ? null : Number(e.target.value) })} aria-label={f.label}>
                  <option value="">{t("（不使用）")}</option>
                  {headers.map((h, i) => (h === null || h === "" ? null : <option key={i} value={i}>{String(h)}</option>))}
                </select>
              </label>
            ))}
          </div>
          <button className="btn primary" disabled={save.isPending} onClick={() => save.mutate()}>{save.isPending ? t("檢查中…") : t("套用並重新檢查")}</button>
        </>
      )}
    </div>
  );
}

function OrderRowsTable({ rows }: { rows: Row[] }) {
  return (
    <div className="table-wrap">
      <table className="data responsive">
        <thead><tr><th>{t("列")}</th><th>{t("狀態")}</th><th>{t("訂單")}</th><th>{t("商品")}</th><th className="num">{t("數量")}</th><th className="num">{t("成交價")}</th><th>{t("說明")}</th></tr></thead>
        <tbody>
          {rows.map((r) => {
            const n = r.normalized;
            const a = ACTION[r.action] ?? [r.action, ""];
            return (
              <tr key={r.row_number}>
                <td className="lead">{t("第")} {r.row_number} {t("列")}</td>
                <td data-label={t("狀態")}><span className={`badge ${a[1]}`}>{a[0]}</span></td>
                <td data-label={t("訂單")} className="mono">{n.order_id ?? "—"}</td>
                <td data-label={t("商品")} className="small">{n.title ?? "—"}</td>
                <td data-label={t("數量")} className="num">{n.qty ?? "—"}</td>
                <td data-label={t("成交價")} className="num">{n.price !== undefined ? fmtUsd(n.price) : "—"}</td>
                <td data-label={t("說明")} className="small" style={{ minWidth: 200 }}>
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
