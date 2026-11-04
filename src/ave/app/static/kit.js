// /app/kit.html: draws every component in every state, from the same helpers the app uses. Live examples work for real.

import { h, $, button, icon, toast, popover } from "./dom.js";
import { renderStrip } from "./room/strip.js";
import { createCut } from "./room/cut.js";
import { avatar } from "./workspace/common.js";

const root = document.documentElement;
function setTheme(t) {
  root.dataset.theme = t;
  themeBtn.replaceChildren(icon(t === "dark" ? "sun" : "moon"), h("span", {}, t === "dark" ? "Light theme" : "Dark theme"));
  history.replaceState(null, "", t === "dark" ? "#dark" : "#light");
}
const themeBtn = button("Dark theme", { variant: "secondary", onclick: () => setTheme(root.dataset.theme === "dark" ? "light" : "dark") });
$("#theme-tools").append(themeBtn);
setTheme(location.hash === "#dark" ? "dark" : "light");

// ---- colour
const SW = [["Surface", "--surface"], ["Canvas", "--canvas"], ["Border", "--border"], ["Text", "--text"], ["Secondary text", "--text-secondary"],
  ["Primary", "--primary"], ["Positive", "--positive"], ["Negative", "--negative"], ["Waiting on you", "--waiting"], ["Plan", "--stage-plan"],
  ["Draft", "--stage-draft"], ["Refine", "--stage-refine"], ["Finish", "--stage-finish"], ["Out", "--stage-out"], ["Around the picture", "--stage-bg"]];
$("#swatches").append(...SW.map(([name, v]) => h("div", { class: "kit-swatch" }, h("span", { class: "kit-chip", style: { background: `var(${v})` } }),
  h("span", {}, name), h("span", { class: "secondary-text" }, v))));

// ---- buttons: each variant in each state
const STATES = ["Default", "Hover", "Focus", "Active", "Disabled", "Loading"];
const grid = $("#buttons");
grid.append(h("span"), ...STATES.map((s) => h("span", { class: "kit-label" }, s)));
function stateful(make, state) {
  const b = make();
  if (state === "Hover") b.classList.add("is-hover");
  if (state === "Focus") b.classList.add("is-focus");
  if (state === "Active") b.classList.add("is-active");
  if (state === "Disabled") b.disabled = true;
  if (state === "Loading") { b.setAttribute("aria-busy", "true"); b.disabled = true; }
  return b;
}
const ROWS = [
  ["Primary", () => button("Keep this", { variant: "primary", iconName: "check" })],
  ["Secondary", () => button("Go back", { variant: "secondary", iconName: "undo" })],
  ["Tertiary", () => button("Watch the full cut", { variant: "tertiary", iconName: "film" })],
  ["Danger", () => button("Remove version", { variant: "danger" })],
  ["Small", () => button("Flag this moment", { variant: "secondary", size: "sm", iconName: "flag" })],
  ["Icon", () => button("Play", { variant: "tertiary", iconName: "play", iconOnly: true })],
];
for (const [name, make] of ROWS) grid.append(h("span", { class: "kit-label" }, name), ...STATES.map((s) => h("div", {}, stateful(make, s))));
const working = button("Run for two seconds", { variant: "primary", onclick: (e) => {
  const b = e.currentTarget; b.setAttribute("aria-busy", "true"); b.disabled = true;
  setTimeout(() => { b.removeAttribute("aria-busy"); b.disabled = false; toast("Done"); }, 2000);
} });
grid.append(h("span", { class: "kit-label" }, "Live"), h("div", { style: { "grid-column": "span 3" } }, working));

