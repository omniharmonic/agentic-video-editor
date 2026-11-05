// The thin strip under the Room's title: what is coming in, where the artifact is in its five stages, and what is going out.
// People read labels ("Host camera", "Draft of version 2"); the file names stay in title attributes.

import { h, icon } from "../dom.js";

const STAGES = ["Plan", "Draft", "Refine", "Finish", "Out"];
const SHOW = 2;          // more than this collapse into "and N more", whose title lists them

function items(list, empty) {
  if (!list.length) return h("span", { class: "strip-none" }, empty);
  const shown = list.slice(0, SHOW), rest = list.slice(SHOW);
  return h("ul", { class: "strip-items" },
    shown.map((x) => h("li", { class: "strip-item", title: x.label }, x.label)),
    rest.length ? h("li", { class: "strip-item strip-more", title: rest.map((x) => x.label).join(", ") }, `and ${rest.length} more`) : null);
}

export function renderStrip(cut, { trim } = {}) {
  if (!cut) return h("div", { class: "strip" });
  const at = STAGES.indexOf(cut.stage);
  const stages = [];
  STAGES.forEach((s, i) => {
    if (i) stages.push(h("li", { class: `strip-link ${i <= at ? "is-done" : ""}`, "aria-hidden": "true" }));
    stages.push(h("li", {
      class: `strip-stage ${i < at ? "is-done" : i === at ? `is-current pill pill-${s.toLowerCase()}` : "is-next"}`,
      "aria-current": i === at ? "step" : null }, i < at ? icon("check") : null, s));
  });
  return h("div", { class: "strip" },
    h("div", { class: "strip-side strip-in" }, h("span", { class: "strip-label" }, icon("arrive"), "Coming in"), items(cut.sources, "Nothing yet"),
      cut.trim && trim ? trim(cut.trim) : null),
    h("ol", { class: "strip-stages", "aria-label": `Stage: ${cut.stage}` }, stages),
    h("div", { class: "strip-side strip-out" }, h("span", { class: "strip-label" }, icon("send"), "Going out"), items(cut.outputs, "Nothing yet")));
}
