// Review mode (spec 7.2 #/review, 7.6): step through variants waiting for approval, look at the EXACT bytes, then approve with a PIN.
//
//   mountReview(root, ctx, {post?, variant?}) -> {destroy, state}
//
// Approval is a human act. The server enforces it (a `view` event with the sha it served, shown for at least 2 s, plus the per-run PIN
// printed on the human's terminal); this page makes the rule visible and never offers a shortcut around it: no single-key approve, the
// PIN is never stored, and a variant must have been looked at before it can be ticked.
//
// What an approval SIGNS is the queue item, not the post. So the sheet is drawn from the queue item's own fields (caption, first comment, link,
// hashtags, alt texts, the sha256 of every file) as the server returns them, and when the item differs from the post a banner says exactly where
// and the post cannot be ticked: the server refuses it too.
import {
  ApiError, announce, clear, effectiveCaption, effectiveFirstComment, effectiveHashtags, effectiveMedia, empty, errorBox, field, findVariant,
  fmtBytes, fmtWhen, groupSha, h, hashQuery, localUrl, PLATFORM_NAMES, postTitle, say, shortSha, statusChip, storeGet, storeSet,
} from "./panels.js";
import { flagInfo } from "./dom.js";
import { openSendBack } from "./feedback.js";
import { filterBar, readFilters, writeFilters } from "./filters.js";
import { buildPreview, loadPreviewConfig, previewModel } from "./previews/index.js";

export const VIEW_MS = 2000;

const keyOf = (it) => `${it.post.id}/${it.variant.id}`;
const FIELD_NAMES = { caption: "Caption", first_comment: "First comment", link_url: "Link", link: "Link", tags: "Hashtags", hashtags: "Hashtags", alt_texts: "Alt text", account: "Account", post_type: "Post type", files: "Files", ai_disclosure: "AI disclosure", post: "Post" };
const isVideo = (name) => /\.(mp4|mov|m4v|webm)$/i.test(name || "");