// ---- split button with a real menu
function menu(anchor, items) {
  const list = h("div", { class: "menu kit-static", role: "menu" }, items.map((it) => it === "-" ? h("div", { class: "menu-sep", role: "separator" }) :
    h("button", { type: "button", class: "menu-item", role: "menuitem", onclick: () => { pop.close(); toast(`${it[1]} chosen`); } }, icon(it[0]), it[1])));
  const pop = popover(anchor, list);
  pop.el.classList.add("kit-menu-host");
  pop.el.style.padding = "0"; pop.el.style.background = "transparent"; pop.el.style.boxShadow = "none"; pop.el.style.border = "0";
  list.querySelector(".menu-item")?.focus();
  list.addEventListener("keydown", (e) => {
    const all = [...list.querySelectorAll(".menu-item")]; const i = all.indexOf(document.activeElement);
    if (e.key === "ArrowDown") { e.preventDefault(); all[(i + 1) % all.length].focus(); }
    if (e.key === "ArrowUp") { e.preventDefault(); all[(i - 1 + all.length) % all.length].focus(); }
  });
}
const ITEMS = [["film", "New version from this cut"], ["plus", "New short from this moment"], "-", ["search", "Find a line in the transcript"]];
const arrow = button("More ways to start", { variant: "primary", iconName: "chevron", iconOnly: true, "aria-haspopup": "menu", onclick: (e) => menu(e.currentTarget, ITEMS) });
$("#split").append(
  h("div", { class: "split" }, button("New artifact", { variant: "primary", onclick: () => toast("New artifact started") }), arrow),
  h("div", { class: "split" }, button("New artifact", { variant: "primary", size: "sm", onclick: () => toast("New artifact started") }),
    button("More ways to start", { variant: "primary", size: "sm", iconName: "chevron", iconOnly: true, "aria-haspopup": "menu", onclick: (e) => menu(e.currentTarget, ITEMS) })),
  h("div", { class: "menu kit-static", role: "menu", "aria-label": "Menu, shown open" },
    h("p", { class: "menu-heading" }, "Group by"),
    h("button", { type: "button", class: "menu-item", role: "menuitemradio", "aria-checked": "true" }, icon("check"), "Project"),
    h("button", { type: "button", class: "menu-item is-hover", role: "menuitemradio", "aria-checked": "false" }, h("span", { style: { width: "16px" } }), "Stage"),
    h("button", { type: "button", class: "menu-item", role: "menuitemradio", "aria-checked": "false", disabled: true }, h("span", { style: { width: "16px" } }), "Due date (no dates yet)")));

// ---- text fields
const field = (label, props) => h("label", { class: "field" }, h("span", { class: "field-label" }, label), h("input", { class: "input", ...props }));
$("#fields").append(
  field("Title", { value: "The Long Table, episode 12" }),
  field("Search", { placeholder: "Search artifacts" }),
  field("Due date", { value: "No date", disabled: true }),
  h("label", { class: "field" }, h("span", { class: "field-label" }, "Note for Claude"), h("input", { class: "input", value: "", "aria-invalid": "true", placeholder: "Say what to change" }),
    h("span", { class: "field-label", style: { color: "var(--negative)" } }, "Write a note before flagging")));

// ---- popover and toasts
const flagBtn = button("Flag this moment", { variant: "secondary", iconName: "flag", "aria-haspopup": "dialog", onclick: (e) => {
  const note = h("textarea", { class: "input", rows: "3", placeholder: "Too long here, try the wide shot" });
  const pop = popover(e.currentTarget, h("div", { style: { width: "280px", display: "grid", gap: "8px" } },
    h("p", { class: "popover-title" }, "Flag 0:12.0 to 0:15.0"), h("label", { class: "field" }, h("span", { class: "field-label" }, "Note for Claude"), note),
    h("div", { class: "popover-actions" }, button("Cancel", { variant: "tertiary", size: "sm", onclick: () => pop.close() }),
      button("Flag for Claude", { variant: "primary", size: "sm", onclick: () => { pop.close(); toast("Flagged 0:12.0 to 0:15.0 for Claude"); } }))));
  note.focus();
} });
$("#popovers").append(
  h("div", { class: "popover kit-static", role: "dialog", "aria-label": "Popover, shown open" },
    h("p", { class: "popover-title" }, "Stage"),
    h("div", { style: { display: "grid", gap: "4px", width: "180px" } }, ["Plan", "Draft", "Refine", "Finish"].map((s) =>
      h("span", { class: `pill pill-${s.toLowerCase()}`, style: { height: "32px" } }, s)))),
  h("div", { style: { display: "grid", gap: "12px" } },
    flagBtn,
    button("Show a toast", { variant: "secondary", onclick: () => toast("Kept version 9. Claude hears about it with your next message.") }),
    button("Show an undo toast", { variant: "secondary", onclick: () => toast("Back on version 8", { action: "Undo", onAction: () => toast("On version 9 again") }) }),
    button("Show an error toast", { variant: "secondary", onclick: () => toast("Could not keep this version. The app is not answering: check that AVE Studio is still open.", { tone: "negative" }) })),
  h("div", { style: { display: "grid", gap: "8px" } },
    h("div", { class: "toast kit-toast-static" }, h("span", { class: "toast-text" }, "Back on version 8"),
      button("Undo", { variant: "secondary", size: "sm", class: "toast-action" }), button("Close", { variant: "tertiary", size: "sm", iconName: "x", iconOnly: true })),
    h("div", { class: "toast toast-positive kit-toast-static" }, h("span", { class: "toast-text" }, "Published to YouTube")),
    h("div", { class: "toast toast-negative kit-toast-static" }, h("span", { class: "toast-text" }, "The full cut could not be rendered. Ask Claude to look at the render."))));

