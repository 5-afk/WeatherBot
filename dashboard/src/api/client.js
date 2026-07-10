/** Resolve ATLAS Flask API base URL from VITE_API_URL or sensible defaults. */
export function getApiBase() {
  const envUrl = import.meta.env.VITE_API_URL;
  if (envUrl !== undefined && envUrl !== null && String(envUrl).trim() !== "") {
    return String(envUrl).replace(/\/$/, "");
  }
  if (import.meta.env.DEV) {
    return "";
  }
  if (typeof window !== "undefined" && window.location.origin.startsWith("http")) {
    return window.location.origin;
  }
  return "";
}

export const API_BASE = getApiBase();

const SECRET = import.meta.env.VITE_DASHBOARD_SECRET || "";

/** Headers for mutating API calls (POST). */
export function authHeaders() {
  const headers = { "Content-Type": "application/json" };
  if (SECRET) {
    headers["X-Atlas-Secret"] = SECRET;
  }
  return headers;
}

export async function api(path, opts = {}) {
  const method = (opts.method || "GET").toUpperCase();
  const needsAuth = method !== "GET" && method !== "HEAD";
  const baseHeaders = needsAuth ? authHeaders() : { "Content-Type": "application/json" };
  const res = await fetch(`${API_BASE}${path}`, {
    ...opts,
    headers: { ...baseHeaders, ...(opts.headers || {}) },
  });

  if (!res.ok) {
    const text = await res.text().catch(() => "");
    let message = `${res.status} ${res.statusText}`;
    try {
      const errJson = JSON.parse(text);
      if (errJson.error) message = errJson.error;
    } catch {
      if (text) message = `${message}: ${text.slice(0, 200)}`;
    }
    throw new Error(message);
  }

  if (res.status === 204) return null;

  const json = await res.json();
  if (json && typeof json.ok === "boolean" && !json.ok) {
    throw new Error(json.error || `API error ${res.status}`);
  }
  return json.data;
}

export async function apiPost(path, body) {
  return api(path, { method: "POST", body: JSON.stringify(body ?? {}) });
}
