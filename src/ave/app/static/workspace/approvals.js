// The approval sheet: what waits on a person's approval, and the way to give it. Two kinds live here.
//
//   Posts to approve   the studio's posts in review. The sheet shows exactly what an approval signs, read from the queue item
//                      (caption, first comment, link, hashtags, alt text, every file and its sha256), and how it differs from
//                      the post when it does (then it cannot be approved). After the exact files have been on screen for two
//                      seconds, "Approve on this Mac" hands the post's variant and that sha to the app (aveNative.approvePublish).
//   Trims to confirm   Claude's draft trims (in and out of each session). "Confirm the trim on this Mac" hands the project and the
//                      session to the app (aveNative.approveTrim).
//
// The app then shows the exact words again in a native sheet, runs the existing device-owner check (Touch ID or the Mac's
// password) and asks for the PIN it shows there. The page never sees or sends a PIN, a name or an authority, and an agent can
// never approve: an agent marker refuses both sheets. In a browser there is no app to hand the approval to, so the buttons say
// so, and the terminal command a person runs instead is shown. Nothing here sends a post: it goes out at its time from the
// publishing computer. `ave sequence approve` and the other human acts stay in the terminal.

import { h, button, icon, toast, busy, popover } from "../dom.js";
import { api, plain, menu } from "./common.js";
import { findVariant, PLATFORM_NAMES, localUrl, groupSha } from "/app/shared/panels.js";
import { flagInfo } from "/app/shared/dom.js";
import { postName, humanWhen } from "/app/shared/plan.js";
import { buildFeedbackBody, KINDS as SEND_BACK } from "/app/shared/feedback.js";

export const VIEW_MS = 2000;                 // the studio's own rule (studio/ui/api.py MIN_VIEW_MS); the native sheet measures it again
const FIELD = { caption: "Caption", first_comment: "First comment", link_url: "Link", link: "Link", tags: "Hashtags", hashtags: "Hashtags",
  alt_texts: "Alt text", account: "Account", post_type: "Post type", files: "Files", ai_disclosure: "AI disclosure", post: "Post" };
const isVideo = (name) => /\.(mp4|mov|m4v|webm)$/i.test(name || "");
const NO_APP = "Approving needs the AVE Studio app on the publishing computer: a browser cannot check that it is you.";
const NO_APP_TRIM = "Confirming a trim needs the AVE Studio app: a browser cannot check that it is you.";
const secs = (s) => {
  if (s == null) return "the recording's own";
  const m = Math.floor(s / 60), r = s - m * 60;
  return `${m}:${r.toFixed(2).padStart(5, "0")}`;
};

