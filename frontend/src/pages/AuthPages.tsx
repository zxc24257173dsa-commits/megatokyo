import { useEffect, useState, type FormEvent } from "react";
import QRCode from "qrcode";
import { api, type TokenResponse } from "../api";
import { useAuth } from "../auth";
import { ErrorBox, Field } from "../components/ui";

function AuthCard({ title, children }: { title: string; children: React.ReactNode }) {
  return (
    <div className="center-page">
      <div className="card auth-card">
        <div className="brand">
          Megatokyo <span>ERP</span>
        </div>
        <h2 style={{ marginBottom: 16 }}>{title}</h2>
        {children}
      </div>
    </div>
  );
}

export function LoginPage() {
  const { acceptToken } = useAuth();
  const [email, setEmail] = useState("");
  const [password, setPassword] = useState("");
  const [mfaToken, setMfaToken] = useState<string | null>(null);
  const [code, setCode] = useState("");
  const [error, setError] = useState<unknown>(null);
  const [busy, setBusy] = useState(false);

  async function submit(e: FormEvent) {
    e.preventDefault();
    setBusy(true);
    setError(null);
    try {
      if (!mfaToken) {
        const r = await api<{ mfa_required: boolean; mfa_token: string | null; token: TokenResponse | null }>("/auth/login", {
          body: { email, password },
        });
        if (r.mfa_required && r.mfa_token) setMfaToken(r.mfa_token);
        else if (r.token) await acceptToken(r.token);
      } else {
        await acceptToken(await api<TokenResponse>("/auth/mfa/verify", { body: { mfa_token: mfaToken, code } }));
      }
    } catch (err) {
      setError(err);
      if (mfaToken) setCode("");
    } finally {
      setBusy(false);
    }
  }

  return (
    <AuthCard title={mfaToken ? "輸入驗證碼" : "登入"}>
      <form onSubmit={submit}>
        <ErrorBox error={error} />
        {!mfaToken ? (
          <>
            <Field label="Email">
              <input type="email" autoComplete="username" required value={email} onChange={(e) => setEmail(e.target.value)} autoFocus />
            </Field>
            <Field label="密碼">
              <input type="password" autoComplete="current-password" required value={password} onChange={(e) => setPassword(e.target.value)} />
            </Field>
          </>
        ) : (
          <Field label="驗證器 App 上的 6 位數字">
            <input
              inputMode="numeric"
              autoComplete="one-time-code"
              pattern="[0-9]{6}"
              maxLength={6}
              required
              value={code}
              onChange={(e) => setCode(e.target.value.replace(/\D/g, ""))}
              autoFocus
            />
          </Field>
        )}
        <button className="btn primary" style={{ width: "100%", justifyContent: "center" }} disabled={busy}>
          {busy ? "處理中…" : mfaToken ? "驗證" : "登入"}
        </button>
        {mfaToken && (
          <button type="button" className="btn" style={{ width: "100%", justifyContent: "center", marginTop: 8 }} onClick={() => setMfaToken(null)}>
            返回
          </button>
        )}
      </form>
    </AuthCard>
  );
}

