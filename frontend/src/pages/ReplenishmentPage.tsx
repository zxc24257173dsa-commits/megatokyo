import { Fragment, useState } from "react";
import { Link } from "react-router-dom";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { api, downloadFile } from "../api";
import { useAuth } from "../auth";
import { ErrorBox, Loading, fmtDate, fmtDateTime, fmtInt } from "../components/ui";
import { t } from "../i18n";

interface Settings { supplier_lead_time_days: number; transit_days: number; review_period_days: number; service_level_z: number; weights: number[]; min_history_days: number }
interface Suggestion {
  id: number; sku_id: number; sku_code: string; name: string; action: "buy" | "open_box" | "none"; status_color: "red" | "orange" | "yellow" | "green";
  v7: number; v14: number; v30: number; v_blended: number; sigma_daily: number; horizon_days: number; forecast_demand: number; safety_stock: number;
  us_available: number; committed_demand: number; in_transit_in_time: number; in_transit_late: number; on_order: number;
  days_to_stockout: number | null; stockout_date: string | null; latest_order_date: string | null;
  suggested_purchase_qty: number; override_purchase_qty: number | null; override_reason: string | null; final_qty: number;
  boxes_to_open: number; box_sku_code: string | null; confirmed_at: string | null; confirmed_by_name: string | null;
  explanation: { notes: (string | { key: string; args: Record<string, string | number> })[]; target: number; position: number; lead_days: number; review_days: number; v_direct?: number; box_open_rate?: number };
}
interface Run { run: { id: number; data_as_of: string; created_at: string; note: string | null; params: Settings } | null; suggestions: Suggestion[] }

const COLOR: Record<string, [string, string]> = {
  red: [t("立刻處理"), "danger"], orange: [t("3 天內"), "warn"], yellow: [t("本週內"), "info"], green: [t("充足"), "ok"],
};
const Z_LABEL: Record<string, string> = { "1.28": "90%", "1.65": "95%", "2.05": "98%", "2.33": "99%" };
const n1 = (v: number) => (Math.round(v * 100) / 100).toLocaleString("en-US");

