// API client.
// - Access token lives only in memory (never localStorage), so a malicious script can't read it from storage.
// - The refresh token is an httpOnly cookie the browser sends to /api/v1/auth only.
// - On 401 we refresh once (single-flight, so parallel requests don't race the rotation) and retry.

export class ApiError extends Error {
  constructor(public status: number, public code: string, message: string, public details: any = {}) {
    super(message);
  }
}

let accessToken: string | null = null;
let onSessionLost: () => void = () => {};

export function setAccessToken(t: string | null) {
  accessToken = t;
}
export function setSessionLostHandler(fn: () => void) {
  onSessionLost = fn;
}

export interface TokenResponse {
  access_token: string;
  expires_in: number;
  restrictions: string[];
}

let refreshing: Promise<TokenResponse | null> | null = null;

export function refreshSession(): Promise<TokenResponse | null> {
  if (!refreshing) {
    refreshing = fetch("/api/v1/auth/refresh", { method: "POST", credentials: "same-origin" })
      .then(async (r) => (r.ok ? ((await r.json()) as TokenResponse) : null))
      .catch(() => null)
      .then((t) => {
        setAccessToken(t?.access_token ?? null);
        return t;
      })
      .finally(() => {
        refreshing = null;
      });
  }
  return refreshing;
}

export function newIdempotencyKey(): string {
  return crypto.randomUUID();
}

interface Options {
  method?: string;
  body?: unknown;
  form?: FormData;
  idempotencyKey?: string;
  params?: Record<string, string | number | boolean | undefined | null>;
}

export async function api<T = any>(path: string, opts: Options = {}, retried = false): Promise<T> {
  const url = new URL("/api/v1" + path, window.location.origin);
  for (const [k, v] of Object.entries(opts.params ?? {})) {
    if (v !== undefined && v !== null && v !== "") url.searchParams.set(k, String(v));
  }
  const headers: Record<string, string> = {};
  if (accessToken) headers["Authorization"] = `Bearer ${accessToken}`;
  if (opts.idempotencyKey) headers["Idempotency-Key"] = opts.idempotencyKey;
  let body: BodyInit | undefined;
  if (opts.form) body = opts.form;
  else if (opts.body !== undefined) {
    headers["Content-Type"] = "application/json";
    body = JSON.stringify(opts.body);
  }
  const res = await fetch(url.pathname + url.search, {
    method: opts.method ?? (body ? "POST" : "GET"),
    headers,
    body,
    credentials: "same-origin",
  });
  if (res.status === 401 && !retried && !path.startsWith("/auth/")) {
    const t = await refreshSession();
    if (t) return api<T>(path, opts, true);
    onSessionLost();
  }
  if (res.status === 204) return undefined as T;
  const data = await res.json().catch(() => ({}));
  if (!res.ok) {
    const e = data?.error ?? {};
    throw new ApiError(res.status, e.code ?? `http_${res.status}`, e.message ?? res.statusText, e.details ?? {});
  }
  return data as T;
}

// Chinese messages for the error codes users actually meet. Unknown codes fall back to the server text.
const MESSAGES: Record<string, string> = {
  invalid_credentials: "Email 或密碼錯誤，或帳號暫時鎖定（連續錯誤 5 次會鎖定 15 分鐘）",
  mfa_code_invalid: "驗證碼錯誤",
  mfa_challenge_invalid: "驗證逾時，請重新登入",
  weak_password: "密碼強度不足",
  forbidden: "你沒有這個操作的權限",
  insufficient_stock: "庫存不足",
  sku_code_taken: "SKU 編號已存在",
  duplicate_sku_code: "同一個 SKU 編號重複出現",
  modified_by_someone_else: "這筆資料剛被其他人修改，請重新整理後再試",
  unit_cost_required: "需要輸入單位成本（USD）",
  bundle_no_stock: "組合商品本身沒有庫存，請調整組成 SKU",
  nested_bundle: "組合商品不能包含另一個組合商品",
  count_incomplete: "每個 SKU 都要填寫盤點數量才能送出",
  last_admin: "這是最後一位管理員，不能移除",
  cannot_deactivate_self: "不能停用自己的帳號",
  email_taken: "這個 Email 已經有帳號",
  unsupported_file_type: "只接受 JPEG、PNG、WebP 圖片",
  file_too_large: "圖片需小於 5 MB",
  category_exists: "分類已存在",
  validation_error: "輸入資料格式不正確",
  password_login_disabled: "已停用密碼登入，請使用 Google 登入",
  no_password: "這個帳號使用 Google 登入，沒有密碼",
  account_not_registered: "這個 Google 帳號還沒有被加入系統，請聯絡管理員",
  google_account_mismatch: "這個 Email 已綁定另一個 Google 帳號",
  google_account_in_use: "這個 Google 帳號已綁定其他使用者",
  google_email_unverified: "這個 Google 帳號的 Email 尚未驗證",
  google_domain_not_allowed: "只允許公司網域的 Google 帳號",
  google_state_invalid: "登入逾時，請再試一次",
  google_token_invalid: "Google 登入驗證失敗，請再試一次",
  google_exchange_failed: "Google 登入失敗，請再試一次",
  google_unreachable: "暫時無法連線到 Google，請稍後再試",
  google_cancelled: "已取消 Google 登入",
  file_already_imported: "這個檔案已經匯入過了（同一份檔案不能匯入兩次）",
  unreadable_file: "檔案無法讀取，請確認是 .xlsx 或 .csv",
  too_many_rows: "工作表列數太多（上限 20,000 列）",
  import_has_errors: "有錯誤的列；請修正檔案，或勾選「略過有錯誤的列」",
  new_skus_not_confirmed: "有系統中還沒有的 SKU，請勾選「一併建立」",
  nothing_to_import: "沒有可以匯入的資料列",
  import_closed: "這次匯入已經完成或取消",
  over_receipt: "入庫數量超過採購數量",
  future_date: "日期不能是未來",
  invalid_mapping: "每個欄位必須對應到不同的欄",
};

export function messageFor(code: string): string {
  return MESSAGES[code] ?? "登入失敗，請再試一次";
}

export function errorText(e: unknown): string {
  if (e instanceof ApiError) {
    let msg = MESSAGES[e.code] ?? e.message;
    if (e.code === "insufficient_stock" && e.details?.available !== undefined)
      msg += `（${e.details.sku_code}：可用 ${e.details.available}，需要 ${e.details.requested}）`;
    if (e.code === "weak_password" && Array.isArray(e.details?.rules)) msg += "：" + e.details.rules.join("、");
    if (e.code === "sku_code_taken" && e.details?.sku_codes) msg += "：" + e.details.sku_codes.join(", ");
    return msg;
  }
  return "連線失敗，請檢查網路後再試";
}