// ---- tabs (roving focus with arrow keys)
const TABS = ["Cut", "Script", "Selects"];
const tablist = h("div", { class: "tabs", role: "tablist", "aria-label": "Views" });
const panel = h("p", { class: "secondary-text", role: "tabpanel", style: { padding: "12px 4px 0" } });
function pick(i, focus) {
  [...tablist.children].forEach((t, j) => { t.setAttribute("aria-selected", String(i === j)); t.tabIndex = i === j ? 0 : -1; });
  panel.textContent = `${TABS[i]} view is showing.`;
  if (focus) tablist.children[i].focus();
}
TABS.forEach((t, i) => tablist.append(h("button", { type: "button", class: "tab", role: "tab", onclick: () => pick(i) }, t)));
tablist.append(h("button", { type: "button", class: "tab", role: "tab", disabled: true, "aria-selected": "false", tabindex: "-1" }, "Layout (posts only)"));
tablist.addEventListener("keydown", (e) => {
  const i = [...tablist.children].indexOf(document.activeElement);
  if (e.key === "ArrowRight") pick((i + 1) % TABS.length, true);
  if (e.key === "ArrowLeft") pick((i - 1 + TABS.length) % TABS.length, true);
});
pick(0);
$("#tabs").append(tablist, panel);

// ---- avatars and pills
$("#people").append(
  h("span", { class: "avatar avatar-sm" }, "BL"), h("span", { class: "avatar" }, "BL"), h("span", { class: "avatar avatar-lg", style: { background: "var(--stage-finish)" } }, "JR"),
  h("span", { class: "avatar avatar-claude avatar-live", "aria-label": "Claude, working now" }, "C"), h("span", { class: "avatar avatar-empty", "aria-label": "Nobody yet" }, icon("plus")),
  h("span", { class: "avatars" }, h("span", { class: "avatar" }, "BL"), h("span", { class: "avatar", style: { background: "var(--stage-finish)" } }, "JR"), h("span", { class: "avatar avatar-claude" }, "C")));
$("#pills").append(...["Plan", "Draft", "Refine", "Finish", "Out"].map((s) => h("span", { class: `pill pill-${s.toLowerCase()}` }, s)),
  h("span", { class: "pill pill-waiting" }, "Waiting on you"), h("span", { class: "pill pill-quiet" }, "Not started"));

// ---- table cells: a group with its coloured edge, solid stage cells, people, plain dates
const rows = [["Episode 12", "Refine", "BL", "Nov 4", "Waiting on you"], ["Short: the question", "Draft", "C", "Nov 6", ""], ["Thumbnail", "Plan", "JR", "", ""]];
$("#table").append(h("p", { class: "kit-group-title" }, "The Long Table"),
  h("table", { class: "table" },
    h("thead", {}, h("tr", {}, ["Artifact", "Stage", "Who", "Due", "Waiting on"].map((c, i) => h("th", { class: i ? "" : "group-edge", style: { "--group": "var(--stage-refine)" } }, c)))),
    h("tbody", {}, rows.map((r, i) => h("tr", { class: i === 1 ? "is-hover" : "", "aria-selected": i === 2 ? "true" : null },
      h("td", { class: "group-edge cell-edit", style: { "--group": "var(--stage-refine)" }, tabindex: "0" }, r[0]),
      h("td", { class: `cell-status pill-${r[1].toLowerCase()}`, tabindex: "0" }, r[1]),
      h("td", {}, h("span", { class: `avatar avatar-sm ${r[2] === "C" ? "avatar-claude" : ""}` }, r[2])),
      h("td", { class: r[3] ? "" : "secondary-text" }, r[3] || "No date"),
      r[4] ? h("td", { class: "cell-status pill-waiting" }, r[4]) : h("td", { class: "secondary-text" }, "Nothing"))))));

