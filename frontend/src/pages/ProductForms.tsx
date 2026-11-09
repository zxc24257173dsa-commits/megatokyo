import { useState, type FormEvent } from "react";
import { useNavigate } from "react-router-dom";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { api } from "../api";
import { ErrorBox, Field, SkuPicker } from "../components/ui";
import type { Category, SkuDetail } from "../types";

function CategorySelect({ value, onChange }: { value: string; onChange: (v: string) => void }) {
  const qc = useQueryClient();
  const cats = useQuery({ queryKey: ["categories"], queryFn: () => api<Category[]>("/categories") });
  const [error, setError] = useState<unknown>(null);
  async function add() {
    const name = prompt("新分類名稱")?.trim();
    if (!name) return;
    try {
      const c = await api<Category>("/categories", { body: { name } });
      await qc.invalidateQueries({ queryKey: ["categories"] });
      onChange(String(c.id));
    } catch (e) {
      setError(e);
    }
  }
  return (
    <>
      <ErrorBox error={error} />
      <div className="row">
        <select style={{ flex: 1 }} value={value} onChange={(e) => onChange(e.target.value)}>
          <option value="">（未分類）</option>
          {cats.data?.map((c) => <option key={c.id} value={c.id}>{c.name}</option>)}
        </select>
        <button type="button" className="btn sm" onClick={add}>＋ 分類</button>
      </div>
    </>
  );
}

interface VariantRow {
  sku_code: string;
  variant_name: string;
  barcode: string;
  list_price: string;
}

export function NewProductPage() {
  const nav = useNavigate();
  const qc = useQueryClient();
  const [name, setName] = useState("");
  const [brand, setBrand] = useState("");
  const [category, setCategory] = useState("");
  const [description, setDescription] = useState("");
  const [currency, setCurrency] = useState("JPY");
  const [rows, setRows] = useState<VariantRow[]>([{ sku_code: "", variant_name: "", barcode: "", list_price: "" }]);
  const m = useMutation({
    mutationFn: () =>
      api<{ sku_ids: number[] }>("/products", {
        body: {
          name,
          brand: brand || null,
          category_id: category ? Number(category) : null,
          description: description || null,
          skus: rows.map((r) => ({
            sku_code: r.sku_code.trim(),
            variant_name: r.variant_name || null,
            barcode: r.barcode || null,
            list_price: r.list_price || null,
            list_currency: "USD",
            default_purchase_currency: currency,
          })),
        },
      }),
    onSuccess: (p) => {
      void qc.invalidateQueries({ queryKey: ["skus"] });
      nav(`/skus/${p.sku_ids[0]}`);
    },
  });
  const setRow = (i: number, k: keyof VariantRow, v: string) => setRows(rows.map((r, j) => (j === i ? { ...r, [k]: v } : r)));

  return (
    <form onSubmit={(e: FormEvent) => (e.preventDefault(), m.mutate())}>
      <div className="page-head"><h1>新增商品</h1></div>
      <div className="card">
        <ErrorBox error={m.error} />
        <Field label="商品名稱"><input required value={name} onChange={(e) => setName(e.target.value)} /></Field>
        <div className="inline-inputs">
          <Field label="品牌"><input value={brand} onChange={(e) => setBrand(e.target.value)} /></Field>
          <Field label="分類"><CategorySelect value={category} onChange={setCategory} /></Field>
          <Field label="採購幣別">
            <select value={currency} onChange={(e) => setCurrency(e.target.value)}><option>JPY</option><option>USD</option><option>TWD</option></select>
          </Field>
        </div>
        <Field label="說明"><textarea rows={2} value={description} onChange={(e) => setDescription(e.target.value)} /></Field>
      </div>
      <div className="card">
        <h2>SKU / 規格</h2>
        <p className="small muted">每個規格（顏色、尺寸、版本…）一個 SKU。SKU 編號建立後不能修改。</p>
        {rows.map((r, i) => (
          <div key={i} className="inline-inputs" style={{ borderTop: i ? "1px solid var(--border)" : undefined, paddingTop: i ? 12 : 0 }}>
            <Field label="SKU 編號"><input required className="mono" value={r.sku_code} onChange={(e) => setRow(i, "sku_code", e.target.value)} /></Field>
            <Field label="規格"><input value={r.variant_name} onChange={(e) => setRow(i, "variant_name", e.target.value)} placeholder="例：Box / 日版" /></Field>
            <Field label="條碼"><input value={r.barcode} onChange={(e) => setRow(i, "barcode", e.target.value)} /></Field>
            <Field label="售價 (USD)"><input inputMode="decimal" value={r.list_price} onChange={(e) => setRow(i, "list_price", e.target.value)} /></Field>
            {rows.length > 1 && (
              <div style={{ alignSelf: "end", marginBottom: 12 }}>
                <button type="button" className="btn sm danger" onClick={() => setRows(rows.filter((_, j) => j !== i))}>移除</button>
              </div>
            )}
          </div>
        ))}
        <div className="row">
          <button type="button" className="btn" onClick={() => setRows([...rows, { sku_code: "", variant_name: "", barcode: "", list_price: "" }])}>＋ 新增規格</button>
          <button className="btn primary" disabled={m.isPending}>{m.isPending ? "建立中…" : "建立商品"}</button>
        </div>
      </div>
    </form>
  );
}

