// Sources: what each project is made from, like a Monday table grouped by project. Footage, graphics and music from the
// project folders, and the studio's library with its licences. Each file says where it is in plain words, by name: "Shared with
// Tomas", "Only on this computer", "Proxies only on Tomas's computer" (GET /api/workspace/sources, project/where.py).
//
// Files are added through the app's own panel (aveNative.importLibrary), never a web file input: the app's web view cannot
// show a file picker, and the originals stay where they are. "Find B-roll" searches the stock providers and the library
// (POST broll/search) and adds a pick only with a named approver (POST broll/pick, which the app protects), as the old Library's
// B-roll dialog did. The licence words, the listing and the candidate facts are the old Library screen's own logic
// (static/library-core.js), shared, not copied.

import { h, button, icon, toast, busy, popover } from "../dom.js";
import { api, plain, menu, avatar } from "./common.js";
import { licenceState, thumbUrl, usageText, importToLibrary, NO_APP, KINDS, SOURCE_WORDS, LICENCES, PROVIDERS, DEFAULT_PROVIDERS, candidateFacts,
  brollSearch, brollPick, NEED_NAME } from "/app/shared/library-core.js";
import { fmtBytes } from "/app/shared/panels.js";

const REACH = [["", "Anywhere"], ["local", "Only on this computer"], ["shared", "Shared with everyone"], ["partial", "Not on every computer"]];
const KIND_ICON = { video: "film", audio: "volume", image: "grid", document: "folder" };

