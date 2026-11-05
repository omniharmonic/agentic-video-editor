// A local visual feed plan. Moving tiles never changes schedules or approvals.
import { announce, clear, h, icon, store, thumbImg, humanWhen } from "/app/shared/dom.js";
import { KeyboardMover } from "./dnd.js";
import { brandAccounts, brandsOf, filterBar, readFilters, writeFilters } from "/app/shared/filters.js";
import {mountEditor} from "./editor.js";
import {makeCtx} from "/app/shared/panels.js";
import {leaveSafely} from "./navigation-guard.js";
import {dialog,input,select as makeSelect,field} from "./production-ui.js";
import {openAssetPicker} from "./library.js";
import {editProfile} from "./authoring.js";
import { feedMedia, onSurface } from "./feed-media.js";

export const parseAspect = (s) => { const [a, b] = String(s).split(":").map(Number); return a > 0 && b > 0 ? a / b : 0.75; };
export function cropGeometry(w, hgt, ca) {
  const ia = w / hgt, wider = ia > ca;
  const frameW = wider ? ia / ca : 1, frameH = wider ? 1 : ca / ia;
  return { lostX: wider ? 1 - ca / ia : 0, lostY: wider ? 0 : 1 - ia / ca, frameW, frameH, fit: 1 / Math.max(frameW, frameH) };
}

