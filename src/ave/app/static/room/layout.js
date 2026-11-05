// The Layout view: a post, carousel or other visual artifact, composed beside a phone preview. The composition logic is the post
// editor's (app/static/shared/compose.js, shared with the post editor kept under More tools), the preview cards are the studio's
// (previews/index.js), and only the chrome is the Room's. Claude edits the same post through its tools; the app reports each new
// revision on the stream as a `post` event and the layout follows it, field by field, without touching what the director is typing.
//
// Nothing here approves, queues or publishes: those stay the director's own acts in the review sheet on the publishing computer.

import { h, button, icon, busy, toast } from "../dom.js";
import { makeApi, findVariant, postTitle, PLATFORM_NAMES, localUrl, debounce, getPath } from "/app/shared/panels.js";
import { AUTOSAVE_MS, displayOf, toStored, dirtyText, sortAgainst, loadAssets, previewOf, slidesOf, importNative } from "/app/shared/compose.js";
import { buildPreview, loadPreviewConfig, captionStats, platformCfg } from "/app/shared/previews/index.js";

const FIELDS = [["caption", "Caption", 6], ["first_comment", "First comment", 2], ["hashtags", "Hashtags", 1]];
const STAND_IN = "__layout";                               // a draft with no channel yet previews as an Instagram post

// the app's own words for a failed call; the server's text goes in a title, never in the sentence
function plainCall(e, doing) {
  if (e.status === 0) return `Could not ${doing}. The app is not answering: check that AVE Studio is still open.`;
  if (e.status === 404) return `Could not ${doing}. This post is no longer in the studio.`;
  if (e.status === 409) return `Could not ${doing}. The post changed a moment ago: look again and try once more.`;
  if (e.status === 400 || e.status === 422) return `Could not ${doing}. ${e.message}`;
  return `Could not ${doing}. Try again.`;
}
const who = (ev) => (!ev?.actor ? "Someone" : ev.actor.kind === "agent" ? "Claude" : ev.actor.name || "Someone");
const fieldName = (path) => {
  const m = /(caption|first_comment|hashtags|base)$/.exec(path);
  return m ? { caption: "caption", base: "caption", first_comment: "first comment", hashtags: "hashtags" }[m[1]] : "post";
};

