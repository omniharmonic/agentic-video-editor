// Jobs: the background `ave` steps run from the app (the retired `ave ui`'s jobs page). A side panel lists what is running and what
// finished, across the episode projects on this computer, with each one's log and a Cancel for a running one. "Run a step" starts
// one of the allowlisted steps (nothing on the list publishes, approves, sends or spends money); a step runs as an agent would,
// never as the person, so a human gate inside it still refuses.

import { h, button, icon, toast, busy } from "../dom.js";
import { api, plain, menu } from "./common.js";

const STATE = { running: ["Running", "var(--claude)"], stopping: ["Stopping", "var(--sync-warn)"], done: ["Done", "var(--positive)"], failed: ["Failed", "var(--negative)"],
  cancelled: ["Cancelled", "var(--stage-plan)"] };
const POLL_MS = 1500;

export function createJobs(ctx) {
  const el = h("aside", { class: "side-panel jobs-panel", "aria-label": "Jobs", hidden: true, tabindex: "-1" });
  const closeBtn = button("Close", { variant: "tertiary", size: "sm", iconName: "x", iconOnly: true, onclick: () => close() });
  const body = h("div", { class: "ws-panel-body ap-body jobs-body" });
  el.append(h("div", { class: "ws-panel-head" }, h("div", { class: "ws-panel-top" }, h("p", { class: "ws-panel-crumb" }, icon("clock"), h("span", {}, "Jobs")),
    h("div", { class: "ws-panel-tools" }, closeBtn)), h("h2", {}, "Steps running on this computer")), body);
  el.addEventListener("keydown", (e) => { if (e.key === "Escape" && !document.querySelector(".popover")) { e.preventDefault(); e.stopPropagation(); close(); } });
  document.body.append(el);

  const S = { jobs: [], catalog: [], projects: [], error: null, loaded: false, open: new Set(), tails: new Map(), pick: { project: null, cmd: "status" } };
  const listeners = new Set();
  let timer = 0, opener = null;
  const running = () => S.jobs.filter((j) => j.status === "running").length;

  async function load() {
    try {
      const r = await api("GET", "/api/workspace/jobs");
      S.jobs = r.jobs || []; S.catalog = r.catalog || []; S.projects = r.projects || []; S.error = null; S.loaded = true;
      if (!S.pick.project || !S.projects.some((p) => p.id === S.pick.project)) S.pick.project = S.projects[0]?.id || null;
      for (const id of S.open) { const j = S.jobs.find((x) => x.id === id); if (j) await tail(j); }
    } catch (e) { S.error = e; }
    for (const fn of listeners) fn();
    if (!el.hidden) paint();
    schedule();
  }
  function schedule() {
    clearTimeout(timer);
    if (!el.hidden || running()) timer = setTimeout(load, POLL_MS);
  }
  async function tail(j) {
    try { S.tails.set(j.id, (await api("GET", `/api/workspace/jobs/${encodeURIComponent(j.project)}/${encodeURIComponent(j.id)}`)).tail || ""); }
    catch { /* the list still shows it */ }
  }

  function open() {
    if (el.hidden) opener = document.activeElement;
    el.hidden = false;
    paint();
    el.focus({ preventScroll: true });
    load();
  }
  function close() {
    if (el.hidden) return;
    el.hidden = true;
    clearTimeout(timer); schedule();
    if (opener?.isConnected) opener.focus({ preventScroll: true });
  }

  async function run(btn) {
    if (!S.pick.project) { toast("There is no episode project on this computer to run a step in."); return; }
    await busy(btn, async () => {
      try {
        await api("POST", "/api/workspace/jobs", { project: S.pick.project, cmd: S.pick.cmd, args: {} });
        toast(`Started ave ${S.pick.cmd}`);
        await load();
      } catch (e) { toast(plain(e, "start it"), { tone: "negative" }); }
    });
  }
  async function cancel(j, btn) {
    await busy(btn, async () => {
      try { await api("POST", `/api/workspace/jobs/${encodeURIComponent(j.project)}/${encodeURIComponent(j.id)}/cancel`, {}); toast(`Stopping ave ${j.cmd} and everything it started. Steps pick up where they left off when run again.`); await load(); }
      catch (e) { toast(plain(e, "stop it"), { tone: "negative" }); }
    });
  }

  // The panel is built once and then updated in place, row by row (keyed by job id), so a poll never moves focus, closes an open
  // menu or scrolls a log back to its top.
  const projBtn = button("Project", { variant: "secondary", size: "sm", iconName: "folder", "aria-haspopup": "menu",
    onclick: (e) => menu(e.currentTarget, S.projects.map((p) => ({ label: p.title, checked: p.id === S.pick.project, onSelect: () => { S.pick.project = p.id; paintPick(); } })), { label: "Project" }) });
  const cmdBtn = button("Step", { variant: "secondary", size: "sm", "aria-haspopup": "menu",
    onclick: (e) => menu(e.currentTarget, S.catalog.map((c) => ({ label: `ave ${c}`, checked: c === S.pick.cmd, onSelect: () => { S.pick.cmd = c; paintPick(); } })), { label: "Step" }) });
  const runBtn = button("Run", { variant: "primary", size: "sm", iconName: "play", onclick: (e) => run(e.currentTarget) });
  const countChip = h("span", { class: "chip chip-count" }, "0");
  const list = h("ul", { class: "jobs-list" });
  const none = h("p", { class: "secondary-text" }, "Nothing has run from the app yet.");
  const errorBox = h("div", { class: "ws-error", role: "alert", hidden: true });
  const shell = [
    h("section", { class: "ps-section jobs-run", "aria-label": "Run a step" }, h("h3", {}, "Run a step"),
      h("div", { class: "jobs-pick" }, projBtn, cmdBtn, runBtn),
      h("p", { class: "secondary-text" }, "These steps never publish, approve, send or spend money. Claude usually runs them for you; this is for when you want one now.")),
    h("section", { class: "ps-section", "aria-label": "Jobs" }, h("h3", {}, "Running and finished", countChip), errorBox, list, none)];
  const rowEls = new Map();
  let built = false;

  function paintPick() {
    const proj = S.projects.find((p) => p.id === S.pick.project);
    projBtn.querySelector("span").textContent = proj ? proj.title : "No episode project";
    projBtn.disabled = runBtn.disabled = !S.projects.length;
    cmdBtn.querySelector("span").textContent = `ave ${S.pick.cmd}`;
    cmdBtn.setAttribute("aria-label", `Step: ave ${S.pick.cmd}`);
  }
  function rowFor(j) {
    let r = rowEls.get(j.id);
    if (r) return r;
    const pill = h("span", { class: "pill job-state" });
    const what = h("span", { class: "secondary-text" });
    const logBtn = button("Show the log", { variant: "tertiary", size: "sm", "aria-expanded": "false",
      onclick: async () => { S.open.has(j.id) ? S.open.delete(j.id) : (S.open.add(j.id), await tail(j)); paintRow(rowEls.get(j.id), S.jobs.find((x) => x.id === j.id) || j); } });
    const cancelBtn = button("Cancel", { variant: "secondary", size: "sm", "data-act": "cancel", onclick: (e) => cancel(S.jobs.find((x) => x.id === j.id) || j, e.currentTarget) });
    const log = h("pre", { class: "job-log", tabindex: "0", "aria-label": `Log of ave ${j.cmd}`, hidden: true });
    const li = h("li", { class: "job", dataset: { job: j.id } },
      h("div", { class: "job-head" }, pill, h("span", { class: "job-what" }, h("strong", {}, `ave ${j.cmd}`), what), h("div", { class: "job-acts" }, logBtn, cancelBtn)), log);
    r = { li, pill, what, logBtn, cancelBtn, log };
    rowEls.set(j.id, r);
    return r;
  }
  function paintRow(r, j) {
    const [word, colour] = STATE[j.status] || [j.status, "var(--stage-plan)"];
    r.li.dataset.status = j.status;
    if (r.pill.textContent !== word) r.pill.textContent = word;
    r.pill.style.background = colour; r.pill.style.color = "var(--on-solid)";
    const text = `${j.title} · started ${j.started}${j.finished ? `, ended ${j.finished}` : ""}`;
    if (r.what.textContent !== text) r.what.textContent = text;
    const shown = S.open.has(j.id);
    r.logBtn.querySelector("span").textContent = shown ? "Hide the log" : "Show the log";
    r.logBtn.setAttribute("aria-expanded", String(shown));
    const hide = j.status !== "running";
    if (hide && document.activeElement === r.cancelBtn) r.logBtn.focus();         // Cancel goes away: focus stays on the row
    r.cancelBtn.hidden = hide;
    r.log.hidden = !shown;
    const next = S.tails.get(j.id) || "Nothing written yet.";
    if (shown && r.log.textContent !== next) {
      const atEnd = r.log.scrollHeight - r.log.scrollTop - r.log.clientHeight < 8;
      const top = r.log.scrollTop;
      r.log.textContent = next;
      r.log.scrollTop = atEnd ? r.log.scrollHeight : top;          // follow the end only when the person was already there
    }
  }
  function paint() {
    if (!built) { body.replaceChildren(...shell); built = true; }
    errorBox.hidden = !(S.error && !S.loaded);
    if (!errorBox.hidden) errorBox.replaceChildren(h("h2", {}, "The jobs could not be read"), h("p", { class: "secondary-text" }, plain(S.error, "read the jobs")),
      button("Try again", { variant: "primary", onclick: (e) => busy(e.currentTarget, load) }));
    paintPick();
    countChip.textContent = String(S.jobs.length);
    none.hidden = !!S.jobs.length || !S.loaded;
    const want = S.jobs.map((j) => { const r = rowFor(j); paintRow(r, j); return r.li; });
    for (const [id, r] of rowEls) if (!S.jobs.some((j) => j.id === id)) { r.li.remove(); rowEls.delete(id); }
    want.forEach((li, i) => { if (list.children[i] !== li) list.insertBefore(li, list.children[i] || null); });   // order only, no rebuild
  }

  return { el, open, close, load, running, isOpen: () => !el.hidden, onChange: (fn) => { listeners.add(fn); return () => listeners.delete(fn); } };
}