export function createApprovals(ctx, studio) {
  const el = h("aside", { class: "side-panel approval-sheet", "aria-label": "Approvals", hidden: true, tabindex: "-1" });
  const crumb = h("p", { class: "ws-panel-crumb" });
  const titleEl = h("h2", { id: "ap-title" }, "Waiting for your approval");
  const meta = h("div", { class: "ws-panel-meta" });
  const body = h("div", { class: "ws-panel-body ap-body" });
  const backBtn = button("All approvals", { variant: "tertiary", size: "sm", iconName: "right", class: "ap-back", hidden: true, onclick: () => showList() });
  const closeBtn = button("Close", { variant: "tertiary", size: "sm", iconName: "x", iconOnly: true, onclick: () => close() });
  el.append(h("div", { class: "ws-panel-head" }, h("div", { class: "ws-panel-top" }, crumb, h("div", { class: "ws-panel-tools" }, backBtn, closeBtn)), titleEl, meta), body);
  el.addEventListener("keydown", (e) => {
    if (document.querySelector(".popover")) return;
    if (e.key === "Escape") { e.preventDefault(); e.stopPropagation(); close(); return; }
    if (e.target.closest("input, textarea, select, [contenteditable]")) return;
    if (e.key === "j" || e.key === "k") { e.preventDefault(); step(e.key === "j" ? 1 : -1); }
    else if (e.key === "n") { e.preventDefault(); e.stopPropagation(); nextUnseen(); }
  });
  document.body.append(el);

  // seen: posts whose exact files were on screen for 2 s (key -> sha); picked: the ones ticked for one approval together (the old
  // Review page's batch); results: what the last approval said about each post, kept until it leaves the list.
  const S = { posts: [], postsWaiting: 0, trims: [], content: [], episodePosts: [], accounts: {}, campaigns: [], native: false, loaded: false, error: null, cur: null, seen: new Map(),
    picked: new Set(), results: new Map(),
    filter: { brand: null, campaign: null }, timer: 0, token: 0, onClose: null };
  const listeners = new Set();
  let opener = null;
  const app = () => window.aveNative || null;
  const keyOf = (it) => (it.kind === "publish" ? `p:${it.post}/${it.variant}` : it.kind === "content" ? `c:${it.project}/${it.doc}/${it.id}`
    : it.kind === "episode_post" ? `e:${it.project}/${it.id}` : `t:${it.project}/${it.session}`);
  const takeEpisodes = (r) => { S.content = (r.content || []).map((x) => ({ ...x, kind: "content" })); S.episodePosts = (r.episode_posts || []).map((x) => ({ ...x, kind: "episode_post" })); };

  // ---------------------------------------------------------------- reading
  // Closed, the sheet reads only the Workspace's own summary (Claude's trims and how many posts wait), so an idle Workspace never
  // reads the studio. Opening it reads the posts themselves; while it is open, a `posts` event reads them again.
  let summing = null;
  function refresh() {
    if (!el.hidden) return load();
    summing ||= api("GET", "/api/workspace/approvals").then((r) => {
      S.trims = (r.trims || []).map((t) => ({ kind: "trim", ...t })); S.postsWaiting = r.posts_waiting || 0; S.native = !!r.native; takeEpisodes(r);
      for (const fn of listeners) fn();
    }).catch(() => {}).finally(() => { summing = null; });
    return summing;
  }
  let reading = null;
  async function load() {
    if (reading) return reading;
    reading = (async () => {
      try {
        const [posts, trims, accounts, campaigns] = await Promise.all([
          studio.api.get("posts?status=in_review&thumbs=1").catch((e) => (e.status === 404 ? { posts: [] } : Promise.reject(e))),
          api("GET", "/api/workspace/approvals"),
          studio.api.get("accounts").catch(() => ({ accounts: [] })),
          studio.api.get("campaigns").catch(() => ({ campaigns: [] }))]);
        S.accounts = Object.fromEntries((accounts.accounts || []).map((a) => [a.id, a]));
        S.campaigns = (campaigns.campaigns || []).map((c) => ({ id: c.campaign.id, name: c.campaign.name }));
        S.posts = [];
        for (const p of posts.posts || []) for (const v of p.variants || []) if (v.status === "in_review")
          S.posts.push({ kind: "publish", post: p.id, variant: v.id, title: postName(p, v), platform: v.platform, account: v.account,
            post_type: v.post_type, thumb: v.thumb || null, campaign: p.campaign || null, flags: v.flags || [], when: v.schedule ? v.schedule.local : null });
        S.trims = (trims.trims || []).map((t) => ({ kind: "trim", ...t }));
        takeEpisodes(trims);
        S.postsWaiting = S.posts.length;
        const here = new Set(S.posts.map(keyOf));
        for (const k of [...S.picked]) if (!here.has(k)) S.picked.delete(k);
        S.native = !!trims.native;
        S.loaded = true; S.error = null;
      } catch (e) { S.error = e; }
      finally { reading = null; }
      for (const fn of listeners) fn();
      return S;
    })();
    return reading;
  }
  const brandOf = (it) => (S.accounts[it.account] || {}).brand || null;
  const shown = () => S.posts.filter((it) => (S.filter.brand === null || brandOf(it) === S.filter.brand) && (!S.filter.campaign || it.campaign === S.filter.campaign));
  const all = () => [...shown(), ...S.trims, ...S.content, ...S.episodePosts];
  const count = () => S.postsWaiting + S.trims.length + S.content.length + S.episodePosts.length;

  // ---------------------------------------------------------------- opening and closing
  async function open(focus = null, { onClose } = {}) {
    if (el.hidden) opener = document.activeElement;
    S.onClose = onClose || null;
    el.hidden = false;
    el.focus({ preventScroll: true });
    if (!S.loaded || focus) { body.replaceChildren(skeleton()); await load(); }
    if (focus) {
      const it = [...S.posts, ...S.trims, ...S.content, ...S.episodePosts].find((x) => (focus.post ? x.post === focus.post && (!focus.variant || x.variant === focus.variant)
        : x.project === focus.project && x.session === focus.session));
      if (it) return showItem(it);
    }
    showList();
  }
  function close() {
    if (el.hidden) return;
    stopTimer(); S.token++;
    el.hidden = true; S.cur = null;
    if (opener?.isConnected) opener.focus({ preventScroll: true });
    S.onClose?.();
  }
  const skeleton = () => h("div", { class: "ps-skel", "aria-hidden": "true" }, h("span", { class: "skeleton" }), h("span", { class: "skeleton" }), h("span", { class: "skeleton" }));

  // ---------------------------------------------------------------- the list
  function showList() {
    stopTimer(); S.cur = null; S.token++;
    backBtn.hidden = true;
    crumb.replaceChildren(icon("lock"), h("span", {}, "Approvals"));
    titleEl.textContent = "Waiting for your approval";
    el.setAttribute("aria-label", "Approvals");
    meta.replaceChildren(h("span", { class: "secondary-text" }, count() ? `${count()} waiting` : "Nothing waiting"));
    if (S.error) {
      body.replaceChildren(h("div", { class: "ws-error", role: "alert" }, h("h2", {}, "The approvals could not be read"),
        h("p", { class: "secondary-text" }, plain(S.error, "read the approvals")),
        button("Try again", { variant: "primary", onclick: (e) => busy(e.currentTarget, () => load().then(showList)) })));
      return;
    }
    const brands = [...new Set(Object.values(S.accounts).map((a) => a.brand).filter(Boolean))];
    const filters = (brands.length || S.campaigns.length) && S.posts.length ? h("div", { class: "ap-filters" },
      brands.length ? button(S.filter.brand ? `Brand: ${S.filter.brand}` : "All brands", { variant: "tertiary", size: "sm", iconName: "filter", "aria-haspopup": "menu",
        onclick: (e) => menu(e.currentTarget, [{ label: "All brands", checked: S.filter.brand === null, onSelect: () => { S.filter.brand = null; showList(); } },
          ...brands.map((b) => ({ label: b, checked: S.filter.brand === b, onSelect: () => { S.filter.brand = b; showList(); } }))], { label: "Brand" }) }) : null,
      S.campaigns.length ? button(S.filter.campaign ? `Campaign: ${S.campaigns.find((c) => c.id === S.filter.campaign)?.name}` : "All campaigns", { variant: "tertiary", size: "sm",
        iconName: "group", "aria-haspopup": "menu", onclick: (e) => menu(e.currentTarget, [{ label: "All campaigns", checked: !S.filter.campaign, onSelect: () => { S.filter.campaign = null; showList(); } },
          ...S.campaigns.map((c) => ({ label: c.name, checked: S.filter.campaign === c.id, onSelect: () => { S.filter.campaign = c.id; showList(); } }))], { label: "Campaign" }) }) : null) : null;
    const posts = shown();
    const hiddenN = S.posts.length - posts.length;
    const postRow = (it) => {
      const k = keyOf(it), seen = S.seen.has(k), res = S.results.get(k);
      const tick = h("button", { type: "button", class: "check ap-tick", role: "checkbox", "aria-checked": String(S.picked.has(k)), disabled: !seen,
        "aria-label": `Include ${it.title} in one approval`, title: seen ? null : "Look at it first: open it and keep its files on screen for 2 seconds",
        onclick: () => { S.picked.has(k) ? S.picked.delete(k) : S.picked.add(k); showList(); } }, icon("check"));
      return h("li", { class: "ap-row" }, tick, h("button", { type: "button", class: "ap-item", dataset: { kind: "publish", variant: it.variant }, onclick: () => showItem(it) },
      h("span", { class: "ap-thumb" }, it.thumb ? h("img", { src: localUrl(it.thumb), alt: "", loading: "lazy" }) : icon("grid")),
      h("span", { class: "ap-itext" }, h("span", { class: "ap-ititle" }, it.title),
        h("span", { class: "secondary-text" }, [`${PLATFORM_NAMES[it.platform] || it.platform} ${String(it.post_type || "").replace(/_/g, " ")}`, (S.accounts[it.account] || {}).label || it.account].join(" · "))),
      h("span", { class: `chip ap-state ${res ? "is-refused" : it.flags.includes("approval_stale") ? "is-stale" : seen ? "is-seen" : ""}`, title: res || null },
        res ? "Not approved" : it.flags.includes("approval_stale") ? "Approve again" : seen ? "Looked at" : "Not looked at"),
      icon("right")), res ? h("p", { class: "ap-result", role: "status" }, res) : null);
    };
    const picked = posts.filter((it) => S.picked.has(keyOf(it)));
    const batchErr = h("p", { class: "ap-error", role: "alert" });
    const batch = picked.length ? h("div", { class: "ap-actions ap-batch" },
      button(picked.length === 1 ? "Approve 1 post on this Mac" : `Approve ${picked.length} posts on this Mac`, { variant: "primary", iconName: "lock", "data-act": "approve-batch",
        disabled: !app()?.approvePublish, onclick: (e) => approveItems(e.currentTarget, picked, batchErr) }),
      button("Clear", { variant: "tertiary", size: "sm", onclick: () => { S.picked.clear(); showList(); } }),
      app()?.approvePublish ? null : h("span", { class: "secondary-text" }, NO_APP), batchErr) : null;
    const trimRow = (it) => h("li", {}, h("button", { type: "button", class: "ap-item", dataset: { kind: "trim", session: it.session }, onclick: () => showItem(it) },
      h("span", { class: "ap-thumb ap-thumb-trim" }, icon("film")),
      h("span", { class: "ap-itext" }, h("span", { class: "ap-ititle" }, `${it.title}: ${it.session === "main" ? "the recording" : it.session}`),
        h("span", { class: "secondary-text" }, `Claude's trim for the draft: starts ${secs(it.in)}, ends ${secs(it.out)}`)),
      h("span", { class: "chip chip-claude ap-state" }, "Claude's draft"), icon("right")));
    body.replaceChildren(...[
      filters,
      h("section", { class: "ap-group", "aria-label": "Posts to approve" }, h("h3", {}, "Posts to approve", h("span", { class: "chip chip-count" }, String(posts.length))),
        posts.length ? h("ul", { class: "ap-list" }, posts.map(postRow))
          : h("p", { class: "secondary-text ap-empty" }, hiddenN ? `${hiddenN} ${hiddenN === 1 ? "post is" : "posts are"} waiting under other brands or campaigns.` : "No post is waiting. When Claude finishes one, it shows here."),
        hiddenN && posts.length ? h("p", { class: "secondary-text" }, `${hiddenN} more under other brands or campaigns.`) : null),
      h("section", { class: "ap-group", "aria-label": "Trims to confirm" }, h("h3", {}, "Trims to confirm", h("span", { class: "chip chip-count" }, String(S.trims.length))),
        S.trims.length ? h("ul", { class: "ap-list" }, S.trims.map(trimRow))
          : h("p", { class: "secondary-text ap-empty" }, "No trim is waiting. Claude's trims for drafts show here until you confirm them, before the final cut.")),
      S.content.length ? h("section", { class: "ap-group", "aria-label": "Content to review" }, h("h3", {}, "Content to review", h("span", { class: "chip chip-count" }, String(S.content.length))),
        h("ul", { class: "ap-list" }, S.content.map((it) => h("li", {}, h("button", { type: "button", class: "ap-item", dataset: { kind: "content", item: it.id }, onclick: () => showItem(it) },
          h("span", { class: "ap-thumb" }, it.outputs[0]?.url && !isVideo(it.outputs[0].name) ? h("img", { src: it.outputs[0].url, alt: "", loading: "lazy" }) : icon("grid")),
          h("span", { class: "ap-itext" }, h("span", { class: "ap-ititle" }, `${it.title}: ${words(it.template)}`),
            h("span", { class: "secondary-text" }, [it.outputs.length === 1 ? "1 file" : `${it.outputs.length} files`, ...(it.platforms || []).map((x) => PLATFORM_NAMES[x] || x)].join(" · "))),
          h("span", { class: `chip ap-state ${it.critic && it.critic.ok === false ? "is-stale" : ""}` }, it.critic && it.critic.ok === false ? "Flagged" : "Rendered"), icon("right")))))) : null,
      S.episodePosts.length ? h("section", { class: "ap-group", "aria-label": "Episode posts to approve" }, h("h3", {}, "Episode posts to approve", h("span", { class: "chip chip-count" }, String(S.episodePosts.length))),
        h("ul", { class: "ap-list" }, S.episodePosts.map((it) => h("li", {}, h("button", { type: "button", class: "ap-item", dataset: { kind: "episode_post", item: it.id }, onclick: () => showItem(it) },
          h("span", { class: "ap-thumb ap-thumb-trim" }, icon("send")),
          h("span", { class: "ap-itext" }, h("span", { class: "ap-ititle" }, `${it.title}: ${it.name}`),
            h("span", { class: "secondary-text" }, (it.platforms || []).map((x) => PLATFORM_NAMES[x] || x).join(", ") || "No platform yet")),
          h("span", { class: "chip ap-state" }, "In the queue"), icon("right")))))) : null,
      batch,
      h("p", { class: "ap-foot secondary-text" }, icon("info"), h("span", {}, "Approving checks that it is you, with Touch ID or this Mac's password, and sends nothing now. Claude can prepare these, but only you can approve them.")),
    ].filter(Boolean));
  }

  // One device-owner check and one PIN for every post handed over; each post gets its own answer, and one that fails does not
  // undo the others. The page hands over only {post, variant, sha} for each.
  async function approveItems(btn, list, err) {
    err.textContent = "";
    const items = list.map((it) => ({ post: it.post, variant: it.variant, sha: S.seen.get(keyOf(it)) }));
    await busy(btn, async () => {
      try {
        const r = await app().approvePublish(items);
        const ok = r.approved || [], bad = r.refused || [];
        for (const x of ok) { S.seen.delete(`p:${x.post}/${x.variant}`); S.picked.delete(`p:${x.post}/${x.variant}`); S.results.delete(`p:${x.post}/${x.variant}`); }
        for (const x of bad) { S.results.set(`p:${x.post}/${x.variant}`, `Not approved: ${x.reason}`); S.picked.delete(`p:${x.post}/${x.variant}`); }
        const name = (x) => (S.posts.find((it) => it.post === x.post && it.variant === x.variant) || {}).title || x.post;
        toast(!bad.length ? (ok.length === 1 ? `Approved: ${name(ok[0])}. It goes out at its time from this computer.` : `Approved ${ok.length} posts. Each goes out at its time from this computer.`)
          : `Approved ${ok.length} of ${ok.length + bad.length}. ${bad.length === 1 ? "One was" : `${bad.length} were`} not: the list says why.`,
          { tone: bad.length ? (ok.length ? undefined : "negative") : "positive", timeout: 9000 });
        await load(); showList(); ctx.approvalsChanged?.();
      } catch (e) { err.textContent = /cancel/i.test(e.message) ? "Nothing was approved." : e.message; }
    });
  }

  // ---------------------------------------------------------------- one item
  function step(d) {
    if (!S.cur) return;
    const list = all(), i = list.findIndex((x) => keyOf(x) === keyOf(S.cur));
    const next = list[(i + d + list.length) % list.length];
    if (next && keyOf(next) !== keyOf(S.cur)) showItem(next);
  }
  function nextUnseen() {
    const list = all(), i = S.cur ? list.findIndex((x) => keyOf(x) === keyOf(S.cur)) : -1;
    const next = [...list.slice(i + 1), ...list.slice(0, i + 1)].find((x) => !S.seen.has(keyOf(x)));
    if (next) showItem(next);
  }
  function showItem(it) {
    stopTimer();
    S.cur = it;
    backBtn.hidden = false;
    backBtn.querySelector(".icon")?.style.setProperty("transform", "rotate(180deg)");
    return it.kind === "publish" ? showPost(it) : it.kind === "content" ? showContent(it) : it.kind === "episode_post" ? showEpisodePost(it) : showTrim(it);
  }

  async function showPost(it) {
    const my = ++S.token;
    crumb.replaceChildren(icon("lock"), h("span", {}, `Approve a post · ${(S.accounts[it.account] || {}).label || it.account}`));
    titleEl.textContent = it.title;
    el.setAttribute("aria-label", `Approve ${it.title}`);
    meta.replaceChildren();
    body.replaceChildren(skeleton());
    let detail;
    try { detail = await studio.api.get(`posts/${encodeURIComponent(it.post)}`); }
    catch (e) { if (my === S.token) body.replaceChildren(h("p", { class: "ap-why", role: "alert" }, plain(e, "read the post"))); return; }
    if (my !== S.token) return;
    const v = findVariant(detail.post, it.variant);
    const q = (detail.queue || {})[it.variant] || null;
    const bound = q && q.bound ? q.bound : null;
    const mism = (q && q.mismatch) || [];
    const sha = (bound && bound.sha256) || (q && q.sha256) || null;
    const flags = (v && v.flags) || [];
    meta.replaceChildren(h("span", { class: "pill", style: { background: "var(--group-6)", color: "var(--on-solid)" } }, "Waiting for your approval"),
      ...flags.map((f) => h("span", { class: `chip flag-chip f-${f}`, title: flagInfo(f)[1] }, icon("flag"), h("span", {}, flagInfo(f)[0]))),
      h("span", { class: "ps-when" }, icon("calendar"), it.when ? humanWhen(it.when) : "No time yet"));

    const approve = button("Approve on this Mac", { variant: "primary", iconName: "lock", "data-act": "approve", disabled: true, onclick: (e) => doApprove(e.currentTarget) });
    const err = h("p", { class: "ap-error", role: "alert" });
    const meter = h("div", { class: "ap-meter", role: "progressbar", "aria-label": "Time spent looking at the files", "aria-valuemin": "0", "aria-valuemax": String(VIEW_MS), "aria-valuenow": "0" }, h("span"));
    const meterText = h("span", { class: "ap-metertext secondary-text" });
    const why = h("p", { class: "ap-why" });
    let blocked = null;
    if (!bound) blocked = "Only the publishing computer approves posts, and this post has nothing to approve here. Approve it there, in its approval sheet.";
    else if (mism.length) blocked = "This review item differs from the post, so it cannot be approved until they match. Ask Claude to sync the queue, then look again.";
    else if (!app()?.approvePublish) blocked = NO_APP;
    why.textContent = blocked || "";
    why.hidden = !blocked;

    const files = bound && (bound.files || []).length && bound.files.every((f) => f.url) ? bound.files : [];
    const alts = (bound && bound.alt_texts) || [];
    const frames = files.map((f, i) => {
      const url = localUrl(f.url);
      const node = isVideo(f.name) ? h("video", { src: url, controls: true, preload: "metadata", "aria-label": alts[i] || f.name, onloadeddata: ready, onerror: broken })
        : h("img", { src: url, alt: alts[i] || "", onload: ready, onerror: broken });
      return h("figure", { class: "ap-frame", dataset: { sha: f.sha256 || "" } }, node,
        h("figcaption", {}, h("strong", {}, `File ${i + 1}`), h("code", {}, (f.sha256 || "").slice(0, 12)),
          f.name && !f.name.startsWith((f.sha256 || "").slice(0, 12)) ? h("span", { class: "secondary-text" }, f.name) : null,
          !String(alts[i] || "").trim() ? h("span", { class: "ap-bad" }, "No alt text") : null));
    });
    if (bound?.cover?.url) frames.push(h("figure", { class: "ap-frame ap-cover" }, h("img", { src: localUrl(bound.cover.url), alt: bound.cover.alt || "Separate cover", onload: ready, onerror: broken }),
      h("figcaption", {}, h("strong", {}, "Cover"), h("span", { class: "secondary-text" }, "A separate cover, never a carousel slide"))));
    function ready() {
      const nodes = frames.flatMap((f) => [...f.querySelectorAll("img,video")]);
      if (nodes.length && nodes.every((n) => (n.tagName === "IMG" ? n.complete && n.naturalWidth > 0 : n.readyState >= 2))) startTimer();
    }
    function broken() { stopTimer(); meterText.textContent = "A file could not be shown. Ask Claude to repair it before approving."; approve.disabled = true; }
    let elapsed = S.seen.has(keyOf(it)) ? VIEW_MS : 0;
    function startTimer() {
      if (S.timer || blocked) return;
      let last = performance.now();
      const tick = () => {
        const now = performance.now();
        if (document.visibilityState === "visible") elapsed += now - last;
        last = now;
        meter.firstChild.style.width = `${Math.min(100, (elapsed / VIEW_MS) * 100)}%`;
        meter.setAttribute("aria-valuenow", String(Math.round(Math.min(VIEW_MS, elapsed))));
        if (elapsed < VIEW_MS) { meterText.textContent = `Keep looking: ${Math.ceil((VIEW_MS - elapsed) / 1000)} s`; return; }
        stopTimer();
        S.seen.set(keyOf(it), sha);
        S.picked.add(keyOf(it));                       // looked at: ticked for an approval together with others, as on the old page
        meterText.textContent = "Looked at. You can approve it now, or with others from the list.";
        approve.disabled = false;
      };
      tick();
      S.timer = setInterval(tick, 100);
    }
    async function doApprove(btn) {
      S.seen.set(keyOf(it), sha);
      await approveItems(btn, [it], err);
    }

    const row = (label, value, wide = false) => value == null || value === "" ? null : [h("dt", {}, label), h("dd", { class: wide ? "is-wide" : "" }, value)];
    const none = (t = "None") => h("span", { class: "secondary-text" }, t);
    const signs = bound ? h("dl", { class: "ps-dl ap-signs" },
      row("Account", `${(S.accounts[bound.account] || {}).label || bound.account} · ${PLATFORM_NAMES[it.platform] || it.platform} ${String(bound.post_type || "").replace(/_/g, " ")}`),
      row("Delivery", (bound.delivery && bound.delivery.method) || "Automatic"),
      bound.presentation && bound.presentation.feed_presence !== undefined ? row("Profile feed", bound.presentation.feed_presence ? "Shown in the profile feed" : "Kept off the profile feed") : null,
      row("Caption", bound.caption ? h("span", { class: "ps-pre" }, bound.caption) : none("Empty"), true),
      row("Hashtags", (bound.tags || []).length ? h("span", { class: "ps-tags" }, bound.tags.map((t) => h("span", { class: "chip" }, "#" + String(t).replace(/^#/, "")))) : none()),
      row("First comment", bound.first_comment ? h("span", { class: "ps-pre" }, bound.first_comment) : none()),
      row("Link", bound.link_url ? h("span", { class: "ps-pre" }, bound.link_url) : none()),
      row("AI disclosure", bound.ai_disclosure ? "Yes: this post carries an AI disclosure" : "No"),
      row("Alt text", alts.length ? h("ol", { class: "ps-alts" }, alts.map((a, i) => h("li", { class: String(a || "").trim() ? "" : "missing" },
        h("span", { class: "ps-altn" }, `Image ${i + 1}`), h("span", {}, String(a || "").trim() || "Missing: screen readers get nothing here.")))) : none("No images"), true),
      row("Files", h("ul", { class: "ap-files" }, (bound.files || []).map((f) => h("li", {}, h("span", {}, f.name || "file"), h("code", { title: f.sha256 || "" }, f.sha256 ? f.sha256.slice(0, 16) : f.error || "cannot be read")))))) : null;

    body.replaceChildren(...[
      mism.length ? h("div", { class: "notice notice-negative ap-mismatch", role: "alert", dataset: { mismatch: mism.map((m) => m.field).join(",") } }, icon("alert"),
        h("div", { class: "notice-body" }, h("p", { class: "notice-title" }, "This review item is not the post you see"),
          h("p", { class: "notice-text" }, `An approval signs the review item, not the post. They differ in ${mism.map((m) => (FIELD[m.field] || m.field).toLowerCase()).join(", ")}.`),
          h("table", { class: "table ap-mmtable" }, h("thead", {}, h("tr", {}, h("th", {}, "Field"), h("th", {}, "Review item (what you would sign)"), h("th", {}, "The post"))),
            h("tbody", {}, mism.map((m) => h("tr", {}, h("th", { scope: "row" }, FIELD[m.field] || m.field), h("td", {}, fmt(m.queue)), h("td", {}, fmt(m.post)))))))) : null,
      flags.includes("approval_stale") ? h("div", { class: "notice ap-stale", role: "note" }, icon("info"), h("div", { class: "notice-body" },
        h("p", { class: "notice-title" }, "Your earlier approval no longer holds"),
        h("p", { class: "notice-text" }, `${v && v.status_reason ? v.status_reason.replace(/^approval no longer valid:\s*/, "") + ". " : ""}Look at it again and approve it again.`))) : null,
      bound ? h("section", { class: "ap-seal", "aria-label": "What you are approving" },
        h("p", { class: "ap-sealhead" }, icon("lock"), "You are approving these exact files"),
        h("code", { class: "ap-sha", dataset: { sha: sha || "" } }, groupSha(sha || "")),
        h("p", { class: "secondary-text" }, "It goes out only if this still matches at its time. A change to anything below voids the approval.")) : null,
      frames.length ? h("div", { class: "ap-frames" }, frames) : bound ? h("p", { class: "secondary-text" }, "This post has no files to look at.") : null,
      bound ? h("div", { class: "ap-view" }, meter, meterText) : null,
      bound ? h("section", { class: "ps-section", "aria-label": "What the approval signs" }, h("h3", {}, "What your approval signs"), signs) : null,
      h("div", { class: "ap-actions" }, approve, sendBackButton(it, detail, v)), why, err,
      !app()?.approvePublish && bound && !mism.length ? h("p", { class: "secondary-text ap-cli" }, "In your own terminal instead: ", h("code", {}, `ave studio approve ${it.post} ${it.variant} --by YOUR-NAME`)) : null,
    ].filter(Boolean));
    if (!frames.length || S.seen.has(keyOf(it))) startTimer();
  }
  const fmt = (x) => (x === null || x === undefined || x === "" ? h("em", {}, "empty") : typeof x === "string" ? x : JSON.stringify(x));

  // Ask Claude for changes instead of approving: the old Review page's "Request changes", the same studio feedback request.
  function sendBackButton(it, detail, v) {
    return button("Ask for changes", { variant: "secondary", "data-act": "sendback", onclick: (e) => {
      const anchor = e.currentTarget;
      let kind = "rework_copy";
      const note = h("textarea", { class: "input ap-note", rows: "3", "aria-label": "What should change", placeholder: "Say what to change, in your own words." });
      const msg = h("p", { class: "ap-error", role: "alert" });
      const opts = h("div", { class: "ap-kinds", role: "radiogroup", "aria-label": "What should happen" }, SEND_BACK.map(([k, label]) => {
        const r = h("button", { type: "button", class: "ap-kind", role: "radio", "aria-checked": String(k === kind), dataset: { kind: k },
          onclick: () => { kind = k; for (const b of opts.children) b.setAttribute("aria-checked", String(b.dataset.kind === k)); } }, label);
        return r;
      }));
      const send = button("Send to Claude", { variant: "primary", size: "sm", onclick: (ev) => busy(ev.currentTarget, async () => {
        msg.textContent = "";
        if ((kind === "rework_copy" || kind === "rework_visual") && !note.value.trim()) { msg.textContent = "Add a note so Claude knows what to change."; note.focus(); return; }
        const media = (v && v.media) || detail.post.media || [];
        const req = buildFeedbackBody({ variant: it.variant, kinds: [kind], note: note.value.trim(),
          copy: { field: "caption", before: (v && v.copy && v.copy.caption) || (detail.post.copy && detail.post.copy.base) || "", selection: null },
          visual: { asset: (media[0] || {}).asset || null, problem: "other", wanted: "" },
          retime: { from: v && v.schedule ? v.schedule.local : null }, drop: { reason: "other" } });
        req.rev = detail.post.rev;
        try {
          await studio.api.post(`posts/${encodeURIComponent(it.post)}/feedback`, req);
          pop.close();
          toast(kind === "drop" ? `Dropped: ${it.title}` : `Sent to Claude: ${it.title} waits for the changes.`, { tone: "positive" });
          await load(); showList(); ctx.approvalsChanged?.();
        } catch (er) { msg.textContent = plain(er, "send it"); }
      }) });
      const pop = popover(anchor, h("div", { class: "ap-sendback" }, h("p", { class: "popover-title" }, "Ask for changes"), opts,
        h("label", { class: "field" }, h("span", { class: "field-label" }, "Note for Claude"), note), msg,
        h("div", { class: "popover-actions" }, send)), { label: "Ask for changes", place: "above", align: "start" });
      opts.firstChild?.focus();
    } });
  }

  function showTrim(it) {
    S.token++;
    crumb.replaceChildren(icon("lock"), h("span", {}, `Confirm a trim · ${it.title}`));
    titleEl.textContent = `Trim for ${it.session === "main" ? "the recording" : it.session}`;
    el.setAttribute("aria-label", titleEl.textContent);
    meta.replaceChildren(h("span", { class: "chip chip-claude" }, "Claude's draft"),
      h("span", { class: "secondary-text" }, "The final cut waits for your confirmation"));
    const err = h("p", { class: "ap-error", role: "alert" });
    const ok = !!app()?.approveTrim;
    const confirm = button("Confirm the trim on this Mac", { variant: "primary", iconName: "lock", "data-act": "approve", disabled: !ok,
      onclick: (e) => busy(e.currentTarget, async () => {
        err.textContent = "";
        try {
          await app().approveTrim(it.project, it.session);
          toast(`Confirmed: the trim for ${it.session === "main" ? it.title : it.session} is yours. Ask Claude to make the assembly and rough cut again from it.`, { tone: "positive", timeout: 10000 });
          await load(); showList(); ctx.approvalsChanged?.();
        } catch (er) { err.textContent = /cancel/i.test(er.message) ? "Nothing was confirmed." : er.message; }
      }) });
    body.replaceChildren(...[
      h("section", { class: "ap-trim", "aria-label": "Claude's trim" },
        h("div", { class: "ap-ends" },
          h("div", {}, h("span", { class: "secondary-text" }, "Starts at"), h("strong", { dataset: { end: "in" } }, secs(it.in))),
          h("div", {}, h("span", { class: "secondary-text" }, "Ends at"), h("strong", { dataset: { end: "out" } }, secs(it.out)))),
        h("p", { class: "secondary-text" }, "Times in the recording. Everything before the start and after the end is left out of the cut.")),
      it.why.length ? h("section", { class: "ps-section", "aria-label": "Why Claude chose these" }, h("h3", {}, "Why Claude chose these"),
        h("ul", { class: "ap-whylist" }, it.why.map((w) => h("li", {}, w)))) : null,
      it.room ? h("p", {}, h("a", { class: "btn btn-secondary btn-sm", href: `#/room/${encodeURIComponent(it.room)}` }, icon("film"), h("span", {}, "Listen in the Room"))) : null,
      h("div", { class: "ap-actions" }, confirm),
      ok ? null : h("p", { class: "ap-why" }, NO_APP_TRIM),
      err,
      h("p", { class: "secondary-text ap-cli" }, "To use other times, tell Claude, or set them in your own terminal: ",
        h("code", {}, `ave trim set <project folder> ${it.session} --in SECONDS --out SECONDS`)),
    ].filter(Boolean));
  }

  // ---------------------------------------------------------------- a content item (`ave content`): approve in the app, reject here
  // Approving is the device-owner's act: the app's sheet (aveNative.approveContent) checks it is you. Rejecting records you and
  // needs no check, as on the old `ave ui` page.
  function rejectBox(label, place, onReject) {
    const note = h("textarea", { class: "input ap-note", rows: "2", "aria-label": place, placeholder: "Why, in your own words (optional)" });
    const err = h("p", { class: "ap-error", role: "alert" });
    const btn = button(label, { variant: "secondary", "data-act": "reject", onclick: (e) => busy(e.currentTarget, async () => {
      err.textContent = "";
      try { await onReject(note.value.trim()); } catch (er) { err.textContent = plain(er, "reject it"); }
    }) });
    return h("section", { class: "ps-section ap-reject", "aria-label": label }, h("label", { class: "field" }, h("span", { class: "field-label" }, place), note), h("div", { class: "ap-actions" }, btn), err);
  }
  function showContent(it) {
    S.token++;
    crumb.replaceChildren(icon("lock"), h("span", {}, `Review content · ${it.title}`));
    titleEl.textContent = words(it.template);
    el.setAttribute("aria-label", `Review ${titleEl.textContent}`);
    const flagged = it.critic && it.critic.ok === false;
    meta.replaceChildren(h("span", { class: "chip" }, "Rendered, waiting for you"), ...(flagged ? [h("span", { class: "chip flag-chip" }, icon("flag"), h("span", {}, "The critic flagged it"))] : []));
    const err = h("p", { class: "ap-error", role: "alert" });
    const ok = !!app()?.approveContent;
    const override = h("textarea", { class: "input ap-note", rows: "2", "aria-label": "Why it is fine anyway", placeholder: "The critic flagged it: say why you accept it" });
    const approve = button(flagged ? "Approve despite the critic on this Mac" : "Approve on this Mac", { variant: "primary", iconName: "lock", "data-act": "approve", disabled: !ok, onclick: (e) => busy(e.currentTarget, async () => {
      err.textContent = "";
      if (flagged && !override.value.trim()) { err.textContent = "Say why you accept what the critic flagged."; override.focus(); return; }
      try {
        await app().approveContent(it.project, it.doc, it.id, flagged ? override.value.trim() : undefined);
        toast(`Approved: ${words(it.template)} for ${it.title}. Nothing was posted.`, { tone: "positive" });
        await load(); showList(); ctx.approvalsChanged?.();
      } catch (er) { err.textContent = /cancel/i.test(er.message) ? "Nothing was approved." : er.message; }
    }) });
    body.replaceChildren(...[
      h("div", { class: "ap-frames" }, it.outputs.map((o, i) => h("figure", { class: "ap-frame" },
        o.url ? (isVideo(o.name) ? h("video", { src: o.url, controls: true, preload: "metadata", "aria-label": o.alt_text || o.name }) : h("img", { src: o.url, alt: o.alt_text || "" })) : h("p", { class: "secondary-text" }, "This file cannot be shown here."),
        h("figcaption", {}, h("strong", {}, `File ${i + 1}`), h("code", {}, (o.sha256 || "").slice(0, 12)),
          String(o.alt_text || "").trim() ? h("span", { class: "secondary-text" }, o.alt_text) : h("span", { class: "ap-bad" }, "No alt text"))))),
      flagged ? h("div", { class: "notice ap-stale", role: "note" }, icon("info"), h("div", { class: "notice-body" }, h("p", { class: "notice-title" }, "The critic flagged this"),
        h("p", { class: "notice-text" }, (it.critic.notes || []).join(" ")))) : null,
      flagged ? h("label", { class: "field" }, h("span", { class: "field-label" }, "Why it is fine anyway"), override) : null,
      h("div", { class: "ap-actions" }, approve), ok ? null : h("p", { class: "ap-why" }, "Approving needs the AVE Studio app: a browser cannot check that it is you."), err,
      rejectBox("Reject", "Note for Claude", async (note) => {
        // through the app it is recorded as you; over the web it is recorded as Claude's (a browser cannot show it is you)
        if (app()?.rejectContent) await app().rejectContent(it.project, it.doc, it.id, note || undefined);
        else await api("POST", "/api/workspace/content/reject", { project: it.project, doc: it.doc, id: it.id, note });
        toast(`Rejected: ${words(it.template)} for ${it.title}.${app()?.rejectContent ? "" : " A browser cannot show it is you, so it is recorded as Claude's."}`);
        await load(); showList(); ctx.approvalsChanged?.();
      }),
      h("p", { class: "ap-foot secondary-text" }, icon("info"), h("span", {}, "Approving sends nothing. An approved item becomes an episode post in the queue, which you approve separately.")),
    ].filter(Boolean));
  }
  // ---------------------------------------------------------------- an episode publish item (`ave publish`): approve in a terminal, reject here
  async function showEpisodePost(it) {
    const my = ++S.token;
    crumb.replaceChildren(icon("lock"), h("span", {}, `Episode post · ${it.title}`));
    titleEl.textContent = it.name;
    el.setAttribute("aria-label", `Episode post ${it.name}`);
    meta.replaceChildren(h("span", { class: "pill", style: { background: "var(--group-6)", color: "var(--on-solid)" } }, "Waiting for your approval"));
    body.replaceChildren(skeleton());
    let d;
    try { d = await api("GET", `/api/workspace/publish/${encodeURIComponent(it.project)}/${encodeURIComponent(it.id)}`); }
    catch (e) { if (my === S.token) body.replaceChildren(h("p", { class: "ap-why", role: "alert" }, plain(e, "read the post"))); return; }
    if (my !== S.token) return;
    const row = (label, value) => (value == null || value === "" ? null : [h("dt", {}, label), h("dd", {}, value)]);
    body.replaceChildren(...[
      h("section", { class: "ap-seal", "aria-label": "What an approval would sign" }, h("p", { class: "ap-sealhead" }, icon("lock"), "An approval signs these exact files"),
        h("code", { class: "ap-sha" }, groupSha(d.current_sha256 || d.sha256 || ""))),
      h("div", { class: "ap-frames" }, (d.files || []).map((f, i) => h("figure", { class: "ap-frame" },
        f.url ? (isVideo(f.name) ? h("video", { src: f.url, controls: true, preload: "metadata", "aria-label": f.name }) : h("img", { src: f.url, alt: (d.alt_texts || [])[i] || "" })) : h("p", { class: "secondary-text" }, f.exists ? "This file cannot be shown here." : "The file is missing."),
        h("figcaption", {}, h("strong", {}, f.name), h("code", {}, (f.sha256 || "").slice(0, 12)))))),
      h("dl", { class: "ps-dl" }, row("Platforms", (d.platforms || []).map((x) => PLATFORM_NAMES[x] || x).join(", ")), row("Title", d.title),
        row("Caption", d.caption || d.description ? h("span", { class: "ps-pre" }, d.caption || d.description) : null), row("Visibility", d.visibility),
        row("When", d.schedule ? humanWhen(d.schedule.local || d.schedule.utc || "") : "No time yet")),
      (d.preflight_problems || []).length ? h("div", { class: "notice notice-negative", role: "alert" }, icon("alert"), h("div", { class: "notice-body" },
        h("p", { class: "notice-title" }, "It would not go out as it is"), h("p", { class: "notice-text" }, d.preflight_problems.join(" ")))) : null,
      h("section", { class: "ps-section", "aria-label": "Approve it" }, h("h3", {}, "Approve it in your own terminal"),
        h("p", { class: "secondary-text" }, "Episode posts are approved with the command below, typed by you. Claude cannot run it."),
        h("code", { class: "ap-cli-line" }, d.approve_line)),
      rejectBox("Reject", "Why (kept with the post)", async (reason) => {
        if (app()?.rejectEpisodePost) await app().rejectEpisodePost(it.project, it.id, reason || undefined);
        else await api("POST", "/api/workspace/publish/reject", { project: it.project, id: it.id, reason });
        toast(`Rejected: ${it.name}. It will not be posted.${app()?.rejectEpisodePost ? "" : " A browser cannot show it is you, so it is recorded as Claude's."}`);
        await load(); showList(); ctx.approvalsChanged?.();
      }),
    ].filter(Boolean));
  }
  const words = (t) => { const s = String(t || "").replace(/[_-]+/g, " ").trim(); return s ? s[0].toUpperCase() + s.slice(1) : "Content item"; };

  function stopTimer() { clearInterval(S.timer); S.timer = 0; }

  return { el, open, close, load: refresh, count, isOpen: () => !el.hidden, onChange: (fn) => { listeners.add(fn); return () => listeners.delete(fn); } };
}