export function ReplenishmentPage() {
  const { can } = useAuth();
  const qc = useQueryClient();
  const q = useQuery({ queryKey: ["replenishment"], queryFn: () => api<Run>("/replenishment/runs/latest") });
  const settings = useQuery({ queryKey: ["replenishment-settings"], queryFn: () => api<Settings>("/replenishment/settings") });
  const [open, setOpen] = useState<number | null>(null);
  const [picked, setPicked] = useState<Set<number>>(new Set());
  const [showSettings, setShowSettings] = useState(false);
  const [onlyAction, setOnlyAction] = useState(true);
  const [dlErr, setDlErr] = useState<unknown>(null);
  const run = useMutation({
    mutationFn: () => api<Run>("/replenishment/runs", { body: {} }),
    onSuccess: (d) => { qc.setQueryData(["replenishment"], d); setPicked(new Set()); },
  });
  const confirm = useMutation({
    mutationFn: () => api(`/replenishment/runs/${q.data!.run!.id}/confirm`, { body: { suggestion_ids: [...picked] } }),
    onSuccess: () => { setPicked(new Set()); void qc.invalidateQueries({ queryKey: ["replenishment"] }); },
  });
  const d = q.data;
  const rows = (d?.suggestions ?? []).filter((s) => !onlyAction || s.action !== "none" || s.status_color !== "green");
  const count = (c: string) => d?.suggestions.filter((s) => s.status_color === c).length ?? 0;
  const toBuy = d?.suggestions.filter((s) => s.final_qty > 0) ?? [];
  const stale = d?.run && d.run.data_as_of < new Date(Date.now() - 2 * 86400_000).toISOString().slice(0, 10);
  const p = d?.run?.params ?? settings.data;

  return (
    <>
      <div className="page-head">
        <h1>{t("補貨建議")}</h1>
        <div className="row">
          {d?.run && <button className="btn" onClick={() => { setDlErr(null); downloadFile(`/replenishment/runs/${d.run!.id}/purchase-list.csv`, {}, `megatokyo-purchase-list-${d.run!.data_as_of}.csv`).catch(setDlErr); }}>{t("匯出採購清單")}</button>}
          {can("replenishment.edit") && <button className="btn primary" disabled={run.isPending} onClick={() => run.mutate()}>{run.isPending ? t("計算中…") : t("產生最新建議")}</button>}
        </div>
      </div>
      <div className="alert info small">
        {t("這裡只提供建議，")}<b>{t("系統不會自動下單")}</b>{t("。依最近的實際需求、需求波動、交期和現有庫存計算；運送中和已買未寄的貨會算進「快到的庫存」，但")}<b>{t("不算美國可售")}</b>{t("。")}
      </div>
      <ErrorBox error={q.error ?? run.error ?? confirm.error ?? dlErr} />
      {p && (
        <p className="small muted">
          {t("交期")} {p.supplier_lead_time_days} {t("天（下單到美國可賣）・每次涵蓋之後")} {p.review_period_days} {t("天・安全庫存服務水準")} {Z_LABEL[String(p.service_level_z)] ?? `z = ${p.service_level_z}`}{t("・ 需求權重 7 天")} {p.weights[0] * 100}{t("% / 14 天")} {p.weights[1] * 100}{t("% / 30 天")} {p.weights[2] * 100}%
          {can("replenishment.edit") && <> ・<button className="linklike" onClick={() => setShowSettings(!showSettings)}>{showSettings ? t("收起設定") : t("修改設定")}</button></>}
        </p>
      )}
      {showSettings && settings.data && <SettingsForm s={settings.data} onDone={() => setShowSettings(false)} />}
      {q.isLoading ? <Loading /> : !d?.run ? (
        <div className="card empty">{t("還沒有產生過建議。")}{can("replenishment.edit") ? t("按「產生最新建議」。") : t("請有補貨權限的人產生。")}</div>
      ) : (
        <>
          <div className="tiles">
            <div className="tile"><div className="label">{t("立刻處理")}</div><div className="value" style={{ color: count("red") ? "var(--danger)" : undefined }}>{fmtInt(count("red"))}</div><div className="sub">{t("已經到了最晚下單日")}</div></div>
            <div className="tile"><div className="label">{t("3 天內")}</div><div className="value">{fmtInt(count("orange"))}</div><div className="sub">{t("要準備下單")}</div></div>
            <div className="tile"><div className="label">{t("本週內")}</div><div className="value">{fmtInt(count("yellow"))}</div><div className="sub">{t("注意")}</div></div>
            <div className="tile"><div className="label">{t("建議採購")}</div><div className="value">{fmtInt(toBuy.reduce((a, s) => a + s.final_qty, 0))} {t("件")}</div><div className="sub">{toBuy.length} {t("個 SKU")}</div></div>
          </div>
          <div className="card">
            <div className="row" style={{ marginBottom: 10, alignItems: "center" }}>
              <span className="small muted" style={{ flex: 1 }}>{t("資料到")} {fmtDate(d.run.data_as_of)}{t("（最後一個完整營業日），產生於")} {fmtDateTime(d.run.created_at)}{stale && <b style={{ color: "var(--danger)" }}>{t("・已經舊了，請重新產生")}</b>}</span>
              <label className="row small"><input type="checkbox" checked={onlyAction} onChange={(e) => setOnlyAction(e.target.checked)} />{t("只看需要處理的")}</label>
              {can("replenishment.edit") && picked.size > 0 && <button className="btn sm primary" disabled={confirm.isPending} onClick={() => confirm.mutate()}>{t("標記")} {picked.size} {t("項為已決定採購")}</button>}
            </div>
            {!rows.length ? <div className="empty">{t("所有 SKU 庫存都充足")}</div> : (
              <div className="table-wrap">
                <table className="data responsive">
                  <thead>
                    <tr>{can("replenishment.edit") && <th></th>}<th>SKU</th><th>{t("狀態")}</th><th>{t("建議")}</th><th className="num">{t("每日需求")}</th><th className="num">{t("美國可售")}</th><th className="num">{t("運送中")}</th><th className="num">{t("已買未寄")}</th><th>{t("最晚下單")}</th><th></th></tr>
                  </thead>
                  <tbody>
                    {rows.map((s) => (
                      <Fragment key={s.id}>
                        <tr>
                          {can("replenishment.edit") && (
                            <td>{s.final_qty > 0 && !s.confirmed_at && <input type="checkbox" aria-label={t("選取 {0}", [s.sku_code])} checked={picked.has(s.id)} onChange={(e) => { const n = new Set(picked); if (e.target.checked) n.add(s.id); else n.delete(s.id); setPicked(n); }} />}</td>
                          )}
                          <td className="lead"><Link to={`/skus/${s.sku_id}`} className="mono">{s.sku_code}</Link><div className="small muted" style={{ fontWeight: 400 }}>{s.name}</div></td>
                          <td data-label={t("狀態")}><span className={`badge ${COLOR[s.status_color][1]}`}>{COLOR[s.status_color][0]}</span></td>
                          <td data-label={t("建議")}>
                            {s.action === "buy" || s.final_qty > 0 ? (
                              <QtyCell s={s} editable={can("replenishment.edit")} />
                            ) : s.action === "open_box" ? (
                              <span>{t("拆")} <b>{fmtInt(s.boxes_to_open)}</b> {t("盒")} <span className="mono small">{s.box_sku_code}</span></span>
                            ) : <span className="muted">{t("不用補")}</span>}
                            {s.confirmed_at && <div className="small"><span className="badge ok">{t("已決定")}</span> {s.confirmed_by_name}</div>}
                          </td>
                          <td data-label={t("每日需求")} className="num">{n1(s.v_blended)}</td>
                          <td data-label={t("美國可售")} className="num">{fmtInt(s.us_available)}{s.days_to_stockout !== null && <div className="small muted">{t("約")} {Math.floor(s.days_to_stockout)} {t("天")}</div>}</td>
                          <td data-label={t("運送中")} className="num">{fmtInt(s.in_transit_in_time + s.in_transit_late) || "—"}</td>
                          <td data-label={t("已買未寄")} className="num">{fmtInt(s.on_order) || "—"}</td>
                          <td data-label={t("最晚下單")} className="small">{s.latest_order_date ? fmtDate(s.latest_order_date) : "—"}</td>
                          <td><button className="btn sm" onClick={() => setOpen(open === s.id ? null : s.id)}>{open === s.id ? t("收起") : t("為什麼")}</button></td>
                        </tr>
                        {open === s.id && (
                          <tr><td colSpan={10}><Why s={s} /></td></tr>
                        )}
                      </Fragment>
                    ))}
                  </tbody>
                </table>
              </div>
            )}
          </div>
        </>
      )}
    </>
  );
}

