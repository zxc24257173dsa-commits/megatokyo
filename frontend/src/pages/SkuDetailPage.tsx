import { Fragment, useEffect, useState, type FormEvent } from "react";
import { Link, useParams } from "react-router-dom";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { api, newIdempotencyKey } from "../api";
import { useAuth } from "../auth";
import { ErrorBox, Field, Loading, Qty, SkuPicker, fmtDateTime, fmtInt, fmtUsd, useBusinessSettings } from "../components/ui";
import { LOCATION_LABEL, MOVEMENT_LABEL, type Movement, type SkuDetail } from "../types";
import { t } from "../i18n";

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
            <Link to="/inventory">{t("庫存總覽")}</Link> / {s.product_name}
          </div>
          <h1>
            <span className="mono" style={{ fontSize: "inherit" }}>{s.sku_code}</span> {s.is_bundle && <span className="badge accent">{t("組合商品")}</span>}{" "}
            {s.status === "discontinued" && <span className="badge">{t("已停售")}</span>}
          </h1>
          <div className="muted">
            {s.name}
            {s.variant_name ? ` · ${s.variant_name}` : ""}
          </div>
        </div>
      </div>

      <div className="tiles" style={japanEnabled ? undefined : { gridTemplateColumns: "repeat(3, minmax(0, 1fr))" }}>
        {japanEnabled && <div className="tile"><div className="label">{t("日本倉")}</div><div className="value"><Qty n={s.jp_on_hand} /></div></div>}
        <div className="tile"><div className="label">{t("運送中")}</div><div className="value"><Qty n={s.in_transit} kind="transit" /></div></div>
        <div className="tile"><div className="label">{t("美國倉")}</div><div className="value"><Qty n={s.us_on_hand} /></div><div className="sub">{t("已預留")} {fmtInt(s.us_reserved)}</div></div>
        <div className="tile"><div className="label">{t("美國可銷售")}</div><div className="value"><Qty n={s.us_available} /></div>
          {s.can_view_cost && !s.is_bundle && <div className="sub">{t("全部庫存成本（含運送中）")}{fmtUsd(s.inventory_value_usd)}</div>}</div>
      </div>

      <div className="grid-2">
        <div className="stack">
          {editable ? <SkuEditForm sku={s} onSaved={refresh} /> : <SkuInfo sku={s} />}
          {s.is_bundle && <ComponentsCard sku={s} editable={editable} onSaved={refresh} />}
          {!s.is_bundle && (editable || s.box_contents.length > 0) && <BoxCard sku={s} editable={editable} onSaved={refresh} />}
          {!s.is_bundle && s.used_in_bundles.length > 0 && (
            <div className="card">
              <h2>{t("用於組合商品")}</h2>
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
              <h2>{t("批次（FIFO，先進先出）")}</h2>
              {s.lots.length === 0 ? (
                <div className="muted small">{t("尚無庫存批次")}</div>
              ) : (
                <div className="table-wrap">
                  <table className="data">
                    <thead>
                      <tr>
                        <th>{t("批次")}</th>
                        <th>{japanEnabled ? t("入庫") : t("日本寄出")}</th>
                        {japanEnabled && <th className="num">{t("日本")}</th>}
                        <th className="num">{t("運送中")}</th>
                        <th className="num">{t("美國")}</th>
                        {s.can_view_cost && <th className="num">{t("單位成本")}</th>}
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
          <h2>{t("庫存異動紀錄")}</h2>
          {moves.isLoading ? (
            <Loading />
          ) : !moves.data?.items.length ? (
            <div className="muted small">{t("尚無異動")}</div>
          ) : (
            <div className="table-wrap">
              <table className="data responsive">
                <thead>
                  <tr>
                    <th>{t("時間")}</th>
                    <th>{t("類型")}</th>
                    <th>{t("從 → 到")}</th>
                    <th className="num">{t("數量")}</th>
                    <th>{t("批次")}</th>
                    <th>{t("操作人 / 說明")}</th>
                  </tr>
                </thead>
                <tbody>
                  {moves.data.items.map((m) => (
                    <tr key={m.id}>
                      <td className="lead small">{fmtDateTime(m.occurred_at, me?.timezone)}</td>
                      <td data-label={t("類型")}><span className="badge">{movementLabel(m)}</span></td>
                      <td data-label={t("從 → 到")} className="small">{LOCATION_LABEL[m.from_location]} → {LOCATION_LABEL[m.to_location]}</td>
                      <td data-label={t("數量")} className="num">{fmtInt(m.qty)}</td>
                      <td data-label={t("批次")} className="mono">{m.lot_no}</td>
                      <td data-label={t("操作人")} className="small">{m.created_by ?? t("系統")}{m.note ? ` · ${m.note}` : ""}</td>
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

function movementLabel(m: Movement): string {
  // Purchased goods now ship straight from Japan to the US: supplier -> in transit is a shipment.
  if (m.movement_type === "purchase_receipt" && m.to_location === "IN_TRANSIT") return t("日本寄出");
  if (m.movement_type === "purchase_receipt" && m.to_location === "US_MAIN") return t("採購直接入美國倉");
  if (m.movement_type === "transfer_receive") return t("美國簽收");
  return MOVEMENT_LABEL[m.movement_type] ?? m.movement_type;
}

function SkuInfo({ sku }: { sku: SkuDetail }) {
  return (
    <div className="card">
      <h2>{t("商品資料")}</h2>
      <dl className="kv">
        <dt>{t("售價")}</dt><dd>{sku.list_price ? `${sku.list_currency} ${Number(sku.list_price).toFixed(2)}` : "—"}</dd>
        <dt>{t("條碼")}</dt><dd>{sku.barcode ?? "—"}</dd>
        <dt>{t("分類")}</dt><dd>{sku.category_name ?? "—"}</dd>
        <dt>{t("安全庫存")}</dt><dd>{sku.safety_stock_override ?? t("自動計算")}</dd>
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
      <h2>{t("商品資料")}</h2>
      <ErrorBox error={m.error} />
      {saved && <div className="alert ok">{t("已儲存")}</div>}
      <Field label={t("名稱")}><input required value={form.name} onChange={set("name")} /></Field>
      <div className="inline-inputs">
        <Field label={t("規格")}><input value={form.variant_name} onChange={set("variant_name")} /></Field>
        <Field label={t("條碼")}><input value={form.barcode} onChange={set("barcode")} /></Field>
        <Field label={t("售價")}><input inputMode="decimal" value={form.list_price} onChange={set("list_price")} /></Field>
        <Field label={t("售價幣別")}>
          <select value={form.list_currency} onChange={set("list_currency")}><option>USD</option><option>JPY</option><option>TWD</option></select>
        </Field>
        <Field label={t("採購幣別")}>
          <select value={form.default_purchase_currency} onChange={set("default_purchase_currency")}><option>JPY</option><option>USD</option><option>TWD</option></select>
        </Field>
        <Field label={t("安全庫存（空白=自動）")}><input inputMode="numeric" value={form.safety_stock_override} onChange={set("safety_stock_override")} /></Field>
        <Field label={t("狀態")}>
          <select value={form.status} onChange={set("status")}><option value="active">{t("販售中")}</option><option value="discontinued">{t("已停售")}</option></select>
        </Field>
      </div>
      <button className="btn primary" disabled={m.isPending}>{m.isPending ? t("儲存中…") : t("儲存")}</button>
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
        <h2 style={{ margin: 0 }}>{t("組成 SKU")}</h2>
        {editable && !editing && <button className="btn sm" onClick={() => setEditing(true)}>{t("修改")}</button>}
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
              <button className="btn sm danger" type="button" onClick={() => setRows(rows.filter((_, j) => j !== i))}>{t("移除")}</button>
            </div>
          ))}
          <div className="row">
            <button className="btn sm" type="button" onClick={() => setRows([...rows, { sku_id: null, qty: 1 }])}>{t("＋ 加入 SKU")}</button>
            <button className="btn sm primary" onClick={() => m.mutate()} disabled={m.isPending}>{t("儲存")}</button>
            <button className="btn sm" onClick={() => setEditing(false)}>{t("取消")}</button>
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
      <h2>{t("商品照片")}</h2>
      <ErrorBox error={error} />
      <div className="gallery">
        {sku.images.map((img) => (
          <figure key={img.id}>
            <img src={img.url} className={img.is_primary ? "primary" : undefined} alt="" />
            {editable && (
              <div className="row" style={{ gap: 4, marginTop: 4 }}>
                {!img.is_primary && <button className="btn sm" onClick={() => act(`/skus/${sku.id}/images/${img.id}/primary`, "POST")}>{t("設為主圖")}</button>}
                <button className="btn sm danger" onClick={() => confirm(t("刪除這張照片？")) && act(`/skus/${sku.id}/images/${img.id}`, "DELETE")}>{t("刪除")}</button>
              </div>
            )}
          </figure>
        ))}
        {!sku.images.length && <div className="muted small">{t("尚無照片")}</div>}
      </div>
      {editable && (
        <label className="btn" style={{ marginTop: 12 }}>
          {busy ? t("上傳中…") : t("上傳照片")}
          <input type="file" accept="image/jpeg,image/png,image/webp" multiple hidden onChange={(e) => void upload(e.target.files)} />
        </label>
      )}
    </div>
  );
}

function BoxCard({ sku, editable, onSaved }: { sku: SkuDetail; editable: boolean; onSaved: () => void }) {
  const { can } = useAuth();
  const { japanEnabled } = useBusinessSettings();
  const [editing, setEditing] = useState(false);
  const [rows, setRows] = useState(sku.box_contents.map((c) => ({ sku_id: c.sku_id as number | null, qty: c.qty })));
  const [qty, setQty] = useState("1");
  const [note, setNote] = useState("");
  const [key, setKey] = useState(newIdempotencyKey);
  const [done, setDone] = useState<string | null>(null);
  const locations = [can("inventory.adjust_us") && "US_MAIN", japanEnabled && can("inventory.adjust_jp") && "JP_MAIN"].filter(Boolean) as string[];
  const [loc, setLoc] = useState(locations[0] ?? "");
  const units = sku.box_contents.reduce((a, c) => a + c.qty, 0);
  const save = useMutation({
    mutationFn: () => api(`/skus/${sku.id}/box-contents`, { method: "PUT", body: { version: sku.version, contents: rows.filter((r) => r.sku_id).map((r) => ({ sku_id: r.sku_id, qty: r.qty })) } }),
    onSuccess: () => (setEditing(false), onSaved()),
  });
  const open = useMutation({
    mutationFn: () => api<{ qty: number; units_per_box: number }>("/inventory/box-openings", { body: { box_sku_id: sku.id, location_code: loc, qty: Number(qty), note: note || null }, idempotencyKey: key }),
    onSuccess: (r) => {
      setDone(t("已拆 {0} 盒，取出 {1} 件；每件成本 = 盒子成本 ÷ {2}", [r.qty, r.qty * r.units_per_box, r.units_per_box]));
      setKey(newIdempotencyKey());
      setQty("1");
      setNote("");
      onSaved();
    },
  });
  return (
    <div className="card">
      <div className="page-head" style={{ marginBottom: 8 }}>
        <h2 style={{ margin: 0 }}>{t("盒內內容（拆盒）")}</h2>
        {editable && !editing && <button className="btn sm" onClick={() => setEditing(true)}>{sku.box_contents.length ? t("修改") : t("設定")}</button>}
      </div>
      <ErrorBox error={save.error ?? open.error} />
      {done && <div className="alert ok small">{done}</div>}
      {!editing ? (
        sku.box_contents.length ? (
          <>
            {sku.box_contents.map((c) => (
              <div key={c.sku_id} className="small"><Link className="mono" to={`/skus/${c.sku_id}`}>{c.sku_code}</Link> {c.name} × {c.qty}</div>
            ))}
            <p className="small muted">{t("每盒共")} {units} {t("件；拆盒時盒子成本")}<b>{t("平均分給每一件")}</b>{t("（每件 = 盒子成本 ÷")} {units}{t("）。")}</p>
            {locations.length > 0 && (
              <div className="row">
                <input style={{ width: 80 }} inputMode="numeric" value={qty} onChange={(e) => setQty(e.target.value.replace(/\D/g, ""))} aria-label={t("拆幾盒")} />
                {locations.length > 1 && (
                  <select style={{ width: "auto" }} value={loc} onChange={(e) => setLoc(e.target.value)} aria-label={t("倉庫")}>
                    {locations.map((l) => <option key={l} value={l}>{LOCATION_LABEL[l]}</option>)}
                  </select>
                )}
                <input style={{ flex: "1 1 140px" }} placeholder={t("備註（選填）")} value={note} onChange={(e) => setNote(e.target.value)} aria-label={t("拆盒備註")} />
                <button className="btn primary" disabled={!Number(qty) || open.isPending} onClick={() => confirm(t("拆 {0} 盒？會扣掉 {1} 盒、加上 {2} 件內容。", [qty, qty, Number(qty) * units])) && open.mutate()}>{t("拆盒")}</button>
              </div>
            )}
          </>
        ) : <div className="muted small">{t("設定盒子裡有哪些商品後，就可以「拆盒」：扣掉盒子、加上內容，成本平均分攤。")}</div>
      ) : (
        <>
          {rows.map((r, i) => (
            <div className="row" key={i} style={{ marginBottom: 8 }}>
              <div style={{ flex: 1 }}>
                <SkuPicker value={r.sku_id} excludeIds={[sku.id, ...rows.map((x) => x.sku_id ?? -1)]} onChange={(v) => setRows(rows.map((x, j) => (j === i ? { ...x, sku_id: v } : x)))} />
              </div>
              <input style={{ width: 80 }} type="number" min={1} value={r.qty} aria-label={t("每盒數量")} onChange={(e) => setRows(rows.map((x, j) => (j === i ? { ...x, qty: Number(e.target.value) } : x)))} />
              <button className="btn sm danger" type="button" onClick={() => setRows(rows.filter((_, j) => j !== i))}>{t("移除")}</button>
            </div>
          ))}
          <div className="row">
            <button className="btn sm" type="button" onClick={() => setRows([...rows, { sku_id: null, qty: 1 }])}>{t("＋ 加入內容")}</button>
            <button className="btn sm primary" onClick={() => save.mutate()} disabled={save.isPending}>{t("儲存")}</button>
            <button className="btn sm" onClick={() => setEditing(false)}>{t("取消")}</button>
          </div>
        </>
      )}
    </div>
  );
}