export function createLayout({ call, onInfo }) {
  const api = makeApi("/api/studio/");
  if (!document.querySelector('link[data-layout-css]')) {          // the preview cards' own styles, shared with the studio screen
    document.head.append(h("link", { rel: "stylesheet", href: "/app/shared/panels.css", "data-layout-css": "" }));
  }
  const S = { artifact: null, postId: null, post: null, rev: null, events: [], variantId: null, dirty: new Map(), saving: null,
    assets: {}, accounts: {}, server: {}, cfg: null, slide: 0, conflict: null, showHidden: false, seq: 0, error: null };

  // ---- the frame
  const channels = h("div", { class: "lay-channels", role: "tablist", "aria-label": "Channels" });
  const status = h("span", { class: "lay-save secondary-text", role: "status", "aria-live": "polite" });
  const notice = h("div", { class: "lay-notice", hidden: true });
  const canvas = h("div", { class: "lay-canvas" });
  const rail = h("div", { class: "lay-rail", role: "list", "aria-label": "Slides" });
  const importBtn = button("Import media", { "data-edit": "1", variant: "secondary", size: "sm", iconName: "plus", onclick: (e) => importMedia(e.currentTarget) });
  const earlier = button("Move earlier", { "data-edit": "1", variant: "tertiary", size: "sm", iconName: "rewind", iconOnly: true, onclick: (e) => moveSlide(-1, e.currentTarget) });
  const later = button("Move later", { "data-edit": "1", variant: "tertiary", size: "sm", iconName: "fastforward", iconOnly: true, onclick: (e) => moveSlide(1, e.currentTarget) });
  const remove = button("Remove slide", { "data-edit": "1", variant: "tertiary", size: "sm", iconName: "x", iconOnly: true, onclick: (e) => removeSlide(e.currentTarget) });
  const slideLabel = h("span", { class: "lay-slide-label secondary-text" });
  const alt = h("textarea", { class: "input lay-alt", rows: 2, "aria-label": "Alt text", placeholder: "Describe the picture for someone who cannot see it" });
  const altField = h("label", { class: "field" }, h("span", { class: "field-label" }, "Alt text"), alt);
  // the library picker, the cover, crop and the delivery check stay in the post editor, under the Workspace's More tools
  const editorBtn = button("More in the post editor", { variant: "tertiary", size: "sm", iconName: "open", "data-act": "post-editor",
    onclick: () => { const route = `#/post/${encodeURIComponent(S.postId)}`; const n = window.aveNative?.openTools;
      if (n) n(route).catch((er) => toast(er.message, { tone: "negative" })); else window.open(`/tools/${route}`, "_blank", "noopener"); } });
  const mediaTools = h("div", { class: "lay-media-tools" }, slideLabel, h("span", { class: "sc-spacer" }), earlier, later, remove, importBtn, editorBtn);
  const media = h("section", { class: "lay-media", "aria-label": "Media" }, canvas, rail, mediaTools, altField);
  const copy = h("section", { class: "lay-copy", "aria-label": "Writing" });
  const gate = h("p", { class: "lay-gate secondary-text" }, icon("lock"),
    h("span", {}, "Approving and publishing stay yours, in the review sheet on the publishing computer. Nothing on this page sends."));
  const body = h("div", { class: "lay-body" }, notice, media, copy, gate);
  const main = h("section", { class: "lay-main", "aria-label": "Composition" },
    h("div", { class: "lay-toolbar" }, channels, h("span", { class: "sc-spacer" }), status), h("div", { class: "lay-scroll" }, body));
  const phoneHead = h("p", { class: "lay-phone-title" });
  const foldBox = h("button", { type: "button", class: "check", role: "checkbox", "aria-checked": "false", "aria-labelledby": "lay-fold-text" }, icon("check"));
  const fold = h("span", { class: "lay-fold secondary-text" }, foldBox, h("span", { id: "lay-fold-text" }, "Show the text after “more”"));
  const toggleFold = () => { S.showHidden = !S.showHidden; drawPreview(); };
  foldBox.addEventListener("click", toggleFold);
  fold.lastChild.addEventListener("click", toggleFold);
  const screen = h("div", { class: "lay-screen" });
  const phone = h("div", { class: "lay-phone", role: "region", "aria-label": "Phone preview" }, h("span", { class: "lay-notch", "aria-hidden": "true" }), screen);
  const phoneNote = h("p", { class: "lay-phone-note secondary-text" });
  const side = h("aside", { class: "lay-side", "aria-label": "Preview" }, h("div", { class: "lay-phone-head" }, phoneHead, fold), phone, phoneNote);
  const msg = h("div", { class: "lay-msg", hidden: true });
  const el = h("div", { class: "layout", hidden: true }, main, side, msg);

  const variant = () => (S.post ? findVariant(S.post, S.variantId) || null : null);
  const ownMedia = () => { const v = variant(); return v && v.media !== null && v.media !== undefined ? v.id : null; };
  const path = (key) => {
    const v = variant();
    if (v) return `variants.${v.id}.copy.${key}`;
    return key === "caption" ? "copy.base" : `copy.${key}`;
  };

  // ---- loading
  function skeleton() {
    canvas.replaceChildren(h("span", { class: "skeleton lay-canvas-skel" }));
    rail.replaceChildren(...[0, 1, 2].map(() => h("span", { class: "skeleton lay-thumb-skel" })));
    copy.replaceChildren(h("span", { class: "skeleton skeleton-title" }), h("span", { class: "skeleton lay-text-skel" }));
    screen.replaceChildren(h("div", { class: "lay-screen-skel" }, h("span", { class: "skeleton skeleton-title" }), h("span", { class: "skeleton lay-shot-skel" }),
      h("span", { class: "skeleton skeleton-text" })));
    channels.replaceChildren(); phoneHead.textContent = ""; phoneNote.textContent = ""; status.textContent = "";
  }
  function showMsg(...kids) { msg.replaceChildren(...kids); msg.hidden = false; main.hidden = side.hidden = true; }

  async function open(artifact, { force = false } = {}) {
    if (!artifact || (!force && artifact === S.target)) return;
    S.target = artifact;
    const mine = ++S.seq;
    await leave();                                                   // what the director typed on the last post is saved first
    if (mine !== S.seq) return;
    Object.assign(S, { artifact, postId: null, post: null, rev: null, variantId: null, dirty: new Map(), conflict: null, slide: 0, server: {}, loading: true });
    msg.hidden = true; main.hidden = side.hidden = false; notice.hidden = true;
    skeleton();
    try {
      const ref = await call("GET", `/api/room/layout?artifact=${encodeURIComponent(artifact)}`);
      const res = await api.get(`posts/${encodeURIComponent(ref.post)}`);
      if (!S.cfg) S.cfg = await loadPreviewConfig().catch(() => ({ platforms: {}, approximate_label: "Approximate" }));
      if (!Object.keys(S.accounts).length) {
        try { for (const a of (await api.get("accounts")).accounts || []) S.accounts[a.id] = a; } catch { /* labels are optional */ }
      }
      if (mine !== S.seq) return;
      S.postId = ref.post;
      adopt(res);
      S.variantId = (res.post.variants.find((v) => !v.dropped) || res.post.variants[0] || {}).id || null;
      await Promise.all([loadAssets(api, S.post, S.assets), loadServerPreview()]);
      if (mine !== S.seq) return;
      S.loading = false;
      drawAll();
    } catch (e) {
      if (mine !== S.seq) return;
      S.loading = false;
      showMsg(h("h2", {}, e.status === 404 ? "This is not a post Layout can open" : "The post could not be read"),
        h("p", { class: "secondary-text" }, e.status === 404
          ? "Claude put something on stage that is not a post in this studio. Ask Claude which post it meant."
          : plainCall(e, "read the post")),
        button("Try again", { variant: "primary", onclick: () => open(artifact, { force: true }) }));
    }
  }
  function adopt(res) { S.post = res.post; S.rev = res.post.rev; S.events = res.events || S.events; }
  async function loadServerPreview() {
    const v = variant();
    if (!v) return;
    try { S.server[v.id] = await api.get(`posts/${encodeURIComponent(S.postId)}/preview/${encodeURIComponent(v.id)}`); } catch { S.server[v.id] = null; }
  }

  // ---- drawing
  function drawAll() {
    const v = variant();
    onInfo?.({ artifact: S.artifact, title: S.post.production?.title || postTitle(S.post, v) });
    drawChannels(); drawMedia(); drawCopy(); drawPreview(); drawNotice();
  }

  function drawChannels() {
    const live = S.post.variants.filter((v) => !v.dropped);
    if (!live.length) {
      channels.replaceChildren(h("span", { class: "chip lay-nochannel" }, "No channel yet"));
      return;
    }
    channels.replaceChildren(...live.map((v) => {
      const on = v.id === S.variantId;
      const label = S.accounts[v.account]?.label || v.account;
      const t = h("button", { type: "button", role: "tab", class: "tab lay-channel", "aria-selected": String(on), tabindex: on ? "0" : "-1", dataset: { variant: v.id } },
        h("span", {}, label), h("span", { class: "lay-channel-kind" }, `${PLATFORM_NAMES[v.platform] || v.platform} ${v.post_type}`));
      t.addEventListener("click", () => chooseChannel(v.id));
      t.addEventListener("keydown", (e) => {
        const i = live.findIndex((x) => x.id === S.variantId);
        const j = e.key === "ArrowRight" ? (i + 1) % live.length : e.key === "ArrowLeft" ? (i - 1 + live.length) % live.length : null;
        if (j === null) return;
        e.preventDefault(); chooseChannel(live[j].id).then(() => channels.querySelector(`[data-variant="${live[j].id}"]`)?.focus());
      });
      return t;
    }));
  }
  async function chooseChannel(id) {
    if (id === S.variantId) return;
    await flush();
    S.variantId = id; S.slide = 0;
    drawAll();
    await loadServerPreview(); drawPreview();
  }

  const urlOf = (m) => { const a = S.assets[m?.asset] || {}; return localUrl(a.kind === "image" ? (a.preview_url || a.url) : a.url); };
  function picture(m, cls) {
    const a = S.assets[m?.asset] || {};
    const url = urlOf(m);
    if (!url) return h("span", { class: `${cls} is-missing` }, a.missing ? "Missing file" : "No preview");
    return a.kind === "video" ? h("video", { class: cls, src: url, muted: true, playsinline: true, preload: "metadata" })
      : h("img", { class: cls, src: url, alt: m.alt || "" });
  }
  function drawMedia() {
    const slides = slidesOf(S.post, variant()).filter((m) => m.role !== "cover");
    S.slide = Math.min(S.slide, Math.max(0, slides.length - 1));
    const sel = slides[S.slide];
    canvas.classList.toggle("is-empty", !sel);
    if (!sel) {
      canvas.replaceChildren(h("div", { class: "lay-empty" }, h("h2", {}, "No media yet"),
        h("p", { class: "secondary-text" }, "Import a photo or a video. A post with more than one picture becomes a carousel."),
        button("Import media", { variant: "primary", iconName: "plus", onclick: (e) => importMedia(e.currentTarget) })));
    } else canvas.replaceChildren(picture(sel, "lay-picture"));
    rail.replaceChildren(...slides.map((m, i) => {
      const b = h("button", { type: "button", class: `lay-thumb${i === S.slide ? " is-current" : ""}`, "aria-label": `Slide ${i + 1}`,
        "aria-current": i === S.slide ? "true" : "false" }, picture(m, "lay-thumb-img"), h("span", { class: "lay-thumb-n" }, String(i + 1)));
      b.addEventListener("click", () => { S.slide = i; drawMedia(); drawPreview(); });
      return h("div", { role: "listitem" }, b);
    }));
    rail.hidden = slides.length < 2;
    slideLabel.textContent = slides.length ? `Slide ${S.slide + 1} of ${slides.length}` : "";
    earlier.disabled = !sel || S.slide === 0;
    later.disabled = !sel || S.slide >= slides.length - 1;
    remove.disabled = !sel;
    importBtn.hidden = !sel;
    editorBtn.hidden = !S.postId;
    mediaTools.hidden = !sel;
    altField.hidden = !sel;
    if (sel && document.activeElement !== alt) alt.value = sel.alt || "";
    alt.dataset.item = sel ? sel.id : "";
  }

  function drawCopy() {
    const v = variant();
    const own = v && v.copy && v.copy.caption !== null && v.copy.caption !== undefined;
    copy.replaceChildren(h("h2", { class: "lay-h" }, "Writing"), ...FIELDS.map(([key, label, rows]) => {
      const p = path(key);
      const ta = h("textarea", { class: "input lay-input", rows, name: p, spellcheck: "true", dataset: { path: p, key } });
      ta.value = S.dirty.has(p) ? dirtyText(S.dirty.get(p)) : displayOf(S.post, p);
      ta.addEventListener("input", () => { markDirty(p, ta.value); grow(ta); });
      ta.addEventListener("blur", () => { if (S.dirty.has(p) && !S.conflict) save.flush(); });   // leaving the field saves at once
      requestAnimationFrame(() => grow(ta));
      const hint = key === "caption" && v && !own ? "Shared with every channel until you change it here."
        : key === "hashtags" ? "Separate them with spaces. The # is added for you." : null;
      return h("label", { class: "field lay-field", dataset: { key } }, h("span", { class: "field-label" }, label,
        h("span", { class: "lay-changed", hidden: true }, h("span", { class: "lay-avatar", "aria-hidden": "true" }, "C"),
          h("span", { class: "lay-changed-text" }))), ta, hint ? h("span", { class: "lay-hint secondary-text" }, hint) : null);
    }));
  }
  function refreshFields(changed = []) {
    for (const ta of copy.querySelectorAll("textarea[data-path]")) {
      const p = ta.dataset.path;
      if (S.dirty.has(p)) continue;
      const val = displayOf(S.post, p);
      if (ta.value !== val) { ta.value = val; grow(ta); }
    }
    for (const [p, name] of changed) {
      const f = copy.querySelector(`.lay-field[data-key="${/hashtags$/.test(p) ? "hashtags" : /first_comment$/.test(p) ? "first_comment" : "caption"}"]`);
      if (!f) continue;
      const chip = f.querySelector(".lay-changed");
      chip.querySelector(".lay-changed-text").textContent = `${name} changed this`;
      chip.querySelector(".lay-avatar").hidden = name !== "Claude";
      chip.hidden = false;
      f.classList.remove("lay-arrived"); void f.offsetWidth; f.classList.add("lay-arrived");
      clearTimeout(f._t); f._t = setTimeout(() => { f.classList.remove("lay-arrived"); chip.hidden = true; }, 4000);
    }
  }

  function drawPreview() {
    if (!S.post || !S.cfg) return;
    const real = variant();
    const slides = slidesOf(S.post, real).filter((m) => m.role !== "cover");
    const v = real || { id: STAND_IN, platform: "instagram", post_type: slides.length > 1 ? "carousel" : "feed", account: "your account", copy: null, media: null };
    const model = previewOf(S.post, S.dirty, v, { assets: S.assets, accounts: S.accounts, server: S.server[v.id], showHidden: S.showHidden });
    const card = buildPreview(model, S.cfg);
    screen.replaceChildren(card);
    const stats = captionStats(model, platformCfg(S.cfg, v.platform) || { counter: { unit: "chars" } });
    screen.dataset.state = stats.state;
    const plat = PLATFORM_NAMES[v.platform] || v.platform;
    const kind = `${plat} ${v.post_type === "feed" ? "post" : v.post_type}`;
    phoneHead.textContent = real ? kind : `${kind} · no channel yet`;
    phoneNote.textContent = S.cfg.approximate_label || "";
    foldBox.setAttribute("aria-checked", String(S.showHidden));
  }

  function drawNotice() {
    const c = S.conflict;
    notice.hidden = !c;
    if (!c) { notice.replaceChildren(); return; }
    const name = fieldName(c.path);
    notice.replaceChildren(h("div", { class: "notice", role: "status" }, h("span", { class: "notice-icon", "aria-hidden": "true" }, icon("people")),
      h("div", { class: "notice-body" },
        h("p", { class: "notice-title" }, `${c.who} changed the ${name} while you were typing`),
        h("p", { class: "notice-text" }, `${c.who}’s version: “${c.theirs || "empty"}”`),
        h("div", { class: "notice-actions" },
          button("Keep mine", { "data-edit": "1", variant: "secondary", size: "sm", onclick: () => settle("mine") }),
          button(`Use ${c.who}’s`, { "data-edit": "1", variant: "secondary", size: "sm", onclick: () => settle("theirs") })))));
  }

  // ---- saving: debounced like the editor, compare-and-swap on the revision
  const save = debounce(() => doSave(), AUTOSAVE_MS);
  function setStatus(text, tone = "") { status.textContent = text; status.dataset.tone = tone; }
  function markDirty(p, text) {
    const stored = toStored(p, text);
    const prior = S.dirty.get(p);
    const base = prior ? prior.base : getPath(S.post, p) ?? null;
    if (JSON.stringify(stored) === JSON.stringify(base)) S.dirty.delete(p); else S.dirty.set(p, { value: stored, base });
    if (S.dirty.size) { setStatus("Editing…", "pending"); if (!S.conflict) save(); }
    drawPreview();
  }
  function doSave() { if (!S.saving) S.saving = performSave().finally(() => { S.saving = null; }); return S.saving; }
  async function flush() { save.cancel(); if (S.saving) await S.saving; if (S.dirty.size && !S.conflict) await doSave(); }
  async function performSave() {
    if (S.conflict || !S.dirty.size || !S.postId) return;
    const snapshot = new Map(S.dirty);
    setStatus("Saving…", "pending");
    try {
      const res = await api.post(`posts/${encodeURIComponent(S.postId)}/edit`, { expected_rev: S.rev, patch: Object.fromEntries([...snapshot].map(([p, e]) => [p, e.value])) });
      S.post = res.post; S.rev = res.post.rev;
      for (const [p, e] of snapshot) {
        const cur = S.dirty.get(p);
        if (cur && JSON.stringify(cur.value) === JSON.stringify(e.value)) S.dirty.delete(p); else if (cur) cur.base = e.value;
      }
      setStatus(`Saved ${new Date().toLocaleTimeString([], { hour: "numeric", minute: "2-digit" })}`, "saved");
      loadServerPreview().then(drawPreview);
      if (S.dirty.size) save();
    } catch (e) {
      if (e.status === 409 && e.latest) {                      // refused: someone saved first. Read who, then rebase like a stream event
        let ev = null;
        try { const r = await api.get(`posts/${encodeURIComponent(S.postId)}`); if (r.post.rev === e.latest.rev) { ev = (r.events || []).at(-1); S.events = r.events; } }
        catch { /* the name is a courtesy */ }
        await rebase(e.latest, ev);
        if (S.dirty.size && !S.conflict) setTimeout(() => doSave(), 0);   // straight away, as the editor does: the text was already due
      }
      else { setStatus("Not saved", "bad"); status.title = e.message; toast(plainCall(e, "save your change"), { tone: "negative" }); }
    }
  }
  // A newer revision arrived (from the stream or a refused save): fields only I changed keep my text and save again; a field we
  // both changed asks; everything else takes the new text, and says who changed it.
  async function rebase(latest, ev = null) {
    const before = S.post;
    const sorted = sortAgainst(latest, S.dirty);
    for (const p of sorted.same) S.dirty.delete(p);
    for (const p of sorted.rebase) S.dirty.get(p).base = getPath(latest, p) ?? null;
    S.post = latest; S.rev = latest.rev;
    const name = who(ev);
    const first = sorted.conflict[0];
    if (S.conflict && sorted.conflict.includes(S.conflict.path)) Object.assign(S.conflict, conflictOf(S.conflict.path, name));  // a newer version of theirs
    else if (first && !S.conflict) S.conflict = conflictOf(first, name);
    const changed = FIELDS.map(([k]) => path(k)).filter((p) => !S.dirty.has(p) && displayOf(before, p) !== displayOf(latest, p)).map((p) => [p, name]);
    refreshFields(changed);                                          // before any wait, so a keystroke lands on the new text
    drawNotice();
    await loadAssets(api, S.post, S.assets);
    drawChannels(); drawMedia();
    await loadServerPreview(); drawPreview();
    if (changed.length) setStatus(`${name} changed the ${changed.map(([p]) => fieldName(p)).join(" and ")}`, "live");
    else if (JSON.stringify(slidesOf(before, variant())) !== JSON.stringify(slidesOf(latest, variant()))) setStatus(`${name} changed the media`, "live");
    onInfo?.({ artifact: S.artifact, title: S.post.production?.title || postTitle(S.post, variant()) });
  }
  const conflictOf = (p, name) => ({ path: p, who: name, theirs: displayOf(S.post, p), theirsRaw: getPath(S.post, p) ?? null, rev: S.rev });
  // the director's answer to the notice: "mine" keeps their text (saved over the other version), "theirs" drops it
  function resolve(choice) {
    const c = S.conflict;
    if (!c) return null;
    if (choice === "theirs") S.dirty.delete(c.path); else S.dirty.get(c.path).base = getPath(S.post, c.path) ?? null;
    S.conflict = null;
    const next = sortAgainst(S.post, S.dirty).conflict[0];
    if (next) S.conflict = conflictOf(next, c.who);
    return c;
  }
  function settle(choice) {
    if (!resolve(choice)) return;
    refreshFields(); drawNotice(); drawPreview();
    if (S.dirty.size && !S.conflict) doSave(); else if (!S.dirty.size) setStatus("Up to date", "saved");
  }

  // Leaving this post (the stage moved on, the view changed, the Room closed) never drops what the director typed. Unsaved text
  // is saved. A field someone else changed meanwhile is kept as the director's, the notice's Keep mine, and a toast offers the
  // other version back. If the save itself fails, the toast quotes the text so it can be copied.
  async function leave() {
    save.cancel();
    await altSave.flush();
    if (S.saving) await S.saving;
    if (!S.postId) return;
    const pid = S.postId, replaced = [];
    for (let i = 0; i < 4 && (S.dirty.size || S.conflict); i++) {
      while (S.conflict) replaced.push(resolve("mine"));
      if (S.dirty.size) await doSave();
    }
    for (const c of replaced) {
      if (!c || S.dirty.has(c.path)) continue;
      toast(`Kept your ${fieldName(c.path)}. ${c.who}’s version was replaced.`, { key: `lay-kept-${c.path}`, timeout: 0,
        action: `Use ${c.who}’s`, onAction: () => putBack(pid, c) });
    }
    for (const [p, e] of S.dirty) {
      toast(`Your ${fieldName(p)} could not be saved. It was: “${dirtyText(e)}”`, { tone: "negative", timeout: 30000 });
    }
  }
  async function putBack(pid, c) {
    try {
      const cur = await api.get(`posts/${encodeURIComponent(pid)}`);
      await api.post(`posts/${encodeURIComponent(pid)}/edit`, { expected_rev: cur.post.rev, patch: { [c.path]: c.theirsRaw } });
      toast(`Back to ${c.who}’s ${fieldName(c.path)}`);
    } catch (e) { toast(plainCall(e, `put ${c.who}’s version back`), { tone: "negative", title: e.message }); }
  }

  // the stream says the post moved: read it, and follow it unless this page made the change
  let following = Promise.resolve();
  function onPost(d) {
    if (!S.postId || d.post !== S.postId || d.rev === S.rev) return;
    following = following.then(async () => {
      if (d.rev === S.rev) return;
      if (S.saving) await S.saving;
      try {
        const res = await api.get(`posts/${encodeURIComponent(S.postId)}`);
        if (res.post.rev <= S.rev || res.post.id !== S.postId) return;     // an older read than what is on screen: never roll back
        const ev = (res.events || []).at(-1);
        S.events = res.events || S.events;
        await rebase(res.post, ev);
      } catch { /* gone for a moment: the next event or reopening reads it again */ }
    });
  }

  // ---- media: the editor's operations, through the same endpoint; files come in only through the app (aveNative)
  async function mediaOp(op, args, btn, doing, done = null) {
    const run = async () => {
      if (S.conflict) { toast(`Choose Keep mine or Use ${S.conflict.who}’s first, then change the media.`, { tone: "negative" }); return; }
      await flush();
      if (S.dirty.size) { toast("Save your writing before changing the media.", { tone: "negative" }); return; }
      const res = await api.post(`posts/${encodeURIComponent(S.postId)}/media`, { expected_rev: S.rev, variant: ownMedia(), op, ...args });
      adopt(res);
      done?.();
      await loadAssets(api, S.post, S.assets);
      await loadServerPreview();
      drawAll();
      setStatus(`Saved ${new Date().toLocaleTimeString([], { hour: "numeric", minute: "2-digit" })}`, "saved");
    };
    try { await (btn ? busy(btn, run) : run()); }
    catch (e) { toast(plainCall(e, doing), { tone: "negative", title: e.message }); }
  }
  async function importMedia(btn) {
    if (!window.aveNative?.importLibrary) {
      toast("Importing needs the AVE Studio app. Open this project there to add photos or video.", { tone: "negative" });
      return;
    }
    let added = 0;
    const go = () => importNative(async (id) => {
      await mediaOp("add", { asset: id }, null, "add the media");
      added += 1;
    });
    try {
      await busy(btn, go);
      if (added) { S.slide = Math.max(0, slidesOf(S.post, variant()).filter((m) => m.role !== "cover").length - 1); drawMedia(); drawPreview(); }
      if (added) toast(added === 1 ? "Added 1 file to the post" : `Added ${added} files to the post`);
    } catch (e) { toast(plainCall(e, "import the media"), { tone: "negative", title: e.message }); }
  }
  function current() { return slidesOf(S.post, variant()).filter((m) => m.role !== "cover"); }
  function moveSlide(dir, btn) {
    const slides = current(), sel = slides[S.slide];
    if (!sel) return;
    const before = dir < 0 ? slides[S.slide - 1]?.id : slides[S.slide + 2]?.id || null;
    const to = S.slide + dir;
    mediaOp("move", { item: sel.id, before }, btn, "move the slide", () => { S.slide = to; });   // only once the move is saved
  }
  function removeSlide(btn) {
    const sel = current()[S.slide];
    if (sel) mediaOp("remove", { item: sel.id }, btn, "remove the slide");
  }
  // alt text autosaves like the writing (it goes through the media operation, so a shared slide stays shared)
  let altPending = null;
  const altSave = debounce(() => { const a = altPending; altPending = null; return a ? mediaOp("alt", a, null, "save the alt text") : null; }, AUTOSAVE_MS);
  alt.addEventListener("input", () => { if (alt.dataset.item) { altPending = { item: alt.dataset.item, alt: alt.value }; altSave(); } });
  alt.addEventListener("blur", () => altSave.flush());

  function grow(ta) {
    if (!ta.isConnected) return;
    ta.style.height = "auto";
    ta.style.height = `${Math.min(ta.scrollHeight + 2, 360)}px`;
  }

  function show(on) {
    const was = el.hidden;
    el.hidden = !on;
    if (!on && !was) leave();
    if (on && was && S.postId) onPost({ post: S.postId, rev: null });   // back on this post: catch up with what changed meanwhile
  }
  return { el, open, show, onPost, flush, close: () => { S.seq += 1; return leave(); }, state: S };
}
