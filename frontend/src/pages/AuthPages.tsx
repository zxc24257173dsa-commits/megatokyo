import { useEffect, useState, type FormEvent } from "react";
import QRCode from "qrcode";
import { api, messageFor, type TokenResponse } from "../api";
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

function GoogleMark() {
  return (
    <svg width="18" height="18" viewBox="0 0 48 48" aria-hidden="true">
      <path fill="#EA4335" d="M24 9.5c3.5 0 6.6 1.2 9.1 3.6l6.8-6.8C35.8 2.4 30.3 0 24 0 14.6 0 6.6 5.4 2.7 13.3l7.9 6.1C12.5 13.6 17.8 9.5 24 9.5z" />
      <path fill="#4285F4" d="M46.1 24.5c0-1.6-.1-3.1-.4-4.5H24v9h12.4c-.5 2.9-2.2 5.3-4.6 6.9l7.4 5.7c4.3-4 6.9-9.9 6.9-17.1z" />
      <path fill="#FBBC05" d="M10.5 28.6c-.5-1.4-.8-3-.8-4.6s.3-3.2.8-4.6l-7.9-6.1C1 16.6 0 20.2 0 24s1 7.4 2.7 10.7l7.8-6.1z" />
      <path fill="#34A853" d="M24 48c6.5 0 11.9-2.1 15.9-5.8l-7.4-5.7c-2.1 1.4-4.8 2.3-8.5 2.3-6.2 0-11.5-4.1-13.4-9.8l-7.9 6.1C6.6 42.6 14.6 48 24 48z" />
    </svg>
  );
}

export function LoginPage() {
  const { acceptToken } = useAuth();
  const [config, setConfig] = useState<{ google_enabled: boolean; password_enabled: boolean } | null>(null);
  const [showPassword, setShowPassword] = useState(false);
  const [email, setEmail] = useState("");
  const [password, setPassword] = useState("");
  const [mfaToken, setMfaToken] = useState<string | null>(null);
  const [code, setCode] = useState("");
  const [error, setError] = useState<unknown>(null);
  const [googleError, setGoogleError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);

  useEffect(() => {
    api<{ google_enabled: boolean; password_enabled: boolean }>("/auth/config")
      .then(setConfig)
      .catch(() => setConfig({ google_enabled: false, password_enabled: true }));
    // Errors from the Google redirect come back as ?login_error=<code>; show once, then clean the URL.
    const params = new URLSearchParams(window.location.search);
    const err = params.get("login_error");
    if (err) {
      setGoogleError(messageFor(err));
      params.delete("login_error");
      const qs = params.toString();
      window.history.replaceState(null, "", window.location.pathname + (qs ? `?${qs}` : ""));
    }
  }, []);

  function signInWithGoogle() {
    const next = window.location.pathname + window.location.search;
    window.location.href = `/api/v1/auth/google/start?next=${encodeURIComponent(next)}`;
  }

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

  if (!config) return null;
  const google = config.google_enabled;
  const passwordVisible = config.password_enabled && (!google || showPassword || mfaToken);

  return (
    <AuthCard title={mfaToken ? "輸入驗證碼" : "登入"}>
      {googleError && <div className="alert error" role="alert">{googleError}</div>}
      {google && !mfaToken && (
        <>
          <button type="button" className="btn primary" style={{ width: "100%", justifyContent: "center", minHeight: 46 }} onClick={signInWithGoogle}>
            <span style={{ background: "#fff", borderRadius: 4, padding: 3, display: "inline-flex" }}><GoogleMark /></span>
            使用 Google 帳號登入
          </button>
          <p className="small muted" style={{ textAlign: "center", margin: "10px 0 0" }}>只有管理員加入的 Google 帳號可以登入</p>
        </>
      )}
      {google && config.password_enabled && !passwordVisible && (
        <button type="button" className="btn" style={{ width: "100%", justifyContent: "center", marginTop: 16, border: 0, color: "var(--muted)" }} onClick={() => setShowPassword(true)}>
          使用 Email 和密碼登入（備用）
        </button>
      )}
      {!google && !config.password_enabled && <div className="alert error">目前沒有可用的登入方式，請聯絡管理員。</div>}
      {passwordVisible && (
        <form onSubmit={submit} style={google && !mfaToken ? { marginTop: 18, paddingTop: 18, borderTop: "1px solid var(--border)" } : undefined}>
          <ErrorBox error={error} />
          {!mfaToken ? (
            <>
              <Field label="Email">
                <input type="email" autoComplete="username" required value={email} onChange={(e) => setEmail(e.target.value)} autoFocus={!google} />
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
          <button className={google ? "btn" : "btn primary"} style={{ width: "100%", justifyContent: "center" }} disabled={busy}>
            {busy ? "處理中…" : mfaToken ? "驗證" : "登入"}
          </button>
          {mfaToken && (
            <button type="button" className="btn" style={{ width: "100%", justifyContent: "center", marginTop: 8 }} onClick={() => setMfaToken(null)}>
              返回
            </button>
          )}
        </form>
      )}
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
