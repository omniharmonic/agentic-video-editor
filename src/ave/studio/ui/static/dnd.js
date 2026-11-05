// Drag and drop on Pointer Events (spec 7.5). Not HTML5 DnD: it has no touch, no ghost control and is hard to test.
//
//   const dnd = createDnd();
//   dnd.source(cardEl, { payload: () => ({...}), enabled: () => true, handleSel: ".grip" });
//   dnd.target(cellEl, { accept: (p) => true, drop: async (p, info) => {...}, label: "Tuesday 3 November" });
//
// Pointer path: press, move 5px, a ghost follows the pointer, the target under it (document.elementsFromPoint) lights up,
// release drops. Escape cancels. Touch drags start only from the card's grip, so a finger on the card still scrolls the page.
// Keyboard path: KeyboardMover below. It is the accessibility path to every drag, not an extra.

import { announce } from "/app/shared/dom.js";

const THRESHOLD = 5;

export function createDnd() {
  const targets = new Map();          // element -> {accept, drop, label, kind}
  let drag = null;
  const listeners = new Set();
  const dnd = {
    get active() { return !!drag; },
    onChange(fn) { listeners.add(fn); return () => listeners.delete(fn); },
    reset() { targets.clear(); },     // views call this before they re-render and re-register
    target(el, spec) { targets.set(el, spec); el.dataset.dropKind = spec.kind || "target"; return el; },
    source(el, spec) {
      el.addEventListener("pointerdown", (e) => onDown(e, el, spec));
      if (spec.wholeSurface) el.addEventListener("dragstart", (e) => e.preventDefault());
      return el;
    },
    targetAt,
    cancel: () => end(false),
  };

  function emit() { for (const fn of listeners) fn(!!drag); }

  function targetAt(x, y, payload) {
    for (const el of document.elementsFromPoint(x, y)) {
      for (let n = el; n && n !== document.body; n = n.parentElement) {
        const t = targets.get(n);
        if (t && (!t.accept || t.accept(payload))) return { el: n, spec: t };
        if (t) break;                  // an element that refuses is still "under" the pointer: keep looking for an enclosing one only if it is not itself a target
      }
    }
    return null;
  }

  function onDown(e, el, spec) {
    if (e.button !== 0 || !e.isPrimary || drag) return;
    if (spec.enabled && !spec.enabled()) return;
    if (e.pointerType === "touch" && !spec.wholeSurface && !e.target.closest(spec.handleSel || ".grip")) return;
    if (e.target.closest("button, input, textarea, select, [data-nodrag]") &&
        !(spec.handleSel && e.target.closest(spec.handleSel))) return;
    drag = { el, spec, id: e.pointerId, x0: e.clientX, y0: e.clientY, started: false, payload: null, ghost: null, over: null, raf: 0, y: e.clientY };
    window.addEventListener("pointermove", onMove);
    window.addEventListener("pointerup", onUp);
    window.addEventListener("pointercancel", onCancel);
    window.addEventListener("keydown", onKey, true);
  }

  function start() {
    const d = drag;
    d.started = true;
    d.payload = d.spec.payload();
    const r = d.el.getBoundingClientRect();
    d.dx = d.x0 - r.left; d.dy = d.y0 - r.top;
    const g = d.el.cloneNode(true);
    g.removeAttribute("id");
    g.classList.add("dnd-ghost");
    g.setAttribute("aria-hidden", "true");
    g.querySelectorAll("[tabindex]").forEach((n) => n.removeAttribute("tabindex"));
    g.style.width = r.width + "px";
    g.style.height = r.height + "px";
    document.body.append(g);
    d.ghost = g;
    d.el.classList.add("is-dragging");
    document.body.classList.add("is-dragging");
    for (const [el, t] of targets) if (!t.accept || t.accept(d.payload)) el.classList.add("can-drop");
    try { d.el.setPointerCapture(d.id); } catch { /* element may be gone */ }
    emit();
    loop();
  }

  function loop() {                    // edge auto-scroll while dragging
    const d = drag;
    if (!d) return;
    const vh = innerHeight, edge = 56;
    if (d.y < edge) scrollBy(0, -Math.ceil((edge - d.y) / 4));
    else if (d.y > vh - edge) scrollBy(0, Math.ceil((d.y - (vh - edge)) / 4));
    d.raf = requestAnimationFrame(loop);
  }

  function onMove(e) {
    const d = drag;
    if (!d || e.pointerId !== d.id) return;
    if (!d.started) {
      if (Math.hypot(e.clientX - d.x0, e.clientY - d.y0) < THRESHOLD) return;
      start();
    }
    e.preventDefault();
    d.y = e.clientY;
    d.ghost.style.transform = `translate(${e.clientX - d.dx}px, ${e.clientY - d.dy}px) rotate(1.4deg)`;
    const hit = targetAt(e.clientX, e.clientY, d.payload);
    if ((hit && hit.el) !== (d.over && d.over.el)) {
      if (d.over) d.over.el.classList.remove("drop-hover");
      if (hit) hit.el.classList.add("drop-hover");
      d.over = hit;
    }
    d.lastX = e.clientX; d.lastY = e.clientY;
  }

  function onUp(e) {
    const d = drag;
    if (!d || e.pointerId !== d.id) return;
    end(!!d.started, e);
  }
  function onCancel(e) { if (drag && e.pointerId === drag.id) end(false); }
  function onKey(e) { if (e.key === "Escape" && drag && drag.started) { e.preventDefault(); e.stopPropagation(); end(false, null, true); } }

  function end(drop, e, escaped = false) {
    const d = drag;
    if (!d) return;
    drag = null;
    window.removeEventListener("pointermove", onMove);
    window.removeEventListener("pointerup", onUp);
    window.removeEventListener("pointercancel", onCancel);
    window.removeEventListener("keydown", onKey, true);
    cancelAnimationFrame(d.raf);
    const wasStarted = d.started;
    const hit = wasStarted && drop && e ? targetAt(e.clientX, e.clientY, d.payload) : null;     // resolved BEFORE cleanup: dropping the highlight reflows the page
    if (wasStarted) {
      try { d.el.releasePointerCapture(d.id); } catch { /* already released */ }
      d.ghost && d.ghost.remove();
      d.el.classList.remove("is-dragging");
      document.body.classList.remove("is-dragging");
      for (const el of targets.keys()) el.classList.remove("can-drop", "drop-hover");
      // a drag must not also "click" the card link under the pointer
      const swallow = (ev) => { ev.preventDefault(); ev.stopPropagation(); };
      window.addEventListener("click", swallow, { capture: true, once: true });
      setTimeout(() => window.removeEventListener("click", swallow, { capture: true }), 0);
    }
    emit();
    if (!wasStarted) return;
    if (drop && e) {
      if (hit) { hit.el.classList.add("drop-landed"); setTimeout(() => hit.el.classList.remove("drop-landed"), 400); return hit.spec.drop(d.payload, { el: hit.el, via: "pointer" }); }
      announce("Dropped outside a destination. Nothing changed.");
    } else if (escaped) {
      announce("Move cancelled.");
    }
  }
  return dnd;
}