export async function mount(root, ctx, route) {
  if (!document.querySelector('link[data-feed-planner]')) document.head.append(h("link", { rel: "stylesheet", href: "/static/planner.css", "data-feed-planner": "" }));
  const accountId = route.args[0] === "local" ? "" : (route.args[0] || "");
  const mover = new KeyboardMover();
  const touch = matchMedia("(pointer: coarse)").matches;
  let arranging = false, composer=null, composerRoot=null;
  let alive = true, serial = 0, data = null, selected = null, saving = false, fullFrame = !!store("gridFrame");
  let surface = route.query.get("surface") || "feed", filters = readFilters(ctx.accounts);
  if (!["feed", "reel", "story", "all"].includes(surface)) surface = "feed";
  const selectionKey = `feedSelection:${accountId}:${surface}`;
  selected = store(selectionKey);
  const host = h("section", { class: `feed-planner${touch ? " planner-touch" : ""}` });
  const body = h("div", { class: "planner-layout" });
  const status = h("p", { class: "planner-save", role: "status", "aria-live": "polite" }, "Loading your plan…");
  const error = h("p", { class: "planner-error", role: "alert", hidden: true });
  const footnote = h("p", { class: "planner-footnote" }, "Visual order is saved locally. Publishing dates and approvals stay unchanged. Profile crop is approximate.");
  const select = h("select", { "aria-label": "Feed account", onchange: () => ctx.navigate(select.value ? `#/grid/${encodeURIComponent(select.value)}` : "#/grid/local") },
    h("option", { value: "", selected: !accountId }, "All drafts · local plan"), brandAccounts(ctx.accounts, filters).map((a) => h("option", { value: a.id, selected: a.id === accountId }, a.label || a.id)));
  const surfaceSelect = h("select", { "aria-label": "Content surface", onchange: () => ctx.navigate(`#/grid/${encodeURIComponent(accountId || "local")}?surface=${surfaceSelect.value}`) },
    [["feed", "Posts"], ["reel", "Reels"], ["story", "Stories"], ["all", "All content"]].map(([value, label]) => h("option", { value, selected: surface === value }, label)));
  const crop = h("button", { class: "planner-crop", type: "button", "aria-pressed": String(fullFrame), onclick: () => { fullFrame = !fullFrame; store("gridFrame", fullFrame); crop.setAttribute("aria-pressed", String(fullFrame)); draw(); } }, icon("crop"), "Full images");
  const help = h("p", { class: "planner-help", id: "planner-help" }, touch ? "Swipe to browse. Tap Arrange to move images." : "Drag any image to move it. Select a post for details. Keyboard: M, arrows, Enter.");
  const arrange = h("button", { type: "button", hidden: !touch, "aria-pressed": "false", onclick: () => {
    arranging = !arranging; host.classList.toggle("is-arranging", arranging); arrange.setAttribute("aria-pressed", String(arranging));
    arrange.textContent = arranging ? "Done arranging" : "Arrange";
    help.textContent = arranging ? "Drag an image to move it. Tap Done arranging to scroll." : "Swipe to browse. Tap Arrange to move images.";
  } }, "Arrange");
  const add = h("button", { type: "button", class: "primary", onclick: async () => {
    add.disabled = true;
    try { let result = await ctx.api.post("posts", { idea: "Untitled draft" }); if(accountId)result=await ctx.api.post(`posts/${result.post.id}/variants`,{expected_rev:result.post.rev,account:accountId,post_type:"feed",delivery:"manual"});await load();await openComposer({post:result.post.id,variant:result.post.variants[0]?.id}); }
    catch (e) { showError(e.message); add.disabled = false; }
  } }, icon("plus"), "Add draft");
  host.append(...[h("header", { class: "planner-heading" }, h("div", {}, h("h1", {}, "Feed planner"), h("p", {}, "Arrange your images. Find the flow.")),
    h("div", { class: "planner-heading-actions" }, h("a", { class: "btn", "aria-label": "Phone preview", href: "/app/#/workspace" }, "Preview in the Workspace"), add)),
    brandsOf(ctx.accounts).length ? filterBar({ accounts: ctx.accounts, filters, showAccounts: false, label: "Brand", onChange: (next) => {
      filters = next; writeFilters(next); const first = brandAccounts(ctx.accounts, next)[0]; ctx.navigate(first ? `#/grid/${encodeURIComponent(first.id)}` : "#/grid/local");
    } }) : null,
    h("div", { class: "planner-toolbar" }, select, h("button",{type:"button",onclick:()=>editProfile(ctx,accountId||null,p=>{if(p)ctx.navigate(`#/grid/${p.id}`);else ctx.navigate("#/grid/local");})},accountId?"Edit profile":"New profile"), surfaceSelect, crop, arrange, accountId?h("button",{type:"button",onclick:addContext},"Add published context"):null, status), error, help, body,
    footnote].filter(Boolean));
  clear(root).append(host);

  function showError(message) { error.textContent = message; error.hidden = false; announce(message, { tone: "error" }); }
  async function load({ silent = false } = {}) {
    if (saving || ctx.dnd.active || mover.active || composer) return;
    const n = ++serial;
    try {
      const result = await ctx.api.get("feed-plan", { account: accountId });
      if (!alive || n !== serial) return;
      const unchanged = data && data.rev === result.rev && data.snapshot === result.snapshot;
      data = result;
      add.disabled = !!data.read_only;
      arrange.disabled = !!data.read_only;
      if (data.read_only) { help.textContent = "This is a read-only sample. Open a studio folder to arrange your own feed."; footnote.textContent = "Sample order is not saved. Profile crop is approximate."; }
      selected = data.cells.some((c) => c.key === selected) ? selected : (data.cells[0] || {}).key;
      if (!unchanged) draw();
      status.textContent = data.read_only ? "Read-only sample · open a studio to save a plan" : data.rev ? "All changes saved" : "Ready to arrange";
      if (!silent) error.hidden = true;
    } catch (e) { if (alive) { status.textContent = "Plan unavailable"; showError(e.message); if (!data) body.replaceChildren(h("div", { class: "planner-empty" }, h("h2", {}, "The plan could not load"), h("button", { type: "button", onclick: () => load() }, "Try again"))); } }
  }
  const visible = () => data.cells.filter((c) => onSurface(c, surface));
  const mutable = () => visible().filter((c) => !c.locked);
  const columns = () => data.layout.columns || 3;
  const tileFor = (key) => [...body.querySelectorAll(".gcell")].find((el) => el.dataset.key === key);
  function focus(key, { reveal = false } = {}) {
    const tile = tileFor(key);
    if (!tile) return;
    tile.querySelector(".g-link").focus({ preventScroll: true });
    if (reveal) tile.scrollIntoView({ block: "nearest", inline: "nearest" });
  }
  function selectCell(c) { selected = c.key; store(selectionKey, selected); body.querySelectorAll(".gcell").forEach((el) => { const yes = el.dataset.key === selected; el.classList.toggle("is-selected", yes); el.querySelector(".g-link").tabIndex = yes ? 0 : -1; }); drawInspector(); }

  function draw() {
    if (!alive || !data) return;
    const active = document.activeElement && document.activeElement.closest(".gcell");
    const focusKey = active && active.dataset.key;
    ctx.dnd.reset();
    const cells = visible();
    selected = cells.some((c) => c.key === selected) ? selected : (cells[0] || {}).key;
    const list = h("ul", { class: `g-list planner-feed kind-${data.layout.kind}${fullFrame ? " planner-full" : ""}`, "aria-label": "Visual feed plan", style: { "--cols": columns(), "--cell-aspect": String(parseAspect(data.layout.cell_aspect)) } });
    for (const [i, c] of cells.entries()) {
      const li = h("li", { class: `gcell planner-tile${c.locked ? " is-locked" : ""}${c.key === selected ? " is-selected" : ""}`, dataset: { key: c.key, post: c.post, variant: c.variant || "", index: String(i) } });
      const label = `${c.title}. Position ${i + 1} of ${cells.length}. ${c.status.replace(/_/g, " ")}${data.read_only ? ". Read-only sample" : c.locked ? ". Locked" : ". Press M to move"}`;
      const link = h("a", { class: "g-link", href: `#/post/${encodeURIComponent(c.post)}${c.variant ? "?variant=" + encodeURIComponent(c.variant) : ""}`, draggable: "false", tabindex: c.key === selected ? "0" : "-1", "aria-label": label, "aria-describedby": "planner-help", "aria-keyshortcuts": c.locked ? null : "m", onclick: (e) => { e.preventDefault(); selectCell(c);openComposer(c); } },
        h("span", { class: "planner-image" }, feedMedia(c)),
        c.pinned?h("span",{class:"planner-badge"},"Pinned · local"):c.locked ? h("span", { class: "planner-badge" }, icon("lock"), c.external?"Reported":c.status === "published" ? "Published" : "Publishing") : h("span", { class: "planner-position", "aria-hidden": "true" }, String(i + 1)),
        c.media_count > 1 ? h("span", { class: "planner-count", title: `${c.media_count} slides` }, icon("grid"), String(c.media_count)) : null,
        h("span", { class: "planner-caption" }, c.title));
      li.append(link);
      if (!c.locked && !data.read_only) {
        ctx.dnd.source(li, { payload: () => ({ key: c.key, title: c.title }), enabled: () => !saving && (!touch || arranging), wholeSurface: true });
        ctx.dnd.target(li, { kind: "feed-position", label: `Move to position ${i + 1}`, accept: (p) => !saving && p.key !== c.key,
          drop: (p) => moveTo(p.key, i) });
      }
      list.append(li);
    }
    if (!cells.length) list.append(h("li", { class: "planner-empty" }, h("h2", {}, data.cells.length ? "No posts on this surface yet" : "Your feed starts here"), h("p", {}, "Add a draft, choose its images and post type, then arrange them here."), h("button", { type: "button", onclick: () => add.click() }, "Create a draft")));
    const canvas = h("div", { class: "planner-canvas" }, h("div", { class: "planner-feed-header" }, h("strong", {}, data.account.profile?.avatar_url?h("img",{src:data.account.profile.avatar_url,alt:"",class:"planner-profile-avatar"}):null,data.account.label || "All drafts"), h("span", {}, `${cells.length} of ${data.cells.length} posts`)), list);
    body.replaceChildren(canvas, h("aside", { class: "planner-inspector", "aria-label": "Selected post" }));
    drawInspector();
    if (focusKey) focus(focusKey);
  }

  function drawInspector() {
    const aside = body.querySelector(".planner-inspector");
    if (!aside) return;
    const c = data.cells.find((c) => c.key === selected);
    if (!c) { aside.replaceChildren(h("h2", {}, "A little space to experiment"), h("p", {}, "Images and captions live together. Arrange the feed here, then open a draft to finish the details.")); return; }
    if(c.external){aside.replaceChildren(h("h2",{},c.title),h("p",{},"Published elsewhere · reported by you"),h("a",{href:c.url,target:"_blank",rel:"noopener"},"Open reported post"),h("button",{type:"button",onclick:()=>contextAction("pin",c.key)},c.pinned?"Unpin preview":"Pin in preview"),h("button",{type:"button",onclick:()=>contextAction("remove-external",c.key)},"Remove preview context"));return;}
    const options = mutable();
    const index = options.findIndex((x) => x.key === c.key);
    const position = h("select", { "aria-label": "Move selected post to position", disabled: saving }, options.map((x, i) => h("option", { value: String(i), selected: i === index }, `Position ${i + 1}`)));
    aside.replaceChildren(h("p", { class: "planner-detail-label" }, c.locked ? "Published history" : "Selected post"), h("h2", {}, c.title),
      h("p", { class: "planner-detail-status" }, icon(c.status), c.status.replace(/_/g, " "), c.unassigned ? " · No account assigned" : ""),
      h("p", { class: "planner-detail-time" }, c.at ? `Publishing date: ${humanWhen(c.at)}` : "No publishing date"),
      c.thumb || c.media_url ? h("div", { class: "planner-detail-image" }, feedMedia(c, c.title)) : h("p", {}, "Open this draft to add an image or video."),
      h("a", { class: "btn primary", href: `#/post/${encodeURIComponent(c.post)}${c.variant ? "?variant=" + encodeURIComponent(c.variant) : ""}`,onclick:e=>{e.preventDefault();openComposer(c);} }, "Edit post"),
      accountId?h("button",{type:"button",onclick:()=>contextAction("pin",c.key)},c.pinned?"Unpin from preview":"Pin in this preview"):"",
      data.read_only ? h("p", { class: "planner-help" }, "This sample is read-only. Open a studio folder to arrange and save your own feed.") : !c.locked ? h("div", { class: "planner-position-control" }, h("label", {}, "Position in the plan", position), h("button", { type: "button", disabled: saving, onclick: () => moveTo(c.key, Number(position.value)) }, "Move")) : h("p", { class: "planner-help" }, "Published posts keep their place."));
  }

  async function contextAction(op,key=null,entry=null){try{data=await ctx.api.post('feed-plan/context',{account:accountId,expected_rev:data.rev,snapshot:data.snapshot,op,key,entry});draw();}catch(e){showError(e.message);throw e;}}
  function addContext(){const title=input('Published post title'),url=input('Published post URL'),at=input('Reported publication time','','datetime-local'),kind=makeSelect('Published format',[['feed','Post'],['reel','Reel'],['story','Story']],'feed');let thumbnail=null;dialog('Add published context',[field('Title',title),field('URL',url),field('Published at',at),field('Format',kind),h('button',{type:'button',onclick:()=>openAssetPicker(makeCtx({...ctx}),{kind:'image',allowDraft:true,onPick:a=>{thumbnail=a.id;}})},'Choose local thumbnail')],()=>contextAction('external',null,{title:title.value,url:url.value,posted_at:new Date(at.value).toISOString(),post_type:kind.value,thumbnail}),{submit:'Add to preview',note:'Human-reported context only. This does not create a publishing record or fetch remote media.'});}
  async function openComposer(c,remember=true){
    if(c.external)return;
    if(composer&&!await leaveSafely())return;
    if(remember){const query=new URLSearchParams(route.query);query.set('compose',c.post);if(c.variant)query.set('variant',c.variant);else query.delete('variant');ctx.remember(`#/grid/${encodeURIComponent(accountId||'local')}?${query}`);}
    composer?.destroy();composerRoot?.remove();
    composerRoot=h('aside',{class:'planner-composer','aria-label':'Post composer'});host.append(composerRoot);host.classList.add('has-composer');
    const position=window.scrollY;window.scrollTo({top:0,behavior:'instant'});document.body.classList.add('composer-open');
    composer=mountEditor(composerRoot,makeCtx({...ctx}),{postId:c.post,variantId:c.variant||null,contextual:true,onClose:()=>{composer.destroy();composer=null;composerRoot.remove();composerRoot=null;host.classList.remove('has-composer');document.body.classList.remove('composer-open');const query=new URLSearchParams(route.query);query.delete('compose');query.delete('variant');ctx.remember(`#/grid/${encodeURIComponent(accountId||'local')}${query.size?'?'+query:''}`,true);load({silent:true});window.scrollTo(0,position);focus(selected);}});
    composerRoot.addEventListener('keydown',e=>{if(e.key==='Escape'){e.preventDefault();composerRoot.querySelector('.ed-tools button')?.click();}if(e.key==='Tab'){const nodes=[...composerRoot.querySelectorAll('button,a,input,textarea,select,[tabindex="0"]')].filter(n=>!n.disabled&&n.getClientRects().length);const first=nodes[0],last=nodes.at(-1);if(e.shiftKey&&document.activeElement===first){e.preventDefault();last?.focus();}else if(!e.shiftKey&&document.activeElement===last){e.preventDefault();first?.focus();}}});
    composerRoot.tabIndex=-1;composerRoot.focus({preventScroll:true});
  }
  async function moveTo(key, index) {
    if (saving || data.read_only) return;
    const ordered = mutable();
    const from = ordered.findIndex((c) => c.key === key);
    if (from < 0 || index === from) return;
    index = Math.max(0, Math.min(ordered.length - 1, index));
    ordered.splice(from, 1);
    const before = (ordered[index] || {}).key || null;
    const c = data.cells.find((c) => c.key === key);
    saving = true; ++serial; error.hidden = true; status.textContent = "Saving order…";
    host.setAttribute("aria-busy", "true");
    try {
      const result = await ctx.api.post("feed-plan", { account: accountId, expected_rev: data.rev, snapshot: data.snapshot, key, before });
      if (!alive) return;
      data = result; selected = key; store(selectionKey, selected); saving = false; draw(); focus(key);
      status.textContent = "All changes saved";
      announce(`Moved ${c.title} to position ${index + 1}. Visual order saved.`);
    } catch (e) {
      if (!alive) return;
      saving = false; showError(e.message); status.textContent = "Move not saved";
      // Keep the server's current ordering visible after stale-tab conflicts.
      if (e.status === 409) { await load({ silent: true }); status.textContent = "Latest order loaded · move not saved"; }
    } finally { saving = false; host.removeAttribute("aria-busy"); }
  }

  function pickUp(c) {
    if (data.read_only) return announce("This sample is read-only. Open a studio folder to save a plan.");
    if (saving || c.locked) return announce("Published and publishing posts keep their place.");
    const cells = mutable();
    mover.begin(c, { describe: (p) => p.title, targets: () => cells.map((c, i) => ({ el: tileFor(c.key), i, label: `Position ${i + 1}: ${c.title}` })),
      start: (p) => cells.findIndex((c) => c.key === p.key),
      nav(e, i, targets) { const d = { ArrowLeft: -1, ArrowRight: 1, ArrowUp: -columns(), ArrowDown: columns() }[e.key]; return d ? Math.max(0, Math.min(targets.length - 1, i + d)) : null; },
      drop: (p, t) => moveTo(p.key, t.i) });
  }
  const onKey = (e) => {
    if (mover.active || e.ctrlKey || e.metaKey || e.altKey) return;
    const tile = e.target.closest(".gcell");
    if (!tile || !data) return;
    const i = Number(tile.dataset.index), cells = visible(), c = cells[i];
    if (e.key.toLowerCase() === "m" || e.key === " ") { e.preventDefault(); pickUp(c); return; }
    const d = { ArrowLeft: -1, ArrowRight: 1, ArrowUp: -columns(), ArrowDown: columns() }[e.key];
    if (d) { e.preventDefault(); const next = cells[Math.max(0, Math.min(cells.length - 1, i + d))]; selectCell(next); focus(next.key, { reveal: true }); }
  };
  root.addEventListener("keydown", onKey);
  await load();
  if(route.query.get('compose')){const c=data?.cells.find(c=>c.post===route.query.get('compose')&&(!route.query.get('variant')||c.variant===route.query.get('variant')));if(c){selectCell(c);await openComposer(c,false);}}
  // Feed-order writes intentionally have no publishing ledger event. Poll only
  // this small projection so another open planner converges after a save.
  const timer = setInterval(() => { if (document.visibilityState === "visible") load({ silent: true }); }, 10000);
  return { refresh: () => load({ silent: true }), unmount() { document.body.classList.remove("composer-open");composer?.destroy();composerRoot?.remove();alive = false; ++serial; clearInterval(timer); root.removeEventListener("keydown", onKey); mover.cancel(true); ctx.dnd.cancel(); ctx.dnd.reset(); } };
}