function Why({ s }: { s: Suggestion }) {
  const e = s.explanation;
  const transit = s.in_transit_in_time + s.in_transit_late;
  return (
    <div className="small" style={{ background: "var(--surface-2)", borderRadius: 8, padding: "10px 12px" }}>
      <div>{t("每日需求 = 0.5 × 最近 7 天 {0} + 0.3 × 14 天 {1} + 0.2 × 30 天 {2}", [n1(s.v7), n1(s.v14), n1(s.v30)])}
        {e.box_open_rate ? t(" = 直接賣出 {0} ＋ 拆盒給盒內商品 {1}", [n1(e.v_direct ?? 0), n1(e.box_open_rate)]) : ""}
        {" ≈ "}<b>{n1(s.v_blended)}</b>{t("（依權重設定；組合商品已拆成組成 SKU）")}</div>
      <div>{t("需求波動（每日標準差）σ = {0} → 安全庫存 = z × σ × √{1} 天 =", [n1(s.sigma_daily), e.lead_days])} <b>{fmtInt(s.safety_stock)}</b></div>
      <div>{t("目標庫存 = {0} × ({1} 天交期 + {2} 天) + {3} =", [n1(s.v_blended), e.lead_days, e.review_days, fmtInt(s.safety_stock)])} <b>{n1(e.target)}</b></div>
      <div>{t("現有與快到的庫存 = 美國可售 {0} + 運送中 {1} + 已買未寄 {2} =", [fmtInt(s.us_available), fmtInt(transit), fmtInt(s.on_order)])} <b>{fmtInt(e.position)}</b>
        {t("（已扣掉已付款訂單預留的 {0} 件）", [fmtInt(s.committed_demand)])}</div>
      <div>{t("建議 = 目標 − 現有 = {0} →", [n1(Math.max(0, e.target - e.position))])} {s.action === "open_box" ? t("拆 {0} 盒", [s.boxes_to_open]) : t("採購 {0} 件（依整箱數、最小數量進位）", [fmtInt(s.suggested_purchase_qty)])}</div>
      {s.override_reason && <div>{t("手動改為 {0} 件：{1}", [fmtInt(s.override_purchase_qty ?? 0), s.override_reason])}</div>}
      {e.notes.map((n, i) => <div key={i} style={{ color: "var(--warn)" }}>・{typeof n === "string" ? n : t(n.key, n.args)}</div>)}
    </div>
  );
}