/** Keyboard move: pick up (m), step through the ordered target list, drop (Enter) or cancel (Escape).
 *  spec: { targets: () => [{el, label, ...}], nav: (event, index, targets) => newIndex | null, drop: (payload, target) => Promise,
 *          start: (payload) => index, describe: (payload) => string } */
export class KeyboardMover {
  constructor() { this.cur = null; this._key = (e) => this.onKey(e); }
  get active() { return !!this.cur; }

  begin(payload, spec) {
    if (this.cur) this.cancel(true);
    const targets = spec.targets();
    if (!targets.length) { announce("There is nowhere to move this to in the visible range."); return false; }
    this.cur = { payload, spec, targets, i: Math.max(0, Math.min(targets.length - 1, spec.start(payload, targets))) };
    document.body.classList.add("kb-moving");
    window.addEventListener("keydown", this._key, true);
    announce(`Picked up ${spec.describe(payload)}. Arrow keys choose where it goes, Enter drops it, Escape cancels. ${this.cur.targets[this.cur.i].label}`);
    this.mark();
    return true;
  }

  mark() {
    const c = this.cur;
    for (const t of c.targets) t.el.classList.remove("kb-target");
    const t = c.targets[c.i];
    t.el.classList.add("kb-target");
    t.el.scrollIntoView({ block: "nearest", inline: "nearest" });
  }

  onKey(e) {
    const c = this.cur;
    if (!c) return;
    if (e.key === "Escape") { e.preventDefault(); e.stopPropagation(); this.cancel(); return; }
    if (e.key === "Enter") { e.preventDefault(); e.stopPropagation(); this.drop(); return; }
    const j = c.spec.nav(e, c.i, c.targets);
    if (j == null) {
      if (e.key.startsWith("Arrow") || e.key === "Tab") { e.preventDefault(); e.stopPropagation(); }
      return;
    }
    e.preventDefault(); e.stopPropagation();
    if (j === c.i) { announce(c.targets[c.i].label + " (no further in that direction)"); return; }
    c.i = j;
    this.mark();
    announce(c.targets[j].label);
  }

  clean() {
    window.removeEventListener("keydown", this._key, true);
    document.body.classList.remove("kb-moving");
    if (this.cur) for (const t of this.cur.targets) t.el.classList.remove("kb-target");
  }

  cancel(silent = false) {
    if (!this.cur) return;
    this.clean();
    this.cur = null;
    if (!silent) announce("Move cancelled.");
  }

  async drop() {
    const c = this.cur;
    if (!c) return;
    this.clean();
    this.cur = null;
    await c.spec.drop(c.payload, c.targets[c.i]);
  }
}
