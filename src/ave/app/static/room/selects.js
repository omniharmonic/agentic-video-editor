// The Selects view: the clips Claude selected, as cards in a bin beside the viewer. Each card has a still, its title, length and
// speakers, why it was picked, and where it stands: picked by the director, new (Claude's pick, not yet decided), an alternate the
// finder also surfaced (it keeps saying so once decided), a maybe, skipped, and whether the director flagged it. Keep, Maybe and Skip go through the clip finder's own decision
// path (what building the selects cut reads) and into the project log as the director's; each can be undone from its toast.

import { h, button, icon, busy, fmtTime, toast } from "../dom.js";
import { createViewer } from "./viewer.js";
import { flagPopover } from "./flag.js";
// a copy in line behind another camera's (x is minus the number of copies ahead)
const waitText = (x) => `Waiting for another camera to finish: ${-x === 1 ? "1 copy" : `${-x} copies`} ahead of this one.`;

const STATES = { picked: "Picked", new: "New", alternate: "Alternate", maybe: "Maybe", skipped: "Skipped" };
const FILTERS = [["all", "All"], ["picked", "Picked"], ["new", "New"], ["alternate", "Alternates"], ["flagged", "Flagged by you"]];
const VERBS = [["keep", "Keep", "check"], ["maybe", "Maybe", null], ["skip", "Skip", "x"]];
const DONE = { keep: (t) => `Picked “${t}”. It goes into the selects cut.`, maybe: (t) => `Marked “${t}” as a maybe`,
  skip: (t) => `Skipped “${t}”`, pending: (t) => `“${t}” is undecided again` };
const STILL_RETRIES = [2000, 5000, 12000];              // a still is made on first ask; a slow one gets three more looks
// the app's own words for a failed call on a select (never the server's text, never "the stage changed")
function decidePlain(e) {
  if (e.network) return "Could not record your call. The app is not answering: check that AVE Studio is still open.";
  if (e.status === 409) return "This select came from an older version of the cut, so it can only be skipped.";
  if (e.status === 404) return "That select is no longer in this project. Ask Claude to look at the selects again.";
  if (e.status === 400) return "That call cannot be recorded for this select. Ask Claude to look at it.";
  return "Could not record your call. Try again.";
}

// why a move was refused, in the app's words (the clip finder's reasons, reworded)
function nudgePlain(e) {
  if (e.status !== 400) return decidePlain(e);
  const m = String(e.message || "");
  const out = /would be ([\d.]+)s, outside ([\d.]+)-([\d.]+)s/.exec(m);
  if (out) return `That move does not work: the select would be ${(+out[1]).toFixed(1)} s long, and selects run ${out[2]} to ${out[3]} s.`;
  if (/overlap/.test(m)) return "That move does not work: it would run into another select.";
  if (/leaves the transcript/.test(m)) return "That move does not work: there is no sentence there.";
  if (/fillers/.test(m)) return "That move does not work: what is left is only fillers.";
  return "That move does not work for this select.";
}

const len = (s) => { s = Math.max(0, Math.floor(s)); return `${Math.floor(s / 60)}:${String(s % 60).padStart(2, "0")}`; };
const clock = (t) => fmtTime(t).replace(/\.\d$/, "");