// ---- skeleton and scrollbar
$("#loading").append(
  h("div", { class: "kit-skeleton-card", "aria-label": "Loading" },
    h("div", { class: "kit-skeleton-row" }, h("span", { class: "skeleton skeleton-circle" }), h("span", { class: "skeleton skeleton-title" })),
    h("span", { class: "skeleton skeleton-text", style: { width: "90%" } }), h("span", { class: "skeleton skeleton-text", style: { width: "70%" } }),
    h("span", { class: "skeleton skeleton-text", style: { width: "80%" } })),
  h("div", { class: "kit-scroll", tabindex: "0", "aria-label": "A scrolling list" },
    ...Array.from({ length: 14 }, (_, i) => h("p", { style: { margin: "0 0 8px" } }, `Rough cut, version ${14 - i}`))));

// ---- long text and many items: the Room's strip and timeline under load
const long = { title: "The Long Table, episode 12: what enough looks like when everyone is tired and the money is late", stage: "Refine",
  sources: Array.from({ length: 9 }, (_, i) => ({ name: `cam${i}.mp4`, label: ["Host camera", "Guest camera", "Wide camera", "Second guest camera", "Audience camera", "Recorder audio", "Room microphone", "Phone camera", "Drone camera"][i] })),
  outputs: [{ name: "rough.s2.v12.proxy.mp4", label: "Draft of version 12" }, { name: "rough.s2.v12.master.mp4", label: "Master of version 12" }] };
const segs = [];
const lanesAll = [{ src: "A", label: "Host camera" }, { src: "B", label: "Guest camera" }, { src: "C", label: "Wide camera" }];
for (let i = 0, t = 0; i < 400; i++) { const d = 2 + ((i * 7) % 9); const src = "ABC"[(i * 5) % 3];
  segs.push({ id: `v${i}`, o0: t, o1: t + d, src, label: lanesAll.find((l) => l.src === src).label.replace(" camera", ""), alternates: "ABC".split("").filter((x) => x !== src && i % 4) }); t += d; }
const many = createCut({});
many.setCut({ artifact: "kit", version: "rough.s2.v12", duration: segs.at(-1).o1, lanes: lanesAll, segments: segs });
many.setChanged([1200, 1260]);
$("#long").append(
  h("h3", { class: "kit-long-title", title: long.title }, long.title),
  h("div", { class: "kit-strip" }, renderStrip(long)),
  h("div", { class: "kit-timeline" }, many.el.querySelector(".timeline")));

