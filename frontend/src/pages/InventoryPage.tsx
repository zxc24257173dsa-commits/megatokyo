import { useState } from "react";
import { Link, useNavigate } from "react-router-dom";
import { useQuery } from "@tanstack/react-query";
import { api } from "../api";
import { useAuth } from "../auth";
import { ErrorBox, Loading, Qty, fmtDate, fmtInt, fmtUsd, useBusinessSettings, useDebounced, useSkus } from "../components/ui";
import type { Category } from "../types";

interface SummaryLoc {
  location_code: string;
  location_name: string;
  units: number;
  sku_count: number;
  value_usd?: string;
  reserved_units?: number;
  available_units?: number;
}

export default function InventoryPage() {
  const { can, me } = useAuth();
  const { japanEnabled } = useBusinessSettings();
  const nav = useNavigate();
  const [search, setSearch] = useState("");
  const [category, setCategory] = useState("");
  const [status, setStatus] = useState("active");
  const q = useDebounced(search);
  const skus = useSkus({ search: q, category_id: category || undefined, status });
  const summary = useQuery({ queryKey: ["inv-summary"], queryFn: () => api<{ locations: SummaryLoc[]; can_view_cost: boolean }>("/inventory/summary") });
  const cats = useQuery({ queryKey: ["categories"], queryFn: () => api<Category[]>("/categories") });
  const loc = (code: string) => summary.data?.locations.find((l) => l.location_code === code);
  const showCost = summary.data?.can_view_cost;

  const tile = (code: string, label: string, sub?: string) => {
    const l = loc(code);
    return (
      <div className="tile">
        <div className="label">{label}</div>
        <div className="value">{fmtInt(l?.units)}</div>
        <div className="sub">{sub ?? (showCost ? `成本 ${fmtUsd(l?.value_usd)}` : `${fmtInt(l?.sku_count)} 個 SKU`)}</div>
      </div>
    );
  };

  return (
    <>
      <div className="page-head">
        <h1>庫存總覽</h1>
        {can("catalog.edit") && (
          <div className="row">
            <Link className="btn" to="/stock/operations">入庫/調整</Link>
            <Link className="btn" to="/bundles/new">
              新增組合商品
            </Link>
            <Link className="btn primary" to="/products/new">
              新增商品
            </Link>
          </div>
        )}
      </div>

      <div className="tiles" style={japanEnabled ? undefined : { gridTemplateColumns: "repeat(3, minmax(0, 1fr))" }}>
        {japanEnabled && tile("JP_MAIN", "日本倉")}
        {tile("IN_TRANSIT", "運送中（不可銷售）")}
        {tile("US_MAIN", "美國倉")}
        <div className="tile">
          <div className="label">美國可銷售</div>
          <div className="value">{fmtInt(loc("US_MAIN")?.available_units)}</div>
          <div className="sub">已預留 {fmtInt(loc("US_MAIN")?.reserved_units)}</div>
        </div>
      </div>

      <div className="card">
        <div className="row" style={{ marginBottom: 12 }}>
          <input style={{ flex: "2 1 220px" }} placeholder="搜尋 SKU、名稱、條碼…" value={search} onChange={(e) => setSearch(e.target.value)} aria-label="搜尋" />
          <select style={{ flex: "1 1 140px" }} value={category} onChange={(e) => setCategory(e.target.value)} aria-label="分類">
            <option value="">全部分類</option>
            {cats.data?.map((c) => (
              <option key={c.id} value={c.id}>
                {c.name}
              </option>
            ))}
          </select>
          <select style={{ flex: "1 1 120px" }} value={status} onChange={(e) => setStatus(e.target.value)} aria-label="狀態">
            <option value="active">販售中</option>
            <option value="discontinued">已停售</option>
          </select>
        </div>
        <ErrorBox error={skus.error} />
        {skus.isLoading ? (
          <Loading />
        ) : !skus.data?.length ? (
          <div className="empty">{search ? "找不到符合的商品" : "還沒有商品。先新增商品，再登記期初庫存。"}</div>
        ) : (
          <div className="table-wrap">
            <table className="data responsive compact">
              <thead>
                <tr>
                  <th>商品</th>
                  {japanEnabled && <th className="num">日本倉</th>}
                  <th className="num">運送中</th>
                  <th className="num">美國倉</th>
                  <th className="num">已預留</th>
                  <th className="num">可銷售</th>
                  {showCost && <th className="num">庫存成本（含運送中）</th>}
                  <th>最近入庫</th>
                </tr>
              </thead>
              <tbody>
                {skus.data.map((s) => (
                  <tr key={s.id} className="click" onClick={() => nav(`/skus/${s.id}`)}>
                    <td className="lead">
                      <div className="sku-cell">
                        {s.image_url ? <img className="thumb" src={s.image_url} alt="" loading="lazy" /> : <div className="thumb" />}
                        <div style={{ minWidth: 0 }}>
                          <div className="mono">{s.sku_code}</div>
                          <div className="small" style={{ overflow: "hidden", textOverflow: "ellipsis" }}>
                            {s.name}
                            {s.variant_name ? ` · ${s.variant_name}` : ""} {s.is_bundle && <span className="badge accent">組合</span>}
                          </div>
                        </div>
                      </div>
                    </td>
                    {japanEnabled && <td className="num" data-label="日本倉"><Qty n={s.jp_on_hand} /></td>}
                    <td className="num" data-label="運送中"><Qty n={s.in_transit} kind="transit" /></td>
                    <td className="num" data-label="美國倉"><Qty n={s.us_on_hand} /></td>
                    <td className="num" data-label="已預留"><Qty n={s.us_reserved} /></td>
                    <td className="num" data-label="可銷售" style={{ fontWeight: 700 }}><Qty n={s.us_available} /></td>
                    {showCost && <td className="num wide" data-label="庫存成本">{s.is_bundle ? "—" : fmtUsd(s.inventory_value_usd)}</td>}
                    <td data-label="最近入庫" className="small muted wide">{fmtDate(s.last_received_at, me?.timezone)}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        )}
        <p className="small muted" style={{ marginBottom: 0 }}>
          可銷售 = 美國倉 − 已預留。運送中的商品在美國簽收前不計入可銷售。組合商品顯示的是組成 SKU 可湊成的完整組數。
        </p>
      </div>
    </>
  );
}
