import { useState, type FormEvent } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { api } from "../api";
import { useAuth } from "../auth";
import { ErrorBox, Field, Loading, fmtDateTime } from "../components/ui";
import { ROLE_LABEL, type UserRow } from "../types";
import { MfaSetupForm, PasswordChangeForm } from "./AuthPages";

const ROLES = Object.keys(ROLE_LABEL);
const TIMEZONES = [
  ["Asia/Tokyo", "東京"],
  ["America/New_York", "美東"],
  ["America/Los_Angeles", "美西"],
  ["Asia/Taipei", "台北"],
];

export function UsersPage() {
  const { me } = useAuth();
  const qc = useQueryClient();
  const [showInactive, setShowInactive] = useState(false);
  const users = useQuery({ queryKey: ["users", showInactive], queryFn: () => api<UserRow[]>("/users", { params: { include_inactive: showInactive } }) });
  const [error, setError] = useState<unknown>(null);
  const [notice, setNotice] = useState<string | null>(null);
  const refresh = () => qc.invalidateQueries({ queryKey: ["users"] });

  async function act(fn: () => Promise<unknown>, msg: string) {
    setError(null);
    setNotice(null);
    try {
      await fn();
      setNotice(msg);
      await refresh();
    } catch (e) {
      setError(e);
    }
  }

  return (
    <>
      <div className="page-head">
        <h1>使用者</h1>
        <label className="row small"><input type="checkbox" checked={showInactive} onChange={(e) => setShowInactive(e.target.checked)} /> 顯示已停用</label>
      </div>
      <ErrorBox error={error} />
      {notice && <div className="alert ok">{notice}</div>}
      <NewUserForm onCreated={(u) => (setNotice(`已建立 ${u.email}，請把臨時密碼交給對方；首次登入會要求更改。`), void refresh())} />
      <div className="card">
        {users.isLoading ? <Loading /> : (
          <table className="data responsive">
            <thead><tr><th>使用者</th><th>角色</th><th>狀態</th><th>最近登入</th><th></th></tr></thead>
            <tbody>
              {users.data?.map((u) => (
                <tr key={u.id}>
                  <td className="lead"><div>{u.display_name}</div><div className="small muted">{u.email}</div></td>
                  <td data-label="角色">
                    <select
                      aria-label="角色"
                      value={u.roles[0] ?? ""}
                      disabled={u.id === me?.id}
                      onChange={(e) => act(() => api(`/users/${u.id}`, { method: "PATCH", body: { roles: [e.target.value] } }), "角色已更新，對方需重新登入")}
                    >
                      {ROLES.map((r) => <option key={r} value={r}>{ROLE_LABEL[r]}</option>)}
                    </select>
                  </td>
                  <td data-label="狀態">
                    <div className="row" style={{ gap: 4 }}>
                      {!u.is_active && <span className="badge">已停用</span>}
                      {u.locked && <span className="badge danger">鎖定中</span>}
                      {u.must_change_password && <span className="badge warn">待改密碼</span>}
                      {u.mfa_enabled ? <span className="badge ok">雙因素</span> : <span className="badge">無雙因素</span>}
                    </div>
                  </td>
                  <td data-label="最近登入" className="small">{fmtDateTime(u.last_login_at, me?.timezone)}</td>
                  <td data-label="操作">
                    {u.id !== me?.id && (
                      <div className="row" style={{ gap: 4, justifyContent: "flex-end" }}>
                        {u.locked && <button className="btn sm" onClick={() => act(() => api(`/users/${u.id}/unlock`, { method: "POST" }), "已解除鎖定")}>解鎖</button>}
                        <button className="btn sm" onClick={() => {
                          const pw = prompt("輸入新的臨時密碼（至少 12 字元）");
                          if (pw) void act(() => api(`/users/${u.id}/reset-password`, { body: { temporary_password: pw } }), "已重設密碼，對方所有裝置已登出");
                        }}>重設密碼</button>
                        {u.mfa_enabled && <button className="btn sm" onClick={() => confirm("重設對方的雙因素驗證（手機遺失時使用）？") && act(() => api(`/users/${u.id}/reset-mfa`, { method: "POST" }), "已重設雙因素")}>重設雙因素</button>}
                        <button className={`btn sm ${u.is_active ? "danger" : ""}`} onClick={() => act(() => api(`/users/${u.id}`, { method: "PATCH", body: { is_active: !u.is_active } }), u.is_active ? "已停用，對方立即登出" : "已重新啟用")}>
                          {u.is_active ? "停用" : "啟用"}
                        </button>
                      </div>
                    )}
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        )}
      </div>
    </>
  );
}

function NewUserForm({ onCreated }: { onCreated: (u: UserRow) => void }) {
  const [open, setOpen] = useState(false);
  const [f, setF] = useState({ email: "", display_name: "", temporary_password: "", role: "jp_inventory", timezone: "Asia/Tokyo", locale: "zh-TW" });
  const m = useMutation({
    mutationFn: () => api<UserRow>("/users", { body: { email: f.email, display_name: f.display_name, temporary_password: f.temporary_password, roles: [f.role], timezone: f.timezone, locale: f.locale } }),
    onSuccess: (u) => {
      setOpen(false);
      setF({ ...f, email: "", display_name: "", temporary_password: "" });
      onCreated(u);
    },
  });
  if (!open) return <div style={{ marginBottom: 16 }}><button className="btn primary" onClick={() => setOpen(true)}>＋ 新增使用者</button></div>;
  const set = (k: keyof typeof f) => (e: React.ChangeEvent<HTMLInputElement | HTMLSelectElement>) => setF({ ...f, [k]: e.target.value });
  return (
    <form className="card" onSubmit={(e: FormEvent) => (e.preventDefault(), m.mutate())} style={{ marginBottom: 16 }}>
      <h2>新增使用者</h2>
      <ErrorBox error={m.error} />
      <div className="inline-inputs">
        <Field label="Email"><input type="email" required value={f.email} onChange={set("email")} /></Field>
        <Field label="顯示名稱"><input required value={f.display_name} onChange={set("display_name")} /></Field>
        <Field label="臨時密碼" hint="至少 12 字元；對方首次登入須更改"><input required minLength={12} value={f.temporary_password} onChange={set("temporary_password")} /></Field>
        <Field label="角色"><select value={f.role} onChange={set("role")}>{ROLES.map((r) => <option key={r} value={r}>{ROLE_LABEL[r]}</option>)}</select></Field>
        <Field label="時區"><select value={f.timezone} onChange={set("timezone")}>{TIMEZONES.map(([v, l]) => <option key={v} value={v}>{l}</option>)}</select></Field>
        <Field label="語言"><select value={f.locale} onChange={set("locale")}><option value="zh-TW">繁體中文</option><option value="ja">日本語</option><option value="en">English</option></select></Field>
      </div>
      <div className="row">
        <button className="btn primary" disabled={m.isPending}>建立</button>
        <button type="button" className="btn" onClick={() => setOpen(false)}>取消</button>
      </div>
    </form>
  );
}

export function AccountPage() {
  const { me, logout } = useAuth();
  const [mfaDone, setMfaDone] = useState(false);
  if (!me) return null;
  return (
    <>
      <div className="page-head">
        <h1>我的帳號</h1>
        <button className="btn" onClick={() => void logout()}>登出</button>
      </div>
      <div className="grid-2">
        <div className="card">
          <h2>基本資料</h2>
          <dl className="kv">
            <dt>名稱</dt><dd>{me.display_name}</dd>
            <dt>Email</dt><dd>{me.email}</dd>
            <dt>角色</dt><dd>{me.roles.map((r) => ROLE_LABEL[r] ?? r).join("、")}</dd>
            <dt>時區</dt><dd>{me.timezone}</dd>
            <dt>雙因素</dt><dd>{me.mfa_enabled ? <span className="badge ok">已啟用</span> : <span className="badge">未啟用</span>}</dd>
          </dl>
          {!me.mfa_enabled && (
            <div style={{ marginTop: 16 }}>
              <h2>啟用雙因素驗證（建議）</h2>
              {mfaDone ? <div className="alert ok">已啟用</div> : <MfaSetupForm onDone={() => setMfaDone(true)} />}
            </div>
          )}
        </div>
        <div className="card">
          <h2>變更密碼</h2>
          <PasswordChangeForm />
        </div>
      </div>
    </>
  );
}
