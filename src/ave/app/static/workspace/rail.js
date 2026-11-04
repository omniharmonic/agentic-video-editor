// The left rail: Home, what is waiting on you, every artifact, favourites, and the projects on this computer (with a search), then
// the way into the Room. It collapses to icons and remembers that.

import { h, button, icon } from "../dom.js";
import { midTruncate, tileOf } from "./common.js";

export function createRail(ctx) {
  const { S } = ctx;
  const el = h("nav", { class: "rail", "aria-label": "Workspace" });
  const toggle = button("Collapse the sidebar", { variant: "tertiary", size: "sm", iconName: "sidebar", iconOnly: true, "aria-expanded": "true",
    onclick: () => { S.railCollapsed = !S.railCollapsed; ctx.savePrefs(); paint(); } });
  const q = h("input", { class: "input", type: "search", placeholder: "Search projects", "aria-label": "Search projects", autocomplete: "off" });
  q.addEventListener("input", () => paint());
  q.addEventListener("keydown", (e) => { if (e.key === "Escape" && q.value) { e.preventDefault(); e.stopPropagation(); q.value = ""; paint(); } });
  const nav = h("div", { class: "rail-nav" });
  const favs = h("div", { class: "rail-list", style: { flex: "none", overflow: "visible" } });
  const list = h("div", { class: "rail-list" });
  const foot = h("div", { class: "rail-foot" });
  el.append(
    h("div", { class: "rail-top" }, h("div", { class: "rail-ws" }, h("span", { class: "rail-swatch", "aria-hidden": "true" }, "W"), h("span", {}, "Workspace")), toggle),
    nav,
    h("div", {}, favs, h("div", { class: "rail-head" }, h("span", {}, "Projects")), h("div", { class: "rail-search" }, h("label", { class: "search-field" }, icon("search"), q))),
    list, foot);

  const here = () => location.hash || "#/workspace";
  // a long name is cut in the middle so the end that tells projects apart stays; the full name is always the tooltip
  function item(href, label, iconOrSwatch, count, { quiet = false, current, short } = {}) {
    const on = current ?? (here() === href || (href === "#/workspace" && here() === "#/workspace/"));
    return h("a", { class: "rail-item", href, "aria-current": on ? "page" : null, title: label, "aria-label": label },
      iconOrSwatch, h("span", { class: "rail-label", "data-full": short ? label : null }, short || label), count ? h("span", { class: `rail-count ${quiet ? "is-quiet" : ""}` }, String(count)) : null);
  }
  const swatch = (p, titles) => h("span", { class: "rail-swatch", style: { "--group": p.colour }, "aria-hidden": "true" }, tileOf(p.title, titles));

  function paint() {
    el.classList.toggle("is-collapsed", S.railCollapsed);
    toggle.setAttribute("aria-expanded", String(!S.railCollapsed));
    toggle.setAttribute("aria-label", S.railCollapsed ? "Expand the sidebar" : "Collapse the sidebar");
    toggle.dataset.tip = toggle.getAttribute("aria-label");
    const all = [...ctx.rows.values()].filter((r) => !r.archived);
    const waiting = all.filter((r) => r.waiting === "you").length;
    nav.replaceChildren(
      item("#/workspace/home", "Home", icon("home")),
      item("#/workspace/waiting", "Waiting on you", icon("inbox"), waiting),
      item("#/workspace/approvals", "Approvals", icon("lock"), ctx.approvalsCount?.() || 0),
      item("#/workspace", "All artifacts", icon("grid"), all.length, { quiet: true }));
    const ps = [...ctx.projects.values()];
    const fav = ps.filter((p) => S.favs.has(p.id));
    const titles = ps.map((p) => p.title);
    favs.replaceChildren(...(fav.length ? [h("div", { class: "rail-head" }, h("span", {}, "Favourites")), ...fav.map((p) =>
      item(`#/workspace/p/${encodeURIComponent(p.id)}`, p.title, icon("star-fill"), 0, { short: midTruncate(p.title) }))] : []));
    const needle = q.value.trim().toLowerCase();
    const shown = ps.filter((p) => !needle || p.title.toLowerCase().includes(needle));
    list.replaceChildren(...(shown.length ? shown.map((p) => item(`#/workspace/p/${encodeURIComponent(p.id)}`, p.title, swatch(p, titles), 0, { short: midTruncate(p.title) }))
      : S.loading && !ps.length ? [h("div", { class: "rail-skel", role: "status", "aria-label": "Loading projects" }, [70, 85, 60].map((w) => h("span", { class: "skeleton skeleton-text", style: { width: `${w}%` } })))]
      : [h("p", { class: "rail-empty" }, ps.length ? `No project matches “${q.value.trim()}”` : "No projects yet")]));
    requestAnimationFrame(fit);
    foot.replaceChildren(
      h("a", { class: "rail-item", href: "#/settings", "aria-label": "Settings", title: S.railCollapsed ? "Settings" : null }, icon("gear"), h("span", { class: "rail-label" }, "Settings")),
      h("a", { class: "rail-item", href: "#/room/", "aria-label": "The Room", title: S.railCollapsed ? "The Room" : null }, icon("film"), h("span", { class: "rail-label" }, "The Room")));
  }
  // cut each long name in the middle until it fits the rail, so the end that differs is never the part that is hidden
  function fit() {
    for (const lab of el.querySelectorAll(".rail-label[data-full]")) {
      const full = lab.dataset.full;
      let max = full.length;
      lab.textContent = full;
      while (lab.scrollWidth > lab.clientWidth + 1 && max > 8) lab.textContent = midTruncate(full, --max);
    }
  }
  return { el, paint, focusSearch: () => { if (S.railCollapsed) { S.railCollapsed = false; paint(); } q.focus(); } };
}