export function mountReview(root, ctx, { post = null, variant = null } = {}) {
  root.classList.add("st");
  const S = {
    items: [], all: [], index: 0, viewed: new Map(), picked: new Set(), assets: {}, cfg: null, accounts: {}, campaigns: [], detail: new Map(), filters: null,
    mode: "pin_after_review", alive: true, elapsed: 0, timer: null, mediaReady: false, busy: false, results: new Map(), typed: new Map(),
  };
  const list = h("ol.rv-list", { "aria-label": "Waiting for review" });
  const stage = h("div.rv-stage");
  const bar = h("aside.rv-bar", { id: "rv-approval", "aria-label": "Approve" });
  const live = h("p.st-sr", { role: "status", "aria-live": "polite" });
  const filters = h("div.rv-filters");
  clear(root).append(h("section.st-page.rv",
    h("header.st-pagehead", h("h1", "Review"), h("p.st-hint", "Look at each post as it will go out, then approve. ", h("span.kbd", "j"), " and ", h("span.kbd", "k"), " move between posts, ", h("span.kbd", "n"), " jumps to the next one you have not looked at.")),
    filters,
    h("div.rv-grid", h("nav.rv-queue", { "aria-label": "Posts to review" }, list), h("div.rv-main", stage), bar), live));

  // ---- load --------------------------------------------------------------------------------------------------------------------------
  async function load() {
    try {
      const [posts, summary] = await Promise.all([ctx.api.get("posts?status=in_review&thumbs=1"), ctx.api.get("summary").catch(() => ({}))]);
      if (!S.alive) return;
      S.mode = (summary && ((summary.ui && summary.ui.approval_mode) || summary.approval_mode)) || "pin_after_review";
      S.cfg = await loadPreviewConfig().catch(() => ({ platforms: {} }));
      try { (await ctx.api.get("accounts")).accounts.forEach((a) => { S.accounts[a.id] = a; }); } catch { /* optional */ }
      try { S.campaigns = ((await ctx.api.get("campaigns")).campaigns || []).map((c) => ({ id: c.campaign.id, name: c.campaign.name })); } catch { /* optional */ }
      if (!S.filters) S.filters = readFilters(Object.values(S.accounts));
      const items = [];
      for (const p of posts.posts || []) for (const v of p.variants || []) if (v.status === "in_review") items.push({ post: p, variant: v });
      if (post && variant && !items.some((it) => it.post.id === post && it.variant.id === variant)) {
        try { const r = await ctx.api.get(`posts/${post}`); const v = findVariant(r.post, variant); if (v) items.unshift({ post: r.post, variant: v }); } catch { /* not found: ignore */ }
      }
      S.all = items;
      applyFilters(post && variant ? `${post}/${variant}` : null);
      drawFilters();
      drawList();
      await select(S.index, { silent: true });
    } catch (ex) { clear(stage).append(errorBox(ex, load)); }
  }

  function applyFilters(keepKey = null) {
    const f = S.filters || { brand: null, campaign: null };
    const brandOf = (v) => (S.accounts[v.account] || {}).brand || null;
    S.items = S.all.filter((it) => keepKey === keyOf(it) || ((f.brand === null || brandOf(it.variant) === f.brand) && (!f.campaign || it.post.campaign === f.campaign)));
    const at = keepKey ? S.items.findIndex((it) => keyOf(it) === keepKey) : -1;
    S.index = at >= 0 ? at : 0;
  }
  function drawFilters() {
    clear(filters);
    const accounts = Object.values(S.accounts);
    if (!S.filters || (!accounts.some((a) => a.brand) && !S.campaigns.length)) return;
    filters.append(filterBar({ accounts, campaigns: S.campaigns, filters: S.filters, showAccounts: false, label: "Brand and campaign filters",
      onChange: (next) => { S.filters = next; writeFilters(next); applyFilters(); drawFilters(); drawList(); select(S.index, { silent: true }); } }));
  }

  async function ensureAssets(item) {
    const ids = effectiveMedia(item.post, item.variant).map((m) => m.asset);
    await Promise.all(ids.filter((id) => id && !S.assets[id]).map(async (id) => {
      try { const r = await ctx.api.get(`library/${id}`); S.assets[id] = { ...r.asset, url: r.url, coverage: r.license }; } catch { S.assets[id] = { id, missing: true }; }
    }));
  }
  /** The queue item as the server sees it right now: `bound` (what an approval signs) and `mismatch` (how it differs from the post). */
  async function ensureDetail(item) {
    const k = keyOf(item);
    try {
      const r = await ctx.api.get(`posts/${encodeURIComponent(item.post.id)}`);
      S.detail.set(k, (r.queue && r.queue[item.variant.id]) || null);
    } catch { S.detail.set(k, null); }
    return S.detail.get(k);
  }
  const queueOf = (it) => S.detail.get(keyOf(it)) || null;
  const shaOf = (it) => (queueOf(it) && queueOf(it).sha256) || it.variant.queue_sha;
  const mismatchOf = (it) => (queueOf(it) && queueOf(it).mismatch) || [];

  // ---- list --------------------------------------------------------------------------------------------------------------------------
  function drawList() {
    clear(list);
    if (!S.items.length) {
      const filtered = S.all.length && S.filters && (S.filters.brand !== null || S.filters.campaign);
      clear(stage).append(filtered
        ? h("div.st-empty", h("p.st-empty-title", "Nothing waiting under these filters."), h("p.st-hint", `${S.all.length} ${S.all.length === 1 ? "post is" : "posts are"} waiting under other brands or campaigns.`),
          h("button.primary", { type: "button", onclick: () => { S.filters = { brand: null, campaign: null, off: [] }; writeFilters(S.filters); applyFilters(); drawFilters(); drawList(); select(0, { silent: true }); } }, "Clear filters"))
        : h("div.st-empty", h("p.st-empty-title", "Nothing is waiting for review."), h("p.st-hint", "When Claude finishes a post it shows up here."), h("p", h("a.st-btn", { href: "#/calendar" }, "Back to the calendar"))));
      clear(bar);
      return;
    }
    S.items.forEach((it, i) => {
      const k = keyOf(it);
      const seen = S.viewed.has(k);
      const result = S.results.get(k);
      const bad = mismatchOf(it).length;
      const flags = (it.variant.flags || []).filter((f) => f === "approval_stale");
      const thumb = it.variant.thumb ? localUrl(it.variant.thumb) : "";
      list.append(h("li.rv-li", { class: i === S.index ? "on" : "", dataset: { key: k } },
        h("button.rv-pick", { type: "button", "aria-current": i === S.index ? "true" : "false", onclick: () => select(i) },
          h("span.rv-thumb", thumb ? h("img", { src: thumb, alt: "", loading: "lazy", decoding: "async" }) : h("span.rv-nothumb", "No image")),
          h("span.rv-li-text",
            h("span.rv-li-title", postTitle(it.post, it.variant)),
            h("span.st-hint.rv-li-meta", `${PLATFORM_NAMES[it.variant.platform] || it.variant.platform} ${it.variant.post_type} · ${(S.accounts[it.variant.account] || {}).label || it.variant.account}`),
            h("span.st-hint.rv-li-state", result ? result : bad ? "Differs from the post" : seen ? "Looked at" : "Not looked at yet"),
            flags.map((f) => h("span.rv-flag", { title: flagInfo(f)[1] }, flagInfo(f)[0])))),
        h("label.st-check.rv-inc", h("input", { type: "checkbox", name: "include", disabled: !seen || !!result || !!bad, checked: S.picked.has(k), "aria-label": `Include ${it.post.id} ${it.variant.id} in the approval`, onchange: (e) => { if (e.target.checked) S.picked.add(k); else S.picked.delete(k); drawBar(); } }), "Approve")));
    });
  }

  // ---- stage -------------------------------------------------------------------------------------------------------------------------
  async function select(i, { silent = false } = {}) {
    stopTimer();
    if (!S.items.length) return;
    S.index = Math.max(0, Math.min(S.items.length - 1, i));
    const it = S.items[S.index];
    S.elapsed = S.viewed.has(keyOf(it)) ? VIEW_MS : 0;
    S.mediaReady = false;
    drawList();
    clear(stage).append(h("p.st-hint", "Loading…"));
    await Promise.all([ensureAssets(it), ensureDetail(it)]);
    if (!S.alive || S.items[S.index] !== it) return;
    drawList();
    drawStage(it);
    drawBar();
    list.querySelector(".rv-li.on")?.scrollIntoView({ block: "nearest", inline: "nearest" });
    if (!silent) announce(ctx, `${(it.post.idea && it.post.idea.text) || it.post.id}, ${PLATFORM_NAMES[it.variant.platform] || it.variant.platform}. Post ${S.index + 1} of ${S.items.length}.${mismatchOf(it).length ? " This review item differs from the post: it cannot be approved." : ""}`);
  }

  function row(label, value, cls = "") { return value === null || value === undefined || value === "" ? null : h("div.rv-row" + (cls ? "." + cls : ""), h("dt", label), h("dd", value)); }

  function drawStage(it) {
    const { post: p, variant: v } = it;
    const q = queueOf(it);
    const bound = q && q.bound ? q.bound : null;
    const mism = mismatchOf(it);
    const k = keyOf(it);
    const already = S.viewed.has(k);
    const meter = h("progress.rv-meter", { max: VIEW_MS, value: already ? VIEW_MS : 0, "aria-label": "Time spent looking at the media" });
    const meterText = h("span.rv-metertext", { "aria-live": "off" }, already ? "Looked at. You can approve." : `Keep looking: ${(VIEW_MS / 1000).toFixed(0)} s needed`);
    // what is shown is what is signed: the queue item's files when the server serves them, else the post's media
    const files = bound && bound.files && bound.files.length && bound.files.every((f) => f.url) ? bound.files : null;
    const media = effectiveMedia(p, v).filter(m=>m.role!=="cover");
    const frames = files ? files.map((f, idx) => {
      const url = localUrl(f.url);
      const alt = (bound.alt_texts || [])[idx] || "";
      const node = isVideo(f.name) ? h("video.rv-media", { src: url, controls: true, preload: "metadata", "aria-label": alt || f.name, onloadeddata: onReady, onerror: onFailure })
        : h("img.rv-media", { src: url, alt, onload: onReady, onerror: onFailure });
      return h("figure.rv-frame", { dataset: { file: f.sha256 } }, node,
        h("figcaption", h("strong", `File ${idx + 1}`), " ", h("code", shortSha(f.sha256, 12)), ` · ${f.name || ""}`, !String(alt).trim() ? h("span.bad", " · no alt text") : null));
    }) : media.map((m, idx) => {
      const a = S.assets[m.asset] || {};
      const url = localUrl(a.url || "");
      const node = !url ? h("div.lib-ph.rv-ph", "media file not available")
        : a.kind === "video" ? h("video.rv-media", { src: url, controls: true, preload: "metadata", "aria-label": m.alt || m.asset, onloadeddata: onReady, onerror: onFailure })
          : h("img.rv-media", { src: url, alt: m.alt || "", onload: onReady, onerror: onFailure });
      return h("figure.rv-frame", { dataset: { asset: m.asset } }, node,
        h("figcaption", h("strong", `Image ${idx + 1}`), " ", h("code", shortSha(a.sha256 || m.asset, 12)), a.bytes ? ` · ${fmtBytes(a.bytes)}` : "", a.w ? ` · ${a.w}×${a.h}` : "", a.license ? ` · ${a.license.summary}` : "",
          a.coverage && a.coverage.covered === false ? h("span.bad", " · no licence entry") : null,
          !String(m.alt || "").trim() ? h("span.bad", " · no alt text") : null));
    });
    if(bound?.cover?.url)frames.push(h("figure.rv-cover",h("img.rv-media",{src:localUrl(bound.cover.url),alt:bound.cover.alt||"Separate cover",onload:onReady,onerror:onFailure}),h("figcaption","Separate cover · never a carousel slide")));
    function onReady() { const nodes=frames.flatMap(f=>[...f.querySelectorAll('img,video')]);S.mediaReady=nodes.length>0&&nodes.every(n=>n.tagName==='IMG'?n.complete&&n.naturalWidth>0:n.readyState>=2&&n.videoWidth>0);if(S.mediaReady)startTimer(it,meter,meterText); }
    function onFailure(){S.mediaReady=false;stopTimer();S.viewed.delete(k);S.picked.delete(k);meterText.textContent='Media could not be decoded. Repair the file before reviewing.';drawBar();}

    // the words: the queue item's when the server gave them, else the post's (an older server, or no review item yet)
    const caption = bound ? bound.caption : effectiveCaption(p, v);
    const tags = bound ? bound.tags || [] : effectiveHashtags(p, v);
    const firstComment = bound ? bound.first_comment : effectiveFirstComment(p, v);
    const link = bound ? bound.link_url : p.copy && p.copy.link;
    const alts = bound ? bound.alt_texts || [] : media.map((m) => m.alt || "");
    const acct = S.accounts[v.account];
    const pv = JSON.parse(JSON.stringify(p));
    const vv = findVariant(pv, v.id);
    if (bound) {
      vv.copy = { ...vv.copy, caption: bound.caption || "", first_comment: bound.first_comment || null, hashtags: bound.tags || [] };
      pv.copy = { ...pv.copy, link: bound.link_url || null };
      if (files) vv.media = files.map((f, i) => ({ asset: (media[i] && media[i].asset) || "", alt: alts[i] || "", crop: (media[i] && media[i].crop) || {}, slot: i }));
      else (effectiveMedia(pv, vv) || []).forEach((m, i) => { m.alt = alts[i] || m.alt || ""; });
    }
    const model = previewModel(pv, vv, { assets: files ? Object.fromEntries(vv.media.map((m, i) => [m.asset, { ...(S.assets[m.asset] || {}), url: files[i].url }])) : S.assets, account: acct ? { id: v.account, label: acct.label } : null });
    const flags = v.flags || [];

    clear(stage).append(
      h("div.rv-sheet",
        h("header.rv-sheet-head", h("div", h("h2", postTitle(p, v)), h("p.st-hint", `${PLATFORM_NAMES[v.platform] || v.platform} ${v.post_type} · ${(acct || {}).label || v.account} · ${v.schedule ? fmtWhen(v.schedule.local, v.schedule.tz) : "no time set yet"}`)),
          h("div.rv-chips", statusChip(v.status), flags.map((f) => h("span.st-chip.rv-flagchip", { title: flagInfo(f)[1] }, h("span.g", { "aria-hidden": "true" }, "⚑"), flagInfo(f)[0])))),
        h("a.rv-skip", { href: "#rv-approval", onclick: (e) => { e.preventDefault(); bar.scrollIntoView({ block: "start" }); bar.querySelector("input")?.focus({ preventScroll: true }); } }, "Go to approval"),
        mism.length ? h("div.rv-mismatch", { role: "alert", dataset: { mismatch: mism.map((m) => m.field).join(",") } },
          h("p.rv-mm-head", h("strong", "This review item is not the post you see in the editor. ")),
          h("p", "An approval signs the review item below, not the post. They differ in " + mism.map((m) => (FIELD_NAMES[m.field] || m.field).toLowerCase()).join(", ") + ". It cannot be approved until they match. Open the composer and prepare review again. ", ""),
          h("table.rv-mm-table", h("thead", h("tr", h("th", "Field"), h("th", "Review item (what you would sign)"), h("th", "The post"))),
            h("tbody", mism.map((m) => h("tr", h("th", { scope: "row" }, FIELD_NAMES[m.field] || m.field), h("td", fmtVal(m.queue)), h("td", fmtVal(m.post))))))) : null,
        flags.includes("approval_stale") ? h("p.st-alert.warn.rv-stale", { role: "note" }, h("strong", "Your earlier approval no longer holds. "), v.status_reason ? v.status_reason.replace(/^approval no longer valid:\s*/, "") + ". " : "", "Look at it again and approve it again.") : null,
        h("div.rv-seal", { "aria-label": "What you are approving" },
          h("p.rv-sealhead", "You are approving these exact bytes"),
          h("p.rv-sha", { dataset: { sha: shaOf(it) || "" } }, h("code", groupSha(shaOf(it) || "not yet hashed"))),
          h("p.st-hint", "The post goes out only if this hash still matches at send time. Change anything below and the approval is voided.")),
        h("div.rv-frames", frames.length ? frames : h("p.st-alert.warn", "This post has no media.")),
        h("div.rv-view", meter, meterText),
        h("section.rv-signs", { "aria-label": "What the approval signs" },
          h("h3", bound ? "What your approval signs" : "Words and details"),
          bound ? null : h("p.st-hint", "This post has no review item yet, so these are the post's own words."),
          h("dl.rv-fields",
            row("Account", h("span", `${(acct || {}).label || bound?.account || v.account}`, h("span.st-hint", ` · ${PLATFORM_NAMES[v.platform] || v.platform} ${bound?.post_type || v.post_type}`))),
            row("Delivery",bound?.delivery?.method||"Automatic (legacy)"),
            row("Reel feed presence",bound?.presentation?.feed_presence===undefined?null:bound.presentation.feed_presence?"Show in profile feed · manual setting":"Keep off profile feed · manual setting"),
            row("Caption", h("pre.rv-pre", caption || "(empty)"), "wide"),
            row("Hashtags", tags.length ? tags.map((t) => "#" + t).join(" ") : "None"),
            row("First comment", firstComment ? h("pre.rv-pre", firstComment) : "None", firstComment ? "wide" : ""),
            row("Link", link ? h("code", link) : "None"),
            row("AI disclosure", bound ? (bound.ai_disclosure ? "Yes: this post carries an AI disclosure" : "No") : (p.disclosure && p.disclosure.ai_generated ? "Yes: AI-generated media" : "No")),
            row("Alt text", alts.length ? h("ol.rv-alts", alts.map((a) => h("li", { class: String(a || "").trim() ? "" : "missing" }, String(a || "").trim() || "Missing: screen readers get nothing here."))) : "No images", "wide"),
            bound ? row("Files", h("ul.rv-files", bound.files.map((f) => h("li", h("span.rv-fname", f.name || "file"), " ", h("code", { title: f.sha256 || "" }, f.sha256 ? groupSha(f.sha256.slice(0, 16)) : f.error || "unreadable"))))) : null)),
        S.cfg ? h("div.rv-preview", h("h3", "How it will look"), buildPreview(model, S.cfg)) : null,
        h("div.rv-actions", h("button", { type: "button", dataset: { act: "sendback" }, onclick: () => sendBack(it) }, "Request changes"), h("a.st-btn", { href: `#/post/${p.id}?variant=${v.id}` }, "Open in editor"))));
    if (!media.length && !files || already) { S.mediaReady = true; if (!already) startTimer(it, meter, meterText); }
  }
  const fmtVal = (x) => (x === null || x === undefined || x === "" ? h("em", "empty") : typeof x === "string" ? x : JSON.stringify(x));

  // ---- the view timer: counts only while the page is visible --------------------------------------------------------------------------
  function startTimer(it, meter, meterText) {
    stopTimer();
    const k = keyOf(it);
    if (S.viewed.has(k)) return;
    let last = performance.now();
    S.timer = setInterval(async () => {
      const now = performance.now();
      if (document.visibilityState === "visible") S.elapsed += now - last;
      last = now;
      meter.value = Math.min(VIEW_MS, S.elapsed);
      if (S.elapsed < VIEW_MS) { meterText.textContent = `Keep looking: ${Math.ceil((VIEW_MS - S.elapsed) / 1000)} s`; return; }
      stopTimer();
      try {
        const sha = shaOf(it);
        await ctx.api.post(`posts/${it.post.id}/view`, { variant: it.variant.id, sha, displayed_ms: Math.round(S.elapsed) });
        S.viewed.set(k, sha);
        if (!mismatchOf(it).length) S.picked.add(k);
        meterText.textContent = mismatchOf(it).length ? "Looked at. It cannot be approved while it differs from the post." : "Looked at. You can approve.";
        live.textContent = "Recorded that you looked at this post.";
        drawList(); drawBar();
      } catch (ex) {
        meterText.textContent = `Could not record the view: ${ex.message}`;
      }
    }, 100);
  }
  function stopTimer() { if (S.timer) { clearInterval(S.timer); S.timer = null; } }

  // ---- approve bar -------------------------------------------------------------------------------------------------------------------
  function drawBar() {
    clear(bar);
    if (!S.items.length) return;
    const chosen = S.items.filter((it) => S.picked.has(keyOf(it)) && S.viewed.has(keyOf(it)) && !S.results.has(keyOf(it)) && !mismatchOf(it).length);
    const by = h("input", { type: "text", name: "by", id: "rv-by", value: ctx.by ? ctx.by() || storeGet("studio.by", "") : storeGet("studio.by", ""), autocomplete: "name", size: 14, "aria-describedby": "rv-by-h" });
    const pin = h("input", { type: "password", name: "pin", id: "rv-pin", inputmode: "numeric", autocomplete: "off", maxlength: 12, size: 9, "aria-describedby": "rv-pin-h", placeholder: "6 digits" });
    const msg = h("p.rv-msg", { role: "alert" });
    const typedInputs = S.mode === "typed" ? chosen.map((it) => {
      const inp = h("input", { type: "text", name: "typed-" + keyOf(it), size: 10, autocomplete: "off", "aria-label": `Type the first 8 characters of the hash for ${it.post.id}` });
      return field(`Type ${shortSha(shaOf(it), 8)} to confirm ${it.post.id}`, inp);
    }) : [];
    const go = h("button.primary", { type: "submit", disabled: !chosen.length || S.busy }, chosen.length ? `Approve ${chosen.length}` : "Approve");
    const unlooked = S.items.filter((it) => !S.viewed.has(keyOf(it)) && !S.results.has(keyOf(it))).length;
    const f = h("form.rv-form", { onsubmit: async (e) => {
      e.preventDefault();
      if (!by.value.trim()) { msg.textContent = "Add your name."; by.focus(); return; }
      if (!pin.value.trim() && S.mode !== "typed") { msg.textContent = window.aveNative ? 'Choose Unlock review in the native app, then enter the PIN shown in its sheet.' : 'Enter the PIN printed in the terminal where ave studio ui is running.'; pin.focus(); return; }
      storeSet("studio.by", by.value.trim());
      await approve(chosen, by.value.trim(), pin.value.trim(), msg, S.mode === "typed" ? typedInputs.map((x) => x.querySelector("input").value.trim()) : null);
      pin.value = "";
    } },
    h("div.rv-formrow", field("Approved by", by, { id: "rv-by" }), S.mode === "typed" ? null : field("PIN", pin, { id: "rv-pin", hint: window.aveNative ? 'Choose Unlock review; the app shows the PIN in a native sheet.' : 'Printed once in the terminal where ave studio ui is running.' }), typedInputs, go),
    msg);
    const cur = S.items[S.index];
    const blocked = cur && mismatchOf(cur).length;
    bar.append(h("div.rv-bar-inner",
      h("h2.rv-bar-h", "Approve"),
      h("p.st-hint", chosen.length ? `${chosen.length} post${chosen.length === 1 ? "" : "s"} ticked. Approving records your approval only; nothing is sent.` : blocked ? "This one differs from the post, so it cannot be ticked. Move to another, or send it back." : "Look at a post, then tick it in the list to approve."),
      f,
      unlooked ? h("p.rv-next", h("button.ghost", { type: "button", dataset: { act: "next-unlooked" }, onclick: nextUnlooked }, `Next not looked at (${unlooked})`), h("span.kbd", "n")) : null));
  }
  function nextUnlooked() {
    const n = S.items.length;
    for (let d = 1; d <= n; d++) { const i = (S.index + d) % n; if (!S.viewed.has(keyOf(S.items[i])) && !S.results.has(keyOf(S.items[i]))) return select(i); }
    announce(ctx, "You have looked at every post.");
  }

  async function approve(chosen, by, pin, msg, typed) {
    if (S.busy || !chosen.length) return;
    S.busy = true; drawBar();
    try {
      const res = await ctx.api.post("approve", { items: chosen.map((it, i) => ({ post: it.post.id, variant: it.variant.id, sha: S.viewed.get(keyOf(it)), ...(typed ? { confirm: typed[i], typed: typed[i] } : {}) })), by, pin: pin || undefined });
      for (const a of res.approved || []) { S.results.set(`${a.post || chosen.find((c) => c.variant.id === a.variant)?.post.id}/${a.variant}`, `Approved by ${a.by}`); }
      for (const r of res.refused || []) S.results.set(`${r.post}/${r.variant}`, `Not approved: ${r.reason}`);
      (res.approved || []).forEach((a) => S.picked.delete(`${a.post}/${a.variant}`));
      const n = (res.approved || []).length;
      say(ctx, n ? `Approved ${n} post${n === 1 ? "" : "s"}. Nothing was sent.` : "Nothing was approved.", { error: !n });
      if ((res.refused || []).length) msg.textContent = (res.refused || []).map((r) => `${r.post}: ${r.reason}`).join(" ");
      S.busy = false; drawList(); drawBar();
      const nextIdx = S.items.findIndex((it) => !S.results.has(keyOf(it)));
      if (n && nextIdx >= 0) select(nextIdx);
    } catch (ex) {
      S.busy = false;
      const left = ex.body && ex.body.attempts_left;
      msg.textContent = ex.code === "pin.wrong" ? `That PIN is not right.${left !== undefined ? ` ${left} tries left.` : ""}`
        : ex.code === "pin.locked" ? window.aveNative ? 'Approval is locked. Reopen this workspace in the app, then unlock review again.' : 'Approval is locked after too many wrong PINs. Restart ave studio ui to unlock it.'
          : ex.code === "view.required" ? "Look at the post first: open it and keep the media on screen for 2 seconds."
            : ex.message;
      announce(ctx, msg.textContent, { assertive: true });
      drawBar();
      root.querySelector(".rv-msg").textContent = msg.textContent;
      if (ex.code === "pin.wrong") root.querySelector("#rv-pin")?.focus();
    }
  }

  function sendBack(it) {
    openSendBack(ctx, { post: it.post, variant: it.variant, rev: it.post.rev, asset: (effectiveMedia(it.post, it.variant)[0] || {}).asset || null, onSent: () => load() });
  }

  const onKey = (e) => {
    if (e.defaultPrevented || e.metaKey || e.ctrlKey || e.altKey) return;
    const t = e.target;
    if (t && (/^(INPUT|TEXTAREA|SELECT)$/.test(t.tagName) || t.isContentEditable)) return;
    if (document.querySelector("dialog[open]")) return;
    if (e.key === "j") { e.preventDefault(); select(S.index + 1); }
    else if (e.key === "k") { e.preventDefault(); select(S.index - 1); }
    else if (e.key === "n") { e.preventDefault(); nextUnlooked(); }
    else if (e.key === "s" && S.items[S.index]) { e.preventDefault(); sendBack(S.items[S.index]); }
  };
  document.addEventListener("keydown", onKey);
  load();
  return { state: S, destroy() { S.alive = false; stopTimer(); document.removeEventListener("keydown", onKey); } };
}

export function mountReviewFromHash(root, ctx, hash = location.hash) {
  const q = hashQuery(hash);
  return mountReview(root, ctx, { post: q.post || null, variant: q.variant || null });
}

void ApiError; void empty;
