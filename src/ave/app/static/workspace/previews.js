// Previews: what the posts will look like, like Monday's Cards view. "Cards" shows every channel of every post as a card with
// its picture, status, channel and time. "Feed" shows one account's profile grid in the planned order on a phone, the old visual
// planner's view. Any card or tile opens the post sheet (posts.js): the platform preview at full size, its slides, why it has its
// status and its time controls. Previews never change the words or pictures; that happens in the Room's Layout view. Changes
// arrive as `posts` events on the Workspace stream (ctx.onPosts); nothing here polls.

import { h, button, icon, toast, busy } from "../dom.js";
import { menu, plain } from "./common.js";
import { statusPill, statusTone, statusWord, platformMark, platformWord } from "./posts.js";
import { localUrl } from "/app/shared/panels.js";
import { humanWhen, postName, slotLabel } from "/app/shared/plan.js";


export function createPreviews(ctx, studio, sheet) {
  const { S } = ctx;
  const P = (S.pv ||= { mode: "cards", account: null });
  let posts = null, feed = null, error = null, seq = 0, alive = false;

  const seg = h("div", { class: "seg-toggle", role: "group", "aria-label": "Previews" },
    [["cards", "Cards"], ["feed", "Feed"]].map(([m, l]) => h("button", { type: "button", class: "seg-opt", dataset: { mode: m }, "aria-pressed": String(P.mode === m), onclick: () => setMode(m) }, l)));
  const acctBtn = h("button", { type: "button", class: "btn btn-tertiary btn-tool", "aria-haspopup": "menu", "aria-expanded": "false", onclick: (e) => accountMenu(e.currentTarget) },
    icon("person"), h("span", { class: "tool-label" }, "All accounts"));
  const sub = h("p", { class: "secondary-text pv-sub" });
  const host = h("div", { class: "pvw-host" });
  const el = h("div", { class: "pvw", "aria-busy": "false" }, h("div", { class: "cal-bar" }, h("div", { class: "cal-nav" }, sub),
    h("div", { class: "cal-tools" }, acctBtn, seg)), host);

  const save = () => ctx.savePrefs();
  function setMode(m) { if (P.mode === m) return; P.mode = m; save(); load(); }
  const accounts = () => studio.st.accounts;
  const feedAccount = () => (P.account && accounts().some((a) => a.id === P.account) ? P.account : (accounts().find((a) => a.platform === "instagram") || accounts()[0] || {}).id || "");
  function accountMenu(anchor) {
    const pick = (id) => { P.account = id; save(); load(); };
    const list = accounts().map((a) => ({ label: a.label || a.id, lead: platformMark(a.platform), checked: (P.mode === "feed" ? feedAccount() : P.account) === a.id, onSelect: () => pick(a.id) }));
    menu(anchor, P.mode === "feed" ? list : [{ label: "All accounts", checked: !P.account, onSelect: () => pick(null) }, "-", ...list],
      { label: "Account", heading: P.mode === "feed" ? "Whose feed" : "Show posts for", align: "end" });
  }
  const postsHere = () => !S.route.project || ctx.projects.get(S.route.project)?.kind === "studio";
  const passQ = (t) => S.q.split(/\s+/).filter(Boolean).every((w) => String(t || "").toLowerCase().includes(w));

  async function load({ quiet = false } = {}) {
    const my = ++seq;
    el.setAttribute("aria-busy", "true");
    if (!quiet) paint();
    try {
      await studio.ready();
      if (P.mode === "feed") feed = await studio.api.get(`feed-plan?account=${encodeURIComponent(feedAccount())}`);
      else posts = (await studio.api.get("posts?thumbs=1")).posts || [];
      if (my !== seq) return;
      error = null;
    } catch (e) {
      if (my !== seq) return;
      error = e;
      if (quiet) toast(plain(e, "read the posts"), { tone: "negative" });
    } finally {
      if (my === seq) { el.setAttribute("aria-busy", "false"); if (alive) paint(); }
    }
  }
  function cardsOf() {
    const out = [];
    for (const p of posts || []) {
      if (p.stage === "dropped" || p.stage === "archived") continue;
      for (const v of p.variants || []) {
        if (v.dropped) continue;
        if (P.account && v.account !== P.account) continue;
        const title = postName(p, v);
        if (!passQ(title)) continue;
        out.push({ p, v, title, at: v.schedule ? v.schedule.local : null });
      }
    }
    return out.sort((a, b) => (a.at || "9999").localeCompare(b.at || "9999") || a.title.localeCompare(b.title));
  }

  function paint() {
    if (!alive) return;
    for (const b of seg.children) b.setAttribute("aria-pressed", String(b.dataset.mode === P.mode));
    acctBtn.querySelector(".tool-label").textContent = P.mode === "feed" ? studio.accountLabel(feedAccount()) || "Account" : P.account ? studio.accountLabel(P.account) : "All accounts";
    acctBtn.classList.toggle("is-on", P.mode === "cards" && !!P.account);
    if (!postsHere()) {
      sub.textContent = "";
      host.replaceChildren(h("div", { class: "ws-empty pvw-empty" }, h("h2", {}, "Posts are planned in the studio"),
        h("p", { class: "secondary-text" }, `${ctx.projects.get(S.route.project)?.title || "This project"} has no posts of its own. Open All artifacts to see the studio's previews.`),
        button("Show all artifacts", { variant: "primary", onclick: () => ctx.go("#/workspace") })));
      return;
    }
    if (error && !(P.mode === "feed" ? feed : posts)) {
      sub.textContent = "";
      host.replaceChildren(h("div", { class: "ws-error", role: "alert" }, h("h2", {}, "The previews could not load"),
        h("p", { class: "secondary-text" }, `${error.message || "The studio did not answer."} Your posts are safe on disk.`),
        button("Try again", { variant: "primary", onclick: (e) => busy(e.currentTarget, () => load()) })));
      return;
    }
    if (P.mode === "feed") return paintFeed();
    if (!posts) { host.replaceChildren(skeleton()); sub.textContent = ""; return; }
    const cards = cardsOf();
    sub.textContent = `${cards.length} ${cards.length === 1 ? "post" : "posts"}${P.account ? ` on ${studio.accountLabel(P.account)}` : " on every account"}. Approximate: each platform draws its own.`;
    if (!cards.length) {
      const filtered = !!S.q || !!P.account;
      host.replaceChildren(h("div", { class: "ws-empty pvw-empty", role: "status" }, h("h2", {}, filtered ? "Nothing matches these filters" : "No posts yet"),
        h("p", { class: "secondary-text" }, filtered ? "No post fits the search and account you chose." : "Ask Claude for posts. Each one appears here as a card as soon as it has a channel."),
        filtered ? button("Clear filters", { variant: "primary", onclick: () => { P.account = null; save(); ctx.clearSearch(); load(); } }) : null));
      return;
    }
    host.replaceChildren(h("ul", { class: "pvw-cards", "aria-label": "Post previews" }, cards.map(card)));
  }
  function card({ p, v, title, at }) {
    const thumb = localUrl(v.thumb || "");
    const acct = studio.account(v.account);
    return h("li", {}, h("button", { type: "button", class: "card pvw-card", dataset: { post: p.id, variant: v.id }, style: { "--tone": statusTone(v.status) },
      "aria-label": `${title}. ${statusWord(v.status)}. ${studio.accountLabel(v.account)}. ${at ? humanWhen(at) : "Not scheduled"}`,
      onclick: () => sheet.open({ post: p.id, variant: v.id, title, account: v.account, status: v.status }, { changed: () => load({ quiet: true }) }) },
      h("span", { class: "pvw-cover" }, thumb ? h("img", { src: thumb, alt: "", loading: "lazy", decoding: "async" }) : h("span", { class: "pvw-noimg" }, icon("grid"), "No picture yet"),
        (p.media || []).length > 1 || (v.media || []).length > 1 ? h("span", { class: "pvw-count" }, `${Math.max((p.media || []).length, (v.media || []).length)} slides`) : null),
      h("span", { class: "pvw-body" },
        h("span", { class: "pvw-title" }, title),
        h("span", { class: "pvw-fields" },
          h("span", { class: "pvw-field" }, h("span", { class: "pvw-label" }, "Status"), statusPill(v.status)),
          h("span", { class: "pvw-field" }, h("span", { class: "pvw-label" }, "Channel"), h("span", { class: "pvw-chan" }, platformMark(v.platform), acct ? acct.label || acct.id : v.account)),
          h("span", { class: "pvw-field" }, h("span", { class: "pvw-label" }, "When"), h("span", {}, at ? slotLabel(at) : "Not scheduled"))))));
  }
  function paintFeed() {
    if (!feed) { host.replaceChildren(skeleton(true)); sub.textContent = ""; return; }
    const acct = studio.account(feedAccount()) || feed.account || {};
    const cells = (feed.cells || []).filter((c) => passQ(c.title));
    sub.textContent = `${acct.label || acct.id || "This account"}'s profile grid in the planned order, newest first. Approximate.`;
    const tiles = cells.map((c) => {
      const src = localUrl(c.thumb || c.media_url || "");
      return h("li", {}, h("button", { type: "button", class: "pvw-tile", dataset: { post: c.post, variant: c.variant || "" }, title: c.title,
        "aria-label": `${c.title}. ${statusWord(c.status)}. ${c.at ? humanWhen(c.at) : "Not scheduled"}`,
        onclick: () => c.external ? null : sheet.open({ post: c.post, variant: c.variant, title: c.title, account: feedAccount(), status: c.status }, { changed: () => load({ quiet: true }) }) },
        src ? h("img", { src, alt: "", loading: "lazy", decoding: "async" }) : h("span", { class: "pvw-noimg" }, "No picture"),
        h("span", { class: "pvw-tilestate", style: { background: statusTone(c.status) } }, statusWord(c.status)),
        c.media_count > 1 ? h("span", { class: "pvw-multi", "aria-hidden": "true" }, icon("grid")) : null));
    });
    const phone = h("div", { class: "pvw-phone", role: "region", "aria-label": `Planned ${platformWord(acct.platform || "instagram")} profile` },
      h("div", { class: "pvw-profile" }, h("span", { class: "avatar avatar-lg pvw-pavatar" }, (acct.label || acct.id || "?").replace(/^@/, "").slice(0, 1).toUpperCase()),
        h("div", {}, h("p", { class: "pvw-handle" }, acct.label || acct.id || "This account"), h("p", { class: "secondary-text" }, `${cells.length} planned ${cells.length === 1 ? "post" : "posts"}`))),
      cells.length ? h("ul", { class: "pvw-grid", "aria-label": "Profile grid" }, tiles)
        : h("p", { class: "pvw-feedempty secondary-text" }, S.q ? "Nothing matches the search." : "Nothing is planned for this account yet."));
    host.replaceChildren(h("div", { class: "pvw-feed" }, phone,
      h("aside", { class: "pvw-feedside" }, h("h3", {}, "How to read the grid"),
        h("p", { class: "secondary-text" }, "The newest post sits top left, as it will on the profile. Each tile shows the crop the grid uses, so faces and words near the edges may be cut."),
        h("p", { class: "secondary-text" }, "Open a tile to see the post itself, its slides and its time."))));
  }
  function skeleton(feedMode = false) {
    if (feedMode) return h("div", { class: "pvw-feed", "aria-hidden": "true" }, h("div", { class: "pvw-phone" }, h("div", { class: "pvw-grid is-skel" },
      Array.from({ length: 9 }, () => h("span", { class: "skeleton" })))));
    return h("ul", { class: "pvw-cards is-skel", "aria-hidden": "true" }, Array.from({ length: 8 }, () => h("li", {}, h("div", { class: "card pvw-card" },
      h("span", { class: "pvw-cover skeleton" }), h("span", { class: "pvw-body" }, h("span", { class: "skeleton skeleton-text" }), h("span", { class: "skeleton skeleton-text", style: { width: "60%" } }))))));
  }

  ctx.onPosts(() => { if (alive) load({ quiet: true }); });           // Claude's or anyone's post change, from the Workspace stream

  return {
    el,
    render() {
      if (!alive) { alive = true; load(); }
      else paint();
    },
    update() { paint(); return true; },
    leave() { alive = false; seq++; },
    reload: () => load({ quiet: true }),
  };
}
