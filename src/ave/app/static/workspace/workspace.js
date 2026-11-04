// The Workspace: every artifact across the projects on this computer, like a production office's board. The director can change any
// field by hand (always written as the person, never as Claude); Claude keeps it current from Claude Code, and its writes arrive on
// the Room's stream as `board` events, so nothing here needs a reload or a save button.
//
// Routes: #/workspace (every project), #/workspace/p/<project> (one project), #/workspace/waiting (what waits on you),
// #/workspace/home. Views: Main table, Kanban, Calendar (release dates and the studio's posts), Previews (how the posts will look)
// and Sources (what each project is made from, and who has each file). Everything that changes the board goes through `change()`,
// which shows the change at once, writes it, and offers Undo.

import { h, button, icon, toast, popover, busy, typing } from "../dom.js";
import { api, plain, STAGES, stageClass, menu, avatar, isClaude, waitingCount, GROUP_COLOURS, stagePopover, peoplePopover, datePopover, fmtDate,
  settingChooser, PER_KEY } from "./common.js";
import { createRail } from "./rail.js";
import { createBoard, COLUMNS } from "./board.js";
import { createKanban } from "./kanban.js";
import { createPanel } from "./panel.js";
import { createHome } from "./home.js";
import { createSync } from "./sync.js";
import { createStudio, createPostSheet } from "./posts.js";
import { createCalendar } from "./calendar.js";
import { createPreviews } from "./previews.js";
import { createSources } from "./sources.js";
import { createIdeas } from "./ideas.js";
import { createApprovals } from "./approvals.js";
import { createCampaigns } from "./campaigns.js";
import { createJobs } from "./jobs.js";
import { postName } from "/app/shared/plan.js";

const PREFS = "ave.workspace.v1";
const RETRIES = 3;
const MINE_MS = 4000;          // a change this browser made: no "changed by" flash when the stream brings it back
const VIEWS = [["table", "Main table", "table"], ["kanban", "Kanban", "kanban"], ["calendar", "Calendar", "calendar"], ["previews", "Previews", "grid"],
  ["sources", "Sources", "folder"]];
const BOARD_VIEWS = new Set(["table", "kanban"]);          // the views the board's own tools (Person, Filter, Sort, Hide, Group by) apply to

function loadPrefs() { try { return JSON.parse(localStorage.getItem(PREFS) || "{}") || {}; } catch { return {}; } }

