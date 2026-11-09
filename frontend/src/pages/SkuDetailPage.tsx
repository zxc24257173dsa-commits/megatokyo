import { Fragment, useEffect, useState, type FormEvent } from "react";
import { Link, useParams } from "react-router-dom";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { api } from "../api";
import { useAuth } from "../auth";
import { ErrorBox, Field, Loading, Qty, SkuPicker, fmtDateTime, fmtInt, fmtUsd, useBusinessSettings } from "../components/ui";
import { LOCATION_LABEL, MOVEMENT_LABEL, type Movement, type SkuDetail } from "../types";

export default function SkuDetailPage() {
  const id = Number(useParams().id);
  const { can, me } = useAuth();
  const { japanEnabled } = useBusinessSettings();
  const qc = useQueryClient();
  const sku = useQuery({ queryKey: ["sku", id], queryFn: () => api<SkuDetail>(`/skus/${id}`) });
  const moves = useQuery({
    queryKey: ["movements", id],
    queryFn: () => api<{ items: Movement[] }>("/inventory/movements", { params: { sku_id: id, limit: 50 } }),
    enabled: !!sku.data && !sku.data.is_bundle,
  });
  const refresh = () => {
    void qc.invalidateQueries({ queryKey: ["sku", id] });
    void qc.invalidateQueries({ queryKey: ["skus"] });
  };

  if (sku.isLoading) return <Loading />;
  if (sku.error || !sku.data) return <ErrorBox error={sku.error} />;
  const s = sku.data;
  const editable = can("catalog.edit");

  return (
    <>
      <div className="page-head">
        <div>
          <div className="small muted">
            <Link to="/">庫存總覽</Link> / {s.product_name}
          </div>
          <h1>
            <span className="mono" style={{ fontSize: "inherit" }}>{s.sku_code}</span> {s.is_bundle && <span className="badge accent">組合商品</span>}{" "}
            {s.status === "discontinued" && <span className="badge">已停售</span>}
          </h1>
          <div className="muted">
            {s.name}
            {s.variant_name ? ` · ${s.variant_name}` : ""}
          </div>
        </div>
      </div>

      <div className="tiles" style={japanEnabled ? undefined : { gridTemplateColumns: "repeat(3, minmax(0, 1fr))" }}>
        {japanEnabled && <div className="tile"><div className="label">日本倉</div><div className="value"><Qty n={s.jp_on_hand} /></div></div>}
        <div className="tile"><div className="label">運送中</div><div className="value"><Qty n={s.in_transit} kind="transit" /></div></div>
        <div className="tile"><div className="label">美國倉</div><div className="value"><Qty n={s.us_on_hand} /></div><div className="sub">已預留 {fmtInt(s.us_reserved)}</div></div>
        <div className="tile"><div className="label">美國可銷售</div><div className="value"><Qty n={s.us_available} /></div>
          {s.can_view_cost && !s.is_bundle && <div className="sub">庫存成本 {fmtUsd(s.inventory_value_usd)}</div>}</div>
      </div>

      <div className="grid-2">
        <div className="stack">
          {editable ? <SkuEditForm sku={s} onSaved={refresh} /> : <SkuInfo sku={s} />}
          {s.is_bundle && <ComponentsCard sku={s} editable={editable} onSaved={refresh} />}
          {!s.is_bundle && s.used_in_bundles.length > 0 && (
            <div className="card">
              <h2>用於組合商品</h2>
              {s.used_in_bundles.map((b) => (
                <div key={b.sku_id}>
                  <Link to={`/skus/${b.sku_id}`} className="mono">{b.sku_code}</Link> {b.name} × {b.qty}
                </div>
              ))}
            </div>
          )}
        </div>
        <div className="stack">
          <ImagesCard sku={s} editable={editable} onChanged={refresh} />
          {!s.is_bundle && (
            <div className="card">
              <h2>批次（FIFO，先進先出）</h2>
              {s.lots.length === 0 ? (
                <div className="muted small">尚無庫存批次</div>
              ) : (
                <div className="table-wrap">
                  <table className="data">
                    <thead>
                      <tr>
                        <th>批次</th>
                        <th>{japanEnabled ? "入庫" : "日本寄出"}</th>
                        {japanEnabled && <th className="num">日本</th>}
                        <th className="num">運送中</th>
                        <th className="num">美國</th>
                        {s.can_view_cost && <th className="num">單位成本</th>}
                      </tr>
                    </thead>
                    <tbody>
                      {s.lots.map((l) => (
                        <tr key={l.id} style={l.remaining === 0 ? { opacity: 0.5 } : undefined}>
                          <td className="mono">{l.lot_no}</td>
                          <td className="small">{fmtDateTime(l.received_at, me?.timezone)}</td>
                          {japanEnabled && <td className="num"><Qty n={l.jp} /></td>}
                          <td className="num"><Qty n={l.in_transit} kind="transit" /></td>
                          <td className="num"><Qty n={l.us} /></td>
                          {s.can_view_cost && <td className="num">{fmtUsd(l.unit_cost_base)}</td>}
                        </tr>
                      ))}
                    </tbody>
                  </table>
                </div>
              )}
            </div>
          )}
        </div>
      </div>

      {!s.is_bundle && (
        <div className="card">
          <h2>庫存異動紀錄</h2>
          {moves.isLoading ? (
            <Loading />
          ) : !moves.data?.items.length ? (
            <div className="muted small">尚無異動</div>
          ) : (
            <div className="table-wrap">
              <table className="data responsive">
                <thead>
                  <tr>
                    <th>時間</th>
                    <th>類型</th>
                    <th>從 → 到</th>
                    <th className="num">數量</th>
                    <th>批次</th>
                    <th>操作人 / 說明</th>
                  </tr>
                </thead>
                <tbody>
                  {moves.data.items.map((m) => (
                    <tr key={m.id}>
                      <td className="lead small">{fmtDateTime(m.occurred_at, me?.timezone)}</td>
                      <td data-label="類型"><span className="badge">{MOVEMENT_LABEL[m.movement_type] ?? m.movement_type}</span></td>
                      <td data-label="從 → 到" className="small">{LOCATION_LABEL[m.from_location]} → {LOCATION_LABEL[m.to_location]}</td>
                      <td data-label="數量" className="num">{fmtInt(m.qty)}</td>
                      <td data-label="批次" className="mono">{m.lot_no}</td>
                      <td data-label="操作人" className="small">{m.created_by ?? "系統"}{m.note ? ` · ${m.note}` : ""}</td>
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

function SkuInfo({ sku }: { sku: SkuDetail }) {
  return (
    <div className="card">
      <h2>商品資料</h2>
      <dl className="kv">
        <dt>售價</dt><dd>{sku.list_price ? `${sku.list_currency} ${Number(sku.list_price).toFixed(2)}` : "—"}</dd>
        <dt>條碼</dt><dd>{sku.barcode ?? "—"}</dd>
        <dt>分類</dt><dd>{sku.category_name ?? "—"}</dd>
        <dt>安全庫存</dt><dd>{sku.safety_stock_override ?? "自動計算"}</dd>
        {Object.entries(sku.attributes).map(([k, v]) => (
          <Fragment key={k}><dt>{k}</dt><dd>{v}</dd></Fragment>
        ))}
      </dl>
    </div>
  );
}

function SkuEditForm({ sku, onSaved }: { sku: SkuDetail; onSaved: () => void }) {
  const [form, setForm] = useState({
    name: sku.name,
    variant_name: sku.variant_name ?? "",
    barcode: sku.barcode ?? "",
    list_price: sku.list_price ? String(Number(sku.list_price)) : "",
    list_currency: sku.list_currency,
    default_purchase_currency: sku.default_purchase_currency,
    safety_stock_override: sku.safety_stock_override?.toString() ?? "",
    status: sku.status,
  });
  const [saved, setSaved] = useState(false);
  useEffect(() => setSaved(false), [form]);
  const m = useMutation({
    mutationFn: () =>
      api(`/skus/${sku.id}`, {
        method: "PATCH",
        body: {
          version: sku.version,
          name: form.name,
          variant_name: form.variant_name || null,
          barcode: form.barcode || null,
          list_price: form.list_price === "" ? null : form.list_price,
          list_currency: form.list_currency,
          default_purchase_currency: form.default_purchase_currency,
          safety_stock_override: form.safety_stock_override === "" ? null : Number(form.safety_stock_override),
          status: form.status,
        },
      }),
    onSuccess: () => {
      setSaved(true);
      onSaved();
    },
  });
  const set = (k: keyof typeof form) => (e: React.ChangeEvent<HTMLInputElement | HTMLSelectElement>) => setForm({ ...form, [k]: e.target.value });
  return (
    <form className="card" onSubmit={(e: FormEvent) => (e.preventDefault(), m.mutate())}>
      <h2>商品資料</h2>
      <ErrorBox error={m.error} />
      {saved && <div className="alert ok">已儲存</div>}
      <Field label="名稱"><input required value={form.name} onChange={set("name")} /></Field>
      <div className="inline-inputs">
        <Field label="規格"><input value={form.variant_name} onChange={set("variant_name")} /></Field>
        <Field label="條碼"><input value={form.barcode} onChange={set("barcode")} /></Field>
        <Field label="售價"><input inputMode="decimal" value={form.list_price} onChange={set("list_price")} /></Field>
        <Field label="售價幣別">
          <select value={form.list_currency} onChange={set("list_currency")}><option>USD</option><option>JPY</option><option>TWD</option></select>
        </Field>
        <Field label="採購幣別">
          <select value={form.default_purchase_currency} onChange={set("default_purchase_currency")}><option>JPY</option><option>USD</option><option>TWD</option></select>
        </Field>
        <Field label="安全庫存（空白=自動）"><input inputMode="numeric" value={form.safety_stock_override} onChange={set("safety_stock_override")} /></Field>
        <Field label="狀態">
          <select value={form.status} onChange={set("status")}><option value="active">販售中</option><option value="discontinued">已停售</option></select>
        </Field>
      </div>
      <button className="btn primary" disabled={m.isPending}>{m.isPending ? "儲存中…" : "儲存"}</button>
    </form>
  );
}

function ComponentsCard({ sku, editable, onSaved }: { sku: SkuDetail; editable: boolean; onSaved: () => void }) {
  const [rows, setRows] = useState(sku.components.map((c) => ({ sku_id: c.sku_id as number | null, qty: c.qty })));
  const [editing, setEditing] = useState(false);
  const m = useMutation({
    mutationFn: () =>
      api(`/skus/${sku.id}/components`, {
        method: "PUT",
        body: { version: sku.version, components: rows.filter((r) => r.sku_id).map((r) => ({ sku_id: r.sku_id, qty: r.qty })) },
      }),
    onSuccess: () => {
      setEditing(false);
      onSaved();
    },
  });
  return (
    <div className="card">
      <div className="page-head" style={{ marginBottom: 8 }}>
        <h2 style={{ margin: 0 }}>組成 SKU</h2>
        {editable && !editing && <button className="btn sm" onClick={() => setEditing(true)}>修改</button>}
      </div>
      <ErrorBox error={m.error} />
      {!editing ? (
        sku.components.map((c) => (
          <div key={c.sku_id}>
            <Link className="mono" to={`/skus/${c.sku_id}`}>{c.sku_code}</Link> × {c.qty}
          </div>
        ))
      ) : (
        <>
          {rows.map((r, i) => (
            <div className="row" key={i} style={{ marginBottom: 8 }}>
              <div style={{ flex: 1 }}>
                <SkuPicker value={r.sku_id} excludeIds={rows.map((x) => x.sku_id ?? -1)} onChange={(v) => setRows(rows.map((x, j) => (j === i ? { ...x, sku_id: v } : x)))} />
              </div>
              <input style={{ width: 80 }} type="number" min={1} value={r.qty} onChange={(e) => setRows(rows.map((x, j) => (j === i ? { ...x, qty: Number(e.target.value) } : x)))} />
              <button className="btn sm danger" type="button" onClick={() => setRows(rows.filter((_, j) => j !== i))}>移除</button>
            </div>
          ))}
          <div className="row">
            <button className="btn sm" type="button" onClick={() => setRows([...rows, { sku_id: null, qty: 1 }])}>＋ 加入 SKU</button>
            <button className="btn sm primary" onClick={() => m.mutate()} disabled={m.isPending}>儲存</button>
            <button className="btn sm" onClick={() => setEditing(false)}>取消</button>
          </div>
        </>
      )}
    </div>
  );
}

function ImagesCard({ sku, editable, onChanged }: { sku: SkuDetail; editable: boolean; onChanged: () => void }) {
  const [error, setError] = useState<unknown>(null);
  const [busy, setBusy] = useState(false);
  async function upload(files: FileList | null) {
    if (!files?.length) return;
    setBusy(true);
    setError(null);
    try {
      for (const f of Array.from(files)) {
        const form = new FormData();
        form.append("file", f);
        await api(`/skus/${sku.id}/images`, { form, method: "POST" });
      }
      onChanged();
    } catch (e) {
      setError(e);
    } finally {
      setBusy(false);
    }
  }
  async function act(path: string, method: string) {
    setError(null);
    try {
      await api(path, { method });
      onChanged();
    } catch (e) {
      setError(e);
    }
  }
  return (
    <div className="card">
      <h2>商品照片</h2>
      <ErrorBox error={error} />
      <div className="gallery">
        {sku.images.map((img) => (
          <figure key={img.id}>
            <img src={img.url} className={img.is_primary ? "primary" : undefined} alt="" />
            {editable && (
              <div className="row" style={{ gap: 4, marginTop: 4 }}>
                {!img.is_primary && <button className="btn sm" onClick={() => act(`/skus/${sku.id}/images/${img.id}/primary`, "POST")}>設為主圖</button>}
                <button className="btn sm danger" onClick={() => confirm("刪除這張照片？") && act(`/skus/${sku.id}/images/${img.id}`, "DELETE")}>刪除</button>
              </div>
            )}
          </figure>
        ))}
        {!sku.images.length && <div className="muted small">尚無照片</div>}
      </div>
      {editable && (
        <label className="btn" style={{ marginTop: 12 }}>
          {busy ? "上傳中…" : "上傳照片"}
          <input type="file" accept="image/jpeg,image/png,image/webp" multiple hidden onChange={(e) => void upload(e.target.files)} />
        </label>
      )}
    </div>
  );
}
