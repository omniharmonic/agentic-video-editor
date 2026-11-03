// Brand, campaign and account filters shared by the calendar, review and ideas pages. One studio can hold two businesses; this keeps them
// apart. The choice is remembered (localStorage, view state only) so the same brand is still selected after a reload and on the next page.
//
//   brand    an account's optional `brand` (accounts/<id>.yaml). A post's brand is its own `brand`, else the brand of its accounts, else its campaign's.
//   campaign a post's `campaign` id (the campaigns page names them)
//   off      account ids switched off with their chip

import { h, icon, monogram, store } from "./dom.js";

const KEY = "filters";
const NONE = "No brand";

export function brandsOf(accounts) {
  const names = [...new Set(accounts.map((a) => a.brand || null))];
  if (!names.some(Boolean)) return [];                       // no account has a brand: the studio is one business, show no brand chips
  return names.sort((a, b) => (a === null) - (b === null) || String(a).localeCompare(String(b))).map((n) => ({ name: n, label: n || NONE, ids: accounts.filter((a) => (a.brand || null) === n).map((a) => a.id) }));
}

export function readFilters(accounts, legacyOff = []) {
  const f = store(KEY) || {};
  const brands = brandsOf(accounts);
  const brand = brands.some((b) => b.name === f.brand) && f.brand !== undefined ? f.brand : null;
  const ids = new Set(accounts.map((a) => a.id));
  return { brand, campaign: typeof f.campaign === "string" ? f.campaign : null, off: [...new Set([...(f.off || []), ...legacyOff])].filter((id) => ids.has(id)) };
}
export const writeFilters = (f) => store(KEY, f);

/** Accounts that pass the brand filter, and of those the ones not switched off. */
export function brandAccounts(accounts, f) { return f.brand === null ? accounts : accounts.filter((a) => (a.brand || null) === f.brand); }
export function activeIds(accounts, f) {
  const ids = brandAccounts(accounts, f).map((a) => a.id).filter((id) => !f.off.includes(id));
  return ids.length ? ids : brandAccounts(accounts, f).map((a) => a.id);
}
export const anyFilter = (f) => f.brand !== null || !!f.campaign || f.off.length > 0;

/** The brand of a post (a post view) given the accounts and campaigns the page knows about. Null when nothing says. */
export function postBrand(post, accounts, campaigns = []) {
  if (post.brand) return post.brand;
  const byId = new Map(accounts.map((a) => [a.id, a]));
  const fromVariants = [...new Set((post.variants || []).map((v) => (byId.get(v.account) || {}).brand).filter(Boolean))];
  if (fromVariants.length) return fromVariants[0];
  const c = campaigns.find((x) => x.id === post.campaign);
  const fromCampaign = c ? [...new Set((c.accounts || []).map((id) => (byId.get(id) || {}).brand).filter(Boolean))] : [];
  return fromCampaign.length === 1 ? fromCampaign[0] : null;
}

/** Drawn identically on every page that filters. opts: {accounts, campaigns, filters, onChange(next), showAccounts, phone, foldOpen, onFold} */
export function filterBar({ accounts, campaigns = [], filters, onChange, showAccounts = true, phone = false, label = "Filters", foldOpen = null, onFold = null }) {
  const brands = brandsOf(accounts);
  const f = filters;
  const change = (patch) => onChange({ ...f, ...patch });
  const bar = h("div", { class: "fbar", role: "group", "aria-label": label });
  if (brands.length) {
    bar.append(h("div", { class: "fbar-row fbar-brands", role: "group", "aria-label": "Brand" },
      h("button", { type: "button", class: "brand-chip", "aria-pressed": String(f.brand === null), onclick: () => change({ brand: null, off: [] }) }, "All brands"),
      brands.map((b, i) => h("button", { type: "button", class: `brand-chip hue-${i % 4}`, "aria-pressed": String(f.brand === b.name), dataset: { brand: b.label }, onclick: () => change({ brand: b.name, off: [] }) },
        h("span", { class: "brand-dot", "aria-hidden": "true" }), b.label, h("span", { class: "brand-n" }, `${b.ids.length} ${b.ids.length === 1 ? "account" : "accounts"}`)))));
  }
  const row = h("div", { class: "fbar-row fbar-more" });
  if (campaigns.length) {
    const sel = h("select", { id: "f-campaign", name: "campaign", "aria-label": "Campaign", onchange: (e) => change({ campaign: e.target.value || null }) },
      h("option", { value: "" }, "All campaigns"), campaigns.map((c) => h("option", { value: c.id, selected: f.campaign === c.id }, c.name || c.id)));
    sel.value = f.campaign || "";
    row.append(h("label", { class: "fbar-sel", for: "f-campaign" }, h("span", { class: "bar-label" }, "Campaign"), sel));
  }
  if (showAccounts && accounts.length > 1) {
    const list = brandAccounts(accounts, f);
    const on = activeIds(accounts, f);
    const chips = h("div", { class: "acct-bar fbar-accts", role: "group", "aria-label": "Accounts shown" },
      list.map((a) => h("button", { type: "button", class: `acct-chip hue-${accounts.findIndex((x) => x.id === a.id) % 4}`, "aria-pressed": String(on.includes(a.id)), dataset: { account: a.id },
        onclick: () => { const off = on.includes(a.id) ? [...f.off, a.id] : f.off.filter((x) => x !== a.id); change({ off: off.length >= list.length ? [] : off }); } },
      h("span", { class: "mono" }, monogram(a.platform)), a.label || a.id)));
    const fold = h("details", { class: "fbar-fold", open: foldOpen === null ? (!phone && !brands.length) : foldOpen, ontoggle: (e) => { if (onFold) onFold(e.target.open); } },
      h("summary", {}, h("span", { class: "bar-label" }, "Accounts"), h("span", { class: "fbar-count" }, `${on.length} of ${list.length}`)), chips);
    row.append(fold);
  }
  if (anyFilter(f)) row.append(h("button", { type: "button", class: "quiet fbar-clear", onclick: () => onChange({ brand: null, campaign: null, off: [] }) }, icon("x"), "Clear filters"));
  if (row.childNodes.length) bar.append(row);
  return bar;
}
