// Alerts page (spec 7.2 #/alerts, 6.8): failures and what to do about each.
//
//   mountAlerts(root, ctx) -> {destroy}
// Acknowledge posts to alerts/<id>/ack. If the server has no such route yet, the page shows the CLI line (`ave studio alerts ack <id>`) instead.
import { ago, ApiError, clear, empty, errorBox, h, say } from "./panels.js";

export const ADVICE = {
  auth_expired: "The token was refused. Open Connections to replace credentials and verify the account.",
  auth_expiring: "The token expires within a week. Open Connections to renew its session or replace credentials.",
  quota: "The platform said slow down. The post waits for the quota to reset; nothing to do unless it is late.",
  rejected: "The platform refused the post. Read the message, change the post, and send it back through review.",
  uncertain: "The platform may have the post. Open the post, refresh its status or use protected outcome review. Never retry while its outcome is uncertain.",
  missed: "The slot passed while the scheduler was off. Pick a new time; missed posts are never sent late on their own.",
  late: "The post went out later than planned.",
  approval_expired: "The approval is too old to send. Review and approve it again.",
  license_gap: "A file in this post has no licence entry. Log the licence in the library, then sync the queue.",
  host_failed: "Open Connections → Media hosting to check this workspace’s host settings.",
  duplicate: "The same post went out recently. Change it or confirm it on the post.",
  daemon_stale: "Open Connections → Background delivery to check the service and repair or restart it.",
  credentials_missing: "Open Connections to enter credentials in the native secure sheet.",
  account_not_live: "Open Connections to verify and live-attest the account, then enable automatic posting separately.",
};
const SEV = { error: ["✕", "Error"], warn: ["!", "Warning"], info: ["i", "Note"] };

export function mountAlerts(root, ctx) {
  root.classList.add("st");
  const list = h("div.st-list", { "aria-live": "polite" });
  clear(root).append(h("section.st-page", h("header.st-pagehead", h("h1", "Alerts"), h("p.st-hint", "Things that stopped or may need you.")), list));
  let alive = true;
  async function load() {
    try {
      const res = await ctx.api.get("alerts");
      if (!alive) return;
      const alerts = res.alerts || [];
      clear(list);
      if (!alerts.length) { list.append(empty("No alerts.", "If a post fails or a token is about to expire, it shows up here.")); return; }
      alerts.forEach((a) => list.append(card(a)));
    } catch (ex) { clear(list).append(errorBox(ex, load)); }
  }
  function card(a) {
    const [g, label] = SEV[a.severity] || SEV.info;
    const acked = !!a.ack;
    const cmd = `ave studio alerts ack ${a.id}`;
    const note = h("p.st-hint", { role: "status" });
    const btn = h("button", { type: "button", disabled: acked, dataset: { act: "ack" }, onclick: async () => {
      btn.disabled = true;
      try { await ctx.api.post(`alerts/${a.id}/ack`, {}); say(ctx, "Acknowledged."); load(); }
      catch (ex) {
        btn.disabled = false;
        if (ex instanceof ApiError && (ex.status === 404 || ex.status === 405)) note.textContent = window.aveNative ? "Acknowledgement is unavailable. Reopen this workspace and retry." : `Acknowledge from a terminal: ${cmd}`;
        else say(ctx, `Could not acknowledge: ${ex.message}`, { error: true });
      }
    } }, acked ? "Acknowledged" : "Acknowledge");
    return h("article.st-card.al", { class: "sev-" + a.severity, dataset: { alert: a.id } },
      h("header", h("span.st-chip.sev-" + a.severity, h("span", { "aria-hidden": "true" }, g), label), h("strong", a.kind.replace(/_/g, " ")), h("span.st-hint", ` ${a.account || ""} · ${ago(a.ts, ctx.now ? ctx.now() : new Date())}`)),
      h("p", a.message),
      ADVICE[a.kind] ? h("p.al-advice", h("strong", "What to do: "), ADVICE[a.kind]) : null,
      a.post ? h("p", h("a", { href: `#/post/${a.post}` }, `Open ${a.post}`)) : null,
      h("footer", h("a", {href:"#/connections"}, "Open Connections"), btn, note));
  }
  load();
  const poll = setInterval(() => { if (document.visibilityState === "visible") load(); }, 20000);
  return { destroy() { alive = false; clearInterval(poll); }, reload: load };
}