export function createSources(ctx, studio) {
  const studioApi = studio.api;
  const { S } = ctx;
  const P = (S.src ||= { kind: "", reach: "" });
  P.licence ||= ""; P.unused ||= false;
  let data = null, error = null, seq = 0, alive = false, reloadTimer = 0, projSig = "";
  const B = { query: "", kind: "photo", providers: new Set(DEFAULT_PROVIDERS), search: null, busy: false, status: "", by: "" };

  const native = () => window.aveNative?.importLibrary ? window.aveNative : null;
  const pasteBtn = button("Paste text", { variant: "secondary", size: "sm", iconName: "bubble-plus", "aria-haspopup": "dialog", onclick: (e) => pasteText(e.currentTarget) });
  const addBtn = button("Add files", { variant: "primary", size: "sm", iconName: "plus", onclick: (e) => addFiles(e.currentTarget) });
  const kindBtn = h("button", { type: "button", class: "btn btn-tertiary btn-tool", "aria-haspopup": "menu", "aria-expanded": "false", onclick: (e) => kindMenu(e.currentTarget) },
    icon("filter"), h("span", { class: "tool-label" }, "Any kind"));
  const reachBtn = h("button", { type: "button", class: "btn btn-tertiary btn-tool", "aria-haspopup": "menu", "aria-expanded": "false", onclick: (e) => reachMenu(e.currentTarget) },
    icon("people"), h("span", { class: "tool-label" }, "Anywhere"));
  const licBtn = h("button", { type: "button", class: "btn btn-tertiary btn-tool", "aria-haspopup": "menu", "aria-expanded": "false", onclick: (e) => licenceMenu(e.currentTarget) },
    icon("check"), h("span", { class: "tool-label" }, "Any licence"));
  const brollInput = h("input", { class: "input", type: "search", placeholder: "Find B-roll and stock", "aria-label": "Find B-roll", autocomplete: "off", spellcheck: "false" });
  brollInput.addEventListener("keydown", (e) => { if (e.key === "Enter") { e.preventDefault(); findBroll(); } if (e.key === "Escape" && B.search) { e.preventDefault(); e.stopPropagation(); closeBroll(); } });
  const brollField = h("label", { class: "search-field src-broll-field" }, icon("search"), brollInput);
  const sub = h("p", { class: "secondary-text" });
  const brollHost = h("section", { class: "src-brollhost", "aria-label": "B-roll results", hidden: true });
  const host = h("div", { class: "src-host" });
  const el = h("div", { class: "src", "aria-busy": "false" },
    h("div", { class: "cal-bar" }, h("div", { class: "cal-nav" }, sub), h("div", { class: "cal-tools" }, brollField, kindBtn, reachBtn, licBtn, pasteBtn, addBtn)), brollHost, host);

  function kindMenu(anchor) {
    menu(anchor, KINDS.map(([k, l]) => ({ label: l, checked: P.kind === k, onSelect: () => { P.kind = k; ctx.savePrefs(); paint(); } })), { label: "Kind", heading: "Kind", align: "end" });
  }
  function licenceMenu(anchor) {
    const pick = (k) => { P.licence = k; ctx.savePrefs(); paint(); };
    menu(anchor, [...LICENCES.map(([k, l]) => ({ label: l, checked: P.licence === k, onSelect: () => pick(k) })), "-",
      { label: "Unused only", checked: !!P.unused, onSelect: () => { P.unused = !P.unused; ctx.savePrefs(); paint(); } }], { label: "Licence", heading: "Library licence", align: "end" });
  }
  function reachMenu(anchor) {
    menu(anchor, REACH.map(([k, l]) => ({ label: l, checked: P.reach === k, onSelect: () => { P.reach = k; ctx.savePrefs(); paint(); } })), { label: "Where", heading: "Where it is", align: "end" });
  }

  async function load({ quiet = false } = {}) {
    const my = ++seq;
    el.setAttribute("aria-busy", "true");
    if (!data) paint();
    try {
      const got = await api("GET", "/api/workspace/sources");
      if (my !== seq) return;
      data = got; error = null;
    } catch (e) {
      if (my !== seq) return;
      error = e;
      if (data && !quiet) toast(plain(e, "read the sources"), { tone: "negative" });
    } finally {
      if (my === seq) { el.setAttribute("aria-busy", "false"); if (alive) paint(); }
    }
  }

  async function addFiles(btn) {
    const n = native();
    if (!n) { toast(NO_APP); return; }
    await busy(btn, async () => {
      try {
        const rows = await importToLibrary();
        if (!rows.length) return;
        const fresh = rows.filter((r) => !r.duplicate), dup = rows.length - fresh.length;
        toast(fresh.length ? `Added ${fresh.length === 1 ? fresh[0].name : `${fresh.length} files`} to the library.${dup ? ` ${dup} ${dup === 1 ? "was" : "were"} already there.` : ""} Add alt text and a licence note before using ${fresh.length === 1 ? "it" : "them"}.`
          : `${rows.length === 1 ? rows[0].name + " is" : "Those files are"} already in the library.`, { tone: fresh.length ? "positive" : undefined });
        await load({ quiet: true });
      } catch (e) { toast(`Could not add files: ${e.message}`, { tone: "negative" }); }
    });
  }

  // Paste an idea or a passage of your own writing: the old desk's intake (`POST intake`). The text is copied into the studio's work
  // files as untrusted source material; nothing is fetched and no path is taken. An idea becomes an idea post in the Ideas group.
  function pasteText(anchor) {
    let kind = "writing";
    const seg = h("div", { class: "seg-toggle", role: "group", "aria-label": "What it is" },
      [["writing", "Your writing"], ["idea", "An idea"]].map(([k, l]) => h("button", { type: "button", class: "seg-opt", dataset: { kind: k }, "aria-pressed": String(k === kind),
        onclick: () => { kind = k; for (const b of seg.children) b.setAttribute("aria-pressed", String(b.dataset.kind === k)); title.closest(".field").hidden = k !== "writing"; save.querySelector("span:last-child").textContent = k === "idea" ? "Add to Ideas" : "Save as a source"; } }, l)));
    const title = h("input", { class: "input", maxlength: "200", "aria-label": "Title", placeholder: "Title (optional)" });
    const text = h("textarea", { class: "input", rows: "8", maxlength: "200000", "aria-label": "Text to add", placeholder: "A thought, a passage, something you want to make…" });
    const err = h("p", { class: "field-error", role: "alert", hidden: true });
    const save = button("Save as a source", { variant: "primary", size: "sm", onclick: (e) => busy(e.currentTarget, submit) });
    async function submit() {
      if (!text.value.trim()) { err.textContent = "Paste some text first."; err.hidden = false; text.focus(); return; }
      try {
        const res = await studioApi.post("intake", { kind, title: kind === "writing" ? title.value.trim() : "", text: text.value });
        pop.close();
        if (res.fixtures) { toast(res.note); return; }
        toast(res.kind === "idea" ? "Idea added. It is in the Ideas group on the Main table." : "Saved as a source, with passages Claude can quote. Ask Claude to plan from it.", { tone: "positive" });
        load({ quiet: true });
      } catch (e) { err.textContent = `${plain(e, "save it")} Your text is still here.`; err.hidden = false; }
    }
    const pop = popover(anchor, h("div", { class: "src-paste" }, h("p", { class: "popover-title" }, "Paste text"),
      h("p", { class: "secondary-text" }, "An idea, or your own essay or passage. It is copied into this studio; the text is never treated as instructions."),
      seg, h("label", { class: "field" }, h("span", { class: "field-label" }, "Title"), title), text, err,
      h("div", { class: "popover-actions" }, button("Cancel", { variant: "tertiary", size: "sm", onclick: () => pop.close() }), save)),
    { label: "Paste text", modal: true, wide: true, align: "end" });
    text.focus();
  }

  // ---------------------------------------------------------------- what to show
  const words = () => S.q.split(/\s+/).filter(Boolean);
  function rowsOf(p) {
    const files = (p.files || []).map((f) => ({ type: "file", key: `${p.id}:${f.path}`, name: f.name, folder: f.folder, kind: f.kind, bytes: f.bytes, where: f.where, f }));
    const lib = (p.library || []).map((a) => ({ type: "asset", key: `${p.id}:${a.id}`, name: assetName(a), folder: "Library", kind: a.kind, bytes: a.bytes, where: a.where, a }));
    const libOnly = !!P.licence || !!P.unused;
    return [...files, ...lib].filter((r) => (!P.kind || r.kind === P.kind)
      && (!libOnly || (r.a && (!P.licence || (P.licence === "missing") === licenceState(r.a).missing) && (!P.unused || !(r.a.usage || []).length)))
      && (!P.reach || reachOf(r) === P.reach)
      && words().every((w) => [r.name, r.folder, r.where?.text, r.a ? (r.a.tags || []).join(" ") : ""].join(" ").toLowerCase().includes(w)));
  }
  const reachOf = (r) => (["local", "solo", "proxy-here"].includes(r.where?.reach) ? (r.where.reach === "proxy-here" ? "partial" : "local") : r.where?.reach === "shared" ? "shared" : "partial");
  const projectsShown = () => (data?.projects || []).filter((p) => !S.route.project || p.id === S.route.project);

  function paint() {
    if (!alive) return;
    const kindL = KINDS.find(([k]) => k === P.kind)?.[1] || "Any kind";
    kindBtn.querySelector(".tool-label").textContent = kindL; kindBtn.classList.toggle("is-on", !!P.kind);
    reachBtn.querySelector(".tool-label").textContent = REACH.find(([k]) => k === P.reach)?.[1] || "Anywhere"; reachBtn.classList.toggle("is-on", !!P.reach);
    licBtn.querySelector(".tool-label").textContent = [P.licence ? LICENCES.find(([k]) => k === P.licence)[1] : "Any licence", P.unused ? "unused" : null].filter(Boolean).join(", ");
    licBtn.classList.toggle("is-on", !!P.licence || !!P.unused);
    addBtn.disabled = !native();
    addBtn.dataset.tip = native() ? "Choose files to add to the library. Your originals stay where they are." : NO_APP;
    addBtn.classList.add("tip");
    if (!data && !error) { sub.textContent = ""; host.replaceChildren(skeleton()); return; }
    if (error && !data) {
      sub.textContent = "";
      host.replaceChildren(h("div", { class: "ws-error", role: "alert" }, h("h2", {}, "The sources could not load"),
        h("p", { class: "secondary-text" }, error.network ? "The app is not answering. Check that AVE Studio is still open, then try again." : `${error.message} Your files are safe on disk.`),
        button("Try again", { variant: "primary", onclick: (e) => busy(e.currentTarget, () => load()) })));
      return;
    }
    const groups = projectsShown().map((p) => ({ p, rows: rowsOf(p) }));
    const all = groups.reduce((n, g) => n + g.rows.length, 0);
    const listed = projectsShown().reduce((n, p) => n + (p.files || []).length + (p.library || []).length, 0);
    sub.textContent = `${all} ${all === 1 ? "file" : "files"}${all !== listed ? ` of ${listed}` : ""} in ${groups.length} ${groups.length === 1 ? "project" : "projects"}. Each one says who else has it.`;
    if (!listed) {
      host.replaceChildren(h("div", { class: "ws-empty", role: "status" }, h("div", { class: "ws-empty-art", "aria-hidden": "true" }, icon("folder")),
        h("h2", {}, "No sources yet"),
        h("p", { class: "secondary-text" }, "Footage, graphics and music in the project folders show here, and so does everything in the studio's library. Add files, or ask Claude to bring in footage."),
        native() ? button("Add files", { variant: "primary", iconName: "plus", onclick: (e) => addFiles(e.currentTarget) }) : h("p", { class: "secondary-text" }, NO_APP)));
      return;
    }
    if (!all) {
      host.replaceChildren(h("div", { class: "ws-empty", role: "status" }, h("h2", {}, "Nothing matches these filters"),
        h("p", { class: "secondary-text" }, "No file fits the search, kind and place you chose."),
        button("Clear filters", { variant: "primary", onclick: () => { P.kind = ""; P.reach = ""; P.licence = ""; P.unused = false; ctx.savePrefs(); ctx.clearSearch(); paint(); } })));
      return;
    }
    host.replaceChildren(...groups.filter((g) => g.rows.length).map(group));
  }

  function group({ p, rows }) {
    const colour = ctx.colourOf(p.id);
    const cut = (p.total || 0) > (p.files || []).length;
    const libCut = (p.library_total || 0) > (p.library || []).length;
    const others = p.others || [];
    return h("section", { class: "src-group", style: { "--group": colour }, "aria-label": p.title },
      h("div", { class: "src-ghead" }, h("h3", { class: "src-gtitle" }, p.title), h("span", { class: "secondary-text" }, `${rows.length} ${rows.length === 1 ? "file" : "files"}`),
        others.length ? h("span", { class: "src-people" }, h("span", { class: "avatars" }, others.slice(0, 4).map((n) => avatar(n))),
          h("span", { class: "secondary-text" }, `With ${others.length === 1 ? others[0] : `${others.slice(0, -1).join(", ")} and ${others.at(-1)}`}`)) : h("span", { class: "secondary-text" }, "Only you")),
      h("div", { class: "src-table", role: "table", "aria-label": `${p.title} sources` },
        h("div", { class: "src-row src-hrow", role: "row" }, ["File", "Kind", "Folder", "Size", "Where it is", "Licence"].map((c, i) =>
          h("div", { class: `src-cell c-${i}`, role: "columnheader" }, c))),
        rows.map((r) => row(r, p)),
        cut ? h("div", { class: "src-row src-cut", role: "row" }, h("div", { class: "src-cell", role: "cell" }, `Showing ${(p.files || []).length} of ${p.total} files. Search to find the rest.`)) : null,
        libCut ? h("div", { class: "src-row src-cut", role: "row" }, h("div", { class: "src-cell", role: "cell" }, `Showing ${(p.library || []).length} of ${p.library_total} library files. Search to find the rest.`)) : null));
  }
  function row(r, p) {
    const lic = r.a ? licenceState(r.a) : null;
    const thumb = r.a ? thumbUrl(r.a) : "";
    return h("div", { class: "src-row", role: "row", dataset: { key: r.key, type: r.type } },
      h("div", { class: "src-cell c-0", role: "cell" },
        h("span", { class: "src-thumb" }, thumb && r.kind === "image" ? h("img", { src: thumb, alt: "", loading: "lazy", decoding: "async" }) : icon(KIND_ICON[r.kind] || "folder")),
        h("button", { type: "button", class: "src-name", title: r.name, onclick: (e) => detail(e.currentTarget, r, p) }, r.name)),
      h("div", { class: "src-cell c-1", role: "cell" }, r.f?.proxy ? "Review proxy" : kindWord(r.kind)),
      h("div", { class: "src-cell c-2", role: "cell" }, r.folder),
      h("div", { class: "src-cell c-3", role: "cell" }, r.bytes == null ? h("span", { class: "empty" }, "–") : fmtBytes(r.bytes)),
      h("div", { class: `src-cell c-4 where is-${r.where?.reach || "unknown"}`, role: "cell", title: r.where ? `${r.where.text}. ${r.where.detail}` : "" }, whereEl(r.where)),
      h("div", { class: "src-cell c-5", role: "cell" }, lic ? h("span", { class: `lic ${lic.missing ? "is-missing" : "is-ok"}`, title: lic.title }, icon(lic.missing ? "alert" : "check"), h("span", {}, lic.text))
        : h("span", { class: "empty" }, "–")));
  }
  function whereEl(w) {
    if (!w) return h("span", { class: "empty" }, "Not known");
    // a member's own media choice ("proxies only") is normal, so it reads with people; only a file someone lacks warns
    const mark = w.reach === "local" || w.reach === "solo" ? icon("lock") : w.reach === "proxy-here" ? icon("film") : w.reach === "partial" || w.reach === "none" ? icon("alert") : icon("people");
    return h("span", { class: "where-text" }, mark, h("span", {}, w.text));
  }
  function detail(anchor, r, p) {
    const a = r.a;
    const lic = a ? licenceState(a) : null;
    const dl = (pairs) => h("dl", { class: "src-dl" }, pairs.filter(Boolean).flatMap(([k, v]) => [h("dt", {}, k), h("dd", {}, v)]));
    const people = (r.where?.people || []).map((x) => h("li", {}, avatar(x.person), h("span", {}, x.person),
      h("span", { class: "secondary-text" }, x.has === "all" ? "has it" : x.has === "proxy" ? "has the review proxy" : "does not have it")));
    const big = a && r.kind === "image" && thumbUrl(a) ? h("img", { class: "src-big", src: thumbUrl(a), alt: a.alt || "" }) : null;
    popover(anchor, h("div", { class: "src-pop" },
      h("p", { class: "popover-title" }, r.name), big,
      h("p", { class: "src-where" }, whereEl(r.where)), r.where?.detail ? h("p", { class: "secondary-text" }, r.where.detail) : null,
      people.length ? h("ul", { class: "src-whom" }, people) : null,
      dl([["Project", p.title], ["Folder", r.folder], ["Kind", `${kindWord(r.kind)}${a && a.w ? `, ${a.w} × ${a.h}` : ""}`], ["Size", r.bytes == null ? "Not known" : fmtBytes(r.bytes)],
        r.f && r.f.folder === "Footage" ? ["Review proxy", r.f.proxy ? "This is the proxy" : r.f.has_proxy ? "Made" : "Not made yet"] : null,
        a ? ["Source", SOURCE_WORDS[a.source?.type] || a.source?.type || "Not known"] : null,
        a ? ["Licence", lic.missing ? h("span", { class: "lic is-missing" }, icon("alert"), h("span", {}, "No licence entry covers this file")) : lic.text] : null,
        a && a.license?.attribution?.required ? ["Credit", a.license.attribution.text] : null,
        a ? ["Alt text", a.alt || h("span", { class: "secondary-text" }, "None yet")] : null,
        a && (a.tags || []).length ? ["Tags", a.tags.join(", ")] : null,
        a ? ["Used", usageText(a)] : null])), { label: r.name, align: "start", place: anchor.getBoundingClientRect().top > innerHeight * 0.55 ? "above" : "below" });
  }
  const kindWord = (k) => ({ video: "Video", audio: "Audio", image: "Image", document: "Document" }[k] || "File");
  const assetName = (a) => a.alt || (a.tags || []).join(", ") || `${kindWord(a.kind)}${a.w ? ` ${a.w} × ${a.h}` : ""}`;

  // ---------------------------------------------------------------- B-roll: search, then a named pick
  function closeBroll() { B.search = null; B.status = ""; brollHost.hidden = true; brollHost.replaceChildren(); }
  async function findBroll() {
    const q = brollInput.value.trim();
    if (!q) { closeBroll(); return; }
    B.query = q; B.busy = true; B.status = "Searching…"; B.search = B.search || { candidates: [] };
    paintBroll();
    try {
      const res = await brollSearch(studioApi, { query: q, providers: [...B.providers], kind: B.kind });
      B.search = res.search || res;
      const n = (B.search.candidates || []).length;
      B.status = n ? `${n} ${n === 1 ? "result" : "results"}. Nothing is downloaded until you pick one.` : "No results. Try other words or another provider.";
    } catch (e) { B.status = `The search did not work: ${e.message}`; B.search = { candidates: [] }; }
    B.busy = false;
    paintBroll();
  }
  function providersMenu(anchor) {
    menu(anchor, PROVIDERS.map(([k, l]) => ({ label: l, checked: B.providers.has(k), onSelect: () => {
      if (B.providers.has(k) && B.providers.size > 1) B.providers.delete(k); else B.providers.add(k);
      paintBroll(); if (B.query) findBroll(); } })), { label: "Where to search", heading: "Where to search", align: "end" });
  }
  function paintBroll() {
    if (!B.search) { brollHost.hidden = true; return; }
    brollHost.hidden = false;
    const by = h("input", { class: "input", id: "broll-by", value: B.by, placeholder: "Your name", "aria-label": "Who is approving the download", autocomplete: "name",
      oninput: (e) => { B.by = e.target.value; } });
    const kinds = h("div", { class: "seg-toggle", role: "group", "aria-label": "Kind" }, [["photo", "Photos"], ["video", "Video"]].map(([k, l]) =>
      h("button", { type: "button", class: "seg-opt", "aria-pressed": String(B.kind === k), onclick: () => { B.kind = k; findBroll(); } }, l)));
    const where = PROVIDERS.filter(([k]) => B.providers.has(k)).map(([, l]) => l).join(", ");
    brollHost.replaceChildren(
      h("div", { class: "src-ghead" }, h("h3", { class: "src-gtitle", style: { color: "var(--text)" } }, `B-roll for “${B.query}”`),
        h("span", { class: "secondary-text", role: "status" }, B.status),
        h("span", { class: "src-brolltools" }, kinds,
          h("button", { type: "button", class: "btn btn-tertiary btn-tool", "aria-haspopup": "menu", "aria-expanded": "false", onclick: (e) => providersMenu(e.currentTarget) },
            icon("filter"), h("span", { class: "tool-label" }, where)),
          button("Close the results", { variant: "tertiary", size: "sm", iconName: "x", iconOnly: true, onclick: () => closeBroll() }))),
      h("div", { class: "src-brollby" }, h("label", { class: "field" }, h("span", { class: "field-label" }, "Approved by"), by),
        h("p", { class: "secondary-text" }, "A named person approves every download, and the name is logged with the licence entry.")),
      B.busy && !(B.search.candidates || []).length ? h("ul", { class: "src-cands", "aria-hidden": "true" }, Array.from({ length: 4 }, () => h("li", { class: "card src-cand" }, h("span", { class: "skeleton src-candthumb" }))))
        : h("ul", { class: "src-cands", "aria-label": "B-roll results" }, (B.search.candidates || []).map(candCard)));
  }
  function candCard(c) {
    const f = candidateFacts(c);
    const pick = button("Download and add", { variant: "primary", size: "sm", onclick: (e) => pickOne(e.currentTarget, f.key) });
    return h("li", { class: "card src-cand", dataset: { cand: f.key } },
      f.thumb ? h("img", { class: "src-candthumb", src: f.thumb, alt: c.title || "", loading: "lazy" })
        : h("span", { class: "src-candthumb src-candph", title: "The preview is fetched by the server; none is cached yet." }, icon(c.kind === "video" ? "film" : "grid"), h("span", {}, `${c.w}×${c.h}`)),
      h("div", { class: "src-candbody" },
        h("p", { class: "src-candby" }, f.by),
        h("span", { class: "lic is-ok" }, icon("check"), h("span", {}, f.licence)),
        h("span", { class: `src-credit${c.attribution_required ? " is-required" : ""}` }, f.credit),
        f.creditLine ? h("p", { class: "secondary-text" }, f.creditLine) : null,
        h("p", { class: f.flagged ? "src-check is-flagged" : "secondary-text" }, f.check),
        h("p", { class: "secondary-text" }, f.size),
        c.page_url ? h("a", { class: "src-provider", href: c.page_url, target: "_blank", rel: "noopener noreferrer" }, "View on the provider's site") : null,
        pick));
  }
  async function pickOne(btn, key) {
    const by = B.by.trim();
    if (!by) { B.status = NEED_NAME; paintBroll(); brollHost.querySelector("#broll-by")?.focus(); return; }
    await busy(btn, async () => {
      try {
        const res = await brollPick(studioApi, B.search.id, key, by);
        toast(`Added to the library with its licence entry, approved by ${by}.`, { tone: "positive" });
        B.status = "Added. It is in the library below."; paintBroll();
        await load({ quiet: true });
        return res;
      } catch (e) { toast(`Could not download: ${e.message}`, { tone: "negative" }); }
    });
  }

  function skeleton() {
    return h("div", { class: "src-group", "aria-hidden": "true" }, h("div", { class: "src-ghead" }, h("span", { class: "skeleton skeleton-title" })),
      h("div", { class: "src-table" }, Array.from({ length: 6 }, () => h("div", { class: "src-row" }, Array.from({ length: 6 }, (_, i) =>
        h("div", { class: `src-cell c-${i}` }, h("span", { class: "skeleton skeleton-text", style: { width: i ? "60%" : "80%" } })))))));
  }

  return {
    el,
    render() { if (!alive) { alive = true; projSig = [...ctx.projects.keys()].join(","); load(); } else paint(); },
    // a board event rarely changes a project's files; read again only when the projects themselves changed (added, joined, left)
    update() {
      const sig = [...ctx.projects.keys()].join(",");
      if (sig !== projSig) { projSig = sig; clearTimeout(reloadTimer); reloadTimer = setTimeout(() => alive && load({ quiet: true }), 300); }
      return true;
    },
    leave() { alive = false; seq++; clearTimeout(reloadTimer); },
    reload: () => load({ quiet: true }),
  };
}
