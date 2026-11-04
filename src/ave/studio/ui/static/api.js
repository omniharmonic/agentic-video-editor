// Thin client for /api/studio/ (spec 7.3). Errors carry {status, code, latest} so views can show the reason and snap back.

const BASE = "/api/studio/";

export class ApiError extends Error {
  constructor(status, body) {
    super((body && body.error) || `request failed (${status})`);
    this.status = status;
    this.code = (body && body.code) || "";
    this.latest = body && body.latest;
  }
}

async function call(method, path, { params, body } = {}) {
  let url = BASE + path;
  if (params) {
    const q = new URLSearchParams();
    for (const [k, v] of Object.entries(params)) if (v != null && v !== "") q.set(k, Array.isArray(v) ? v.join(",") : v);
    const s = q.toString();
    if (s) url += "?" + s;
  }
  const init = { method, credentials: "same-origin", headers: { Accept: "application/json" } };
  if (body !== undefined) { init.body = JSON.stringify(body); init.headers["Content-Type"] = "application/json"; }
  let res;
  try { res = await fetch(url, init); }
  catch { throw new ApiError(0, { error: window.aveNative ? 'The workspace is not reachable. Reopen it from the app’s File menu.' : 'The studio server is not reachable. Is `ave studio ui` still running?', code: "offline" }); }
  let data = null;
  try { data = await res.json(); } catch { /* empty or non-JSON body */ }
  if (!res.ok) throw new ApiError(res.status, data);
  return data;
}

export const api = {
  summary: () => call("GET", "summary"),
  accounts: () => call("GET", "accounts"),
  calendar: (from, to, accounts) => call("GET", "calendar", { params: { from, to, accounts } }),
  grid: (account, limit = 30) => call("GET", `grid/${encodeURIComponent(account)}`, { params: { limit } }),
  changes: (cursor) => call("GET", "changes", { params: { cursor } }),
  move: (post, body) => call("POST", `posts/${encodeURIComponent(post)}/move`, { body }),
  swap: (post, body) => call("POST", `posts/${encodeURIComponent(post)}/swap`, { body }),
  unschedule: (post, body) => call("POST", `posts/${encodeURIComponent(post)}/unschedule`, { body }),
  acceptProposal: (id) => (window.aveNative?.acceptProposal ? window.aveNative.acceptProposal(id) : call("POST", `schedule/proposals/${encodeURIComponent(id)}/accept`, { body: {} })),
  slots: (post, variant, count = 6) => call("GET", "slots", { params: { post, variant, count } }),
  get: (path, params) => call("GET", path, { params }),
  post: (path, body) => call("POST", path, { body }),
};
