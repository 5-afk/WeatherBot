// ATLAS-NOTE: Always use relative /api paths — Vite proxy in dev, same-origin when served by Flask.
// VITE_API_URL configures the proxy target in vite.config.js only (not used as fetch base).
// VITE_DASHBOARD_SECRET must match server DASHBOARD_SECRET; restart pnpm dev after .env.local edits.

const SECRET = import.meta.env.VITE_DASHBOARD_SECRET || "";

/** Sentinel returned when Kelly/API is unreachable (network or proxy ECONNREFUSED). */
export const OFFLINE_RESULT = Object.freeze({
  ok: false,
  offline: true,
  error: "Kelly is offline",
});

export function isOffline(data) {
  return Boolean(data && data.offline);
}

export class ApiError extends Error {
  constructor(status, body) {
    super(`API ${status}: ${body}`);
    this.name = "ApiError";
    this.status = status;
    this.body = body;
  }
}

export function authHeaders(extra = {}) {
  return {
    ...extra,
    ...(SECRET ? { "X-Atlas-Secret": SECRET } : {}),
  };
}

function offlineSentinel(message) {
  console.warn(`[ATLAS] unreachable:`, message);
  return { ...OFFLINE_RESULT, error: message || OFFLINE_RESULT.error };
}

/**
 * @param {string} path - Relative API path e.g. `/api/status`
 * @param {{ method?: string, body?: unknown, signal?: AbortSignal }} opts
 */
export async function api(path, { method = "GET", body, signal } = {}) {
  const upper = method.toUpperCase();
  const isGet = upper === "GET" || upper === "HEAD";

  let res;
  try {
    res = await fetch(path, {
      method: upper,
      headers: {
        ...(body != null ? { "Content-Type": "application/json" } : {}),
        ...(upper !== "GET" && upper !== "HEAD" && SECRET ? { "X-Atlas-Secret": SECRET } : {}),
      },
      body: body != null ? JSON.stringify(body) : undefined,
      signal,
    });
  } catch (e) {
    if (e.name === "AbortError") throw e;
    if (isGet) return offlineSentinel(e.message);
    throw new ApiError(0, `network: ${e.message}`);
  }

  if (!res.ok) {
    const text = await res.text().catch(() => "");
    // Vite proxy surfaces ECONNREFUSED as 500/502/504
    if (isGet && (res.status >= 500 || res.status === 0)) {
      return offlineSentinel(text || `HTTP ${res.status}`);
    }
    throw new ApiError(res.status, text);
  }

  if (res.status === 204) return null;

  let json;
  try {
    json = await res.json();
  } catch (e) {
    if (isGet) return offlineSentinel(e.message);
    throw new ApiError(res.status, "invalid JSON");
  }

  if (json && typeof json.ok === "boolean" && !json.ok) {
    if (isGet) return offlineSentinel(json.error || "unknown error");
    throw new ApiError(res.status, json.error || "unknown error");
  }

  return json?.data !== undefined ? json.data : json;
}

export async function apiPost(path, body) {
  return api(path, { method: "POST", body });
}
