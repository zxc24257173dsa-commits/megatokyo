// Interface language (Phase 11): Traditional Chinese (default, the owner and the Taiwan / Japan team) or English
// (US staff). The Chinese text in the code is the key; locales/en.ts holds the English for each key.
//
// The language is decided once, before any page module runs (so module-level label maps translate too):
// saved choice in this browser -> otherwise the browser language. After sign-in the account's saved
// language wins (setLanguage reloads the page when it differs).
import { EN } from "./locales/en";

export type Lang = "zh-TW" | "en-US";
const KEY = "megatokyo.lang";

function initial(): Lang {
  try {
    const saved = localStorage.getItem(KEY);
    if (saved === "zh-TW" || saved === "en-US") return saved;
  } catch {
    /* storage blocked: fall through */
  }
  return typeof navigator !== "undefined" && navigator.language?.toLowerCase().startsWith("zh") ? "zh-TW" : "en-US";
}

export const lang: Lang = initial();
export const isEnglish = lang === "en-US";
if (typeof document !== "undefined") document.documentElement.lang = isEnglish ? "en" : "zh-Hant";

const missing = new Set<string>();

/** t("已寄出 {0} 件", [n]) -> "Shipped 5" ; placeholders {0}, {1}… or {name}. */
export function t(key: string, args?: unknown[] | Record<string, unknown>): string {
  let s = key;
  if (isEnglish) {
    const en = EN[key];
    if (en !== undefined) s = en;
    else if (import.meta.env.DEV && !missing.has(key)) {
      missing.add(key);
      console.warn("[i18n] missing English for:", key);
    }
  }
  if (args) {
    s = s.replace(/\{(\w+)\}/g, (_m, k) => {
      const v = Array.isArray(args) ? args[Number(k)] : (args as Record<string, unknown>)[k];
      return v === undefined || v === null ? "" : String(v);
    });
  }
  return s;
}

/** Locale for dates (numbers always use en-US grouping: 1,234.50). */
export const dateLocale = isEnglish ? "en-US" : "zh-TW";

/** Save the choice in this browser and reload, so every page (and every label map) uses it. */
export function setLanguage(next: Lang, reload = true): void {
  try {
    localStorage.setItem(KEY, next);
  } catch {
    /* ignore */
  }
  if (reload && next !== lang) window.location.reload();
}