export function createSelects({ call, plain, sendFlag, onInfo, ensureSource }) {
  let info = null, artifact = null, loading = null, clips = [], flags = [], filter = "all", text = "", currentId = null, lastToast = null;
  const cards = new Map();

  const viewer = createViewer({ label: "Select preview" });
  const detail = h("div", { class: "sel-detail", hidden: true });
  const whyHost = h("div", { class: "sel-whyhost" });
  const filterBtns = FILTERS.map(([key, label]) => {
    const b = h("button", { type: "button", class: "btn btn-tool btn-sm sel-filter", "aria-pressed": String(key === filter), dataset: { f: key } },
      h("span", {}, label), h("span", { class: "chip chip-count" }, "0"));
    b.addEventListener("click", () => { filter = key; paintBin(); });
    return b;
  });
  const search = h("input", { class: "input", type: "search", "aria-label": "Find a select", placeholder: "Find", autocomplete: "off", spellcheck: "false" });
  search.addEventListener("input", () => { text = search.value.trim().toLowerCase(); paintBin(); });
  search.addEventListener("keydown", (e) => { if (e.key === "Escape") { e.preventDefault(); e.stopPropagation(); search.blur(); } });
  const summary = h("p", { class: "sel-summary secondary-text" });
  const bin = h("div", { class: "sel-bin", role: "list", "aria-label": "Selects" });
  const msg = h("div", { class: "sel-msg", hidden: true });
  const toolbar = h("div", { class: "sel-toolbar" }, h("div", { class: "sel-filters", role: "group", "aria-label": "Show" }, filterBtns),
    h("span", { class: "sc-spacer" }), h("label", { class: "search-field" }, icon("search"), search));
  const main = h("section", { class: "sel-main", "aria-label": "Bin" }, toolbar, summary, h("div", { class: "sel-scroll" }, bin, msg));
  const el = h("div", { class: "selects", hidden: true }, main, h("aside", { class: "sel-side" }, viewer.el, detail));

  // `force` reads the bin again (Claude showed it, or the clip list changed); `keep` keeps the cards and the one chosen
  async function open(a, { force = false, keep = false } = {}) {
    if (!a || (!force && a === artifact && (info || loading))) return;
    keep = keep && a === artifact && !!info;
    if (a !== artifact) currentId = null;
    artifact = a;
    if (!keep) {
      info = null; cards.clear();
      bin.replaceChildren(...[0, 1, 2, 3, 4, 5].map(() => h("div", { class: "sel-card is-skeleton", "aria-hidden": "true" },
        h("span", { class: "skeleton sel-thumb" }), h("span", { class: "skeleton skeleton-title" }), h("span", { class: "skeleton skeleton-text" }))));
      msg.hidden = true;
    }
    const mine = loading = call("GET", `/api/room/selects?artifact=${encodeURIComponent(a)}`);
    try {
      const got = await mine;
      if (mine !== loading) return;
      info = got;
    } catch (e) {
      if (mine !== loading) return;
      loading = null;
      bin.replaceChildren();
      showMsg(h("h2", {}, "The selects could not be read"), h("p", { class: "secondary-text" }, plain(e, "read the selects")),
        button("Try again", { variant: "primary", onclick: () => open(a, { force: true }) }));
      return;
    }
    loading = null;
    onInfo?.(info);
    clips = info.clips || [];
    for (const id of [...cards.keys()]) if (!clips.some((c) => c.id === id)) cards.delete(id);
    if (currentId && !clips.some((c) => c.id === currentId)) currentId = null;
    paintBin();
    if (!currentId && clips[0]) choose(clips[0], { play: false });
  }
  function showMsg(...kids) { msg.hidden = false; msg.replaceChildren(h("div", { class: "stage-msg" }, ...kids)); }

  const flagFor = (c) => flags.filter((f) => f.view === "selects" && f.clip === c.id && f.artifact === artifact);
  const matches = (c, f) => f === "all" || (f === "flagged" ? flagFor(c).length > 0 : f === "alternate" ? c.origin === "finder" : c.state === f);
  const visible = (c) => matches(c, filter)
    && (!text || `${c.title} ${c.why} ${c.text} ${c.speakers}`.toLowerCase().includes(text));

  function paintBin() {
    for (const b of filterBtns) {
      const f = b.dataset.f;
      b.setAttribute("aria-pressed", String(f === filter));
      b.classList.toggle("is-on", f === filter);
      b.querySelector(".chip-count").textContent = String(clips.filter((c) => matches(c, f)).length);
    }
    const n = (s) => clips.filter((c) => c.state === s).length;
    const alts = clips.filter((c) => c.origin === "finder").length;
    summary.textContent = clips.length ? `${clips.length} ${clips.length === 1 ? "select" : "selects"}: ${n("picked")} picked, ${n("new")} new from Claude, ${alts} ${alts === 1 ? "alternate" : "alternates"}` : "";
    if (!info) return;
    if (!clips.length) {
      bin.replaceChildren();
      const why = { unreadable: ["The selects could not be read", "The clip list is there but damaged. Ask Claude to run the clip finder again."],
        empty: ["No selects in the list", "The clip finder ran but kept nothing. Ask Claude to look again, perhaps with shorter clips."] }[info.clips_state]
        || ["No selects yet", "Ask Claude to find the best moments. Its picks appear here as cards you can keep, maybe or skip."];
      showMsg(h("h2", {}, why[0]), h("p", { class: "secondary-text" }, why[1]));
      return;
    }
    const shown = clips.filter(visible);
    msg.hidden = !!shown.length;
    if (!shown.length) showMsg(h("h2", {}, "Nothing here"), h("p", { class: "secondary-text" }, "No select matches what you chose to show."),
      button("Show all", { variant: "secondary", onclick: () => { filter = "all"; search.value = ""; text = ""; paintBin(); } }));
    bin.replaceChildren(...shown.map((c) => cardFor(c)));
  }

  function cardFor(c) {
    let card = cards.get(c.id);
    if (!card) { card = buildCard(c); cards.set(c.id, card); }
    paintCard(card, c);
    return card.el;
  }

  function buildCard(c) {
    const img = c.thumb ? h("img", { alt: "", loading: "lazy", decoding: "async", src: c.thumb }) : null;
    const thumb = h("span", { class: "sel-thumb" }, img, h("span", { class: "sel-len num" }, len(c.duration)), h("span", { class: "sel-play", "aria-hidden": "true" }, icon("play")));
    if (img) {
      let tries = 0;
      const missing = h("span", { class: "sel-nostill" }, "No still yet");
      thumb.classList.add("is-loading");
      img.addEventListener("load", () => { thumb.classList.remove("is-loading", "is-missing"); missing.remove(); });
      img.addEventListener("error", () => {
        thumb.classList.remove("is-loading"); thumb.classList.add("is-missing");
        if (!missing.isConnected) thumb.prepend(missing);
        if (tries < STILL_RETRIES.length) {
          setTimeout(() => { if (img.isConnected) img.src = `${c.thumb}&try=${tries}`; }, STILL_RETRIES[tries]);
          tries += 1;
        }
      });
    } else thumb.classList.add("is-missing");
    const title = h("span", { class: "sel-title" });
    const open = h("button", { type: "button", class: "sel-open" }, thumb, title);
    open.addEventListener("click", () => choose(currentClip(c.id), { play: true }));
    const state = h("span", { class: "sel-state" });
    const origin = h("span", { class: "sel-state sel-origin", dataset: { state: "alternate" }, hidden: true }, "Alternate");
    const staleNote = h("p", { class: "sel-stale-note", hidden: true });
    const flagged = h("span", { class: "sel-flagged", hidden: true });
    const meta = h("p", { class: "sel-meta" });
    const why = h("p", { class: "sel-why" });
    const verdict = VERBS.map(([v, label, ic]) => button(label, { variant: "tertiary", size: "sm", iconName: ic || undefined, class: `sel-verdict sel-${v}`, "aria-pressed": "false",
      onclick: (e) => decide(currentClip(c.id), v, e.currentTarget) }));
    const flagBtn = button("Flag this select for Claude", { variant: "tertiary", size: "sm", iconName: "flag", iconOnly: true, class: "sel-flag-btn",
      onclick: (e) => flagClip(currentClip(c.id), e.currentTarget) });
    const el = h("article", { class: "sel-card", role: "listitem", dataset: { clip: c.id } }, open,
      h("div", { class: "sel-badges" }, state, origin, flagged), meta, why, staleNote,
      h("div", { class: "sel-actions" }, h("div", { class: "sel-verdicts", role: "group", "aria-label": "Your call" }, verdict), flagBtn));
    return { el, state, origin, staleNote, flagged, meta, why, title, verdict, flagBtn, open };
  }
  const currentClip = (id) => clips.find((x) => x.id === id);

  // arrow keys move between cards (Up and Down by a row of the grid), Home and End to the first and last
  bin.addEventListener("keydown", (e) => {
    const opens = [...bin.querySelectorAll(".sel-open")];
    const i = opens.indexOf(document.activeElement);
    if (i < 0) return;
    const cols = Math.max(1, getComputedStyle(bin).gridTemplateColumns.split(" ").filter(Boolean).length);
    const j = { ArrowRight: i + 1, ArrowLeft: i - 1, ArrowDown: i + cols, ArrowUp: i - cols, Home: 0, End: opens.length - 1 }[e.key];
    if (j == null) return;
    e.preventDefault();
    opens[Math.max(0, Math.min(opens.length - 1, j))].focus();
  });

  function paintCard(card, c) {
    card.title.textContent = c.title;
    card.open.setAttribute("aria-label", `Preview ${c.title}`);
    card.state.textContent = STATES[c.state] || "New";
    card.state.dataset.state = c.state;
    card.origin.hidden = !(c.origin === "finder" && c.state !== "alternate");      // a decided alternate still says it is one
    card.meta.replaceChildren(...[h("span", { class: "num" }, len(c.duration)), c.speakers ? h("span", {}, c.speakers) : null].filter(Boolean));
    card.why.textContent = c.why || "";
    card.why.hidden = !c.why;
    card.staleNote.hidden = !c.stale;
    card.staleNote.textContent = c.stale_reason || "";
    for (const b of card.verdict) {
      const v = VERBS.find(([x]) => b.classList.contains(`sel-${x}`))[0];
      b.setAttribute("aria-pressed", String(c.verdict === v));
      const blocked = !!c.stale && (v === "keep" || v === "maybe") && c.verdict !== v;
      b.disabled = blocked;
      b.dataset.tip = blocked ? "Only skip: this select is from an older cut" : c.verdict === v ? `Take back ${v === "keep" ? "the pick" : v === "maybe" ? "the maybe" : "the skip"}` : "";
    }
    const mine = flagFor(c);
    card.flagged.hidden = !mine.length;
    if (mine.length) {
      const read = mine.every((f) => f.status === "read");
      card.flagged.dataset.state = read ? "read" : "waiting";
      card.flagged.replaceChildren(icon("flag"), h("span", { class: "sel-flagged-who" }, "Flagged by you"),
        h("span", { class: "sel-flagged-state" }, read ? "Claude has it" : "Waiting for your next message"));
      card.flagged.title = mine.map((f) => f.note).filter(Boolean).join("\n");
    }
    card.el.classList.toggle("is-current", c.id === currentId);
  }

  function choose(c, { play }) {
    if (!c) return;
    currentId = c.id;
    for (const [id, card] of cards) card.el.classList.toggle("is-current", id === currentId);
    detail.hidden = false;
    detail.replaceChildren(h("h2", { class: "sel-detail-title" }, c.title),
      h("p", { class: "sel-meta" }, h("span", { class: "num" }, `${clock(c.e0)} to ${clock(c.e1)}`), c.speakers ? h("span", {}, c.speakers) : null),
      c.why ? h("p", { class: "sel-why" }, c.why) : null,
      c.text ? h("blockquote", { class: "sel-text" }, c.text) : null,
      c.stale ? null : nudgeRow(c), whyHost);
    paintWhy(c);
    if (!c.play) {
      viewer.load(null);
      viewer.setMessage(h("div", { class: "stage-msg" }, h("h2", {}, "No preview of this select yet"),
        h("p", { class: "secondary-text" }, "Its recording is not in this project folder. Ask Claude to render the cut it came from.")));
      viewer.setLabel(c.title);
      return;
    }
    const go = () => {
      if (currentId !== c.id) return;
      viewer.setMessage(null);
      const text = `${c.speakers ? c.speakers + ", " : ""}${clock(c.e0)} to ${clock(c.e1)}`;
      if (play) viewer.playRange(c.play, c.play.start, c.play.end, { text });
      else viewer.cue(c.play, c.play.start, { text });
    };
    if (c.play.kind !== "source" || c.play.ready) { go(); return; }
    const bar = h("span", { class: "progress-fill" });
    viewer.load(null);
    viewer.setLabel(c.title);
    viewer.setMessage(h("div", { class: "stage-msg" }, h("span", { class: "progress", role: "progressbar", "aria-label": "Making a copy of the camera to play here" }, bar),
      h("p", {}, "Making a copy of the camera to play here"), h("p", { class: "secondary-text" }, "The original stays untouched. It plays the moment the copy is ready.")));
    const note = viewer.el.querySelector(".viewer-overlay .secondary-text"), meanwhile = note?.textContent;
    ensureSource(artifact, c.play.key, (x) => {
      if (x < 0) { if (note) note.textContent = waitText(x); return; }
      if (note) note.textContent = meanwhile;
      bar.style.width = `${Math.round(x * 100)}%`;
    }).then(() => {
      for (const x of clips) if (x.play && x.play.key === c.play.key) x.play.ready = true;
      go();
    }).catch((e) => {
      if (currentId !== c.id) return;
      viewer.setMessage(h("div", { class: "stage-msg" }, h("h2", {}, "This select could not be prepared"),
        h("p", { class: "secondary-text" }, `Its camera could not be copied for the viewer: ${e.message ? e.message[0].toLowerCase() + e.message.slice(1) : ""}`),
        button("Try again", { variant: "primary", onclick: () => choose(c, { play }) })));
    });
  }

  // Undo restores the call before, and only while the clip still holds the call its toast announced; a newer call replaces the
  // older toast, so there is never an Undo on screen for something that is no longer true.
  async function decide(c, v, btn) {
    if (!c) return;
    const before = c.verdict || "pending";
    const next = c.verdict === v ? "pending" : v;               // pressing the chosen one again takes it back
    try {
      await busy(btn, () => apply(c, next));
      lastToast?.close?.();
      const mine = lastToast = toast(DONE[next](c.title), { key: "select-call", action: "Undo", onAction: () => {
        const now = currentClip(c.id);
        if (!now || (now.verdict || "pending") !== next) { toast("Nothing to undo: the call on this select changed since.", { key: "select-call" }); return; }
        apply(now, before).then(() => { if (lastToast === mine) lastToast = null; toast(`Undone: “${c.title}” is back as it was`, { key: "select-call" }); })
          .catch((e) => toast(decidePlain(e), { tone: "negative", key: "select-call" }));
      } });
    } catch (e) { toast(decidePlain(e), { tone: "negative", key: "select-call" }); }
  }
  async function apply(c, verdict) {
    const got = await call("POST", "/api/room/selects/decide", { artifact, clip: c.id, verdict });
    update(got);
    return got;
  }
  function update(got) {
    const c = currentClip(got.clip); if (!c) return;
    const changed = (c.verdict ?? null) !== (got.verdict ?? null);
    c.state = got.state || c.state; c.verdict = got.verdict ?? null; if (got.origin) c.origin = got.origin;
    if (changed) { c.reasons = got.reasons || []; c.note = got.note || ""; }
    paintBin();
    if (c.id === currentId) paintWhy(c);
  }

  // Why: after a keep, maybe or skip, the reasons that fit that call and a note, as the old clip desk took them. They go to the
  // learning ledger (work/learning/clip_decisions.jsonl) through the clip finder's own decision, so `ave learn taste` reads them.
  const VERDICT_WORD = { keep: "kept", maybe: "a maybe", skip: "skipped" };
  function paintWhy(c) {
    const v = c.verdict;
    if (!v || c.stale) { whyHost.replaceChildren(); return; }
    const all = (info?.reasons || []).filter((r) => (r.verdicts || []).includes(v));
    const chosen = new Set(c.reasons || []);
    const chips = all.map((r) => h("button", { type: "button", class: "chip sel-reason", "aria-pressed": String(chosen.has(r.id)), dataset: { reason: r.id },
      onclick: (e) => { chosen.has(r.id) ? chosen.delete(r.id) : chosen.add(r.id); e.currentTarget.setAttribute("aria-pressed", String(chosen.has(r.id))); } }, r.label));
    const note = h("textarea", { class: "input sel-note", rows: "2", maxlength: "2000", "aria-label": "Note about this call", placeholder: "Anything else Claude should learn from this call" });
    note.value = c.note || "";
    const save = button("Save why", { variant: "secondary", size: "sm", "data-act": "save-why", onclick: (e) => busy(e.currentTarget, async () => {
      try {
        const got = await call("POST", "/api/room/selects/decide", { artifact, clip: c.id, verdict: v, reasons: [...chosen], note: note.value.trim() });
        c.reasons = [...chosen]; c.note = note.value.trim();
        update(got);
        toast(`Saved why “${c.title}” is ${VERDICT_WORD[v]}. Claude learns from it.`, { key: "select-call" });
      } catch (er) { toast(decidePlain(er), { tone: "negative", key: "select-call" }); }
    }) });
    whyHost.replaceChildren(h("div", { class: "sel-why-form", role: "group", "aria-label": "Why this call" },
      h("span", { class: "sel-nudge-label" }, `Why ${VERDICT_WORD[v]}`),
      chips.length ? h("div", { class: "sel-reasons" }, chips) : null, note, h("div", {}, save)));
  }

  // Move where a select starts or ends by a whole sentence (the old clip desk's nudge). The clip finder checks the new range; the
  // select goes back to undecided, so it is kept again on its new range.
  function nudgeRow(c) {
    const step = (end, by, label) => button(label, { variant: "secondary", size: "sm", text: by < 0 ? "A sentence earlier" : "A sentence later", "data-nudge": `${end}${by > 0 ? "+" : "-"}`,
      onclick: (e) => busy(e.currentTarget, async () => {
        try {
          const got = await call("POST", "/api/room/selects/nudge", { artifact, clip: c.id, nudge: { [end]: by } });
          toast(`“${c.title}” now ${end === "start" ? "starts" : "ends"} a sentence ${by < 0 ? "earlier" : "later"}. Keep it again if it works.`, { key: "select-call" });
          await open(artifact, { force: true, keep: true });
          const now = currentClip(c.id); if (now) choose(now, { play: false });
          return got;
        } catch (er) { toast(nudgePlain(er), { tone: "negative", key: "select-call" }); }
      }) });
    return h("div", { class: "sel-nudge", role: "group", "aria-label": "Move the start or end" },
      h("span", { class: "sel-nudge-label" }, "Starts"), step("start", -1, "Start a sentence earlier"), step("start", 1, "Start a sentence later"),
      h("span", { class: "sel-nudge-label" }, "Ends"), step("end", -1, "End a sentence earlier"), step("end", 1, "End a sentence later"));
  }

  function flagClip(c, anchor) {
    if (!c) { toast("Choose a select first, then flag it."); return; }
    const card = cards.get(c.id);
    flagPopover(anchor || card?.flagBtn, { title: `Flag “${c.title}”`,
      send: async (note) => {
        try {
          await sendFlag({ artifact, view: "selects", clip: c.id, start: c.e0, end: c.e1, note, text: c.title.slice(0, 2000) });
          toast(`Flagged “${c.title}” for Claude`, { key: "flag" });
        } catch (e) { toast(plain(e, "flag this select"), { tone: "negative" }); throw e; }
      } });
  }

  return {
    el, open, viewer,
    get info() { return info; },
    setFlags(list) { flags = list; if (info) paintBin(); },
    applyDecision(d) { if (d.artifact === artifact) update(d); },
    show(on) { el.hidden = !on; if (!on) viewer.pause(); },
    focusSearch() { search.focus(); search.select(); },
    flagKey() {
      const focused = document.activeElement?.closest?.(".sel-card");
      const c = currentClip(focused?.dataset.clip) || currentClip(currentId);
      flagClip(c, c ? cards.get(c.id)?.flagBtn : null);
    },
    close() {},
  };
}
