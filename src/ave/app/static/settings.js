// Settings: the app's quiet back office, reached from the Workspace's sidebar and the Room's header. Four sections:
//
//   Accounts       where posts go (local profiles, publishing destinations, credentials, live accounts), the media host,
//                  background delivery and posting permission, and the creative tools (Resolve, Canva, the agent configuration).
//                  This is the old Connections screen. It reads public state only: no credential value is ever part of what the
//                  studio sends here, and every change to a credential or an authority is a typed request to the app
//                  (aveNative.connectionAction), which asks again in a native sheet and checks that it is the device owner.
//   Alerts         what stopped or may need a person, with what to do, and Acknowledge (the old Alerts screen).
//   People         each project's members, what media they keep, who publishes, and the native sheets that share a project,
//                  join one, remove someone or hand publishing over (Task 16's sheets, also on the board's options menu).
//   Collaboration  the editorial exchange (`ave collaborate`): pairing a collaborator, sending chosen drafts (footage moves by sync),
//                  reviewing what arrives before applying it, and conflicts (the old Collaborate screen).
//
// Credentials, approval keys and the publish queue stay on this computer: nothing here shares them, and nothing here approves or
// sends a post. Approving lives in the approval sheet (Workspace › Approvals).

import { h, button, icon, toast, busy, popover } from "./dom.js";
import { api, plain, menu, avatar } from "./workspace/common.js";
import { makeApi } from "/app/shared/panels.js";
import { platformMark } from "./workspace/posts.js";
import { words, LABELS, fieldsFor, PLATFORMS, HOST_KEYS, connectionStates, evidenceRows, PARTIAL_UPDATE, SECURE_NOTE, LIVE_NOTE, HOST_NOTE,
  SERVICE_NOTE, ADVICE, ADVICE_TAB, ackCommand } from "/app/shared/connections-core.js";
import { fetchLibrary, thumbUrl } from "/app/shared/library-core.js";

// A draft in the collaboration lists, by its words: never its id.
const draftName = (p) => { const t = String(p.text || "").trim().split(/\n/)[0]; return t ? (t.length > 80 ? t.slice(0, 79) + "…" : t) : "Untitled draft"; };

export const SECTIONS = [
  ["accounts", "Accounts", "send", "Where your posts go, the media host, background delivery and your creative tools."],
  ["alerts", "Alerts", "alert", "Things that stopped, or may need you."],
  ["people", "People", "people", "Who is on each project, how much media they keep, and which computer publishes it."],
  ["collaboration", "Collaboration", "invite", "Send drafts to a collaborator, and review what they send before you use it."],
];
const TABS = [["publishing", "Profiles and publishing"], ["hosting", "Media hosting"], ["service", "Background delivery"], ["tools", "Creative tools and agents"]];
const NO_APP_SHARE = "Sharing needs the AVE Studio app and Tailscale.";
const SEV = { error: ["Error", "var(--negative)"], warn: ["Warning", "#fdab3d"], info: ["Note", "var(--sync-wait)"] };

export function parseSettings(hash) {
  const m = /^#\/settings(?:\/([a-z]+))?(?:\/([a-z]+))?/.exec(hash || "") || [];
  return { section: SECTIONS.some(([id]) => id === m[1]) ? m[1] : "accounts", tab: TABS.some(([id]) => id === m[2]) ? m[2] : null };
}