function QtyCell({ s, editable }: { s: Suggestion; editable: boolean }) {
  const qc = useQueryClient();
  const [edit, setEdit] = useState(false);
  const [qty, setQty] = useState(String(s.final_qty));
  const [reason, setReason] = useState(s.override_reason ?? "");
  const m = useMutation({
    mutationFn: () => api(`/replenishment/suggestions/${s.id}`, { method: "PATCH", body: { qty: Number(qty), reason } }),
    onSuccess: () => { setEdit(false); void qc.invalidateQueries({ queryKey: ["replenishment"] }); },
  });
  if (!edit) {
    return (
      <span>
        {t("買")} <b>{fmtInt(s.final_qty)}</b> {t("件")}
        {s.override_purchase_qty !== null && <span className="small muted">{t("（建議")} {fmtInt(s.suggested_purchase_qty)}{t("）")}</span>}
        {editable && !s.confirmed_at && <> <button className="linklike small" onClick={() => setEdit(true)}>{t("改")}</button></>}
      </span>
    );
  }
  return (
    <div className="row" style={{ gap: 6, flexWrap: "wrap" }}>
      <ErrorBox error={m.error} />
      <input aria-label={t("{0} 採購數量", [s.sku_code])} inputMode="numeric" value={qty} onChange={(e) => setQty(e.target.value)} style={{ width: 70 }} />
      <input aria-label={t("{0} 修改原因", [s.sku_code])} placeholder={t("原因（必填）")} value={reason} onChange={(e) => setReason(e.target.value)} style={{ flex: "1 1 120px" }} />
      <button className="btn sm primary" disabled={m.isPending || reason.trim().length < 2} onClick={() => m.mutate()}>{t("儲存")}</button>
      <button className="btn sm" onClick={() => setEdit(false)}>{t("取消")}</button>
    </div>
  );
}

function SettingsForm({ s, onDone }: { s: Settings; onDone: () => void }) {
  const qc = useQueryClient();
  const [f, setF] = useState({ lead: String(s.supplier_lead_time_days), review: String(s.review_period_days), z: String(s.service_level_z) });
  const m = useMutation({
    mutationFn: () => api("/replenishment/settings", { method: "PUT", body: { supplier_lead_time_days: Number(f.lead), review_period_days: Number(f.review), service_level_z: f.z } }),
    onSuccess: () => { void qc.invalidateQueries({ queryKey: ["replenishment-settings"] }); onDone(); },
  });
  return (
    <form className="card" onSubmit={(e) => (e.preventDefault(), m.mutate())}>
      <ErrorBox error={m.error} />
      <div className="grid-2">
        <label className="field"><span>{t("交期（天）：在日本下單到美國可以賣")}</span><input inputMode="numeric" value={f.lead} onChange={(e) => setF({ ...f, lead: e.target.value })} /></label>
        <label className="field"><span>{t("每次採購涵蓋之後幾天")}</span><input inputMode="numeric" value={f.review} onChange={(e) => setF({ ...f, review: e.target.value })} /></label>
        <label className="field"><span>{t("安全庫存服務水準")}</span>
          <select value={f.z} onChange={(e) => setF({ ...f, z: e.target.value })}>
            {Object.entries(Z_LABEL).map(([z, l]) => <option key={z} value={z}>{l}{t("（缺貨機率約")} {100 - Number(l.replace("%", ""))}{t("%）")}</option>)}
          </select></label>
      </div>
      <div className="row"><button className="btn primary" disabled={m.isPending}>{t("儲存")}</button><span className="small muted">{t("儲存後按「產生最新建議」重新計算。")}</span></div>
    </form>
  );
}
