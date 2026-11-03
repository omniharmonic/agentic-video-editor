// "Request changes" modal (spec 7.5) and the #/feedback page (spec 7.2).
//
//   openSendBack(ctx, {post, variant, rev, asset?, copy?, onSent?})  -> {close}
//       copy = {field, before, selection:[a,b]|null}  (the editor passes the focused field and its selection)
//   mountFeedback(root, ctx) -> {destroy}
import { ago, ApiError, clear, debounce, effectiveCaption, empty, errorBox, field, findVariant, h, openModal, say, storeGet, storeSet } from "./panels.js";

export const KINDS = [
  ["rework_copy", "Rework the copy", "the editor rewrites the words. Say what is wrong with them."],
  ["rework_visual", "Rework the visual", "the editor picks or makes another image. Say what to change."],
  ["retime", "Change the time", "the editor proposes another slot. The post keeps its place until you accept."],
  ["drop", "Drop this post", "Removes it from the queue and voids any approval."],
];
const COPY_FIELDS = [["caption", "Caption"], ["first_comment", "First comment"], ["hashtags", "Hashtags"], ["base", "Shared copy (all accounts)"]];
const PROBLEMS = [["too_busy", "Too busy"], ["off_brand", "Off brand"], ["wrong_crop", "Wrong crop"], ["low_res", "Low resolution"], ["license", "Licence problem"], ["other", "Something else"]];
const DROP_REASONS = ["off message", "duplicate", "timing", "other"];
const DOWS = ["mon", "tue", "wed", "thu", "fri", "sat", "sun"];

export function copyBefore(post, variant, fieldName) {
  if (fieldName === "base") return (post.copy && post.copy.base) || "";
  if (fieldName === "caption") return effectiveCaption(post, variant);
  if (fieldName === "hashtags") return ((variant && variant.copy && variant.copy.hashtags) || (post.copy && post.copy.hashtags) || []).join(" ");
  const own = variant && variant.copy && variant.copy[fieldName];
  return own === null || own === undefined ? (post.copy && post.copy[fieldName]) || "" : own;
}

/** Build the request body from the form state. Exported so tests can check the exact shape without a browser. */
export function buildFeedbackBody({ variant, kinds, note, copy, visual, retime, drop }) {
  const structured = {};
  if (kinds.includes("rework_copy")) structured.copy = { field: copy.field, before: copy.before, after: null, selection: copy.selection || null };
  if (kinds.includes("rework_visual")) structured.visual = { asset: visual.asset || null, problem: visual.problem, wanted: visual.wanted || "" };
  if (kinds.includes("retime")) {
    const prefer = {};
    if (retime.after) prefer.after = retime.after;
    if (retime.window) prefer.window = retime.window;
    if (retime.avoid_dow && retime.avoid_dow.length) prefer.avoid_dow = retime.avoid_dow;
    structured.retime = { from: retime.from || null, prefer };
  }
  if (kinds.includes("drop")) structured.drop = { reason: drop.reason };
  return { variant, kinds, note: note || "", structured };
}