// ---- Workspace components: checkbox, toolbar buttons, chips and flags, rail items, stage choices, a board group, batch bar, cards, panel
{
  const st = (label, el) => h("div", { class: "kit-state" }, el, h("span", {}, label));
  const sub = (title, ...kids) => h("div", {}, h("h3", {}, title), ...kids);
  const chk = (state, cls = "", dis = false) => h("button", { type: "button", class: `check ${cls}`, role: "checkbox", "aria-checked": state, disabled: dis, "aria-label": "Select" }, icon("check"));
  const tool = (label, ic, cls = "", n = 0, dis = false) => h("button", { type: "button", class: `btn btn-tertiary btn-tool ${cls}`, disabled: dis }, icon(ic), h("span", {}, label),
    n ? h("span", { class: "tool-count" }, String(n)) : null);
  const railItem = (label, ic, extra = {}, count, quiet) => h("a", { class: `rail-item ${extra.cls || ""}`, href: "#light", "aria-current": extra.current ? "page" : null },
    icon(ic), h("span", { class: "rail-label" }, label), count ? h("span", { class: `rail-count ${quiet ? "is-quiet" : ""}` }, String(count)) : null);
  const stageList = h("div", { class: "popover kit-static" }, h("p", { class: "popover-title" }, "Stage"),
    h("div", { class: "stage-list" }, ["Plan", "Draft", "Refine", "Finish", "Out"].map((s, i) => h("button", { type: "button", class: `stage-opt st-${s.toLowerCase()} ${i === 2 ? "is-hover" : ""}`,
      role: "menuitemradio", "aria-checked": String(i === 1), disabled: s === "Out" }, s, s === "Out" ? icon("lock") : null))),
    h("p", { class: "stage-why" }, "Out needs the publish approval, which you give in the approval sheet."));
  const cell = (col, cls, kids) => h("div", { class: `ws-cell ${cls}`, "data-col": col }, kids);
  const row = (name, stage, who, waiting, claude, due, version, cls = "", sel = false, extra = {}) => h("div", { class: `ws-row ${cls}`, "aria-selected": String(sel) },
    cell("check", "c-check", [h("button", { type: "button", class: "row-handle", "aria-label": "Row actions" }, icon("dots")), chk(String(sel))]),
    cell("name", "c-name", [extra.parts ? h("button", { type: "button", class: "ws-expand", "aria-expanded": "true", "aria-label": "Hide parts" }, icon("right")) : h("span", { class: "ws-indent" }),
      h("span", { class: "ws-name-text" }, name), extra.parts ? h("span", { class: "chip chip-count" }, String(extra.parts)) : null,
      extra.review ? h("span", { class: "flag-review" }, icon("flag"), "Needs review") : null,
      h("span", { class: "ws-row-acts" }, h("button", { type: "button", class: `bubble ${extra.updates ? "has-updates" : ""}`, "aria-label": "Updates" }, icon(extra.updates ? "bubble" : "bubble-plus"),
        extra.updates ? h("span", { class: "bubble-n" }, String(extra.updates)) : null))]),
    cell("stage", `c-solid st-${stage.toLowerCase()} ${extra.changed ? "is-changed" : ""}`, [stage, extra.changed ? avatar("Claude", { cls: "changed-by" }) : null]),
    cell("who", "c-who", avatar(who)),
    waiting === "you" ? cell("waiting", "c-solid st-waiting", "Waiting on you") : waiting === "claude" ? cell("waiting", "c-claude-waiting", [avatar("Claude"), "Claude"]) : cell("waiting", "", null),
    claude ? cell("claude", "c-claude", [h("span", { class: "claude-dot" }), h("span", {}, claude)]) : cell("claude", "", null),
    cell("due", `c-due ${extra.late ? "is-late" : ""}`, due ? [extra.late ? icon("info") : null, due] : null),
    cell("version", "c-version", version ? h("span", {}, version) : null), h("div", { class: "ws-cell c-fill" }));
  const head = h("div", { class: "ws-row ws-hrow" }, h("div", { class: "ws-cell c-check" }, chk("mixed")), h("div", { class: "ws-cell c-name" }, "Artifact"),
    ["Stage", "Who", "Waiting on", "Claude is", "Due", "Latest version"].map((c, i) => h("div", { class: `ws-cell c-head ${i === 2 ? "is-drop" : ""}` }, c, h("span", { class: "col-resize" }))),
    h("div", { class: "ws-cell c-fill" }));
  const board = h("div", { class: "ws-board", style: { "--cols": "46px 300px 124px 64px 144px 188px 92px 176px minmax(0, 1fr)" } },
    h("section", { class: "grp", style: { "--group": "var(--group-2)" } },
      h("header", { class: "grp-head" }, h("button", { type: "button", class: "grp-toggle", "aria-expanded": "true" }, icon("chevron"), h("span", { class: "grp-title" }, "The Long Table, episode 12")),
        h("span", { class: "grp-count" }, "3 artifacts \u00b7 1 part")),
      h("div", { class: "grp-table" }, head, h("div", { class: "ws-rows" },
        row("The Long Table, episode 12", "Refine", "Maren", "you", "Tightening the opening", "Nov 10", "Rough cut s2, version 3", "", false, { parts: 1, updates: 2, changed: true }),
        row("Short: the van story", "Draft", "Claude", "", "", "", "Short van, version 2", "is-part is-hover", false, { review: true }),
        row("Newsletter, November", "Plan", "Imani", "claude", "Drafting the intro", "Oct 30", "", "", true, { late: true }),
        row("A name long enough to run past the edge of its column and keep going", "Finish", null, "", "", "", "")),
        h("div", { class: "ws-row ws-addrow" }, h("div", { class: "ws-cell c-check" }), h("div", { class: "ws-cell c-addname" }, h("input", { class: "ws-add-input", placeholder: "+ Add artifact", "aria-label": "Add artifact" })),
          h("div", { class: "ws-cell c-addrest" })))),
    h("section", { class: "grp is-collapsed", style: { "--group": "var(--group-3)" } },
      h("header", { class: "grp-head" }, h("button", { type: "button", class: "grp-toggle", "aria-expanded": "false" }, icon("chevron"), h("span", { class: "grp-title" }, "Collapsed group")),
        h("span", { class: "grp-count" }, "5 artifacts")),
      h("div", { class: "grp-collapsed" }, h("span", {}, "5 artifacts"), h("div", { class: "seg-bar" }, [["plan", 2], ["draft", 1], ["out", 2]].map(([s, n]) =>
        h("span", { style: { "--n": String(n), "--seg": `var(--stage-${s})` } }))), h("span", {}, "0 waiting on you"))));
  const batch = h("div", { class: "batch kit-static" }, h("div", { class: "batch-count" }, "3"),
    h("div", { class: "batch-what" }, h("span", { class: "batch-title" }, "Artifacts selected"), h("span", { class: "batch-dots" }, ["plan", "draft", "refine"].map((s) => h("span", { class: "dot", style: { color: `var(--stage-${s})` } })))),
    h("div", { class: "batch-actions" }, [["Stage", "check", ""], ["Who", "person", "is-hover"], ["Due date", "calendar", ""], ["Archive", "archive", ""]].map(([l, ic, c]) =>
      h("button", { type: "button", class: `batch-act ${c}` }, icon(ic), h("span", {}, l))), h("button", { type: "button", class: "batch-act", disabled: true }, icon("open"), h("span", {}, "Disabled"))),
    button("Clear selection", { variant: "tertiary", iconName: "x", iconOnly: true, class: "batch-close" }));
  const projCard = (cls) => h("a", { class: `card proj-card ${cls}`, href: "#light", style: { "--group": "var(--group-1)", width: "240px" } },
    h("div", { class: "proj-prev" }, h("span", { class: "proj-prev-title" }), ["refine", "draft", "plan"].map((s) => h("div", { class: "proj-prev-row" }, h("span"), h("span"),
      h("span", { class: "seg", style: { "--seg": `var(--stage-${s})` } }), h("span", { class: "seg", style: { "--seg": "var(--surface)" } })))),
    h("div", { class: "proj-meta" }, h("strong", {}, "Grounded conversations"), h("span", { class: "secondary-text" }, "5 artifacts \u00b7 1 waiting on you")));
  const kbCard = (cls) => h("article", { class: `card kb-card ${cls}`, style: { "--group": "var(--group-2)", width: "248px" } }, h("p", { class: "kb-card-title" }, "Short: the van story"),
    h("p", { class: "kb-card-proj" }, h("span", { class: "dot" }), h("span", {}, "The Long Table, episode 12")),
    h("div", { class: "kb-card-foot" }, avatar("Claude"), h("span", { class: "kb-due" }, "Nov 6"), h("span", { class: "pill pill-waiting" }, "Waiting on you"),
      h("button", { type: "button", class: "pill st-draft kb-stage" }, "Draft")));
  const panel = h("aside", { class: "side-panel ws-panel kit-static", style: { "--group": "var(--group-2)" } },
    h("div", { class: "ws-panel-head" }, h("div", { class: "ws-panel-top" }, h("p", { class: "ws-panel-crumb" }, h("span", { class: "dot" }), h("span", {}, "The Long Table, episode 12")),
      h("div", { class: "ws-panel-tools" }, button("Open in the Room", { variant: "primary", size: "sm", iconName: "film" }), button("Close", { variant: "tertiary", size: "sm", iconName: "x", iconOnly: true }))),
      h("h2", {}, "The Long Table, episode 12"),
      h("div", { class: "ws-panel-meta" }, h("button", { type: "button", class: "pill st-refine" }, "Refine"), h("button", { type: "button", class: "btn btn-tertiary btn-sm" }, avatar("Maren"), h("span", {}, "Maren")),
        h("span", { class: "chip chip-claude" }, "Claude is tightening the opening"))),
    h("div", { class: "tabs ws-panel-tabs", role: "tablist" }, ["Updates", "Versions", "Decisions", "Activity"].map((t, i) => h("button", { type: "button", class: "tab", role: "tab", "aria-selected": String(i === 0) }, t))),
    h("div", { class: "ws-panel-body" }, h("div", { class: "composer" }, h("textarea", { class: "input", rows: "2", placeholder: "Write an update", "aria-label": "Write an update" }),
      h("div", { class: "composer-foot" }, h("span", { class: "secondary-text" }, "\u2318 Enter posts it"), button("Post update", { variant: "primary", size: "sm" })))));
  $("#ws-kit").append(
    sub("Checkbox", h("div", { class: "kit-states" }, st("Off", chk("false")), st("Hover", chk("false", "is-hover")), st("Focus", chk("false", "is-focus")), st("On", chk("true")),
      st("Some", chk("mixed")), st("Disabled", chk("false", "", true)))),
    sub("Toolbar", h("div", { class: "kit-states" }, st("Default", tool("Filter", "filter")), st("Hover", tool("Filter", "filter", "is-hover")), st("On", tool("Filter", "filter", "is-on", 2)),
      st("Disabled", tool("Filter", "filter", "", 0, true)), st("Search, empty", h("label", { class: "search-field" }, icon("search"), h("input", { class: "input", placeholder: "Search", "aria-label": "Search" }))),
      st("Search, typed", h("label", { class: "search-field" }, icon("search"), h("input", { class: "input", value: "van", "aria-label": "Search" }))))),
    sub("Counts, flags and updates", h("div", { class: "kit-states" }, st("Parts", h("span", { class: "chip chip-count" }, "3")), st("Claude", h("span", { class: "chip chip-claude" }, "Claude is cutting")),
      st("Needs review", h("span", { class: "flag-review" }, icon("flag"), "Needs review")), st("No updates", h("button", { type: "button", class: "bubble", "aria-label": "Write an update" }, icon("bubble-plus"))),
      st("Hover", h("button", { type: "button", class: "bubble is-hover", "aria-label": "Write an update" }, icon("bubble-plus"))),
      st("Updates", h("button", { type: "button", class: "bubble has-updates", "aria-label": "2 updates" }, icon("bubble"), h("span", { class: "bubble-n" }, "2"))),
      st("Stage summary", h("div", { class: "seg-bar", style: { width: "160px" } }, [["plan", 2], ["draft", 1], ["refine", 3], ["out", 1]].map(([s, n]) => h("span", { style: { "--n": String(n), "--seg": `var(--stage-${s})` } })))))),
    sub("Rail", h("div", { class: "kit-states kit-top" }, h("nav", { class: "rail-kit", "aria-label": "Rail example" }, railItem("Home", "home"), railItem("Waiting on you", "inbox", { cls: "is-hover" }, 2),
      railItem("All artifacts", "grid", { current: true }, 13, true), railItem("A project with a long name that runs out of room", "folder", {}, 4, true)))),
    sub("Stage choices", h("div", { class: "kit-states kit-top" }, stageList)),
    sub("Board group: header, a row, its part (hovered, needs review), a selected row, long text, add row; then a collapsed group",
      h("div", { class: "ws-view-kit" }, board)),
    sub("Batch bar", batch),
    sub("Cards", h("div", { class: "kit-states kit-top" }, st("Project", projCard("")), st("Hover", projCard("is-hover")), st("Kanban", kbCard("")), st("Dragging", kbCard("is-dragging")))),
    sub("Item panel", panel));
}