export function mountSettings(root, route) {
  const studio = makeApi();
  const probe = window.__AVE_TEST__ ? (window.__settings = { native: [] }) : null;
  let current = null, alive = true;
  const navItems = new Map();
  const nav = h("nav", { class: "rail set-nav", "aria-label": "Settings" });
  const head = h("header", { class: "set-head" });
  const body = h("div", { class: "set-body" });
  const main = h("main", { class: "set-main", id: "main", tabindex: "-1" }, head, body);
  nav.append(h("div", { class: "rail-top" }, h("div", { class: "rail-ws" }, h("span", { class: "rail-swatch set-swatch", "aria-hidden": "true" }, icon("gear")), h("span", {}, "Settings"))),
    h("div", { class: "rail-nav" }, SECTIONS.map(([id, label, ic]) => {
      const count = h("span", { class: "rail-count", hidden: true });
      const a = h("a", { class: "rail-item", href: `#/settings/${id}`, dataset: { section: id } }, icon(ic), h("span", { class: "rail-label" }, label), count);
      navItems.set(id, { a, count });
      return a;
    })),
    h("div", { class: "rail-list" }),
    h("div", { class: "rail-foot" },
      h("a", { class: "rail-item", href: "#/workspace" }, icon("grid"), h("span", { class: "rail-label" }, "Workspace")),
      h("a", { class: "rail-item", href: "#/room/" }, icon("film"), h("span", { class: "rail-label" }, "The Room"))));
  root.replaceChildren(h("div", { class: "ws set" }, nav, main));

  function setRoute(r) {
    route = r;
    for (const [id, { a }] of navItems) a.setAttribute("aria-current", id === r.section ? "page" : "false");
    const [, label, , desc] = SECTIONS.find(([id]) => id === r.section);
    document.title = `${label} – Settings`;
    if (current?.id === r.section) { current.tab?.(r.tab); return; }
    current?.close?.();
    head.replaceChildren(h("div", { class: "set-titles" }, h("h1", {}, label), h("p", { class: "secondary-text" }, desc)));
    body.replaceChildren();
    current = { id: r.section, ...({ accounts, alerts, people, collaboration }[r.section])(r) };
  }
  const headAction = (...els) => head.append(h("div", { class: "set-headactions" }, ...els));

  // A typed request to the app. Outside the app there is nothing to hand it to, so the page says so instead of waiting.
  async function native(fn, { notice, args, app = window.aveNative, sample = false } = {}) {
    if (probe) probe.native.push(args ?? null);
    if (!app) { notice.textContent = sample ? "Sample connections are fictional. Protected controls are available in your own workspace in the desktop app."
      : "Open this workspace in the AVE Studio app to use its secure sheets."; return null; }
    notice.textContent = "Continue in the app’s sheet.";
    try { const r = await fn(app); notice.textContent = (r && (r.message || words(r.state))) || "Saved"; return r || {}; }
    catch (e) { notice.textContent = e.message; return null; }
  }

  // ================================================================ Accounts (the old Connections screen)
  function accounts(r) {
    let data = null, tab = r.tab || "publishing", selected = null;
    const notice = h("p", { class: "set-notice", role: "status", "aria-live": "polite" });
    const tabs = h("div", { class: "tabs set-tabs", role: "tablist", "aria-label": "Account sections" });
    const pane = h("div", { class: "set-pane" });
    headAction(button("Refresh status", { variant: "secondary", size: "sm", iconName: "sync", onclick: (e) => busy(e.currentTarget, load) }));
    body.append(tabs, notice, pane);
    for (const [id, label] of TABS) tabs.append(h("button", { type: "button", class: "tab", role: "tab", dataset: { tab: id }, onclick: () => { location.hash = `#/settings/accounts/${id}`; } }, label));
    async function load() {
      try { data = await studio.get("connections"); if (!selected && data.accounts.length) selected = data.accounts[0].id; draw(); }
      catch (e) { pane.replaceChildren(errorBox(e, load, "read your accounts")); }
    }
    const act = (label, action, args, { variant = "secondary", disabled = false } = {}) => button(label, { variant, size: "sm", disabled, "data-native": action,
      onclick: (e) => busy(e.currentTarget, async () => { if (await native((app) => app.connectionAction(action, args), { notice, args: { action, ...args }, app: window.aveNative?.connectionAction ? window.aveNative : null, sample: data?.fixtures })) await load(); }) });
    function draw() {
      if (!data) return;
      for (const t of tabs.children) { const on = t.dataset.tab === tab; t.setAttribute("aria-selected", String(on)); t.tabIndex = on ? 0 : -1; }
      pane.replaceChildren(...[data.fixtures ? h("p", { class: "set-info" }, icon("info"), h("span", {}, "Sample studio: no real credentials or services.")) : null,
        ...{ publishing, hosting, service, tools }[tab]()].filter(Boolean));
    }
    function publishing() {
      const list = h("ul", { class: "set-profiles" }, data.accounts.map((a) => h("li", {}, h("button", { type: "button", class: "set-profile", "aria-pressed": String(a.id === selected),
        onclick: () => { selected = a.id; draw(); } }, platformMark(a.platform),
        h("span", { class: "set-ptext" }, h("strong", {}, a.label), h("span", { class: "secondary-text" },
          `${a.platform} · ${a.mode === "manual" ? "Local profile" : a.verified ? "Identity verified" : a.credential_state && a.credential_state !== "unknown" ? words(a.credential_state) : "Not connected"}`))))));
      const side = h("aside", { class: "set-list", "aria-label": "Your profiles" },
        h("div", { class: "set-listhead" }, h("h2", {}, "Your profiles"), button("New local profile", { variant: "secondary", size: "sm", iconName: "plus", iconOnly: true, onclick: (e) => editProfile(e.currentTarget, null) })),
        data.accounts.length ? list : h("p", { class: "secondary-text" }, "No profiles yet."));
      const a = data.accounts.find((x) => x.id === selected);
      if (!a) return [h("div", { class: "set-split" }, side, h("section", { class: "set-detail set-empty" }, h("h2", {}, "Plan without signing in."),
        h("p", { class: "secondary-text" }, "Create a local profile to draft, review and export your posts.")))];
      const args = { account: a.id, revision: a.revision }, e = a.evidence || {};
      const product = h("select", { class: "input", "aria-label": "Login product" }, h("option", { value: "" }, "Choose login product"),
        Object.entries(data.products).filter(([, p]) => p.platform === a.platform).map(([id, p]) => h("option", { value: id, selected: id === a.product }, `${words(id)} · ${p.version}`)));
      const ids = h("div", { class: "set-fields" }), inputs = {};
      const drawIds = () => {
        const prev = { ...a.public, ...Object.fromEntries(Object.entries(inputs).map(([k, v]) => [k, k === "sandbox" ? v.checked : v.value])) };
        for (const k of Object.keys(inputs)) delete inputs[k];
        ids.replaceChildren(...fieldsFor(a.platform, product.value).map((key) => {
          const input = key === "sandbox" ? h("input", { type: "checkbox", checked: prev[key] === true }) : h("input", { class: "input", type: "text", value: prev[key] || "", autocomplete: "off", spellcheck: "false" });
          inputs[key] = input;
          return key === "sandbox" ? h("label", { class: "set-check" }, input, h("span", {}, LABELS[key])) : field(LABELS[key], input);
        }));
      };
      product.addEventListener("change", drawIds); drawIds();
      const form = h("form", { class: "set-form" }, field("Login product", product), ids,
        fieldsFor(a.platform, "instagram_facebook").length ? h("div", { class: "set-actions" }, button("Confirm destination", { variant: "primary", size: "sm", type: "submit" })) : null);
      form.addEventListener("submit", (ev) => {
        ev.preventDefault();
        const pub = {};
        for (const [k, inp] of Object.entries(inputs)) if (k === "sandbox") pub[k] = inp.checked; else if (inp.value.trim()) pub[k] = inp.value.trim();
        const req = { ...args, values: { product: product.value, public: pub } };
        native((app) => app.connectionAction("connectionConfigure", req), { notice, args: { action: "connectionConfigure", ...req }, app: window.aveNative?.connectionAction ? window.aveNative : null, sample: data.fixtures })
          .then((x) => x && load());
      });
      const detail = h("section", { class: "set-detail", "aria-label": a.label },
        h("header", { class: "set-dhead" }, h("div", {}, h("h2", {}, a.label), h("p", { class: "secondary-text" }, `${a.platform} · ${a.id}`)),
          button("Edit local profile", { variant: "secondary", size: "sm", onclick: (ev) => editProfile(ev.currentTarget, a.id) })),
        h("div", { class: "set-states" }, connectionStates(a).map(([v, k]) => h("div", {}, h("span", { class: "secondary-text" }, k), h("strong", {}, v)))),
        data.manual_note ? h("p", { class: "secondary-text" }, data.manual_note) : null,
        h("h3", {}, "Publishing destination"), form,
        h("p", { class: "secondary-text" }, `Token family: ${a.token_family}. Public identifiers are shown again in the app’s confirmation sheet.`));
      if (a.product) {
        detail.append(h("div", { class: "set-actions" },
          act(a.stored.length ? "Replace credentials" : "Connect credentials", "connectionCredentials", args, { variant: "primary" }),
          act("Remove credentials", "connectionRemoveCredentials", args),
          ["bluesky", "youtube"].includes(a.platform) ? act(a.platform === "bluesky" ? "Sign in / renew session" : "Refresh OAuth session", "connectionSession", args) : null,
          act(a.platform === "x" ? "Verify identity (X may charge)" : "Verify connection", "connectionVerify", args)),
        h("p", { class: "secondary-text" }, SECURE_NOTE),
        a.credential_state === "partial_update" ? h("p", { class: "notice notice-negative" }, icon("alert"), h("span", {}, PARTIAL_UPDATE)) : null,
        h("h3", {}, "Observed evidence"),
        h("dl", { class: "ps-dl set-dl" }, evidenceRows(e).flatMap(([k, v]) => [h("dt", {}, k), h("dd", {}, v)])),
        (e.limitations || []).length ? h("ul", { class: "set-limits" }, e.limitations.map((t) => h("li", {}, t))) : null,
        h("div", { class: "set-actions" }, act("Enable this live account", "connectionLive", args, { disabled: !a.verified }), act("Disable this live account", "connectionOff", args)),
        h("p", { class: "secondary-text" }, LIVE_NOTE),
        button("Open background delivery", { variant: "tertiary", size: "sm", iconName: "right", onclick: () => { location.hash = "#/settings/accounts/service"; } }));
      }
      return [h("div", { class: "set-split" }, side, detail)];
    }
    function hosting() {
      const host = data.host, values = {};
      const form = h("form", { class: "set-form" }, HOST_KEYS.map((key) => {
        const input = h("input", { class: "input", type: "text", value: host.config[key] || (key === "region" ? "auto" : ""), autocomplete: "off", spellcheck: "false",
          placeholder: key === "endpoint" ? "https://your-storage-provider.example" : "" });
        values[key] = input;
        return field(words(key)[0].toUpperCase() + words(key).slice(1), input);
      }), h("div", { class: "set-actions" }, button("Configure host and keys", { variant: "primary", size: "sm", type: "submit" }), act("Remove host keys", "hostRemove", { revision: host.revision })));
      form.addEventListener("submit", (ev) => {
        ev.preventDefault();
        const req = { revision: host.revision, values: Object.fromEntries(Object.entries(values).map(([k, v]) => [k, v.value.trim()])) };
        native((app) => app.connectionAction("hostConfigure", req), { notice, args: { action: "hostConfigure", ...req }, app: window.aveNative?.connectionAction ? window.aveNative : null, sample: data.fixtures })
          .then((x) => x && load());
      });
      return [h("section", { class: "set-card" }, h("h2", {}, "Platform-readable media"),
        h("p", {}, "Instagram and Threads fetch media from a public host. TikTok photos require a verified public domain. Direct-upload formats and manual packages can work without this host."),
        h("p", { class: "set-info" }, icon("info"), h("span", {}, `Host state: ${words(host.state)}. ${(host.stored || []).length} of 2 key entries recorded. No diagnostic upload has run.`)),
        form, h("p", { class: "secondary-text" }, HOST_NOTE))];
    }
    function service() {
      const s = data.service, args = { revision: s.revision };
      const dl = [["Workspace", s.workspace], ["Service mode", s.mode === "send" ? "Sending requested; signed gates checked each run" : "Rehearsal; sends nothing"],
        ["Automatic posting", data.sender.enabled ? "Last native decision: enabled" : "Not enabled"], ["Paused", data.paused ? "Yes: no delivery" : "No"],
        ["Check interval", `${s.interval} seconds`], ["Last check", s.last_check ? new Date(s.last_check * 1000).toLocaleString() : "No heartbeat"], ["Installed executable", s.executable || "Not installed"]];
      return [h("section", { class: "set-card" }, h("div", { class: "set-servicestate" }, h("strong", {}, words(s.state)), h("p", { class: "secondary-text" }, s.reason)),
        h("dl", { class: "ps-dl set-dl" }, dl.flatMap(([k, v]) => [h("dt", {}, k), h("dd", {}, v)])),
        h("div", { class: "set-actions" }, act("Start service", "serviceStart", args, { variant: "primary" }), act("Stop service", "serviceStop", args), act("Repair service", "serviceRepair", args), act("Remove service", "serviceRemove", args))),
      h("section", { class: "set-card" }, h("h2", {}, "Posting permission"),
        h("p", {}, "Starting the service begins rehearsal. Enable automatic posting separately when your accounts and exact posts are reviewed."),
        h("div", { class: "set-actions" }, act("Enable automatic posting", "senderEnable", args), act("Disable automatic posting", "senderDisable", args),
          button("Pause delivery", { variant: "secondary", size: "sm", "data-act": "pause", onclick: (e) => busy(e.currentTarget, async () => {
            try { await studio.post("pause", {}); notice.textContent = "Delivery paused immediately."; await load(); } catch (er) { notice.textContent = er.message; } }) }),
          act("Resume delivery", "deliveryResume", {})),
        h("p", { class: "secondary-text" }, SERVICE_NOTE),
        button("Run diagnostics", { variant: "tertiary", size: "sm", iconName: "sync", onclick: (e) => busy(e.currentTarget, load) }))];
    }
    function tools() {
      const t = data.tools;
      const app = (fn, why, done) => (ev) => busy(ev.currentTarget, async () => { try { if (!window.aveNative) throw Error(why); await fn(window.aveNative); if (done) notice.textContent = done; } catch (er) { notice.textContent = er.message; } });
      return [h("div", { class: "set-tools" },
        h("section", { class: "set-card" }, h("h2", {}, "DaVinci Resolve"), h("p", {}, t.resolve.installed ? "Application found." : "Application not found."), h("p", { class: "secondary-text" }, t.resolve.note),
          button("Open Resolve", { variant: "secondary", size: "sm", iconName: "open", onclick: app((n) => n.openResolve(), "Open the desktop app to launch Resolve.") })),
        h("section", { class: "set-card" }, h("h2", {}, "Canva"), h("p", { class: "secondary-text" }, t.canva.note),
          h("a", { class: "btn btn-secondary btn-sm", href: "https://www.canva.com/", target: "_blank", rel: "noreferrer" }, icon("open"), h("span", {}, "Open Canva"))),
        h("section", { class: "set-card" }, h("h2", {}, "Agent connection"), h("p", { class: "secondary-text" }, t.mcp.note),
          h("p", {}, t.mcp.observed?.at ? `Last MCP handshake observed: ${t.mcp.observed.at}. This does not establish an active session now.` : "No MCP handshake observed."),
          button("Preview and save agent configuration", { variant: "secondary", size: "sm", onclick: app((n) => n.agentConfig(), "Open the desktop app to preview and save the installed-executable configuration.", "Configuration saved; connect it in your agent host.") })))];
    }

    // a local profile: no social login, the studio's own profiles API (the old dialog's fields)
    async function editProfile(anchor, id) {
      let rows;
      try { rows = (await studio.get("profiles")).profiles; } catch (e) { toast(plain(e, "read the profiles"), { tone: "negative" }); return; }
      const row = rows.find((p) => p.id === id), p = row?.profile || {};
      const inp = (label, value) => h("input", { class: "input", type: "text", "aria-label": label, value: value || "" });
      const name = inp("Profile display name", p.display_name || row?.label), handle = inp("Profile handle", p.handle), link = inp("Profile website", p.url);
      const bio = h("textarea", { class: "input", rows: "3", "aria-label": "Profile bio" }, p.bio || "");
      const platform = h("select", { class: "input", "aria-label": "Platform", disabled: !!row }, PLATFORMS.map((v) => h("option", { value: v, selected: v === (row?.platform || "instagram") }, v[0].toUpperCase() + v.slice(1))));
      let pick = p.avatar || null;
      const grid = h("div", { class: "set-avatars", hidden: true });
      const chooser = button(pick ? "Change avatar" : "Choose avatar", { variant: "secondary", size: "sm", onclick: async () => {
        grid.hidden = !grid.hidden;
        if (grid.hidden || grid.childElementCount) return;
        try {
          const list = await fetchLibrary(studio, { kind: "image" });
          grid.replaceChildren(...(list.length ? list.slice(0, 24).map((a) => h("button", { type: "button", class: "set-avatar", "aria-pressed": String(a.id === pick), "aria-label": a.alt || a.id,
            onclick: (ev) => { pick = a.id; for (const b of grid.children) b.setAttribute("aria-pressed", String(b === ev.currentTarget)); chooser.textContent = "Avatar chosen"; } },
            h("img", { src: thumbUrl(a), alt: "" }))) : [h("p", { class: "secondary-text" }, "The library has no images yet.")]));
        } catch (e) { grid.replaceChildren(h("p", { class: "ap-error" }, plain(e, "read the library"))); }
      } });
      const err = h("p", { class: "ap-error", role: "alert" });
      const save = button(row ? "Save profile" : "Create profile", { variant: "primary", size: "sm", onclick: (ev) => busy(ev.currentTarget, async () => {
        err.textContent = "";
        try {
          await studio.post(row ? `profiles/${id}/edit` : "profiles", { expected_rev: row?.revision || null, platform: platform.value,
            profile: { display_name: name.value, handle: handle.value, bio: bio.value, url: link.value, avatar: pick } });
          pop.close(); toast(row ? "Profile saved" : "Profile created", { tone: "positive" }); await load();
        } catch (e) { err.textContent = e.message; }
      }) });
      const remove = row ? button("Remove unused local profile", { variant: "tertiary", size: "sm", onclick: (ev) => busy(ev.currentTarget, async () => {
        try { await studio.post(`profiles/${id}/remove`, { expected_rev: row.revision }); pop.close(); selected = null; toast("Profile removed"); await load(); }
        catch (e) { err.textContent = e.message; }
      }) }) : null;
      const pop = popover(anchor, h("div", { class: "set-profileform" }, h("p", { class: "popover-title" }, row ? "Edit local profile" : "New local profile"),
        h("p", { class: "secondary-text" }, "Plan locally with your own images and writing. No social login is needed."),
        field("Display name", name), field("Handle", handle), field("Bio", bio), field("Website", link), field("Platform", platform),
        h("div", { class: "set-avatarrow" }, chooser), grid, err,
        h("div", { class: "popover-actions" }, remove, save)), { label: row ? "Edit local profile" : "New local profile", modal: true, wide: true, align: "end" });
      name.focus();
    }
    load();
    return { tab: (t) => { tab = t || "publishing"; draw(); }, close() {} };
  }

  // ================================================================ Alerts
  function alerts() {
    const list = h("div", { class: "set-alerts", "aria-live": "polite" });
    body.append(list);
    async function load() {
      try {
        const res = await studio.get("alerts");
        if (!alive || current?.id !== "alerts") return;
        const items = res.alerts || [];
        setAlertCount(items.filter((a) => !a.ack).length);
        list.replaceChildren(...(items.length ? items.map(card) : [h("div", { class: "set-empty" }, icon("check"), h("h2", {}, "No alerts."),
          h("p", { class: "secondary-text" }, "If a post fails or a token is about to expire, it shows up here."))]));
      } catch (e) { list.replaceChildren(errorBox(e, load, "read the alerts")); }
    }
    function card(a) {
      const [sev, tone] = SEV[a.severity] || SEV.info;
      const note = h("p", { class: "secondary-text", role: "status" });
      // Acknowledging names you, so the app checks it is you (aveNative.ackAlert). A browser cannot, so it shows the terminal line.
      const app = window.aveNative?.ackAlert ? window.aveNative : null;
      const ack = button(a.ack ? "Acknowledged" : "Acknowledge", { variant: "secondary", size: "sm", disabled: !!a.ack || !app, "data-act": "ack", class: "tip",
        "data-tip": app ? "This Mac checks that it is you" : "Acknowledging needs the AVE Studio app", onclick: (e) => busy(e.currentTarget, async () => {
          try { await app.ackAlert(a.id); toast("Acknowledged"); await load(); }
          catch (ex) { note.textContent = /cancel/i.test(ex.message) ? "Nothing was acknowledged." : ex.message; }
        }) });
      if (!app && !a.ack) note.append("In your own terminal instead: ", h("code", {}, ackCommand(a.id)));
      return h("article", { class: `set-alert sev-${a.severity}`, dataset: { alert: a.id } },
        h("header", {}, h("span", { class: "pill", style: { background: tone, color: "var(--on-solid)" } }, sev), h("strong", {}, words(a.kind).replace(/^./, (c) => c.toUpperCase())),
          h("span", { class: "secondary-text" }, [a.account, a.ts ? when(a.ts) : ""].filter(Boolean).join(" · "))),
        h("p", {}, a.message),
        ADVICE[a.kind] ? h("p", { class: "set-advice" }, h("strong", {}, "What to do: "), ADVICE[a.kind].replace(/Open Connections/g, "Open Accounts")) : null,
        h("footer", { class: "set-actions" },
          a.post ? h("a", { class: "btn btn-secondary btn-sm", href: `#/workspace/post/${encodeURIComponent(a.post)}` }, h("span", {}, "Open the post")) : null,
          h("a", { class: "btn btn-secondary btn-sm", href: `#/settings/accounts${ADVICE_TAB[a.kind] ? "/" + ADVICE_TAB[a.kind] : ""}` }, h("span", {}, "Open Accounts")),
          ack, note));
    }
    load();
    const poll = setInterval(() => { if (document.visibilityState === "visible") load(); }, 20000);
    return { close: () => clearInterval(poll) };
  }
  function setAlertCount(n) {

    const c = navItems.get("alerts").count;
    c.hidden = !n; c.textContent = String(n || "");
  }
  studio.get("alerts").then((r) => setAlertCount((r.alerts || []).filter((a) => !a.ack).length)).catch(() => {});

  // ================================================================ People
  function people() {
    const notice = h("p", { class: "set-notice", role: "status", "aria-live": "polite" });
    const list = h("div", { class: "set-people" });
    const sharing = () => (window.aveNative?.shareProject ? window.aveNative : null);
    const join = button("Join a shared project", { variant: "secondary", size: "sm", iconName: "inbox", disabled: !sharing(), class: "tip",
      "data-tip": sharing() ? "Paste an invitation someone sent you" : NO_APP_SHARE, onclick: (e) => busy(e.currentTarget, () => run((n) => n.joinProject(), (r) => `You joined ${r.title}. Files arrive while you and the others are online.`)) });
    headAction(join);
    body.append(notice, list);
    async function run(fn, said) {
      if (!sharing()) { notice.textContent = NO_APP_SHARE; return; }
      try { const r = await fn(sharing()); toast(said(r), { tone: "positive", timeout: 12000 }); await load(); }
      catch (e) { toast(e.message, { tone: /cancel|stays|nobody was/i.test(e.message) ? undefined : "negative" }); }
    }
    async function load() {
      try {
        const [res, sync] = await Promise.all([api("GET", "/api/workspace/people"), api("GET", "/api/workspace/sync").catch(() => null)]);
        if (!alive || current?.id !== "people") return;
        list.replaceChildren(...(res.projects.length ? res.projects.map((p, i) => project(p, sync ? (sync.projects?.[p.id] || null) : undefined, i)) : [h("div", { class: "set-empty" }, icon("people"),
          h("h2", {}, "No projects yet"), h("p", { class: "secondary-text" }, "Point Claude at a folder in Claude Code. Each project you make or join lists its people here."))]));
      } catch (e) { list.replaceChildren(errorBox(e, load, "read the projects")); }
    }
    function project(p, sync, i) {
      const others = p.members.filter((m) => !m.you);
      const removable = others.filter((m) => !m.publisher);
      const need = sharing() ? null : NO_APP_SHARE;
      const share = button("Share", { variant: "primary", size: "sm", iconName: "invite", disabled: !!need, class: "tip", "data-tip": need || `Share ${p.title} with a person`,
        onclick: (e) => busy(e.currentTarget, () => run((n) => n.shareProject(p.id), (r) => `${r.person} is on ${r.title} now. Their computer starts receiving it while you are both online.`)) });
      const more = button(`More for ${p.title}`, { variant: "tertiary", size: "sm", iconName: "dots", iconOnly: true, "aria-haspopup": "menu", onclick: (e) => menu(e.currentTarget, [
        { label: "Remove someone from this project", icon: "person", disabled: !!need || !removable.length, note: need || (removable.length ? null : "Nobody here can be removed: the computer that publishes stays until publishing is handed over."),
          onSelect: () => run((n) => n.removeMember(p.id), (r) => r.message) },
        { label: "Hand over publishing", icon: "send", disabled: !!need || !others.length, note: need || (others.length ? null : "Nobody else is on this project."),
          onSelect: () => run((n) => n.handOverPublishing(p.id), (r) => r.message) },
      ], { label: `More for ${p.title}`, align: "end" }) });
      return h("section", { class: "set-project", dataset: { project: p.id }, style: { "--group": `var(--group-${(i % 8) + 1})` }, "aria-label": p.title },
        h("header", {}, h("span", { class: "rail-swatch", "aria-hidden": "true" }, (p.title || "?")[0].toUpperCase()), h("h2", {}, p.title),
          sync === undefined ? h("span", { class: "chip" }, "Sync state unknown")
            : sync && sync.state !== "not_shared" ? h("span", { class: `pill sync-pill sync-${sync.tone}` }, h("span", { class: "sync-text" }, sync.text)) : h("span", { class: "chip" }, "Only on this computer"),
          h("span", { class: "set-grow" }), share, more),
        h("table", { class: "table set-members" }, h("thead", {}, h("tr", {}, h("th", {}, "Person"), h("th", {}, "Keeps"), h("th", {}, "Publishing"), h("th", {}, "Computers"))),
          h("tbody", {}, p.members.map((m) => h("tr", { dataset: { person: m.person } },
            h("td", {}, h("span", { class: "set-who" }, avatar(m.person), h("span", {}, m.person), m.you ? h("span", { class: "chip" }, "You") : null)),
            h("td", {}, m.media),
            h("td", {}, m.publisher ? h("span", { class: "pill", style: { background: "var(--stage-out)", color: "var(--on-solid)" } }, "Publishes") : h("span", { class: "secondary-text" }, "No")),
            h("td", {}, String(m.computers)))))));
    }
    load();
    return { close() {} };
  }

  // ================================================================ Collaboration (the editorial exchange; the old Collaborate screen)
  function collaboration() {
    let data = null, chosen = new Set(), review = null, timer = 0;
    const notice = h("p", { class: "set-notice", role: "status", "aria-live": "polite" });
    const pane = h("div", { class: "set-pane" });
    body.append(notice, pane);
    const nb = (label, action, { variant = "secondary", disabled = false } = {}) => button(label, { variant, size: "sm", disabled, "data-native": action,
      onclick: (e) => busy(e.currentTarget, async () => { if (await native((app) => app.connectionAction(action, {}), { notice, args: { action }, app: window.aveNative?.connectionAction ? window.aveNative : null })) await load(); }) });
    const call = (label, fn, { variant = "secondary", disabled = false } = {}) => button(label, { variant, size: "sm", disabled,
      onclick: (e) => busy(e.currentTarget, async () => { try { await fn(); notice.textContent = ""; } catch (er) { notice.textContent = er.message; } }) });
    async function load() {
      try { data = await studio.get("collaboration"); if (alive && current?.id === "collaboration" && !review) draw(); }
      catch (e) { if (!data) pane.replaceChildren(errorBox(e, load, "read the exchange")); else notice.textContent = e.message; }
    }
    const kb = (n) => (n || 0).toLocaleString();
    function draw() {
      const d = data;
      pane.replaceChildren(...[
        h("section", { class: "set-card" }, h("p", { class: "set-info" }, icon("info"), h("span", {}, d.scope)),
          h("p", {}, d.enabled ? `Sharing is on, on ${d.interface}.` : "Sharing is stopped. Queued work is kept."),
          h("div", { class: "set-actions" }, nb(d.enabled ? "Stop sharing" : "Enable sharing", d.enabled ? "exchangeStop" : "exchangeEnable", { variant: d.enabled ? "secondary" : "primary" }),
            nb("Invite collaborator", "exchangeInvite", { disabled: !d.enabled || !!d.peer }), nb("Add collaborator", "exchangeJoin", { disabled: !d.enabled || !!d.peer })),
          d.error ? h("p", { class: "notice notice-negative", role: "alert" }, icon("alert"), h("span", {}, words(d.error))) : null),
        d.peer ? h("section", { class: "set-card collab-peer" }, h("h2", {}, "Paired collaborator"), h("p", {}, `Device ${d.peer.identity.node} · User ${d.peer.identity.user}`),
          h("div", { class: "set-actions" }, nb("Revoke collaborator", "exchangeRevoke"))) : null,
        d.pairing ? h("section", { class: "set-card collab-pairing" }, h("h2", {}, "Compare before pairing"), h("strong", { class: "set-code" }, d.pairing.code),
          h("p", {}, `Device ${d.pairing.peer.identity.node} · User ${d.pairing.peer.identity.user}`),
          h("p", { class: "secondary-text" }, "Compare the code and identities through your trusted channel. Both people must confirm."),
          h("div", { class: "set-actions" }, nb("Confirm exact pairing", "exchangeConfirm", { variant: "primary" }), nb("Cancel pairing", "exchangeCancelPair"))) : null,
        h("section", { class: "set-card" }, h("h2", {}, "Choose drafts to share"),
          d.posts.length ? h("ul", { class: "set-choices" }, d.posts.map((p) => {
            const c = h("input", { type: "checkbox", checked: chosen.has(p.id), onchange: () => (c.checked ? chosen.add(p.id) : chosen.delete(p.id)) });
            return h("li", {}, h("label", { class: "set-check" }, c, h("span", {}, draftName(p))));
          })) : h("p", { class: "secondary-text" }, "No drafts yet."),
          h("div", { class: "set-actions" }, call("Preview outgoing bundle", async () => outgoing(await studio.post("collaboration/prepare-send", { posts: [...chosen], visuals: [], media_ids: [] })), { disabled: !d.peer }),
            d.peer ? null : h("span", { class: "secondary-text" }, "Pair a collaborator first."))),
        h("section", { class: "set-card" }, h("h2", {}, "Outbox"),
          d.outbox.length ? h("ul", { class: "set-transfers" }, d.outbox.map((r) => h("li", {}, h("strong", {}, words(r.state)), h("span", { class: "secondary-text" }, r.attempts === 1 ? "Sent once" : `Tried ${r.attempts} times`),
            r.error ? h("span", { class: "ap-error" }, words(r.error)) : null,
            !["received", "cancelled"].includes(r.state) ? call("Cancel future attempts", async () => { await studio.post("collaboration/cancel", { digest: r.sha256 }); await load(); }) : null)))
            : h("p", { class: "secondary-text" }, "No bundles sent yet.")),
        h("section", { class: "set-card" }, h("h2", {}, "Inbox"),
          d.inbox.length ? h("ul", { class: "set-transfers" }, d.inbox.map((r) => h("li", {}, h("strong", {}, words(r.review_state)), h("span", { class: "secondary-text" }, `From ${d.peer ? d.peer.identity.user : "your collaborator"} · ${kb(r.bytes)} bytes`),
            call("Review changes", async () => incoming(await studio.post("collaboration/preview", { digest: r.transport_sha256 }))))))
            : h("p", { class: "secondary-text" }, "No incoming bundles."),
          d.conflicts.map((c) => h("div", { class: "set-conflict" }, h("h3", {}, c.post), h("p", { class: "secondary-text" }, c.reason),
            call("Resolve conflict", async () => conflict(await studio.get("collaboration/conflict/" + encodeURIComponent(c.id))))))),
      ].filter(Boolean));
    }
    const content = (v) => h("pre", { class: "set-pre" }, v ? JSON.stringify(v, null, 2) : "No local draft");
    const columns = (row) => h("div", { class: "set-columns" }, ["base", "local", "incoming"].map((k) => h("section", {}, h("h3", {}, words(k)), content(row[k]))));
    function surface(title, ...kids) {
      const box = h("section", { class: "set-card set-review", tabindex: "-1", "aria-label": title }, h("h2", {}, title), ...kids);
      pane.replaceChildren(box); box.focus();
      return box;
    }
    const back = () => call("Back", async () => { review = null; draw(); });
    function outgoing(v) {
      review = v;
      surface("Review outgoing bundle", h("p", {}, `This sends ${v.drafts.length} drafts and ${kb(v.media_bytes)} selected media bytes to ${v.peer.identity ? `${v.peer.identity.user} on ${v.peer.identity.node}` : "your collaborator"}.`),
        h("p", { class: "secondary-text" }, "No approvals, accounts, schedules, licences or raw recordings are included."),
        v.drafts.map((p) => h("details", {}, h("summary", {}, p.id), content(p.content))),
        h("div", { class: "set-actions" }, call("Send bundle", async () => { await studio.post("collaboration/send", { token: v.token }); review = null; await load(); }, { variant: "primary" }), back()));
    }
    function incoming(v) {
      review = v;
      surface("Review incoming changes", h("p", { class: "secondary-text" }, "Apply creates or updates unambiguous drafts and keeps conflicts for your decision. Received media stays untrusted."),
        v.comparisons.map((row) => [h("h3", {}, row.post), row.protected ? h("p", { class: "secondary-text" }, "Publishing history is protected. Create a separate draft to use incoming work.") : null, columns(row)]),
        (v.result.dependencies || []).map((dep) => h("p", {}, `${dep.asset || dep.document.id}: ${dep.missing.length ? dep.missing.join("; ") : "Exact dependencies present; local licensing and review still required"}`)),
        h("div", { class: "set-actions" }, call("Apply reviewed bundle", async () => { await studio.post("collaboration/apply", { token: v.token }); review = null; await load(); }, { variant: "primary" }), back()));
    }
    function conflict(c) {
      review = c;
      const resolve = async (take, text) => { await studio.post("collaboration/resolve", { id: c.id, take, expected_rev: c.current_rev, ...(text === undefined ? {} : { text }) }); review = null; await load(); };
      const merged = h("textarea", { class: "input", rows: "8", "aria-label": "Merged draft copy" }, c.current?.copy?.base || "");
      surface("Resolve " + c.post, h("p", { class: "secondary-text" }, c.reason), columns({ ...c, local: c.current }),
        c.protected ? h("div", { class: "set-actions" }, call("Keep local", () => resolve("local")), call("Create separate draft from incoming", () => resolve("separate")), back())
          : [h("label", { class: "field" }, h("span", { class: "field-label" }, "Merged draft copy"), merged),
            h("div", { class: "set-actions" }, call("Keep local", () => resolve("local")), call("Use incoming", () => resolve("incoming")), call("Save merged draft", () => resolve("merge", merged.value), { variant: "primary" }), back())]);
    }
    load();
    timer = setInterval(() => { if (!review && document.visibilityState === "visible") load(); }, 2500);
    return { close: () => clearInterval(timer) };
  }

  // ---------------------------------------------------------------- shared bits
  function when(ts) {
    try { return new Intl.DateTimeFormat("en-GB", { weekday: "short", day: "numeric", month: "short", hour: "2-digit", minute: "2-digit" }).format(new Date(ts)); }
    catch { return String(ts); }
  }
  function field(label, input) { return h("label", { class: "field" }, h("span", { class: "field-label" }, label), input); }
  function errorBox(e, retry, doing) {
    return h("div", { class: "ws-error", role: "alert" }, h("h2", {}, "This could not be read"), h("p", { class: "secondary-text" }, plain(e, doing)),
      button("Try again", { variant: "primary", onclick: (ev) => busy(ev.currentTarget, retry) }));
  }

  setRoute(route);
  return { setRoute, close: () => { alive = false; current?.close?.(); current = null; } };
}
