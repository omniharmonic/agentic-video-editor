// What Accounts and Alerts say, shared by the app's Settings (app/static/settings.js) and the old Connections and Alerts screens
// until Task 24 retires them. Public state only: no credential value is ever part of a connection's reading, and every change to
// a credential or an authority goes through a typed native request (desktop_connections), never this page.

export const words = (s) => String(s || "unknown").replaceAll("_", " ");
export function liveState(a, paused) { return paused || a.paused ? ["PAUSED", "Delivery paused"] : a.live ? ["LIVE", "Live account attestation recorded"] : ["DRY-RUN", "Sending not enabled"]; }
export const LABELS = { ig_user_id: "Instagram professional account ID", page_id: "Facebook Page ID", threads_user_id: "Threads user ID", author: "LinkedIn author URN",
  user_id: "Acting user ID", board_id: "Pinterest board ID", did: "Bluesky DID", handle: "Login handle", pds: "Personal data server (HTTPS)",
  channel_id: "YouTube channel ID", client_id: "Registered OAuth client ID", sandbox: "Use Pinterest sandbox" };
export const FIELDS = { instagram: ["ig_user_id", "page_id"], facebook: ["page_id"], threads: ["threads_user_id"], linkedin: ["author"], x: ["user_id"],
  pinterest: ["user_id", "board_id", "sandbox"], bluesky: ["did", "handle", "pds"], youtube: ["channel_id", "client_id"], tiktok: ["user_id"] };
/** The identity fields a platform's destination asks for, given the chosen login product. */
export const fieldsFor = (platform, product) => (FIELDS[platform] || []).filter((k) => !(k === "page_id" && platform === "instagram" && product !== "instagram_facebook"));
export const PLATFORMS = ["instagram", "linkedin", "facebook", "threads", "youtube", "tiktok", "bluesky", "pinterest", "x"];
export const HOST_KEYS = ["endpoint", "bucket", "region", "prefix"];
/** The four state tiles of a connection, in words. */
export const connectionStates = (a) => [[a.credential_state === "partial_update" ? "Partial update" : a.stored.length ? "Stored" : "Not stored", "Credentials"],
  [a.verified ? "Identity verified" : "Not verified", "Identity"], [a.live_enabled ? "Enabled" : "Not enabled", "Live account"], [a.live_tested ? "Live tested" : "Not tested", "Publication"]];
/** The observed evidence rows of a connection, in words. */
export function evidenceRows(e = {}) {
  return [["Last result", words(e.state || "not checked")], ["As of", e.checked_at || "Never checked"], ["Source", e.source || "No provider read"],
    ["Acting identity", e.actor ? `${e.actor.namespace}: ${e.actor.id}` : "Unknown"], ["Destination", e.destination || "Not verified"],
    ["Granted permissions", (e.grants || []).join(", ") || "Unknown; typed scopes are not evidence"],
    ["Tasks / roles", [...(e.tasks || []), ...(e.roles || [])].join(", ") || "Not established"], ["Expiry", e.expires_at || "Unknown"],
    ["Publishing access gaps", e.publishing_access ? `Missing: ${e.publishing_access.missing.join(", ") || "none established"}. Unknown: ${e.publishing_access.unknown.join(", ") || "none listed"}. App/product and format certification remain unverified.` : "Not checked"],
    ["Insights / comments", "Not verified by publishing setup"]];
}
export const PARTIAL_UPDATE = "Keychain saved only part of the update. Re-enter all credentials or remove them. Earlier verification and live authority are invalid.";
export const SECURE_NOTE = "Secure entry and verification are separate. A stored token is not proof of identity or permission.";
export const LIVE_NOTE = "This account decision does not approve a post or enable the workspace sender.";
export const HOST_NOTE = "Use an existing bucket. This action stores settings and keys in native secure fields; it creates no bucket and performs no paid upload. Delivery retains the existing per-operation media leases.";
export const SERVICE_NOTE = "Stop ends background execution. Disable revokes unattended sending. Pause blocks delivery without uninstalling the service. Sleeping or offline Macs can miss slots; each account’s catch-up policy still applies.";

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
/** Where an alert's advice points in the app's Settings (Accounts' tabs). */
export const ADVICE_TAB = { host_failed: "hosting", daemon_stale: "service" };
export const ackCommand = (id) => `ave studio alerts ack ${id}`;
