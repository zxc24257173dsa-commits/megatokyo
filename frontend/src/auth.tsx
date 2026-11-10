import { createContext, useCallback, useContext, useEffect, useRef, useState, type ReactNode } from "react";
import { lang, setLanguage } from "./i18n";
import { api, refreshSession, setAccessToken, setSessionLostHandler, type TokenResponse } from "./api";
import type { Me } from "./types";

type State = { status: "loading" } | { status: "anonymous" } | { status: "authenticated"; me: Me };

interface AuthCtx {
  state: State;
  me: Me | null;
  can: (perm: string) => boolean;
  acceptToken: (t: TokenResponse) => Promise<void>;
  reloadMe: () => Promise<void>;
  logout: () => Promise<void>;
}

const Ctx = createContext<AuthCtx | null>(null);

export function AuthProvider({ children }: { children: ReactNode }) {
  const [state, setState] = useState<State>({ status: "loading" });
  // Bumped on logout / session loss. A /auth/me response that started before the bump is stale
  // and must not flip the app back to "logged in" (it would briefly show data after logout).
  const generation = useRef(0);

  const reloadMe = useCallback(async () => {
    const gen = generation.current;
    const me = await api<Me>("/auth/me");
    // the account's saved language wins over this browser's (reloads once if they differ)
    if ((me.locale === "zh-TW" || me.locale === "en-US") && me.locale !== lang) {
      setLanguage(me.locale);
      return;
    }
    if (gen === generation.current) setState({ status: "authenticated", me });
  }, []);

  const acceptToken = useCallback(
    async (t: TokenResponse) => {
      setAccessToken(t.access_token);
      await reloadMe();
    },
    [reloadMe],
  );

  const logout = useCallback(async () => {
    generation.current++;
    await api("/auth/logout", { method: "POST" }).catch(() => undefined);
    setAccessToken(null);
    setState({ status: "anonymous" });
  }, []);

  useEffect(() => {
    setSessionLostHandler(() => {
      generation.current++;
      setAccessToken(null);
      setState({ status: "anonymous" });
    });
    // Restore the session from the refresh cookie on page load.
    const gen = generation.current;
    refreshSession().then((t) => {
      if (gen !== generation.current) return;
      if (t) acceptToken(t).catch(() => setState({ status: "anonymous" }));
      else setState({ status: "anonymous" });
    });
  }, [acceptToken]);

  // Access tokens last 15 minutes; refresh a little before expiry while the app is open.
  useEffect(() => {
    if (state.status !== "authenticated") return;
    const id = window.setInterval(() => void refreshSession(), 12 * 60 * 1000);
    return () => window.clearInterval(id);
  }, [state.status]);

  const me = state.status === "authenticated" ? state.me : null;
  const can = useCallback((perm: string) => !!me?.permissions.includes(perm), [me]);
  return <Ctx.Provider value={{ state, me, can, acceptToken, reloadMe, logout }}>{children}</Ctx.Provider>;
}

export function useAuth(): AuthCtx {
  const v = useContext(Ctx);
  if (!v) throw new Error("useAuth outside AuthProvider");
  return v;
}