export function openSendBack(ctx, { post, variant, rev, asset = null, copy = null, onSent = null }) {
  const v = typeof variant === "string" ? findVariant(post, variant) : variant;
  const media = (v && v.media) || post.media || [];
  const state = { kinds: new Set(), busy: false };
  const form = h("form.st-form", { novalidate: true });
  const err = h("div.st-slot", { "aria-live": "polite" });

  const noteEl = h("textarea", { name: "note", rows: 3, placeholder: "Tell the editor what to change, in your own words." });
  const copyField = h("select", { name: "copy-field" }, COPY_FIELDS.map(([k, l]) => h("option", { value: k, selected: (copy && copy.field ? copy.field : "caption") === k }, l)));
  const copyBeforeEl = h("textarea", { name: "copy-before", rows: 3, readonly: true, "aria-label": "Current text" });
  const selectionNote = h("p.st-hint");
  const syncCopy = () => {
    const f = copyField.value;
    copyBeforeEl.value = copy && copy.field === f && copy.before !== undefined ? copy.before : copyBefore(post, v, f);
    selectionNote.textContent = copy && copy.field === f && copy.selection ? `the editor will focus on characters ${copy.selection[0]} to ${copy.selection[1]}.` : "";
  };
  copyField.addEventListener("change", syncCopy);
  syncCopy();
  const assetSel = h("select", { name: "visual-asset" }, (media.length ? media : [{ asset }]).filter((m) => m.asset).map((m,i) => h("option", { value: m.asset, selected: m.asset === asset }, (m.role==="cover"?"Cover":"Slide "+(i+1))+(m.alt?" · "+m.alt:""))));
  const problemSel = h("select", { name: "visual-problem" }, PROBLEMS.map(([k, l]) => h("option", { value: k }, l)));
  const wanted = h("input", { type: "text", name: "visual-wanted", placeholder: "For example: calmer, more daylight" });
  const after = h("input", { type: "date", name: "retime-after" });
  const windowSel = h("select", { name: "retime-window" }, [["", "Any time of day"], ["morning", "Morning"], ["midday", "Midday"], ["afternoon", "Afternoon"], ["evening", "Evening"]].map(([k, l]) => h("option", { value: k }, l)));
  const dows = DOWS.map((d) => h("label.st-check", h("input", { type: "checkbox", name: "avoid-" + d, value: d }), d));
  const dropReason = h("select", { name: "drop-reason" }, DROP_REASONS.map((r) => h("option", { value: r }, r)));

  const panels = {
    rework_copy: h("fieldset.st-sub", h("legend", "About the copy"), field("Which text", copyField), field("Current text", copyBeforeEl, { hint: selectionNote })),
    rework_visual: h("fieldset.st-sub", h("legend", "About the visual"), field("Which image", assetSel), field("What is wrong", problemSel), field("What you want instead", wanted)),
    retime: h("fieldset.st-sub", h("legend", "About the time"), field("Not before", after), field("Time of day", windowSel), h("div.st-field", h("span.st-label", "Avoid these days"), h("div.st-checks", dows))),
    drop: h("fieldset.st-sub", h("legend", "Why drop it"), field("Reason", dropReason), h("p.st-alert.warn", "Dropping rejects the queue item and voids any approval. It cannot be undone from here.")),
  };
  for (const el of Object.values(panels)) el.hidden = true;

  const checks = KINDS.map(([k, label, hint]) => {
    const cb = h("input", { type: "checkbox", name: "kind", value: k, id: "sb-" + k });
    cb.addEventListener("change", () => {
      if (cb.checked) state.kinds.add(k); else state.kinds.delete(k);
      panels[k].hidden = !cb.checked;
      if (k === "drop" && cb.checked) panels.drop.scrollIntoView({ block: "nearest" });
    });
    return h("div.st-kind", h("label.st-check", { for: "sb-" + k }, cb, h("span", h("strong", label), h("span.st-hint", hint))), panels[k]);
  });

  form.append(h("fieldset.st-kinds", h("legend", "What should happen"), checks), field("Note for the editor", noteEl, { wide: true }), err);

  const send = h("button.primary", { type: "submit", form: "st-sendback" }, "Send to the editor");
  form.id = "st-sendback";
  const modal = openModal({ title: "Request changes", body: form, wide: true, actions: [h("button", { type: "button", onclick: () => modal.close("cancel") }, "Cancel"), send] });

  form.addEventListener("submit", async (e) => {
    e.preventDefault();
    clear(err);
    const kinds = KINDS.map(([k]) => k).filter((k) => state.kinds.has(k));
    if (!kinds.length) { err.append(h("p.st-alert.bad", { role: "alert" }, "Pick at least one thing for the editor to do.")); return; }
    if ((kinds.includes("rework_copy") || kinds.includes("rework_visual")) && !noteEl.value.trim() && !wanted.value.trim()) {
      err.append(h("p.st-alert.bad", { role: "alert" }, "Add a note so the editor knows what to change."));
      noteEl.focus();
      return;
    }
    const body = buildFeedbackBody({
      variant: v ? v.id : null, kinds, note: noteEl.value.trim(),
      copy: { field: copyField.value, before: copyBeforeEl.value, selection: copy && copy.field === copyField.value ? copy.selection : null },
      visual: { asset: assetSel.value || asset, problem: problemSel.value, wanted: wanted.value.trim() },
      retime: { from: v && v.schedule ? v.schedule.local : null, after: after.value, window: windowSel.value, avoid_dow: dows.map((d) => d.querySelector("input")).filter((i) => i.checked).map((i) => i.value) },
      drop: { reason: dropReason.value },
    });
    if (asset) body.asset = asset;
    if (rev !== undefined && rev !== null) body.rev = rev;
    send.disabled = true;
    try {
      const res = await ctx.api.post(`posts/${post.id}/feedback`, body);
      say(ctx, kinds.includes("drop") ? "Dropped. the editor will not rework this post." : "Sent to the editor. The card now shows waiting for the editor.");
      modal.close("sent");
      if (onSent) onSent(res);
    } catch (ex) {
      send.disabled = false;
      err.append(errorBox(ex instanceof ApiError || ex instanceof Error ? ex : new Error(String(ex))));
    }
  });
  return modal;
}