export function NewBundlePage() {
  const nav = useNavigate();
  const qc = useQueryClient();
  const [code, setCode] = useState("");
  const [name, setName] = useState("");
  const [price, setPrice] = useState("");
  const [category, setCategory] = useState("");
  const [rows, setRows] = useState<{ sku_id: number | null; qty: number }[]>([{ sku_id: null, qty: 1 }]);
  const m = useMutation({
    mutationFn: () =>
      api<SkuDetail>("/bundles", {
        body: {
          sku_code: code.trim(),
          name,
          list_price: price || null,
          list_currency: "USD",
          category_id: category ? Number(category) : null,
          components: rows.filter((r) => r.sku_id).map((r) => ({ sku_id: r.sku_id, qty: r.qty })),
        },
      }),
    onSuccess: (s) => {
      void qc.invalidateQueries({ queryKey: ["skus"] });
      nav(`/skus/${s.id}`);
    },
  });
  return (
    <form onSubmit={(e: FormEvent) => (e.preventDefault(), m.mutate())}>
      <div className="page-head"><h1>新增組合商品</h1></div>
      <div className="card">
        <p className="small muted">組合商品由多個 SKU 組成，本身沒有庫存；賣出一組時，系統會扣除每個組成 SKU 的庫存。</p>
        <ErrorBox error={m.error} />
        <div className="inline-inputs">
          <Field label="組合 SKU 編號"><input required className="mono" value={code} onChange={(e) => setCode(e.target.value)} /></Field>
          <Field label="名稱"><input required value={name} onChange={(e) => setName(e.target.value)} /></Field>
          <Field label="售價 (USD)"><input inputMode="decimal" value={price} onChange={(e) => setPrice(e.target.value)} /></Field>
          <Field label="分類"><CategorySelect value={category} onChange={setCategory} /></Field>
        </div>
        <h2>組成 SKU</h2>
        {rows.map((r, i) => (
          <div className="row" key={i} style={{ marginBottom: 8 }}>
            <div style={{ flex: 1, minWidth: 200 }}>
              <SkuPicker value={r.sku_id} excludeIds={rows.map((x) => x.sku_id ?? -1)} onChange={(v) => setRows(rows.map((x, j) => (j === i ? { ...x, sku_id: v } : x)))} />
            </div>
            <input aria-label="數量" style={{ width: 80 }} type="number" min={1} value={r.qty} onChange={(e) => setRows(rows.map((x, j) => (j === i ? { ...x, qty: Number(e.target.value) } : x)))} />
            {rows.length > 1 && <button type="button" className="btn sm danger" onClick={() => setRows(rows.filter((_, j) => j !== i))}>移除</button>}
          </div>
        ))}
        <div className="row">
          <button type="button" className="btn" onClick={() => setRows([...rows, { sku_id: null, qty: 1 }])}>＋ 加入 SKU</button>
          <button className="btn primary" disabled={m.isPending}>{m.isPending ? "建立中…" : "建立組合商品"}</button>
        </div>
      </div>
    </form>
  );
}