export function parseRoute(hash) {
  if (/^#\/workspace\/home\/?$/.test(hash)) return { page: "home" };
  const p = /^#\/workspace\/p\/([^/]+)/.exec(hash);
  if (p) return { page: "board", project: decodeURIComponent(p[1]) };
  if (/^#\/workspace\/waiting/.test(hash)) return { page: "board", waiting: true };
  if (/^#\/workspace\/approvals/.test(hash)) return { page: "board", approvals: true };   // the approval sheet, over every artifact
  const post = /^#\/workspace\/post\/([^/?]+)/.exec(hash);                                // a post's sheet (Settings › Alerts links here)
  if (post) return { page: "board", post: decodeURIComponent(post[1]) };
  return { page: "board" };
}

export function mountWorkspace(root, route) {
  const prefs = loadPrefs();
  const S = {
    route, data: null, error: null, live: "connecting", seq: 0, loading: true,
    view: VIEWS.some(([v]) => v === prefs.view) ? prefs.view : "table", group: prefs.group || "project", sort: prefs.sort || "manual",
    hidden: new Set(prefs.hidden || []), colOrder: (prefs.colOrder || []).filter((c) => COLUMNS.some((x) => x.id === c)), widths: prefs.widths || {},
    collapsed: new Set(prefs.collapsed || []), groupOrder: prefs.groupOrder || {}, favs: new Set(prefs.favs || []), railCollapsed: !!prefs.railCollapsed,
    q: "", person: undefined, stages: new Set(), waitingOnly: false, reviewOnly: false, archived: false,
    expanded: new Set(), selected: new Set(), adding: false,
    cal: prefs.cal || null, pv: prefs.pv || null, src: prefs.src || null,
  };
  for (const c of COLUMNS) if (!S.colOrder.includes(c.id)) S.colOrder.push(c.id);
  const savePrefs = () => {
    try {
      localStorage.setItem(PREFS, JSON.stringify({ view: S.view, group: S.group, sort: S.sort, hidden: [...S.hidden], colOrder: S.colOrder, widths: S.widths,
        collapsed: [...S.collapsed], groupOrder: S.groupOrder, favs: [...S.favs], railCollapsed: S.railCollapsed,
        cal: S.cal ? { mode: S.cal.mode, filter: S.cal.filter, tray: S.cal.tray } : null, pv: S.pv, src: S.src }));
    } catch { /* private window: the view still works, it is just not remembered */ }
  };
  const rows = new Map();
  const projects = new Map();
  const mine = new Map();
  const probe = window.__AVE_TEST__ ? (window.__ws = { events: 0, writes: 0, renderMs: 0, live: "connecting", state: S }) : null;

  // ---------------------------------------------------------------- reading the board
  function ingest(data, { flash = false } = {}) {
    const old = new Map(rows);
    S.data = data;
    if (data.now) S.skew = data.now - Date.now();
    rows.clear(); projects.clear();
    data.projects.forEach((p, i) => {
      projects.set(p.id, { ...p, colour: `var(--group-${(i % GROUP_COLOURS) + 1})`, artifacts: undefined });
      for (const r of p.artifacts) rows.set(r.key, r);
    });
    for (const k of [...S.selected]) if (!rows.has(k)) S.selected.delete(k);
    const changed = [];
    const flashes = new Map();
    for (const [k, r] of rows) {
      const o = old.get(k);
      if (o && JSON.stringify(o) === JSON.stringify(r)) continue;
      changed.push(k);
      if (!flash || !o) continue;
      const recent = mine.get(k) && Date.now() - mine.get(k) < MINE_MS && r.updated_by?.actor === "human";
      if (recent) continue;
      const cols = diffCols(o, r);
      if (cols.length) flashes.set(k, { cols, by: r.updated_by?.name || "Someone" });
    }
    for (const k of old.keys()) if (!rows.has(k)) changed.push(k);
    return { changed, flashes };
  }

  function diffCols(o, r) {
    const cols = [];
    if (o.name !== r.name || o.needs_review !== r.needs_review) cols.push("name");
    if (o.stage !== r.stage) cols.push("stage");
    if (o.owner !== r.owner) cols.push("who");
    if (o.waiting !== r.waiting || waitingCount(o) !== waitingCount(r)) cols.push("waiting");
    if (o.claude_is !== r.claude_is) cols.push("claude");
    if (o.due !== r.due) cols.push("due");
    if (o.version !== r.version) cols.push("version");
    return cols;
  }

  let loadingNow = null, again = false;
  function load({ flash = false } = {}) {
    if (loadingNow) { again = true; return loadingNow; }
    loadingNow = api("GET", "/api/workspace/board")
      .then((data) => {
        S.error = null; S.loading = false;
        if (!S.seq || data.seq > S.seq) S.seq = Math.max(S.seq, data.seq || 0);
        const { changed, flashes } = ingest(data, { flash });
        paintAll({ changed, flashes, first: !es });
        if (!es) { connect(); sync.load(); approvals.load(); }
      })
      .catch((e) => {
        S.loading = false;
        if (!S.data) { S.error = e; paintAll({}); }
        else toast(plain(e, "refresh the board"), { tone: "negative" });
      })
      .finally(() => {
        loadingNow = null;
        if (again) { again = false; load({ flash: true }); }
      });
    return loadingNow;
  }
  let reloadTimer = 0;
  const scheduleReload = () => { clearTimeout(reloadTimer); reloadTimer = setTimeout(() => load({ flash: true }), 80); };

  // ---------------------------------------------------------------- the stream (shared with the Room): `board` events mean "read again"
  let es = null, attempts = 0, retryTimer = 0;
  function connect() {
    es?.close();
    es = new EventSource(`/api/room/stream?last_event_id=${S.seq}`);
    es.onopen = () => { attempts = 0; setLive("live"); };
    es.onerror = () => {
      if (es.readyState !== EventSource.CLOSED) { setLive("reconnecting"); return; }
      es.close();
      attempts += 1;
      if (attempts > RETRIES) { setLive("offline"); return; }
      setLive("reconnecting");
      clearTimeout(retryTimer);
      retryTimer = setTimeout(connect, 1000 * 2 ** (attempts - 1));
    };
    es.onmessage = (m) => {
      let e;
      try { e = JSON.parse(m.data); } catch { return; }
      if (e.type === "resync") { S.seq = 0; scheduleReload(); sync.load(); postsChanged(); return; }
      if (e.seq && e.seq <= S.seq) return;
      if (e.seq) S.seq = e.seq;
      if (probe) probe.events += 1;
      if (e.type === "board" || e.type === "readiness") { scheduleReload(); panel.refreshIf(e.data?.projects || []); if (e.type === "board") approvals.load(); }
      else if (e.type === "sync") sync.apply(e.data);
      else if (e.type === "posts") postsChanged();
    };
  }
  // A post changed somewhere (the watcher saw the studio's change log grow). The views that show posts read again. Their first read
  // always comes after the board's, whose `seq` the stream resumes from, so a change made before a view's read is in that read and
  // a change made after it arrives here: nothing is lost between the two (I-2).
  const postWatchers = new Set();
  let postsTimer = 0;
  function postsChanged() {
    if (probe) probe.posts = (probe.posts || 0) + 1;
    clearTimeout(postsTimer);
    postsTimer = setTimeout(() => { for (const fn of postWatchers) fn(); }, 60);
  }
  function setLive(v) { S.live = v; if (probe) probe.live = v; paintHead(); }

  // ---------------------------------------------------------------- what the board shows
  function matches(r) {
    if (!S.archived && r.archived) return false;
    if (S.route.project && r.project !== S.route.project) return false;
    if ((S.route.waiting || S.waitingOnly) && r.waiting !== "you") return false;
    if (S.reviewOnly && !r.needs_review) return false;
    if (S.stages.size && !S.stages.has(r.stage)) return false;
    if (S.person !== undefined && (r.owner || null) !== S.person) return false;
    if (S.q) {
      const p = projects.get(r.project);
      const hay = [r.name, r.version, r.claude_is, r.stage, r.owner, r.kind, p?.title].join(" ").toLowerCase();
      if (!S.q.split(/\s+/).every((w) => hay.includes(w))) return false;
    }
    return true;
  }
  const visible = () => [...rows.values()].filter(matches);
  const filtersOn = () => (S.waitingOnly ? 1 : 0) + (S.reviewOnly ? 1 : 0) + (S.archived ? 1 : 0) + S.stages.size;

  // ---------------------------------------------------------------- writing: show it at once, write it, offer Undo
  const url = (key) => `/api/workspace/artifact/${encodeURIComponent(key)}`;
  function local(r, body) {
    if (body.fields) for (const [k, v] of Object.entries(body.fields)) {
      if (k === "title") r.name = v; else r[k] = v;
    }
    if (body.stage) r.stage = body.stage;
  }
  async function change(list, make, { undo, say, doing = "save that", btn } = {}) {
    list = list.filter(Boolean);
    const bodies = list.map(make);
    const work = list.map((r, i) => [r, bodies[i]]).filter(([, b]) => b);
    if (!work.length) return [];
    const before = new Map(work.map(([r]) => [r.key, structuredClone(r)]));
    for (const [r, b] of work) { local(r, b); mine.set(r.key, Date.now()); }
    if (probe) probe.writes += 1;
    refresh(work.map(([r]) => r.key));
    const run = () => Promise.allSettled(work.map(([r, b]) => api("PATCH", url(r.key), b)));
    const results = btn ? await busy(btn, run) : await run();
    const ok = [], failed = [];
    results.forEach((res, i) => {
      const [r] = work[i];
      if (res.status === "fulfilled" && res.value.row) { rows.set(r.key, res.value.row); ok.push(r.key); }
      else if (res.status === "fulfilled") ok.push(r.key);
      else { rows.set(r.key, before.get(r.key)); failed.push(res.reason); }
    });
    refresh(work.map(([r]) => r.key));
    if (failed.length) toast(plain(failed[0], doing), { tone: "negative", timeout: 9000 });
    if (ok.length && say) {
      const back = () => change(ok.map((k) => rows.get(k)).filter(Boolean), (r) => undo(before.get(r.key), r), { say: "Undone", doing: "undo that" });
      toast(typeof say === "function" ? say(ok.length) : say, undo ? { action: "Undo", onAction: back } : {});
    }
    return ok;
  }
  async function single(row, body, { say, doing, btn, undoBody } = {}) {
    return change([row], () => body, { say, doing, btn, undo: undoBody ? () => undoBody : undefined });
  }

  const pickBack = (b, fields) => {
    const out = {};
    for (const k of Object.keys(fields)) {
      if (k === "title") out.title = b.name;
      else if (k === "order") out.order = b.order ?? b.created_at ?? 0;
      else if (k === "archived") out.archived = !!b.archived;
      else out[k] = b[k] ?? null;
    }
    return { fields: out };
  };
  const one = (n, word, list) => (n === 1 ? list[0].name : `${n} ${word}`);
  const act = {
    stage(list, to, { btn } = {}) {
      return change(list, (r) => (r.stage === to && !r.conflict ? null : { stage: to, from: r.stage }), {
        btn, doing: `move it to ${to}`,
        undo: (b, now) => ({ stage: b.stage, from: now.stage }),
        say: (n) => `${one(n, "artifacts", list)} moved to ${to}`,
      });
    },
    fields(list, fields, { say, btn } = {}) {
      return change(list, () => ({ fields }), { btn, doing: "save that", undo: (b) => pickBack(b, fields), say: say || "Saved" });
    },
    archive(list) {
      return change(list, () => ({ fields: { archived: true } }), { undo: () => ({ fields: { archived: false } }), doing: "archive it",
        say: (n) => `${one(n, "artifacts", list)} archived` });
    },
    // Confirming what Claude reported is the check on Claude, so only the app can make it: its sheet checks that it is you
    // (aveNative.confirmRelayed). A browser has no way to, so the button says so. Undo goes through the app too when there is one,
    // so it is recorded as yours; a bare request in the app is recorded as Claude's.
    async confirm(row, r, btn) {
      const n = window.aveNative;
      if (!n?.confirmRelayed) { toast("Confirming needs the AVE Studio app: it checks that it is you.", { tone: "negative" }); return []; }
      return (btn ? busy(btn, () => n.confirmRelayed(row.key, r.event)) : n.confirmRelayed(row.key, r.event))
        .then((res) => { if (res?.row) { rows.set(row.key, res.row); refresh([row.key]); } toast("Confirmed. It stays as Claude reported."); return [row.key]; })
        .catch((e) => { toast(/cancel/i.test(e.message) ? "Nothing was confirmed." : e.message, { tone: /cancel/i.test(e.message) ? undefined : "negative" }); return []; });
    },
    undo(row, r, btn) {
      const n = window.aveNative;
      if (n?.undoRelayed) {
        const back = r.type === "resolution" ? `Undone: the decision is open again` : `Undone: ${row.name} is back in ${r.from}`;
        return (btn ? busy(btn, () => n.undoRelayed(row.key, r.event)) : n.undoRelayed(row.key, r.event))
          .then((res) => { if (res?.row) { rows.set(row.key, res.row); refresh([row.key]); } toast(back); return [row.key]; })
          .catch((e) => { toast(e.message, { tone: "negative" }); return []; });
      }
      if (r.type === "settle")
        return single(row, { undo: r.event }, { btn, doing: "undo it", say: `Undone: ${row.name} is back in ${r.from}`, undoBody: { stage: r.to, from: r.from } });
      if (r.type === "sign_off")
        return single(row, { undo: r.event }, { btn, doing: "undo it", say: `Undone: ${row.name} is back in ${r.from}`, undoBody: { stage: r.to, from: r.from } });
      return single(row, { undo: r.event }, { btn, doing: "undo it", say: "Undone: the decision is open again", undoBody: { resolve: r.decision, choice: r.choice } });
    },
    resolvePicks(row, d, picks, btn) {
      return single(row, { resolve: d.decision, choice: PER_KEY, picks }, { btn, doing: "answer it", say: "Chose setting by setting",
        undoBody: { reopen_decision: d.decision } });
    },
    resolve(row, d, choice, btn) {
      return single(row, { resolve: d.decision, choice }, { btn, doing: "answer it", say: `Chose “${choice}”`, undoBody: { reopen_decision: d.decision } });
    },
    async note(row, text, btn) {
      const ok = await single(row, { note: text }, { btn, doing: "post the update" });
      return ok.length > 0;
    },
    async create(projectId, title, { parent, btn } = {}) {
      if (probe) probe.writes += 1;
      const run = () => api("POST", "/api/workspace/artifact", { project: projectId, title, parent: parent || undefined, kind: parent ? "short" : "artifact" });
      try {
        const res = await (btn ? busy(btn, run) : run());
        rows.set(res.row.key, res.row);
        mine.set(res.row.key, Date.now());
        if (parent) S.expanded.add(parent);
        refresh([res.row.key]);
        toast(`Added ${res.row.name} in Plan`, { action: "Undo", onAction: () => act.archive([rows.get(res.row.key)]) });
        return res.row;
      } catch (e) { toast(plain(e, "add it"), { tone: "negative" }); return null; }
    },
  };

  // ---------------------------------------------------------------- navigation
  const ctx = {
    S, rows, projects, act, visible, matches, savePrefs, probe,
    me: () => S.data?.me || null,
    now: () => Date.now() + (S.skew || 0),          // the app's clock, so presence runs out when the app says
    projectOf: (r) => projects.get(r.project),
    colourOf: (pid) => projects.get(pid)?.colour || "var(--group-1)",
    openPanel: (key, tab) => panel.open(key, tab),
    syncPill: (pid, where, title) => sync.pill(pid, where, title),
    missingGroups: () => sync.missingGroups(),
    openRoom: (r) => { location.hash = `#/room/${encodeURIComponent(r.room || r.artifact)}`; },
    select(key, on) { if (on) S.selected.add(key); else S.selected.delete(key); paintBatch(); view()?.paintSelection?.(); },
    selectMany(keys, on) { for (const k of keys) on ? S.selected.add(k) : S.selected.delete(k); paintBatch(); view()?.paintSelection?.(); },
    rerender: () => paintView(),
    startAdding: () => startAdding(),
    go: (hash) => { location.hash = hash; },
    clearSearch: () => { search.value = ""; S.q = ""; },
    onPosts: (fn) => { postWatchers.add(fn); return () => postWatchers.delete(fn); },
    openApprovals: (focus) => openApprovals(focus),
    approvalsCount: () => (approvals.count()),
    approvalsChanged: () => rail.paint(),
  };

  // ---------------------------------------------------------------- layout
  const rail = createRail(ctx);
  const board = createBoard(ctx);
  const kanban = createKanban(ctx);
  const panel = createPanel(ctx);
  const home = createHome(ctx);
  const sync = createSync(ctx);
  const studio = createStudio();
  const sheet = createPostSheet(ctx, studio);
  const calendar = createCalendar(ctx, studio, sheet);
  const previews = createPreviews(ctx, studio, sheet);
  const sources = createSources(ctx, studio);
  const ideas = createIdeas(ctx, studio);
  const approvals = createApprovals(ctx, studio);
  const campaigns = createCampaigns(ctx, studio);
  ctx.campaigns = campaigns;
  campaigns.onChange(() => { if (S.group === "campaign") paintView(); panel.paintRow(); });
  campaigns.load();
  const jobs = createJobs(ctx);
  ctx.openJobs = () => jobs.open();
  approvals.onChange(() => rail.paint());
  ctx.onPosts(() => approvals.load());                          // a post went into review, or left it: the count and the list read again
  act.openTrim = (row, d) => openApprovals({ project: row.project, session: d.session });
  function openApprovals(focus) {
    return approvals.open(focus, { onClose: () => { if (/^#\/workspace\/approvals/.test(location.hash)) history.replaceState(null, "", "#/workspace"); } });
  }
  // Main table: the studio's Ideas group above the board's own groups
  const table = { el: h("div", { class: "ws-tablewrap" }, ideas.el, board.el),
    render: (o) => { ideas.render(); board.render(o); }, update: (k, f) => { ideas.render(); return board.update(k, f); },
    paintSelection: () => board.paintSelection(), leave: () => ideas.leave() };
  const VIEW_OF = { table, kanban, calendar, previews, sources };
  const view = () => (S.route.page === "home" ? null : VIEW_OF[S.view] || board);

  const title = h("h1", {}, "All artifacts");
  const info = button("About this board", { variant: "tertiary", size: "sm", iconName: "info", iconOnly: true, "aria-haspopup": "dialog", "aria-expanded": "false",
    onclick: (e) => showInfo(e.currentTarget) });
  const star = button("Add to favourites", { variant: "tertiary", size: "sm", iconName: "star", iconOnly: true, "aria-pressed": "false", onclick: () => toggleFav() });
  const liveDot = h("span", { class: "ws-live", role: "status" });
  const retry = button("Try again", { variant: "secondary", size: "sm", hidden: true, onclick: () => { attempts = 0; connect(); } });
  const people = h("span", { class: "ws-activity" });
  const headSync = h("span", { class: "ws-sync" });
  // Sharing a project with a person runs in native sheets (who, how much media, the invitation, the six-digit code); the web
  // view only names which project. In a browser there is no app to hand it to, so the control says so instead of waiting.
  const sharing = () => window.aveNative?.shareProject ? window.aveNative : null;
  const noShare = "Sharing needs the AVE Studio app and Tailscale.";
  const shareBtn = button("Share", { variant: "secondary", size: "sm", iconName: "invite", "aria-haspopup": "dialog", hidden: true,
    onclick: (e) => shareProject(e.currentTarget) });
  // More tools: the four older tools the app keeps unchanged, each in its own window (the app's, or a browser tab)
  const openTool = (route) => {
    const n = window.aveNative?.openTools;
    if (n) return n(route).catch((e) => toast(e.message, { tone: "negative" }));
    window.open(`/tools/${route}`, "_blank", "noopener");
  };
  ctx.openTool = openTool;
  async function postsMenu(anchor) {
    let posts = [];
    try { posts = ((await studio.api.get("posts")).posts || []).slice(0, 40); } catch (e) { toast(plain(e, "read the posts"), { tone: "negative" }); return; }
    if (!posts.length) { toast("There are no posts yet."); return; }
    menu(anchor, posts.map((p) => ({ label: postName(p), onSelect: () => openTool(`#/post/${encodeURIComponent(p.id)}`) })), { label: "Post editor", heading: "Open a post in the post editor" });
  }
  const toolsBtn = button("More tools", { variant: "tertiary", size: "sm", iconName: "grid", "aria-haspopup": "menu", "aria-expanded": "false",
    onclick: (e) => { const a = e.currentTarget; menu(a, [
      { label: "Production", icon: "table", note: "Tasks, comments, people and workflows for each post", onSelect: () => openTool("#/pipeline") },
      { label: "Media tools", icon: "film", note: "Transcribe a source, quote images, designs and video projects", onSelect: () => openTool("#/workbench") },
      { label: "Feed planner", icon: "grid", note: "Arrange an account's feed", onSelect: () => openTool("#/grid") },
      { label: "Post editor…", icon: "open", note: "Library picker, cover, crop and the delivery check", onSelect: () => postsMenu(a) },
    ], { label: "More tools", heading: "More tools, each in its own window", align: "end" }); } });
  const jobsBtn = button("Jobs", { variant: "tertiary", size: "sm", iconName: "clock", "aria-haspopup": "dialog", class: "ws-jobs-btn", onclick: () => jobs.open() });
  jobs.onChange(() => paintJobs());
  jobs.load();
  const more = button("Board options", { variant: "tertiary", size: "sm", iconName: "dots", iconOnly: true, "aria-haspopup": "menu", "aria-expanded": "false",
    onclick: (e) => menu(e.currentTarget, [
      { label: "Collapse all groups", icon: "chevron", onSelect: () => { for (const g of board.groupKeys()) S.collapsed.add(g); savePrefs(); paintView(); } },
      { label: "Expand all groups", icon: "right", onSelect: () => { S.collapsed.clear(); savePrefs(); paintView(); } },
      "-",
      ...shareItems(),
      "-",
      { label: "Keyboard shortcuts", icon: "keyboard", hint: "?", onSelect: () => showKeys(more) },
      { label: "Read the board again", icon: "undo", onSelect: () => load({ flash: true }).then(() => toast("The board is up to date")) },
    ], { label: "Board options", align: "end" }) });
  const tabs = h("div", { class: "tabs ws-tabs", role: "tablist", "aria-label": "Views" });
  const tabEls = Object.fromEntries(VIEWS.map(([v, label, ic]) => [v, h("button", { type: "button", class: "tab", role: "tab", dataset: { view: v }, onclick: () => setView(v) }, icon(ic), label)]));
  tabs.append(...Object.values(tabEls));
  tabs.addEventListener("keydown", (e) => {
    if (e.key !== "ArrowRight" && e.key !== "ArrowLeft") return;
    e.preventDefault();
    const i = VIEWS.findIndex(([v]) => v === S.view) + (e.key === "ArrowRight" ? 1 : -1);
    setView(VIEWS[(i + VIEWS.length) % VIEWS.length][0]);
    tabEls[S.view].focus();
  });

  // toolbar
  const newBtn = button("New artifact", { variant: "primary", size: "sm", onclick: () => startAdding() });
  const newMore = button("More ways to add", { variant: "primary", size: "sm", iconName: "chevron", iconOnly: true, "aria-haspopup": "menu", "aria-expanded": "false",
    onclick: (e) => addMenu(e.currentTarget) });
  const search = h("input", { class: "input", type: "search", placeholder: "Search", "aria-label": "Search artifacts", autocomplete: "off", spellcheck: "false" });
  search.addEventListener("input", () => { S.q = search.value.trim().toLowerCase(); paintView(); paintTools(); });
  search.addEventListener("keydown", (e) => { if (e.key === "Escape" && search.value) { e.preventDefault(); e.stopPropagation(); search.value = ""; S.q = ""; paintView(); search.blur(); } });
  const tool = (label, iconName, onclick) => {
    const b = h("button", { type: "button", class: "btn btn-tertiary btn-tool", "aria-haspopup": "dialog", "aria-expanded": "false", onclick }, icon(iconName),
      h("span", { class: "tool-label" }, label), h("span", { class: "tool-count", hidden: true }));
    b.setAttribute("aria-label", label);
    return b;
  };
  const personBtn = tool("Person", "person", (e) => personMenu(e.currentTarget));
  const filterBtn = tool("Filter", "filter", (e) => filterPop(e.currentTarget));
  const sortBtn = tool("Sort", "sort", (e) => sortMenu(e.currentTarget));
  const hideBtn = tool("Hide", "hide", (e) => hidePop(e.currentTarget));
  const groupBtn = tool("Group by", "group", (e) => groupMenu(e.currentTarget));
  const toolbar = h("div", { class: "ws-toolbar", role: "toolbar", "aria-label": "Board tools" },
    h("div", { class: "split" }, newBtn, newMore), h("label", { class: "search-field" }, icon("search"), search), personBtn, filterBtn, sortBtn, hideBtn, groupBtn);

  const head = h("header", { class: "ws-head" },
    h("div", { class: "ws-titlebar" }, h("div", { class: "ws-titlewrap" }, title, info, star), h("div", { class: "ws-presence" }, people, liveDot, retry, headSync, shareBtn, toolsBtn, jobsBtn, more)),
    tabs, toolbar);
  const viewEl = h("div", { class: "ws-view", id: "main", tabindex: "-1" });
  const notices = h("div", { class: "ws-notices", role: "region", "aria-label": "Needs a person", hidden: true });
  const main = h("main", { class: "ws-main" }, head, notices, viewEl);
  const batch = h("div", { class: "batch", role: "region", "aria-label": "Selected artifacts", hidden: true });
  root.replaceChildren(h("div", { class: "ws" }, rail.el, main), panel.el, sheet.el, batch);

  // ---------------------------------------------------------------- painting
  function paintJobs() {
    const n = jobs.running();
    jobsBtn.querySelector("span").textContent = n ? `Jobs · ${n} running` : "Jobs";
    jobsBtn.classList.toggle("is-on", n > 0);
  }
  function paintHead() {
    const p = S.route.project ? projects.get(S.route.project) : null;
    title.textContent = S.route.page === "home" ? "Home" : p ? p.title : S.route.waiting ? "Waiting on you" : "All artifacts";
    document.title = `${title.textContent} – Workspace`;
    star.hidden = !p;
    if ((p?.id || "") !== (headSync.dataset.pid || "")) {
      headSync.dataset.pid = p?.id || "";
      headSync.replaceChildren(...(p ? [sync.pill(p.id, "head", p.title)] : []));
    }
    shareBtn.hidden = !p;
    shareBtn.disabled = !sharing();
    shareBtn.dataset.tip = sharing() ? `Share ${p ? p.title : "this project"} with a person` : noShare;
    shareBtn.classList.add("tip");
    if (p) {
      const fav = S.favs.has(p.id);
      star.setAttribute("aria-pressed", String(fav));
      star.setAttribute("aria-label", fav ? "Remove from favourites" : "Add to favourites");
      star.dataset.tip = star.getAttribute("aria-label");
      star.classList.toggle("is-fav", fav);
      star.replaceChildren(icon(fav ? "star-fill" : "star"));
    }
    liveDot.textContent = { live: "Live", reconnecting: "Reconnecting", connecting: "Connecting", offline: "Not live: changes from Claude will not show" }[S.live];
    liveDot.dataset.state = S.live;
    retry.hidden = S.live !== "offline";
    const recent = (S.data?.activity || []).filter((a) => S.data.now - (a.at || 0) < 15 * 60000);
    const who = [...new Set(recent.filter((a) => a.actor !== "scheduler").map((a) => (a.actor === "claude" ? "Claude" : a.who)))].slice(0, 4);
    people.replaceChildren(...(who.length ? [h("span", {}, "Active"), h("span", { class: "avatars" }, who.map((n) => avatar(n, { live: isClaude(n) })))] : []));
    for (const [v, t] of Object.entries(tabEls)) { t.setAttribute("aria-selected", String(S.view === v)); t.tabIndex = S.view === v ? 0 : -1; }
  }
  function paintTools() {
    const on = (b, yes, n) => { b.classList.toggle("is-on", !!yes); const c = b.querySelector(".tool-count"); c.hidden = !n; c.textContent = n || ""; };
    on(personBtn, S.person !== undefined, 0);
    on(filterBtn, filtersOn() > 0, filtersOn());
    on(sortBtn, S.sort !== "manual", 0);
    on(hideBtn, S.hidden.size > 0, S.hidden.size);
    on(groupBtn, S.group !== "project", 0);
    const none = !!S.data && !projects.size;
    newBtn.disabled = newMore.disabled = none;
    newBtn.title = none ? "Add a project first: point Claude at a folder" : "";
    for (const b of [personBtn, filterBtn, sortBtn, hideBtn, groupBtn]) b.disabled = none;
    search.disabled = none;
    groupBtn.hidden = S.view !== "table";
    hideBtn.hidden = S.view !== "table";
    personBtn.hidden = filterBtn.hidden = sortBtn.hidden = !BOARD_VIEWS.has(S.view);   // the other views have their own filters
    search.setAttribute("aria-label", { calendar: "Search the calendar", previews: "Search the previews", sources: "Search the sources" }[S.view] || "Search artifacts");
    if (S.person !== undefined) personBtn.querySelector(".tool-label").textContent = S.person === null ? "Nobody" : (isClaude(S.person) ? "Claude" : S.person);
    else personBtn.querySelector(".tool-label").textContent = "Person";
  }
  function paintBatch() {
    const sel = [...S.selected].map((k) => rows.get(k)).filter(Boolean);
    document.body.classList.toggle("has-batch", sel.length > 0);
    batch.hidden = !sel.length;
    if (!sel.length) { batch.replaceChildren(); return; }
    const act2 = (label, iconName, onclick) => h("button", { type: "button", class: "batch-act", onclick }, icon(iconName), h("span", {}, label));
    batch.replaceChildren(
      h("div", { class: "batch-count", "aria-hidden": "true" }, String(sel.length)),
      h("div", { class: "batch-what" }, h("span", { class: "batch-title" }, sel.length === 1 ? "Artifact selected" : "Artifacts selected", h("span", { class: "sr-only" }, `: ${sel.length}`)),
        h("span", { class: "batch-dots" }, sel.slice(0, 14).map((r) => h("span", { class: "dot", style: { color: `var(--stage-${r.stage.toLowerCase()})` } })))),
      h("div", { class: "batch-actions" },
        act2("Stage", "check", (e) => stagePopover(e.currentTarget, sel.length === 1 ? sel[0].stage : null, (s) => act.stage(sel, s), { outReason: S.data?.out_refused })),
        act2("Who", "person", (e) => peoplePopover(e.currentTarget, peopleOf(sel), null, (n) => act.fields(sel, { owner: n }, { say: (k) => `Gave ${one(k, "artifacts", sel)} to ${n || "nobody"}` }))),
        act2("Due date", "calendar", (e) => datePopover(e.currentTarget, null, (d) => act.fields(sel, { due: d }, { say: d ? `Due ${fmtDate(d)}` : "Due date cleared" }))),
        act2("Archive", "archive", () => { act.archive(sel); S.selected.clear(); paintBatch(); view()?.paintSelection?.(); })),
      button("Clear selection", { variant: "tertiary", iconName: "x", iconOnly: true, class: "batch-close", onclick: () => { S.selected.clear(); paintBatch(); view()?.paintSelection?.(); } }));
    batch.querySelector(".batch-count").textContent = String(sel.length);
  }
  const peopleOf = (list) => [...new Set(list.flatMap((r) => projects.get(r.project)?.members || []))];

  function paintView(opts = {}) {
    if (S.route.page === "home") return;
    const v = view();
    if (S.loading && !S.data) { viewEl.replaceChildren(board.skeleton()); return; }
    if (S.error && !S.data) {
      viewEl.replaceChildren(h("div", { class: "ws-error", role: "alert" },
        h("h2", {}, "The Workspace could not load"),
        h("p", { class: "secondary-text" }, S.error.network ? "The app is not answering. Check that AVE Studio is still open, then try again."
          : "Something went wrong reading the projects on this computer. Your work is safe on disk."),
        button("Try again", { variant: "primary", onclick: (e) => { S.loading = true; busy(e.currentTarget, () => load()); } })));
      return;
    }
    if (!projects.size) { viewEl.replaceChildren(emptyNoProjects()); return; }
    const t0 = performance.now();
    if (viewEl.firstElementChild !== v.el) viewEl.replaceChildren(v.el);
    v.render(opts);
    if (probe) probe.renderMs = performance.now() - t0;
  }
  function refresh(keys) {
    rail.paint(); paintBatch();
    if (S.route.page === "home") { home.render(); return; }
    const v = view();
    if (!v || !v.update(keys, new Map())) paintView();
    panel.paintRow();
  }
  function paintAll({ changed = [], flashes = new Map(), first = false } = {}) {
    paintHead(); paintTools(); rail.paint(); paintBatch();
    main.classList.toggle("is-home", S.route.page === "home");
    head.hidden = S.route.page === "home";
    if (S.route.page === "home") {
      VIEW_OF[S.view]?.leave?.();
      sheet.close(false);
      if (viewEl.isConnected) main.replaceChildren(home.el);
      home.render();
      return;
    }
    if (!viewEl.isConnected) main.replaceChildren(head, notices, viewEl);
    paintNotices();
    if (first || !view().update || S.error) paintView();
    else view().update(changed, flashes);
    panel.paintRow();
  }

  // Sync found two copies of something: an alert for a kind of file that should never conflict (both copies left in place), or a
  // setting two people changed at once on an artifact with no row of its own. The rest waits on its row's "Waiting on you".
  function paintNotices() {
    const list = (S.data?.projects || []).filter((p) => !S.route.project || p.id === S.route.project)
      .flatMap((p) => (p.notices || []).map((n) => ({ ...n, project: p })));
    const sig = JSON.stringify(list.map((n) => [n.type, n.conflict || n.decision, n.text || n.summary]));
    if (sig === notices.dataset.sig) return;
    notices.dataset.sig = sig;
    notices.hidden = !list.length;
    notices.replaceChildren(...list.map((n) => {
      if (n.type === "update") {
        return h("div", { class: "notice notice-negative notice-update", role: "alert" }, icon("alert"),
          h("div", { class: "notice-body" },
            h("p", { class: "notice-title" }, `Update needed: ${n.project.title} is read-only`),
            h("p", { class: "notice-text" }, n.text),
            h("p", { class: "notice-text" }, "Choose AVE Studio \u203a Check for Updates\u2026, then open it again to edit.")));
      }
      if (n.type === "alert") {
        return h("div", { class: "notice notice-negative", role: "alert" }, icon("alert"),
          h("div", { class: "notice-body" },
            h("p", { class: "notice-title" }, `Two copies of ${n.file.split("/").pop()} in ${n.project.title}`),
            h("p", { class: "notice-text" }, n.text),
            h("span", { class: "notice-file", title: n.conflict }, `${n.file} and ${n.conflict.split("/").pop()}`)));
      }
      if (n.type === "waiting") {
        return h("div", { class: "notice", role: "status" }, icon("clock"),
          h("div", { class: "notice-body" },
            h("p", { class: "notice-title" }, `Answered: two copies of ${n.file.split("/").pop()} in ${n.project.title}`),
            h("p", { class: "notice-text" }, n.text)));
      }
      const row = { key: n.key, name: n.project.title, project: n.project.id };
      const again = () => load({ flash: true });
      return h("div", { class: "notice", role: "status" }, icon("people"),
        h("div", { class: "notice-body" },
          h("p", { class: "notice-title" }, n.summary),
          h("p", { class: "notice-text" }, n.reason || "Both are kept until you choose."),
          settingChooser(n, { all: (o, btn) => act.resolve(row, n, o, btn).then(again),
            picks: (p, btn) => act.resolvePicks(row, n, p, btn).then(again) })));
    }));
  }

  // presence runs out on its own: a row whose last person has gone is redrawn without waiting for a write
  setInterval(() => {
    const now = ctx.now(), gone = [];
    for (const [k, r] of rows) {
      if (!(r.presence || []).length) continue;
      const left = r.presence.filter((p) => p.until > now);
      if (left.length !== r.presence.length) { r.presence = left; gone.push(k); }
    }
    if (gone.length) refresh(gone);
  }, 15000);

  function emptyNoProjects() {
    const native = !!window.aveNative?.selectEpisode;
    return h("div", { class: "ws-empty" },
      h("div", { class: "ws-empty-art", "aria-hidden": "true" }, STAGES.slice(0, 3).map((s) => h("span", { class: stageClass(s) }))),
      h("h2", {}, "No projects yet"),
      h("p", { class: "secondary-text" }, "Point Claude at a folder in Claude Code and say what you are making. Claude reads what is there and puts every artifact on this board as it starts."),
      h("p", { class: "say" }, "Start an episode from the footage in ~/Footage/episode-12"),
      native ? button("Open a project folder", { variant: "primary", iconName: "folder", onclick: (e) => busy(e.currentTarget, () => window.aveNative.selectEpisode().then(() => load()).catch((er) => toast(er.message, { tone: "negative" }))) })
        : button("Copy that sentence", { variant: "secondary", onclick: () => copySay("Start an episode from the footage in ~/Footage/episode-12") }));
  }
  function copySay(text) {
    navigator.clipboard?.writeText(text).then(() => toast("Copied. Paste it to Claude in Claude Code."), () => toast("Select the sentence and copy it to Claude."));
  }

  // ---------------------------------------------------------------- toolbar popovers
  function startAdding() {
    if (S.route.page === "home") { location.hash = "#/workspace"; }
    if (S.view !== "table") setView("table");
    S.adding = true;
    board.startAdding();
  }
  function addMenu(anchor) {
    const sel = [...S.selected].map((k) => rows.get(k)).filter(Boolean);
    const native = window.aveNative;
    const noApp = "Both need the AVE Studio app: a browser cannot hand files or folders to it.";
    menu(anchor, [
      { label: sel.length === 1 ? `New part of ${sel[0].name}` : "New part of the selected artifact", icon: "plus", disabled: sel.length !== 1,
        note: sel.length === 1 ? null : "Select one artifact first.", onSelect: () => board.startAdding({ parent: sel[0].key }) },
      "-",
      { label: "From files", icon: "film", disabled: !native?.importFiles,
        onSelect: () => native.importFiles().then(() => { toast("Files added to the studio. Ask Claude to make them artifacts, or add one here."); load({ flash: true }); })
          .catch((e) => toast(e.message, { tone: "negative" })) },
      { label: "Open a project folder", icon: "folder", disabled: !native?.selectEpisode, note: native?.selectEpisode ? null : noApp,
        onSelect: () => native.selectEpisode().then(() => load({ flash: true })).catch((e) => toast(e.message, { tone: "negative" })) },
      { label: "Join a shared project", icon: "inbox", disabled: !native?.joinProject, onSelect: () => joinProject() },
    ], { label: "More ways to add" });
  }
  // ---------------------------------------------------------------- sharing (native sheets)
  function shareItems() {
    const p = S.route.project ? projects.get(S.route.project) : null;
    const n = sharing();
    const need = !n ? noShare : !p ? "Open a project from the left first." : null;
    const others = p ? p.members.filter((m) => m !== p.person) : [];
    return [
      { label: "Share this project", icon: "invite", disabled: !!need, note: need, onSelect: () => shareProject(more) },
      { label: "Remove someone from this project", icon: "person", disabled: !!need || !others.length,
        note: need ? null : others.length ? null : "Nobody else is on this project.", onSelect: () => projectSheet("removeMember", p) },
      { label: "Hand over publishing", icon: "send", disabled: !!need || !others.length, onSelect: () => projectSheet("handOverPublishing", p) },
      { label: "Join a shared project", icon: "inbox", disabled: !n, onSelect: () => joinProject() },
    ];
  }
  function shareProject(anchor) {
    const p = S.route.project ? projects.get(S.route.project) : null;
    if (!p || !sharing()) return;
    busy(anchor, () => sharing().shareProject(p.id)
      .then((r) => { toast(`${r.person} is on ${r.title} now. Their computer starts receiving it while you are both online.`, { tone: "positive" }); load({ flash: true }); })
      .catch((e) => toast(e.message, { tone: /cancelled/i.test(e.message) ? undefined : "negative" })));
  }
  function projectSheet(action, p) {
    if (!p || !sharing()) return;
    sharing()[action](p.id).then((r) => { toast(r.message, { tone: "positive", timeout: 12000 }); load({ flash: true }); })
      .catch((e) => toast(e.message, { tone: /cancel|stays|nobody was/i.test(e.message) ? undefined : "negative" }));
  }
  function joinProject() {
    if (!sharing()) return;
    sharing().joinProject().then((r) => { toast(`You joined ${r.title}. Files arrive while you and the others are online.`, { tone: "positive" }); load({ flash: true }); })
      .catch((e) => toast(e.message, { tone: /cancelled/i.test(e.message) ? undefined : "negative" }));
  }
  function personMenu(anchor) {
    const names = [...new Set([...rows.values()].map((r) => r.owner).filter(Boolean).concat([...projects.values()].flatMap((p) => p.members)))];
    if (!names.some(isClaude)) names.push("Claude");
    const pick = (v) => { S.person = v; paintTools(); paintView(); };
    menu(anchor, [
      { label: "Everyone", checked: S.person === undefined, onSelect: () => pick(undefined) },
      "-",
      ...names.map((n) => ({ label: isClaude(n) ? "Claude" : n, lead: avatar(n), checked: S.person === n, onSelect: () => pick(n) })),
      { label: "Nobody", lead: avatar(null), checked: S.person === null, onSelect: () => pick(null) },
    ], { label: "Show artifacts for", heading: "Show artifacts for" });
  }
  function checkRow(label, on, toggle, lead) {
    const c = h("button", { type: "button", class: "check", role: "checkbox", "aria-checked": String(on), "aria-label": label }, icon("check"));
    const rowEl = h("div", { class: "menu-item", style: { cursor: "pointer" } }, c, lead || null, h("span", {}, label));
    const flip = () => { const v = c.getAttribute("aria-checked") !== "true"; c.setAttribute("aria-checked", String(v)); toggle(v); };
    rowEl.addEventListener("click", (e) => { if (e.target !== c) flip(); });
    c.addEventListener("click", flip);
    return rowEl;
  }
  function filterPop(anchor) {
    const apply = () => { paintTools(); paintView(); };
    const body = h("div", { style: { width: "260px", display: "grid", gap: "2px" } },
      h("p", { class: "popover-title" }, "Filter"),
      h("p", { class: "menu-heading" }, "Stage"),
      STAGES.map((s) => checkRow(s, S.stages.has(s), (v) => { v ? S.stages.add(s) : S.stages.delete(s); apply(); }, h("span", { class: "dot", style: { color: `var(--stage-${s.toLowerCase()})` } }))),
      h("div", { class: "menu-sep" }),
      checkRow("Waiting on you", S.waitingOnly, (v) => { S.waitingOnly = v; apply(); }),
      checkRow("Needs review", S.reviewOnly, (v) => { S.reviewOnly = v; apply(); }),
      checkRow("Show archived", S.archived, (v) => { S.archived = v; apply(); }),
      h("div", { class: "popover-actions" }, button("Clear all", { variant: "tertiary", size: "sm", onclick: () => {
        S.stages.clear(); S.waitingOnly = S.reviewOnly = S.archived = false; pop.close(); apply(); } })));
    const pop = popover(anchor, body, { label: "Filter" });
    body.querySelector(".check")?.focus();
  }
  const SORTS = [["manual", "Your order"], ["name", "Name"], ["stage", "Stage"], ["due", "Due date"], ["updated", "Recently changed"]];
  function sortMenu(anchor) {
    menu(anchor, SORTS.map(([k, l]) => ({ label: l, checked: S.sort === k, onSelect: () => { S.sort = k; savePrefs(); paintTools(); paintView(); } })),
      { label: "Sort", heading: "Sort by" });
  }
  function hidePop(anchor) {
    const body = h("div", { style: { width: "240px", display: "grid", gap: "2px" } }, h("p", { class: "popover-title" }, "Columns"),
      COLUMNS.map((c) => checkRow(c.label, !S.hidden.has(c.id), (v) => { v ? S.hidden.delete(c.id) : S.hidden.add(c.id); savePrefs(); paintTools(); paintView(); })),
      h("div", { class: "popover-actions" }, button("Show all", { variant: "tertiary", size: "sm", onclick: () => { S.hidden.clear(); savePrefs(); pop.close(); paintTools(); paintView(); } })));
    const pop = popover(anchor, body, { label: "Hide columns" });
    body.querySelector(".check")?.focus();
  }
  const GROUPS = [["project", "Project"], ["stage", "Stage"], ["who", "Who"], ["waiting", "Waiting on"], ["campaign", "Campaign"]];
  function groupMenu(anchor) {
    menu(anchor, GROUPS.map(([k, l]) => ({ label: l, checked: S.group === k, onSelect: () => { S.group = k; savePrefs(); paintTools(); paintView(); } })),
      { label: "Group by", heading: "Group by" });
  }
  function showInfo(anchor) {
    const ps = S.route.project ? [projects.get(S.route.project)].filter(Boolean) : [...projects.values()];
    const n = visible().length;
    popover(anchor, h("div", { style: { width: "320px", display: "grid", gap: "8px" } },
      h("p", { class: "popover-title" }, title.textContent),
      h("p", { class: "secondary-text" }, `${n} ${n === 1 ? "artifact" : "artifacts"} from ${ps.length} ${ps.length === 1 ? "project" : "projects"} on this computer. Claude keeps it current; anything you change here is recorded as you.`),
      ps.map((p) => { const n = [...rows.values()].filter((r) => r.project === p.id && !r.archived).length;
        return h("p", { class: "pop-who" }, h("span", { class: "dot", style: { color: p.colour } }), h("span", {}, p.title), h("span", { class: "secondary-text" }, `${n} ${n === 1 ? "artifact" : "artifacts"}`)); })),
    { label: "About this board" });
  }
  function toggleFav() {
    const id = S.route.project;
    if (!id) return;
    S.favs.has(id) ? S.favs.delete(id) : S.favs.add(id);
    savePrefs(); paintHead(); rail.paint();
  }
  function setView(v) {
    if (S.view === v) return;
    view()?.leave?.();
    if (v !== "calendar" && v !== "previews") sheet.close(false);
    S.view = v; savePrefs(); paintHead(); paintTools();
    viewEl.replaceChildren(); viewEl.scrollLeft = 0; viewEl.scrollTop = 0; paintView();
  }

  const SHORTCUTS = [["/", "Search"], ["?", "Show these shortcuts"], ["N", "New artifact"], ["1 to 5", "Main table, Kanban, Calendar, Previews or Sources"],
    ["Arrow keys", "Move between cells"], ["Enter", "Edit the cell, or open its choices"], ["Space or X", "Select the row"], ["O", "Open the row's panel"],
    ["Esc", "Cancel, close, or clear the selection"]];
  function showKeys(anchor) {
    const list = h("dl", { class: "keys" }, SHORTCUTS.flatMap(([k, v]) => [h("dt", {}, h("kbd", {}, k)), h("dd", {}, v)]));
    popover(anchor || search, h("div", { class: "keys-pop" }, h("p", { class: "popover-title" }, "Keyboard shortcuts"), list,
      h("p", { class: "secondary-text keys-note" }, "Shortcuts wait while you are typing.")), { label: "Keyboard shortcuts", align: "start" });
  }

  // ---------------------------------------------------------------- keyboard (never while typing, never over a popover)
  const life = new AbortController();
  document.addEventListener("keydown", (e) => {
    if (e.metaKey || e.ctrlKey || e.altKey) return;
    if (document.querySelector(".popover")) return;
    if (approvals.isOpen()) return;                            // the approval sheet keeps its own keys
    if (e.key === "Escape" && !typing(e)) {
      if (sheet.isOpen()) { e.preventDefault(); sheet.close(); return; }
      if (panel.isOpen()) { e.preventDefault(); panel.close(); return; }
      if (S.selected.size) { e.preventDefault(); S.selected.clear(); paintBatch(); view()?.paintSelection?.(); return; }
    }
    if (typing(e)) return;
    if (e.key === "/") { e.preventDefault(); if (S.route.page === "home") rail.focusSearch(); else search.focus(); }
    else if (e.key === "?") { e.preventDefault(); showKeys(); }
    else if (e.key === "n" && S.route.page !== "home") { e.preventDefault(); startAdding(); }
    else if (/^[1-5]$/.test(e.key) && S.route.page !== "home") setView(VIEWS[+e.key - 1][0]);
  }, { signal: life.signal });

  // ---------------------------------------------------------------- start
  function openPost(id) {
    sheet.open({ post: id }).then(() => { if (/^#\/workspace\/post\//.test(location.hash)) history.replaceState(null, "", "#/workspace"); });
  }
  function setRoute(r) {
    if (r.approvals) openApprovals();
    if (r.post) openPost(r.post);
    const pageChanged = r.page !== S.route.page || r.project !== S.route.project || r.waiting !== S.route.waiting;
    S.route = r;
    if (!pageChanged) return;
    S.selected.clear();
    viewEl.replaceChildren();
    paintAll({ first: true });
    viewEl.scrollTop = 0;
  }
  paintAll({ first: true });
  load();
  if (route.approvals) openApprovals();
  if (route.post) openPost(route.post);
  if (probe) probe.reload = () => load({ flash: true });
  return {
    setRoute,
    close: () => { es?.close(); clearTimeout(retryTimer); clearTimeout(reloadTimer); life.abort(); panel.close(false); view()?.leave?.(); sheet.close(false); sheet.el.remove(); approvals.el.remove();
      document.body.classList.remove("has-batch"); },
  };
}
