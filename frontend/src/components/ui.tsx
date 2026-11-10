import { useEffect, useMemo, useState, type ReactNode } from "react";
import { useQuery } from "@tanstack/react-query";
import { api, errorText } from "../api";
import type { SkuRow } from "../types";

// ---------- formatting ----------
export const fmtInt = (n: number | null | undefined) => (n ?? 0).toLocaleString("en-US");
export const fmtUsd = (v: string | number | null | undefined) =>
  v === null || v === undefined ? "—" : "$" + Number(v).toLocaleString("en-US", { minimumFractionDigits: 2, maximumFractionDigits: 2 });
export const fmtDateTime = (iso: string | null | undefined, tz?: string) =>
  iso
    ? new Date(iso).toLocaleString("zh-TW", { timeZone: tz, year: "numeric", month: "2-digit", day: "2-digit", hour: "2-digit", minute: "2-digit", hour12: false })
    : "—";
export const fmtDate = (iso: string | null | undefined, tz?: string) =>
  iso ? new Date(iso).toLocaleDateString("zh-TW", { timeZone: tz, year: "numeric", month: "2-digit", day: "2-digit" }) : "—";

export function Qty({ n, kind }: { n: number; kind?: "transit" }) {
  return <span className={n === 0 ? "qty-zero" : kind === "transit" ? "qty-transit" : undefined}>{fmtInt(n)}</span>;
}

export function ErrorBox({ error }: { error: unknown }) {
  if (!error) return null;
  return <div className="alert error" role="alert">{errorText(error)}</div>;
}

export function Loading() {
  return <div className="empty">載入中…</div>;
}

// ---------- icons (inline, no icon font) ----------
const paths: Record<string, string> = {
  box: "M21 8l-9-5-9 5v8l9 5 9-5V8zM3 8l9 5 9-5M12 13v8",
  tag: "M20.6 13.4L13.4 20.6a2 2 0 01-2.8 0L3 13V3h10l7.6 7.6a2 2 0 010 2.8zM7.5 7.5h.01",
  clipboard: "M9 4h6v3H9zM8 5H6a1 1 0 00-1 1v14a1 1 0 001 1h12a1 1 0 001-1V6a1 1 0 00-1-1h-2M9 12h6M9 16h4",
  sliders: "M4 21v-7M4 10V3M12 21v-9M12 8V3M20 21v-5M20 12V3M1 14h6M9 8h6M17 16h6",
  users: "M17 21v-2a4 4 0 00-4-4H5a4 4 0 00-4 4v2M9 11a4 4 0 100-8 4 4 0 000 8zM23 21v-2a4 4 0 00-3-3.9M16 3.1a4 4 0 010 7.8",
  user: "M20 21v-2a4 4 0 00-4-4H8a4 4 0 00-4 4v2M12 11a4 4 0 100-8 4 4 0 000 8z",
  truck: "M1 4h14v12H1zM15 9h4l3 3v4h-7M5.5 19.5a1.5 1.5 0 100-3 1.5 1.5 0 000 3zM18.5 19.5a1.5 1.5 0 100-3 1.5 1.5 0 000 3z",
  receipt: "M4 2h16v20l-3-2-3 2-3-2-3 2-3-2-1 .7V2zM8 7h8M8 11h8M8 15h5",
  link: "M10 13a5 5 0 007.5.5l3-3a5 5 0 00-7-7l-1.7 1.7M14 11a5 5 0 00-7.5-.5l-3 3a5 5 0 007 7l1.7-1.7",
  chart: "M3 3v18h18M7 15l4-4 3 3 6-6M20 8v4h-4",
  wallet: "M3 7a2 2 0 012-2h13v4M3 7v11a2 2 0 002 2h15V9H5a2 2 0 01-2-2zM16 14h.01",
  trend: "M23 6l-9.5 9.5-5-5L1 18M17 6h6v6",
  cart: "M3 3h2l2.4 12.2a2 2 0 002 1.8h8.2a2 2 0 002-1.6L21 8H6M10 21h.01M18 21h.01",
  refill: "M21 12a9 9 0 11-3-6.7M21 4v5h-5M12 8v4l3 2",
  upload: "M21 15v4a2 2 0 01-2 2H5a2 2 0 01-2-2v-4M17 8l-5-5-5 5M12 3v12",
};
export function Icon({ name }: { name: keyof typeof paths | string }) {
  return (
    <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.8" strokeLinecap="round" strokeLinejoin="round" aria-hidden="true">
      <path d={paths[name]} />
    </svg>
  );
}

// ---------- business settings ----------
export function useBusinessSettings() {
  const q = useQuery({
    queryKey: ["business-settings"],
    queryFn: () => api<{ japan_warehouse_enabled: boolean; lead_time_days: number }>("/settings/business"),
    staleTime: 5 * 60_000,
  });
  return { japanEnabled: q.data?.japan_warehouse_enabled ?? false, leadTime: q.data?.lead_time_days ?? 9, loaded: !!q.data };
}

// ---------- SKU picker ----------
export function useSkus(params: Record<string, any> = {}) {
  return useQuery({ queryKey: ["skus", params], queryFn: () => api<SkuRow[]>("/skus", { params }) });
}

export function SkuPicker({
  value,
  onChange,
  excludeBundles = true,
  excludeIds = [],
}: {
  value: number | null;
  onChange: (id: number | null, sku?: SkuRow) => void;
  excludeBundles?: boolean;
  excludeIds?: number[];
}) {
  const { data } = useSkus({ include_bundles: !excludeBundles });
  const options = useMemo(() => (data ?? []).filter((s) => !excludeIds.includes(s.id) || s.id === value), [data, excludeIds, value]);
  return (
    <select
      value={value ?? ""}
      onChange={(e) => {
        const id = e.target.value ? Number(e.target.value) : null;
        onChange(id, options.find((s) => s.id === id));
      }}
    >
      <option value="">選擇 SKU…</option>
      {options.map((s) => (
        <option key={s.id} value={s.id}>
          {s.sku_code} — {s.name}
          {s.variant_name ? ` (${s.variant_name})` : ""}
        </option>
      ))}
    </select>
  );
}

export function Field({ label, children, hint }: { label: string; children: ReactNode; hint?: string }) {
  return (
    <label className="field">
      <span>{label}</span>
      {children}
      {hint && <small className="muted">{hint}</small>}
    </label>
  );
}

export function useDebounced<T>(value: T, ms = 250): T {
  const [v, setV] = useState(value);
  useEffect(() => {
    const id = setTimeout(() => setV(value), ms);
    return () => clearTimeout(id);
  }, [value, ms]);
  return v;
}
