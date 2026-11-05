// Kanban: one column per stage. Dragging a card to another stage is a sign-off by the person (or a reopen, going back); dragging it
// within its column puts it in your own order. Out is locked because it needs the publish approval, so a card dropped there goes back
// with the reason. From the keyboard, Shift with Left or Right moves the focused card one stage.

import { h, icon, toast, button } from "../dom.js";
import { trackDrag, STAGES, stageClass, avatar, fmtDate, isLate, lowerFirst, midTruncate } from "./common.js";

export function createKanban(ctx) {
  const { S, rows, act } = ctx;
  const board = h("div", { class: "kb", role: "list", "aria-label": "Artifacts by stage" });
  const fadeStart = h("div", { class: "kb-fade kb-fade-start", "aria-hidden": "true" });
  const fadeEnd = h("div", { class: "kb-fade kb-fade-end", "aria-hidden": "true" });
  const toStart = button("Earlier stages", { variant: "secondary", size: "sm", iconName: "right", iconOnly: true, class: "kb-scroll kb-scroll-start",
    onclick: () => board.scrollBy({ left: -board.clientWidth * 0.8 }) });
  toStart.querySelector(".icon").style.transform = "rotate(180deg)";
  const toEnd = button("Later stages", { variant: "secondary", size: "sm", iconName: "right", iconOnly: true, class: "kb-scroll kb-scroll-end",
    onclick: () => board.scrollBy({ left: board.clientWidth * 0.8 }) });
  const el = h("div", { class: "kb-wrap" }, board, fadeStart, fadeEnd, toStart, toEnd);
  // when the five stages do not fit, the edge that has more fades and offers a button
  function edges() {
    const more = board.scrollWidth - board.clientWidth > 2;
    const atStart = board.scrollLeft < 4, atEnd = board.scrollLeft + board.clientWidth >= board.scrollWidth - 4;
    fadeStart.hidden = toStart.hidden = !more || atStart;
    fadeEnd.hidden = toEnd.hidden = !more || atEnd;
  }
  board.addEventListener("scroll", edges, { passive: true });
  new ResizeObserver(edges).observe(board);
  let sig = "";
  let dragging = false, pending = false;     // a live update never swaps a card out from under the pointer; it waits for the drop

  const sigOf = (byStage) => JSON.stringify(STAGES.map((s) => byStage[s].map((r) => r.key)));
  function group() {
    const out = Object.fromEntries(STAGES.map((s) => [s, []]));
    for (const r of ctx.visible()) out[r.stage]?.push(r);
    const eff = (r) => r.order ?? r.created_at ?? 0;
    for (const s of STAGES) out[s].sort((a, b) => (S.sort === "name" ? a.name.localeCompare(b.name) : S.sort === "due" ? (a.due || "9999").localeCompare(b.due || "9999")
      : S.sort === "updated" ? (b.updated_at || 0) - (a.updated_at || 0) : eff(a) - eff(b)));
    return out;
  }
  function card(r) {
    const p = ctx.projectOf(r);
    const late = isLate(r);
    return h("article", { class: "card kb-card", role: "listitem", tabindex: "0", "data-key": r.key, "aria-label": `${r.name}, ${r.stage}`,
      style: { "--group": p?.colour || "var(--group-1)" } },
      h("p", { class: "kb-card-title" }, r.name),
      h("p", { class: "kb-card-proj", title: p?.title || "" }, h("span", { class: "dot" }), h("span", {}, midTruncate(p?.title || "", 30))),
      r.parent && rows.get(r.parent) ? h("p", { class: "kb-card-parent" }, `Part of ${rows.get(r.parent).name}`) : null,
      r.needs_review ? h("span", { class: "flag-review", title: r.review_reason || "" }, icon("flag"), "Needs review") : null,
      r.claude_is ? h("p", { class: "kb-card-proj" }, h("span", { class: "dot", style: { color: "var(--claude)" } }), h("span", {}, `Claude is ${lowerFirst(r.claude_is)}`)) : null,
      h("div", { class: "kb-card-foot" }, avatar(r.owner),
        r.due ? h("span", { class: "kb-due", style: late ? { color: "var(--negative)" } : null }, fmtDate(r.due)) : null,
        r.waiting === "you" ? h("span", { class: "pill pill-waiting" }, "Waiting on you") : null));
  }
  function render() {
    const by = group();
    sig = sigOf(by);
    board.replaceChildren(...STAGES.map((s) => {
      const out = s === "Out";
      return h("section", { class: "kb-col", role: "listitem", "data-stage": s, "aria-label": `${s}, ${by[s].length}` },
        h("header", { class: `kb-colhead ${stageClass(s)}` }, h("span", {}, s, " ", h("span", { class: "kb-n" }, `/ ${by[s].length}`)), out ? icon("lock") : null),
        h("div", { class: "kb-list", role: "list" },
          by[s].map(card),
          out ? h("p", { class: "kb-note" }, "Moves here with the publish approval, never by dragging.")
            : !by[s].length ? h("p", { class: "kb-note" }, "Nothing here") : null));
    }));
    requestAnimationFrame(edges);
  }
  function update(keys) {
    if (!el.isConnected) return false;
    if (dragging) { pending = true; return true; }
    const by = group();
    if (sigOf(by) !== sig) { render(); return true; }
    for (const k of keys) { const old = board.querySelector(`.kb-card[data-key="${CSS.escape(k)}"]`); const r = rows.get(k); if (old && r) old.replaceWith(card(r)); }
    return true;
  }

  // open on click or Enter
  board.addEventListener("click", (e) => {
    const c = e.target.closest(".kb-card");
    if (!c || c.dataset.dragged) { if (c) delete c.dataset.dragged; return; }
    ctx.openPanel(c.dataset.key);
  });
  function moveStage(r, d) {
    const to = STAGES[STAGES.indexOf(r.stage) + d];
    if (!to) return;
    if (to === "Out") { toast(S.data?.out_refused || "Out needs the publish approval.", { tone: "negative", timeout: 8000 }); return; }
    act.stage([r], to).then(() => board.querySelector(`.kb-card[data-key="${CSS.escape(r.key)}"]`)?.focus());
  }
  board.addEventListener("keydown", (e) => {
    const c = e.target.closest?.(".kb-card");
    if (!c || e.target !== c) return;
    if (e.shiftKey && (e.key === "ArrowRight" || e.key === "ArrowLeft")) { e.preventDefault(); moveStage(rows.get(c.dataset.key), e.key === "ArrowRight" ? 1 : -1); return; }
    if (e.key === "Enter" || e.key === "o") { e.preventDefault(); ctx.openPanel(c.dataset.key); }
    if (e.key === "ArrowDown" || e.key === "ArrowUp") {
      e.preventDefault();
      const list = [...c.parentElement.querySelectorAll(".kb-card")];
      list[list.indexOf(c) + (e.key === "ArrowDown" ? 1 : -1)]?.focus();
    }
    if (e.key === "ArrowRight" || e.key === "ArrowLeft") {
      e.preventDefault();
      const cols = [...board.querySelectorAll(".kb-col")];
      const i = cols.indexOf(c.closest(".kb-col"));
      for (let j = i + (e.key === "ArrowRight" ? 1 : -1); j >= 0 && j < cols.length; j += e.key === "ArrowRight" ? 1 : -1) {
        const t = cols[j].querySelector(".kb-card"); if (t) { t.focus(); break; }
      }
    }
  });

  // drag a card between stages, or within its stage to reorder
  board.addEventListener("pointerdown", (e) => {
    const c = e.target.closest(".kb-card");
    if (!c || e.button !== 0) return;
    const r = rows.get(c.dataset.key);
    let ghost = null, over = null, before = null;
    const line = h("div", { class: "kb-drop-line", "aria-hidden": "true" });
    const colAt = (x, y) => [...board.querySelectorAll(".kb-col")].find((col) => { const b = col.getBoundingClientRect(); return x >= b.left && x < b.right && y >= b.top - 40 && y < b.bottom + 400; });
    trackDrag(e, { threshold: 5, start: () => {
        const b = c.getBoundingClientRect();
        ghost = c.cloneNode(true);
        ghost.classList.add("ws-drag-ghost");
        Object.assign(ghost.style, { width: `${b.width}px`, left: `${b.left}px`, top: `${b.top}px`, transform: "rotate(2deg)" });
        document.body.append(ghost);
        dragging = true;
        c.classList.add("is-dragging");
        c.dataset.dragged = "1";
      }, move: (ev) => {
      ghost.style.left = `${ev.clientX - (e.clientX - c.getBoundingClientRect().left)}px`;
      ghost.style.top = `${ev.clientY - 20}px`;
      const col = colAt(ev.clientX, ev.clientY);
      if (col !== over) {
        over?.classList.remove("is-over", "is-refused");
        over = col;
        over?.classList.add(over.dataset.stage === "Out" && r.stage !== "Out" ? "is-refused" : "is-over");
      }
      line.remove(); before = null;
      if (over && over.dataset.stage === r.stage) {               // same column: show where it lands
        const cards = [...over.querySelectorAll(".kb-card")].filter((x) => x !== c);
        before = cards.find((x) => { const b = x.getBoundingClientRect(); return ev.clientY < b.top + b.height / 2; }) || null;
        before ? before.before(line) : (cards.at(-1) || c).after(line);
      }
    }, cleanup: () => {
      dragging = false;
      ghost.remove(); c.classList.remove("is-dragging"); line.remove();
      over?.classList.remove("is-over", "is-refused");
      setTimeout(() => { delete c.dataset.dragged; }, 0);
      if (pending) { pending = false; setTimeout(() => update([...rows.keys()]), 0); }
    }, drop: () => {
      const to = over?.dataset.stage;
      if (!to) return;
      if (to === r.stage) {
        const cards = [...over.querySelectorAll(".kb-card")].filter((x) => x !== c).map((x) => rows.get(x.dataset.key));
        const eff = (x) => x.order ?? x.created_at ?? 0;
        const at = before ? cards.findIndex((x) => x.key === before.dataset.key) : cards.length;
        const prev = cards[at - 1], next = cards[at];
        const order = prev && next ? (eff(prev) + eff(next)) / 2 : prev ? eff(prev) + 1000 : next ? eff(next) - 1000 : eff(r);
        if (order !== eff(r)) { if (S.sort !== "manual") { S.sort = "manual"; ctx.savePrefs(); } act.fields([r], { order }, { say: `Moved ${r.name}` }); }
        return;
      }
      if (to === "Out") { toast(S.data?.out_refused || "Out needs the publish approval.", { tone: "negative", timeout: 8000 }); return; }
      act.stage([r], to);
    } });
  });

  return { el, render, update };
}