export function PasswordChangeForm({ onDone, forced }: { onDone?: () => void; forced?: boolean }) {
  const { acceptToken } = useAuth();
  const [current, setCurrent] = useState("");
  const [next, setNext] = useState("");
  const [confirm, setConfirm] = useState("");
  const [error, setError] = useState<unknown>(null);
  const [mismatch, setMismatch] = useState(false);
  const [busy, setBusy] = useState(false);
  const [done, setDone] = useState(false);

  async function submit(e: FormEvent) {
    e.preventDefault();
    setMismatch(next !== confirm);
    if (next !== confirm) return;
    setBusy(true);
    setError(null);
    try {
      await acceptToken(await api<TokenResponse>("/auth/password", { body: { current_password: current, new_password: next } }));
      setDone(true);
      setCurrent("");
      setNext("");
      setConfirm("");
      onDone?.();
    } catch (err) {
      setError(err);
    } finally {
      setBusy(false);
    }
  }
  return (
    <form onSubmit={submit}>
      {forced && <div className="alert info">這是管理員設定的臨時密碼，請先設定你自己的密碼。</div>}
      {done && <div className="alert ok">密碼已更新，其他裝置已登出。</div>}
      <ErrorBox error={error} />
      {mismatch && <div className="alert error">兩次輸入的新密碼不一致</div>}
      <Field label="目前密碼">
        <input type="password" autoComplete="current-password" required value={current} onChange={(e) => setCurrent(e.target.value)} />
      </Field>
      <Field label="新密碼" hint="至少 12 個字元，不可包含 Email 名稱">
        <input type="password" autoComplete="new-password" required minLength={12} value={next} onChange={(e) => setNext(e.target.value)} />
      </Field>
      <Field label="再輸入一次新密碼">
        <input type="password" autoComplete="new-password" required value={confirm} onChange={(e) => setConfirm(e.target.value)} />
      </Field>
      <button className="btn primary" disabled={busy}>
        {busy ? "處理中…" : "更新密碼"}
      </button>
    </form>
  );
}

export function ForcedPasswordPage() {
  return (
    <AuthCard title="設定新密碼">
      <PasswordChangeForm forced />
    </AuthCard>
  );
}

export function MfaSetupForm({ onDone }: { onDone?: () => void }) {
  const { reloadMe } = useAuth();
  const [setup, setSetup] = useState<{ secret: string; otpauth_uri: string } | null>(null);
  const [qr, setQr] = useState<string>("");
  const [code, setCode] = useState("");
  const [error, setError] = useState<unknown>(null);
  const [busy, setBusy] = useState(false);

  useEffect(() => {
    if (setup) QRCode.toDataURL(setup.otpauth_uri, { margin: 1, width: 200 }).then(setQr);
  }, [setup]);

  async function start() {
    setError(null);
    try {
      setSetup(await api("/auth/mfa/setup", { method: "POST" }));
    } catch (err) {
      setError(err);
    }
  }
  async function enable(e: FormEvent) {
    e.preventDefault();
    setBusy(true);
    setError(null);
    try {
      await api("/auth/mfa/enable", { body: { code } });
      await reloadMe();
      onDone?.();
    } catch (err) {
      setError(err);
    } finally {
      setBusy(false);
    }
  }

  return (
    <div>
      <ErrorBox error={error} />
      {!setup ? (
        <>
          <p className="muted small">使用 Google Authenticator、Microsoft Authenticator 或 1Password 等 App，登入時除了密碼還需要輸入 App 上的 6 位數字。</p>
          <button className="btn primary" onClick={start}>
            開始設定
          </button>
        </>
      ) : (
        <form onSubmit={enable}>
          <p className="small">1. 用驗證器 App 掃描 QR Code：</p>
          {qr && <img className="qr" src={qr} width={200} height={200} alt="雙因素驗證 QR Code" />}
          <p className="small muted">
            無法掃描時手動輸入金鑰：<span className="mono">{setup.secret}</span>
          </p>
          <Field label="2. 輸入 App 顯示的 6 位數字">
            <input inputMode="numeric" maxLength={6} required value={code} onChange={(e) => setCode(e.target.value.replace(/\D/g, ""))} autoFocus />
          </Field>
          <button className="btn primary" disabled={busy || code.length !== 6}>
            {busy ? "處理中…" : "啟用雙因素驗證"}
          </button>
        </form>
      )}
    </div>
  );
}

export function ForcedMfaPage() {
  const { logout } = useAuth();
  return (
    <AuthCard title="設定雙因素驗證">
      <div className="alert info">管理員帳號必須啟用雙因素驗證才能使用系統。</div>
      <MfaSetupForm />
      <button className="btn" style={{ marginTop: 12 }} onClick={() => void logout()}>
        登出
      </button>
    </AuthCard>
  );
}