// ---- #/feedback page -----------------------------------------------------------------------------------------------------------------
function claimText(fb, now) {
  if (fb.resolved) return `Resolved by ${fb.resolved.by || "the editor"} ${ago(fb.resolved.ts, now)}${fb.resolved.note ? `: ${fb.resolved.note}` : ""}`;
  if (fb.claim) return `Claimed by ${fb.claim.by} ${ago(fb.claim.ts, now)}`;
  return "Waiting for the editor";
}
const KIND_LABEL = { rework_copy: "Rework copy", rework_visual: "Rework visual", drop: "Drop", retime: "Retime", human_edit: "Your edit", note: "Note" };

function structuredSummary(fb) {
  const s = fb.structured || {};
  const rows = [];
  if (s.copy) rows.push(["Text", `${s.copy.field}${s.copy.selection ? ` (characters ${s.copy.selection[0]} to ${s.copy.selection[1]})` : ""}`], s.copy.before ? ["Was", s.copy.before] : null, s.copy.after ? ["Now", s.copy.after] : null);
  if (s.visual) rows.push(["Problem", `${String(s.visual.problem || "").replace(/_/g, " ")}${s.visual.wanted ? `: ${s.visual.wanted}` : ""}`]);
  if (s.retime) rows.push(["Prefers", JSON.stringify(s.retime.prefer || {})]);
  if (s.drop) rows.push(["Reason", s.drop.reason]);
  return h("dl.st-dl", rows.filter(Boolean).map(([k, v]) => [h("dt", k), h("dd", v)]));
}

export function mountFeedback(root, ctx) {
  const state = { status: storeGet("studio.feedback.status", "open"), list: [] };
  const list = h("div.st-list", { "aria-live": "polite" });
  const tabs = h("div.st-tabs", { role: "tablist", "aria-label": "Feedback status" });
  root.classList.add("st");
  clear(root).append(h("section.st-page", h("header.st-pagehead", h("h1", "Feedback"), h("p.st-hint", "What you sent back to the editor, and what the editor did about it.")), tabs, list));
  let alive = true;

  function drawTabs() {
    clear(tabs);
    ["open", "claimed", "resolved"].forEach((s) => {
      const on = state.status === s;
      tabs.append(h("button.st-tab", { role: "tab", "aria-selected": String(on), tabindex: on ? "0" : "-1", onclick: () => { state.status = s; storeSet("studio.feedback.status", s); load(); } }, s[0].toUpperCase() + s.slice(1)));
    });
  }
  async function load() {
    drawTabs();
    clear(list).append(h("p.st-hint", "Loading..."));
    try {
      const res = await ctx.api.get(`feedback?status=${state.status}`);
      if (!alive) return;
      state.list = res.feedback || [];
      draw();
    } catch (ex) { clear(list).append(errorBox(ex, load)); }
  }
  function draw() {
    clear(list);
    if (!state.list.length) { list.append(empty(state.status === "open" ? "Nothing is waiting for the editor." : `No ${state.status} feedback.`, "Use Request changes on any post to leave a note.")); return; }
    for (const fb of state.list) {
      list.append(h("article.st-card.fb", { dataset: { fb: fb.id, kind: fb.kind } },
        h("header", h("span.st-chip.k-" + fb.kind, KIND_LABEL[fb.kind] || fb.kind), h("a", { href: `#/post/${fb.post}${fb.variant ? "?variant=" + fb.variant : ""}` }, fb.post), h("span.st-hint", ` ${fb.variant || ""} · ${ago(fb.ts, ctx.now ? ctx.now() : new Date())}`)),
        fb.note ? h("p.fb-note", fb.note) : null,
        structuredSummary(fb),
        h("footer", h("span.st-claim", claimText(fb, ctx.now ? ctx.now() : new Date())))));
    }
  }
  load();
  const poll = setInterval(() => { if (document.visibilityState === "visible") load(); }, 15000);
  return { destroy() { alive = false; clearInterval(poll); }, reload: load };
}
