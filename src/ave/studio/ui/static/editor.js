import {openMedia} from './media-workshop.js';
// Post editor (spec 7.2 #/post/<id>, 7.5 inline edit, swap asset, send back).
//
//   mountEditor(root, ctx, {postId, variantId?}) -> {destroy, flush, state}
//
// Inline edit: plain <textarea> per field, debounce 800 ms, POST posts/<id>/edit {expected_rev, patch:{dotted.path: value}}.
// Compare-and-swap: a 409 carries `latest`; fields where only the agent changed are rebased silently, fields where both changed open a
// three-way panel (base, theirs = the agent's, mine) and the human picks. The server writes the human_edit feedback line on every save.
import {
  ApiError, announce, clear, debounce, effectiveCaption, effectiveFirstComment, effectiveHashtags, effectiveMedia, empty, errorBox, field,
  findVariant, fmtWhen, getPath, groupSha, h, hashQuery, localUrl, postTitle, say, shortSha, statusChip, PLATFORM_NAMES, ago,
} from "/app/shared/panels.js";
import { flagInfo } from "/app/shared/dom.js";
import { nextFreeSlot } from "./bulk.js";
import { openReschedule } from "./reschedule.js";
import { openAssetPicker } from "./library.js";
import { openSendBack } from "/app/shared/feedback.js";
import { buildPreview, loadPreviewConfig, captionStats, platformCfg } from "/app/shared/previews/index.js";
import { AUTOSAVE_MS, setPath, classify, pathInfo, displayOf, toStored, dirtyText, loadAssets as loadPostAssets, previewOf, importNative } from "/app/shared/compose.js";

import {createDnd} from "./dnd.js";
import {registerDraft,leaveSafely} from "./navigation-guard.js";
import {writingLibrary,editProfile} from "./authoring.js";
import {dialog,input,select,field as simpleField} from "./production-ui.js";
import { openManualExport } from "./manual.js";
import { deliveryReceipt } from "/app/shared/delivery-receipt.js";

export { AUTOSAVE_MS, setPath, classify, displayOf };
const POLL_MS = 5000;
const FLAG_ORDER = ["approval_stale", "approval_expired", "moved_after_approval", "license_gap", "uncertain"];

export function mountEditor(root, ctx, { postId, variantId = null, contextual = false, onClose = null }) {
  root.classList.add("st");
  const S = {
    full: true, post: null, rev: null, events: [], queue: {}, variantId, dirty: new Map(), saving: false, conflict: null, assets: {}, accounts: {}, server: {},
    cfg: null, showHidden: true, remote: null, savedAt: null, lastSelection: null, notice: null, alive: true,
  };
  const status = h("span.ed-save", { role: "status", "aria-live": "polite" }, "");
  const title = h("h1.ed-title", {tabindex:0,role:"button","aria-label":"Edit post title"});
  async function editTitle(){await save.flush();if(S.dirty.size||S.saving)return;const name=input("Post title",S.post.production?.title||postTitle(S.post,null));dialog("Name this post",[simpleField("Title",name)],async()=>{await ctx.api.post(`posts/${postId}/production`,{expected_rev:S.rev,fields:{title:name.value}});await load();});}
  title.addEventListener("click",editTitle);title.addEventListener("keydown",e=>{if(e.key==="Enter"||e.key===" "){e.preventDefault();editTitle();}});
  const tabs = h("div.st-tabs", { role: "tablist", "aria-label": "Accounts for this post" });
  const form = h("div.ed-form");
  const previewBox = h("div.ed-preview", { "aria-live": "off" });
  const side = h("div.ed-side");
  const banner = h("div.ed-banner", { role: "status" });
  const conflictBox = h("div.ed-conflict");
  const page = h("section.st-page.ed",
    h("header.st-pagehead.ed-head", h("div", title, h("p.st-hint.ed-sub")), h("div.ed-tools", status, h("button", { type: "button", dataset: { act: "export" }, onclick: async () => { await save.flush(); if (S.dirty.size || S.saving) { setStatus("Save the post before exporting.", "bad"); return; } if (S.post) openManualExport(ctx, S.post, variant()); } }, "Export originals"), h("button", { type: "button", dataset: { act: "sendback" }, onclick: () => sendBack() }, "Request changes"))),
    banner, conflictBox, tabs, h("div.ed-grid", h("div.ed-main", form), h("div.ed-aside", previewBox, side)));
  page.classList.add("composer"); if(contextual)page.classList.add("contextual");
  if(onClose)page.querySelector(".ed-tools").prepend(h("button",{type:"button",onclick:async()=>{if(await leaveSafely())onClose();}},"Close composer"));
  clear(root).append(page);

  const mediaDnd=createDnd();
  const save = debounce(() => doSave(), AUTOSAVE_MS);
  const sticky=h("footer.composer-savebar",status,h("button.primary",{type:"button",onclick:async()=>{await save.flush();if(!S.dirty.size&&!S.saving)setStatus("Saved","saved");}},"Save draft"));page.append(sticky);
  const exporter=page.querySelector('[data-act="export"]');sticky.append(exporter);
  const variant = () => (S.post ? findVariant(S.post, S.variantId) || S.post.variants[0] || null : null);

  // ---- loading -----------------------------------------------------------------------------------------------------------------------
  async function load(first = false) {
    try {
      const res = await ctx.api.get(`posts/${postId}`);
      if (!S.alive) return;
      adopt(res);
      if (first && !S.variantId && S.post.variants[0]) S.variantId = S.post.variants[0].id;
      if (!findVariant(S.post, S.variantId) && S.post.variants[0]) S.variantId = S.post.variants[0].id;
      if (!S.cfg) S.cfg = await loadPreviewConfig().catch(() => ({ platforms: {}, approximate_label: "Approximate" }));
      await Promise.all([loadAssets(), loadServerPreview()]);
      drawAll();
    } catch (ex) {
      if (ex.status === 404) {         // a stale link: say so plainly (the server's message names a path on this machine) and offer no actions on nothing
        clear(form).append(empty("This post was not found.", "It may have been dropped, or the link is old. Open it from the calendar."));
        title.textContent = "Post not found";
        page.querySelector(".ed-sub").textContent = "";
        const sb = page.querySelector('[data-act="sendback"]');
        if (sb) sb.hidden = true;
      } else clear(form).append(errorBox(ex, () => load(true)));
    }
  }
  function adopt(res) {
    S.post = res.post; S.rev = res.post.rev; S.events = res.events || S.events || []; S.queue = res.queue || S.queue || {};
    S.remote = null;
  }
  async function loadAssets() {
    await loadPostAssets(ctx.api, S.post, S.assets);
    if (!Object.keys(S.accounts).length) {
      try { const r = await ctx.api.get("accounts"); (r.accounts || []).forEach((a) => { S.accounts[a.id] = a; }); } catch { /* optional */ }
    }
  }
  async function loadServerPreview() {
    const v = variant();
    if (!v) return;
    try { S.server[v.id] = await ctx.api.get(`posts/${postId}/preview/${v.id}`); } catch { S.server[v.id] = null; }
  }

  // ---- saving ------------------------------------------------------------------------------------------------------------------------
  function setStatus(text, cls = "") { status.textContent = text; status.className = "ed-save " + cls; }
  function markDirty(path, text) {
    const stored = toStored(path, text);
    const prior = S.dirty.get(path);
    const base = prior ? prior.base : getPath(S.post, path) ?? null;
    if (JSON.stringify(stored) === JSON.stringify(base) && !(pathInfo(path).kind === "variant-copy" && base === null)) S.dirty.delete(path);
    else S.dirty.set(path, { value: stored, base });
    setStatus(S.dirty.size ? "Editing…" : status.textContent, S.dirty.size ? "pending" : "");
    if (S.dirty.size && !S.conflict) save();
    drawPreview();
  }

  function doSave() {
    if(S.saveTask)return S.saveTask;
    S.saveTask=performSave().finally(()=>{S.saveTask=null;});return S.saveTask;
  }
  async function performSave() {
    if (S.saving || S.conflict || !S.dirty.size) return;
    S.saving = true;
    const snapshot = new Map(S.dirty);
    const patch = Object.fromEntries([...snapshot].map(([p, e]) => [p, e.value]));
    setStatus("Saving…", "pending");
    try {
      const res = await ctx.api.post(`posts/${postId}/edit`, { expected_rev: S.rev, patch });
      S.post = res.post; S.rev = res.post.rev;
      for (const [p, e] of snapshot) {
        const cur = S.dirty.get(p);
        if (cur && JSON.stringify(cur.value) === JSON.stringify(e.value)) S.dirty.delete(p); else if (cur) cur.base = e.value;
      }
      S.savedAt = ctx.now ? ctx.now() : new Date();
      setStatus(`Saved ${S.savedAt.toLocaleTimeString([], { hour: "numeric", minute: "2-digit" })}`, "saved");
      refreshFields(); drawSide(); loadServerPreview().then(drawPreview);
      if(S.refreshMedia||[...snapshot.keys()].some(p=>/(^|\.)media(\.|$)/.test(p))){S.refreshMedia=false;await refreshMedia();}
      if (S.dirty.size) save();
    } catch (ex) {
      if (ex.status === 409 && ex.latest) onConflict(ex.latest, snapshot);
      else { setStatus(`Not saved: ${ex.message}`, "bad"); announce(ctx, `Could not save: ${ex.message}`, { assertive: true }); }
    } finally { S.saving = false; }
  }

  function onConflict(latest, snapshot) {
    if(JSON.stringify([latest.media,...latest.variants.map(v=>v.media)])!==JSON.stringify([S.post.media,...S.post.variants.map(v=>v.media)]))S.refreshMedia=true;
    const rows = [];
    for (const [path, e] of snapshot) {
      const cls = classify(e.base, getPath(latest, path), e.value);
      if (cls === "same") S.dirty.delete(path);
      else if (cls === "conflict") rows.push({ path, base: displayOf({ ...S.post, ...pickBase(path, e.base) }, path), theirs: displayOf(latest, path), mine: displayOf(withValue(latest, path, e.value), path), choice: null, merged: "" });
    }
    if (!rows.length) {                                    // only the agent touched other fields: rebase silently and save again
      S.post = latest; S.rev = latest.rev;
      for (const [p, e] of S.dirty) e.base = getPath(latest, p) ?? null;
      refreshFields(); drawSide();
      announce(ctx, "Another editor changed other parts of this post. Your edit was kept.");
      if (S.dirty.size) doSaveSoon(); else setStatus("Up to date", "saved");
      return;
    }
    S.conflict = { latest, rows };
    setStatus("Another editor changed this at the same time. Choose which version to keep.", "bad");
    announce(ctx, "Another editor edited the same text while you were typing. Choose which version to keep.", { assertive: true });
    drawConflict();
  }
  const doSaveSoon = () => {clearTimeout(S.rebaseTimer);S.rebaseTimer=setTimeout(doSave, 0);};
  function pickBase(path, baseRaw) {
    const c = JSON.parse(JSON.stringify(S.post));
    setPath(c, path, baseRaw);
    return c;
  }
  function withValue(post, path, value) { const c = JSON.parse(JSON.stringify(post)); setPath(c, path, value); return c; }

  function resolveConflict() {
    const { latest, rows } = S.conflict;
    S.post = latest; S.rev = latest.rev;
    for (const r of rows) {
      if (r.choice === "theirs") S.dirty.delete(r.path);
      else {
        const text = r.choice === "merge" ? r.merged : r.mine;
        S.dirty.set(r.path, { value: toStored(r.path, text), base: getPath(latest, r.path) ?? null });
      }
    }
    for (const [p, e] of S.dirty) if (!rows.some((r) => r.path === p)) e.base = getPath(latest, p) ?? null;
    S.conflict = null;
    clear(conflictBox);
    refreshFields({ force: rows.filter((r) => r.choice === "theirs").map((r) => r.path) });
    drawSide(); drawPreview();
    say(ctx, "Resolved. Saving your choice.");
    if (S.dirty.size) doSave(); else setStatus("Up to date", "saved");
  }

  function drawConflict() {
    clear(conflictBox);
    if (!S.conflict) return;
    const { rows } = S.conflict;
    const ready = () => rows.every((r) => r.choice && (r.choice !== "merge" || r.merged.trim() !== ""));
    const apply = h("button.primary", { type: "button", disabled: true, onclick: resolveConflict }, "Save my choice");
    const sync = () => { apply.disabled = !ready(); };
    const panel = h("section.ed-3way", { role: "alertdialog", "aria-labelledby": "c3-title", "aria-describedby": "c3-desc", tabindex: "-1" },
      h("h2#c3-title", "Another editor edited this too"),
      h("p#c3-desc", "You and another editor changed the same text since you opened it. Nothing is lost: pick a version for each field."),
      rows.map((r, i) => {
        const name = `c3-${i}`;
        const merge = h("textarea", { rows: 3, "aria-label": "Merged text", hidden: true, oninput: (e) => { r.merged = e.target.value; sync(); } });
        merge.value = r.mine;
        r.merged = r.mine;
        const pick = (choice, label) => h("label.st-radio", h("input", { type: "radio", name, value: choice, onchange: () => { r.choice = choice; merge.hidden = choice !== "merge"; sync(); } }), label);
        return h("fieldset.ed-3row", { dataset: { path: r.path } }, h("legend", fieldLabel(r.path)),
          h("div.ed-3cols",
            h("div.ed-3col.base", h("h3", "Before either edit"), h("p.ed-3text", r.base || h("em", "empty"))),
            h("div.ed-3col.theirs", h("h3", "Their version"), h("p.ed-3text", r.theirs || h("em", "empty"))),
            h("div.ed-3col.mine", h("h3", "Your version"), h("p.ed-3text", r.mine || h("em", "empty")))),
          h("div.ed-3pick", pick("mine", "Keep mine"), pick("theirs", "Take theirs"), pick("merge", "Merge by hand")), merge);
      }),
      h("div.ed-3foot", apply));
    conflictBox.append(panel);
    panel.focus();
  }

  async function addChannel(){
    await save.flush();if(S.dirty.size)return;
    const r=await ctx.api.get('accounts');const accounts=r.accounts||[];for(const a of accounts)S.accounts[a.id]=a;
    if(!accounts.length){await editProfile(ctx,null,()=>{S.accounts={};loadAssets().then(addChannel);});return;}
    const account=select('Channel profile',accounts.map(a=>[a.id,a.label||a.id]),accounts[0].id),type=select('Post format',[['feed','Post'],['carousel','Carousel'],['reel','Reel'],['story','Story'],['video','Video'],['text','Text']],'feed');
    dialog('Add channel',[simpleField('Profile',account),simpleField('Format',type)],async()=>{const res=await ctx.api.post(`posts/${postId}/variants`,{expected_rev:S.rev,account:account.value,post_type:type.value,delivery:'manual'});adopt(res);S.variantId=S.post.variants.at(-1).id;S.undo=null;await loadAssets();drawAll();},{submit:'Add channel',note:'New channels use manual delivery. You can choose automatic delivery after checking its requirements.'});
  }
  async function insertWriting(group){const v=variant(),key=group.kind==='hashtags'?'hashtags':'caption',path=v?`variants.${v.id}.copy.${key}`:key==='hashtags'?'copy.hashtags':'copy.base';let value=group.text;if(group.kind==='hook')value=group.text+'\n\n'+(S.dirty.has(path)?displayOfDirty(path):displayOf(S.post,path));markDirty(path,value);const provenance=S.dirty.get('copy_provenance');S.dirty.set('copy_provenance',{value:[...(provenance?.value||S.post.copy_provenance||[]),{group:group.id,revision:group.rev,field:path}],base:provenance?provenance.base:S.post.copy_provenance||null});await save.flush();drawForm();}
  function deliveryControls(v){const method=select('Delivery method',[['manual','Manual · original files'],['automatic','Automatic · platform checks']],v.delivery?.method||'automatic',async value=>{S.dirty.set(`variants.${v.id}.delivery`,{value:{method:value},base:v.delivery||null});await save.flush();drawSide();});const box=h('section.composer-delivery',h('h2','Delivery'),simpleField('Method',method));if(v.platform==='instagram'&&v.post_type==='reel'){const check=h('input',{type:'checkbox',checked:v.presentation?.feed_presence===true,onchange:async e=>{S.dirty.set(`variants.${v.id}.presentation`,{value:{feed_presence:e.target.checked},base:v.presentation||null});await save.flush();}});box.append(h('label',check,' Show Reel in the profile feed'),h('p.st-hint',v.presentation?.feed_presence===undefined?'Feed presence is unspecified; this Reel stays out of the planned profile feed. Choose an option to include explicit manual instructions.':'Included in the reviewed Instagram Reel request. Feed eligibility does not guarantee distribution.'),h('button',{type:'button',onclick:async()=>{S.dirty.set(`variants.${v.id}.presentation`,{value:{},base:v.presentation||null});await save.flush();drawForm();}},'Reset feed option'));}const capabilities=h('details',h('summary','Delivery capabilities and readiness'));
    capabilities.addEventListener('toggle',async()=>{if(!capabilities.open||capabilities.dataset.loaded)return;try{const c=await ctx.api.get(`posts/${postId}/delivery-check?variant=${encodeURIComponent(v.id)}`);capabilities.dataset.loaded='true';capabilities.append(h('p',c.provider_contract?.version||'Manual channel'),h('p',`Automatic formats: ${(c.implemented_shapes||[]).join(', ')||'none'}`),h('p',`Pending formats: ${(c.pending_shapes||[]).join(', ')||'none'}`),h('p',`Required access: ${(c.provider_contract?.required_scopes||[]).join('; ')||'none'}`),h('p',`Scheduling: ${c.provider_contract?.scheduling||'manual'}`),h('p',`Selected adapter: ${c.selected_adapter?.adapter||'unknown'} (${c.selected_adapter?.mode||'unknown'}). Cover: ${c.selected_adapter?.cover?'supported':'manual'}. First comment: ${c.selected_adapter?.first_comment?'supported':'manual'}. Feed option: ${c.selected_adapter?.feed_presence?'supported':'manual'}.`),h('p',`Destination permissions: ${c.destination_evidence?.source||'unknown'}. Live verification: not certified.`),h('p',`Credential lifetime: ${c.credential_evidence?.expires_at||'not established'}`),h('p',`Worker: ${c.worker?.reason||c.worker?.status||'not established'}`),h('ul',(c.manual_steps||[]).map(x=>h('li',x))),h('ul',(c.problems||[]).map(x=>h('li',x))));}catch(e){capabilities.append(h('p',e.message));}});box.append(capabilities);
    if(v.result?.receipt||v.delivery_operation?.recoverable)box.append(v.result?.receipt?deliveryReceipt(v):h('p.st-hint','An existing send has an unresolved queue record. Inspect its destination before reviewing the outcome.'),h('button',{type:'button',dataset:{deliveryRefresh:'true'},onclick:async()=>{try{if(window.aveNative){await window.aveNative.connectionAction('providerRefresh',{post:postId,variant:v.id,revision:S.rev,...(v.delivery_operation?{operation:v.delivery_operation.digest}:{})});const r=await ctx.api.get(`posts/${postId}`);adopt(r);drawAll();}else{const r=await ctx.api.post(`posts/${postId}/delivery-refresh`,{variant:v.id});adopt(r);drawAll();}}catch(e){setStatus(e.message,'bad');}}},'Refresh provider status'));
    if(window.aveNative&&(v.result||v.delivery_operation?.recoverable))box.append(h('button',{type:'button',dataset:{outcomeReview:'true'},onclick:async()=>{try{await window.aveNative.connectionAction('reviewProviderOutcome',{post:postId,variant:v.id,revision:S.rev,...(v.delivery_operation?{operation:v.delivery_operation.digest}:{})});adopt(await ctx.api.get(`posts/${postId}`));drawAll();}catch(e){setStatus(e.message,'bad');}}},'Review publication outcome'));
    box.append(h('button.primary' ,{type:'button',onclick:async()=>{await save.flush();if(S.dirty.size)return;try{const res=await ctx.api.post(`posts/${postId}/prepare-review`,{expected_rev:S.rev,variant:v.id});adopt(res);drawAll();const problems=res.preparation?.problems||[];setStatus(problems.length?problems.join('; '):'Prepared for human review',problems.length?'bad':'saved');}catch(e){setStatus(e.message,'bad');}}},'Prepare review'));return box;}
  // ---- drawing -----------------------------------------------------------------------------------------------------------------------
  function fieldLabel(path) {
    const info = pathInfo(path);
    if (info.kind === "variant-copy") return { caption: "Caption", first_comment: "First comment", hashtags: "Hashtags" }[info.key];
    if (info.kind === "base") return "Shared copy";
    if (info.kind === "link") return "Link";
    if (info.kind === "alt") return `Alt text, image ${info.index + 1}`;
    return path;
  }

  function drawAll() {
    const p = S.post;
    title.textContent = p.production?.title||postTitle(p, null);
    page.querySelector(".ed-sub").textContent = `Draft revision ${p.rev}`;
    drawTabs(); drawForm(); drawPreview(); drawSide();
    banner.querySelector('.ed-lock-notice')?.remove();
    const v=variant();if(v&&(["published","publishing"].includes(v.status)||v.flags?.includes("uncertain"))){for(const input of form.querySelectorAll("input,textarea,select,button"))if(!input.dataset.deliveryRefresh&&!input.dataset.outcomeReview)input.disabled=true;banner.append(h("div.ed-lock-notice",h("p","This published or uncertain channel is locked."),h("button",{type:"button",onclick:async()=>{const r=await ctx.api.post(`posts/${postId}/duplicate`,{expected_rev:S.rev});ctx.navigate(`#/post/${r.post.id}`);}},"Duplicate for revision")));}
  }

  function drawTabs() {
    clear(tabs);
    tabs.append(h("button",{type:"button",onclick:addChannel},"Add channel"));
    S.post.variants.forEach((v) => {
      const on = v.id === S.variantId;
      const acct = S.accounts[v.account];
      tabs.append(h("button.st-tab", {
        role: "tab", id: `tab-${v.id}`, "aria-selected": String(on), tabindex: on ? "0" : "-1", dataset: { variant: v.id },
        onclick: () => switchVariant(v.id),
        onkeydown: (e) => {
          const i = S.post.variants.findIndex((x) => x.id === S.variantId);
          const n = S.post.variants.length;
          const to = e.key === "ArrowRight" ? (i + 1) % n : e.key === "ArrowLeft" ? (i - 1 + n) % n : e.key === "Home" ? 0 : e.key === "End" ? n - 1 : null;
          if (to !== null) { e.preventDefault(); switchVariant(S.post.variants[to].id, true); }
        },
      }, h("span.tab-name", (acct && acct.label) || v.account), h("span.st-hint.tab-plat", ` ${PLATFORM_NAMES[v.platform] || v.platform} ${v.post_type}`), statusChip(v.status,v)));
    });
  }
  async function switchVariant(id, focus = false) {
    if(!await leaveSafely())return;
    S.variantId = id;S.slide=0;S.undo=null;
    if(!contextual)history.replaceState(null, "", `#/post/${postId}?variant=${id}`);
    drawTabs(); drawForm(); drawSide(); drawPreview();
    await loadServerPreview(); drawPreview();
    if (focus) tabs.querySelector(`[data-variant="${id}"]`)?.focus();
    tabs.querySelector(`[data-variant="${id}"]`)?.scrollIntoView({ block: "nearest", inline: "center" });
  }

  function textField({ path, label, rows = 3, hint, mono = false, counterOf = null, placeholder = "" }) {
    const ta = h("textarea.ed-input" + (mono ? ".mono" : ""), { rows, name: path, placeholder, spellcheck: "true", dataset: { path } });
    ta.value = S.dirty.has(path) ? displayOfDirty(path) : displayOf(S.post, path);
    ta.addEventListener("input", () => { markDirty(path, ta.value); });
    const rec = () => { S.lastSelection = { path, el: ta }; };
    ["focus", "select", "keyup", "mouseup"].forEach((ev) => ta.addEventListener(ev, rec));
    const node=field(label, ta, { hint, wide: true });
    if(/^variants.*\.copy\./.test(path))node.append(h("button.composer-reset",{type:"button",onclick:async()=>{S.dirty.set(path,{value:null,base:getPath(S.post,path)});await save.flush();if(!S.dirty.size)drawForm();}},"Reset to shared"));
    return node;
  }
  const displayOfDirty = (path) => dirtyText(S.dirty.get(path));

  async function refreshMedia(){
    const active=form.querySelector(':focus'),path=active?.dataset.path,start=active?.selectionStart,end=active?.selectionEnd;
    const opened=[...form.querySelectorAll('details[open]')].map(d=>d.className);
    await loadAssets();if(!S.alive)return;drawForm();
    for(const d of form.querySelectorAll('details'))if(opened.includes(d.className))d.open=true;
    if(path){const node=[...form.querySelectorAll('[data-path]')].find(n=>n.dataset.path===path);node?.focus({preventScroll:true});if(node&&typeof start==='number')node.setSelectionRange(start,end);}
  }

  function drawForm() {
    clear(form);
    const v = variant();
    if (!v) {form.append(mediaStrip(null),h("div.composer-copy-head",h("h2","Writing"),h("button",{type:"button",onclick:()=>writingLibrary(ctx,insertWriting)},"Saved writing")),textField({path:"copy.base",label:"Shared caption",rows:5}),h("button.primary",{type:"button",onclick:addChannel},"Add a channel"));return;}
    const own = v.copy && v.copy.caption !== null && v.copy.caption !== undefined;
    form.append(...[
      mediaStrip(v),
      h("div.composer-copy-head",h("h2","Writing"),h("button",{type:"button",onclick:()=>writingLibrary(ctx,insertWriting)},"Saved writing")),
      textField({ path: `variants.${v.id}.copy.caption`, label: `Caption for ${(S.accounts[v.account] && S.accounts[v.account].label) || v.account}`, rows: 6, hint: own ? null : "Using the shared copy. Typing here makes this account's own version." }),
      own ? h("p", h("button.ghost", { type: "button", dataset: { act: "inherit" }, onclick: () => { S.dirty.set(`variants.${v.id}.copy.caption`, { value: null, base: v.copy.caption }); save.flush(); } }, "Use the shared copy again")) : null,
      textField({ path: `variants.${v.id}.copy.first_comment`, label: "First comment", rows: 2 }),
      textField({ path: `variants.${v.id}.copy.hashtags`, label: "Hashtags", rows: 1, hint: "Separate with spaces. The # is added for you." }),
      h("details.ed-shared", h("summary", "Shared copy and link (all accounts)"), textField({ path: "copy.base", label: "Shared copy", rows: 4 }), textField({ path: "copy.link", label: "Link", rows: 1 })),
      deliveryControls(v),whenSection(v)].filter(Boolean));
  }
  function refreshFields({ force = [] } = {}) {
    for (const ta of form.querySelectorAll("textarea[data-path]")) {
      const p = ta.dataset.path;
      if (S.dirty.has(p) && !force.includes(p)) continue;
      if (document.activeElement === ta && !force.includes(p) && S.dirty.has(p)) continue;
      const val = displayOf(S.post, p);
      if (ta.value !== val) ta.value = val;
    }
  }

  async function mediaAction(op, args={}, focusId=null) {
    await save.flush(); if(S.dirty.size||S.saving)return setStatus('Save your writing before changing media.','bad');
    const opened=op==='crop'?[...form.querySelectorAll('details[open]')].map(d=>d.className):[];
    const v=variant(),before=structuredClone(effectiveMedia(S.post,v||{})),wasApproved=v&&(S.queue[v.id]?.status==='approved'||v.approval);
    try {const res=await ctx.api.post(`posts/${postId}/media`,{expected_rev:S.rev,variant:v?.id||null,op,...args});adopt(res);if(wasApproved&&S.queue[v.id]?.status!=='approved'){S.notice='Approval removed: the media changed. This channel needs a fresh review.';banner.textContent=S.notice;}S.undo=op==='undo'?null:{post:postId,variant:v?.id||null,operation:op,before:before.map((m,i)=>({...m,id:m.id||`legacy_${i}`})),after:effectiveMedia(S.post,variant()||{}).map((m,i)=>({...m,id:m.id||`legacy_${i}`})),inherited:!!v&&v.media===null};await loadAssets();await loadServerPreview();drawAll();for(const d of form.querySelectorAll('details'))if(opened.includes(d.className))d.open=true;if(focusId)form.querySelector(`[data-item="${focusId}"] button`)?.focus();setStatus('Saved','saved');}
    catch(e){setStatus(e.message,'bad');if(e.status===409){banner.textContent='The post changed in another window. Your media operation was not applied. Reload to compare.';}}
  }
  let focalSave=Promise.resolve();
  const pendingFocal=[];
  const storedCrop=item=>item?.crop?.v===1?structuredClone(item.crop):{v:1,x:item?.crop?.grid_focus?.[0]??.5,y:item?.crop?.grid_focus?.[1]??.5,aspect:'3:4',purpose:'preview'};
  function focalPreview(item,channel){
    const crop=storedCrop(item);
    for(const intent of pendingFocal)if(intent.post===postId&&intent.channel===channel&&intent.item===item?.id)crop[intent.axis]=intent.value;
    return crop;
  }
  function saveFocal(item,axis,value){
    const intent={post:postId,channel:S.variantId,item,axis,value};
    pendingFocal.push(intent);
    // Queue only the changed axis. Redraws show pending intent, while each write
    // merges into the latest saved crop and retains a genuine saved Undo snapshot.
    focalSave=focalSave.then(async()=>{
      try{
        if(intent.post!==postId||intent.channel!==S.variantId||!S.alive)return;
        await save.flush();
        if(intent.channel!==S.variantId||!S.alive)return;
        const current=effectiveMedia(S.post,variant()||{}).find((m,i)=>(m.id||`legacy_${i}`)===intent.item);
        if(!current)return setStatus('This media is no longer present. The focal change was not saved.','bad');
        const crop=storedCrop(current);crop[intent.axis]=intent.value;
        setStatus('Saving focal position…','pending');
        await mediaAction('crop',{item:intent.item,crop});
      }finally{pendingFocal.splice(pendingFocal.indexOf(intent),1);}
    });
    return focalSave;
  }
  function mediaStrip(v) {
    const list=effectiveMedia(S.post,v||{}).map((m,i)=>({...m,id:m.id||`legacy_${i}`})),content=list.filter(m=>m.role!=='cover'),cover=list.find(m=>m.role==='cover');
    S.slide=Math.min(S.slide||0,Math.max(0,content.length-1));const selected=content[S.slide],asset=S.assets[selected?.asset]||{};
    const strip=h('section.ed-media',{'aria-label':'Media editor'});
    const pick=(op,item=null)=>openAssetPicker(ctx,{title:op==='cover'?'Choose a separate cover':'Choose media',kind:op==='cover'?'image':'',allowDraft:true,onPick:a=>mediaAction(op,{asset:a.id,...(item?{item}: {})})});
    const mediaNode=(m,controls=false)=>{const a=S.assets[m?.asset]||{},url=localUrl(a.kind==='image'?(a.preview_url||a.url):a.url),crop=focalPreview(m,v?.id||null);if(a.kind==='document')return h('div.composer-document',h('strong','PDF document'),h('p',a.name||a.file?.split('/').pop()||'Original PDF'),url?h('a',{href:url,target:'_blank',rel:'noopener'},'Open original PDF'):null,h('p.st-hint','The original document is delivered as a file. Page rendering is not part of this preview.'));return url?h(a.kind==='video'?'video':'img',{src:url,alt:m?.alt||a.alt||'',...(a.kind==='video'?{controls,muted:!controls,playsinline:true,preload:'metadata'}:{}),style:{objectFit:S.full?'contain':'cover',objectPosition:`${crop.x*100}% ${crop.y*100}%`}}):h('div.composer-empty',h('p','Build your next post'),h('button.primary',{type:'button',onclick:()=>pick('add')},'Add images or video'));};
    let editing=null;const stage=h('div.composer-stage',{class:S.full?'full-frame':'preview-crop'},mediaNode(selected,true));
    if(content.length>1)stage.append(h('div.composer-paging',h('button',{type:'button','aria-label':'Previous slide',onclick:()=>{S.slide=(S.slide-1+content.length)%content.length;drawForm();}},'‹'),h('span',`${S.slide+1} / ${content.length}`),h('button',{type:'button','aria-label':'Next slide',onclick:()=>{S.slide=(S.slide+1)%content.length;drawForm();}},'›')));
    const pixelEditor=selected&&S.assets[selected.asset]?.kind==='image'?h('button',{type:'button',onclick:async()=>{try{await save.flush();if(S.dirty.size||S.saving)return;await openMedia(ctx,{asset:selected.asset,target:{post:postId,variant:v?.id||null,item:selected.id,expected_rev:S.post.rev},onAttached:async()=>{await load();}});}catch(e){ctx.toast(e.message,{error:true});}}},'Edit image'):null;
    if(pixelEditor)pixelEditor.setAttribute('aria-label','Edit image / applied crop');
    strip.append(stage,h('div.composer-media-tools',h('button',{type:'button',onclick:async()=>{if(!window.aveNative?.importLibrary){setStatus('Importing needs the AVE Studio app: a browser cannot hand it files.','bad');return;}try{await importNative(id=>mediaAction('add',{asset:id}));}catch(e){setStatus(e.message,'bad');}}},'Import media'),h('button',{type:'button',onclick:()=>pick('add')},'Add media'),pixelEditor||h('button',{type:'button',onclick:()=>{S.full=!S.full;drawForm();}},S.full?'Preview crop':'Full frame')));
    mediaDnd.reset();const rail=h('div.composer-slides',{'aria-label':'Content slides'});let dragging=null;
    content.forEach((m,i)=>{const tile=h('div.composer-slide',{dataset:{item:m.id},class:i===S.slide?'active':'',draggable:true},h('button',{type:'button','aria-label':`Select slide ${i+1}`,onclick:()=>{S.slide=i;drawForm();}},mediaNode(m),h('span',String(i+1))));
      tile.addEventListener('dragstart',e=>{dragging=m.id;e.dataTransfer.setData('text/plain',m.id);});tile.addEventListener('dragover',e=>e.preventDefault());tile.addEventListener('drop',e=>{e.preventDefault();const id=dragging||e.dataTransfer.getData('text/plain');if(content.some(x=>x.id===id))mediaAction('move',{item:id,before:m.id},id);});
      tile.addEventListener('keydown',e=>{if(e.altKey&&['ArrowLeft','ArrowRight'].includes(e.key)){e.preventDefault();const earlier=e.key==='ArrowLeft',before=earlier?content[i-1]?.id:content[i+2]?.id||null;if((earlier&&i>0)||(!earlier&&i<content.length-1))mediaAction('move',{item:m.id,before},m.id);}});
      tile.draggable=false;const grip=h('button.composer-grip',{type:'button','aria-label':`Drag slide ${i+1}`},'↔');tile.append(grip);mediaDnd.source(tile,{payload:()=>({item:m.id}),handleSel:'.composer-grip'});mediaDnd.target(tile,{kind:'slide',label:`Before slide ${i+1}`,accept:p=>p.item!==m.id,drop:p=>mediaAction('move',{item:p.item,before:m.id},p.item)});
      rail.append(tile);
    });strip.append(rail);
    if(selected){
      const index=list.findIndex(m=>m.id===selected.id),raw=v?.media!==null&&v?v.media:S.post.media,rawIndex=raw.findIndex(m=>m.slot===selected.slot),path=v?.media!==null&&v?`variants.${v.id}.media.${rawIndex}.alt`:`media.${rawIndex}.alt`;
      const ta=h('textarea.ed-input',{rows:2,name:path,dataset:{path},'aria-label':`Alt text for slide ${S.slide+1}`});ta.value=S.dirty.has(path)?displayOfDirty(path):selected.alt||'';
      ta.addEventListener('input',()=>{if(v&&v.media===null){const p=`variants.${v.id}.media`,value=structuredClone(S.dirty.get(p)?.value||list);value[index].alt=ta.value;S.dirty.set(p,{value,base:null});setStatus('Editing…','pending');save();}else markDirty(path,ta.value);});
      editing=h('details.composer-slide-edit',h('summary',`Slide ${S.slide+1} · alt text and order`));editing.append(...[S.undo?h('button',{type:'button',onclick:()=>mediaAction('undo',{change:S.undo})},'Undo media change'):null,v?.media!==null&&v?h('button',{type:'button',onclick:()=>mediaAction('reset')},'Use shared media'):null].filter(Boolean));editing.append(h('div.composer-slide-actions',h('strong',`Slide ${S.slide+1}`),h('button',{type:'button',disabled:S.slide===0,onclick:()=>mediaAction('move',{item:selected.id,before:content[S.slide-1]?.id},selected.id)},'Move earlier'),h('button',{type:'button',disabled:S.slide===content.length-1,onclick:()=>mediaAction('move',{item:selected.id,before:content[S.slide+2]?.id||null},selected.id)},'Move later'),h('button',{type:'button',onclick:()=>pick('replace',selected.id)},'Replace'),h('button',{type:'button',onclick:()=>mediaAction('remove',{item:selected.id})},'Remove')),field('Alt text',ta));strip.append(editing);
      const crop=focalPreview(selected,v?.id||null);
      const focal=h('details.composer-focal',h('summary','Preview focal position'));
      for(const axis of ['x','y']){const slider=h('input',{type:'range',min:0,max:1,step:.01,value:crop[axis]??.5,'aria-label':axis==='x'?'Horizontal focal position':'Vertical focal position'});slider.addEventListener('input',()=>{crop[axis]=Number(slider.value);stage.firstChild.style.objectPosition=`${(axis==='x'?Number(slider.value):crop.x)*100}% ${(axis==='y'?Number(slider.value):crop.y)*100}%`;});slider.addEventListener('change',()=>saveFocal(selected.id,axis,Number(slider.value)));focal.append(field(axis==='x'?'Horizontal':'Vertical',slider));}
      focal.append(h('p.st-hint','Preview only. Original and exported media stay unchanged.'));if(asset.kind!=='document')editing.append(focal);if(pixelEditor)editing.append(h('button',{type:'button',onclick:()=>{S.full=!S.full;drawForm();}},S.full?'Preview crop':'Full frame'));
    }
    if(!selected&&S.undo)strip.append(h('button',{type:'button',onclick:()=>mediaAction('undo',{change:S.undo})},'Undo media change'));
    if(!selected&&v?.media!==null&&v)strip.append(h('button',{type:'button',onclick:()=>mediaAction('reset')},'Use shared media'));
    const coverBox=h('details.composer-cover',h('summary',cover?'Cover selected · separate from slides':'Choose a cover'),h('p.st-hint','A cover is exported separately. It is never added as a content slide. Instagram Reels can deliver a reviewed JPEG cover. Other channels include it as a native app step.'));
    if(cover){
      const comparison=h('div.composer-cover-comparison',h('figure',mediaNode(cover),h('figcaption','Full frame · original')) ,h('figure',mediaNode(cover),h('figcaption','Profile preview · 3:4')));
      comparison.children[0].firstChild.style.objectFit='contain';comparison.children[0].firstChild.style.objectPosition='50% 50%';comparison.children[1].firstChild.style.objectFit='cover';
      const crop=focalPreview(cover,v?.id||null);
      const focal=h('fieldset.composer-cover-focal',h('legend','Cover preview focal position'));
      for(const axis of ['x','y']){const slider=h('input',{type:'range',min:0,max:1,step:.01,value:crop[axis],'aria-label':axis==='x'?'Cover horizontal focal position':'Cover vertical focal position'});slider.addEventListener('input',()=>{crop[axis]=Number(slider.value);comparison.children[1].firstChild.style.objectPosition=`${(axis==='x'?Number(slider.value):crop.x)*100}% ${(axis==='y'?Number(slider.value):crop.y)*100}%`;});slider.addEventListener('change',()=>saveFocal(cover.id,axis,Number(slider.value)));focal.append(field(axis==='x'?'Horizontal':'Vertical',slider));}
      coverBox.append(comparison,focal,h('p.st-hint','Cover preview only. The original and export files stay unchanged.'),h('button',{type:'button',onclick:()=>mediaAction('remove',{item:cover.id})},'Remove cover'));
    }
    coverBox.append(h('button',{type:'button',onclick:()=>pick('cover')},cover?'Change cover':'Choose image cover'));
    if(asset.kind==='video'&&editing){editing.append(h('button',{type:'button',onclick:async()=>{await save.flush();if(S.dirty.size||S.saving)return;await openMedia(ctx,{asset:selected.asset,mediaKind:'video',target:{post:postId,variant:v?.id||null,item:selected.id,expected_rev:S.rev},onAttached:async()=>{location.reload();}});}},'Edit video in Media'));const time=h('input',{type:'number',min:0,step:.1,value:0,'aria-label':'Cover frame seconds'});coverBox.append(field('Frame time (seconds)',time),h('button',{type:'button',onclick:async()=>{await save.flush();if(S.dirty.size||S.saving)return;try{const job=await ctx.api.post('workbench/frame',{post:postId,variant:v?.id||null,asset:selected.asset,seconds:Number(time.value),expected_rev:S.rev});setStatus('Extracting cover frame…');S.frameJob=job.id;const cancel=h('button',{type:'button',onclick:async()=>{await ctx.api.post('workbench/cancel-frame',{job_id:S.frameJob});cancel.remove();setStatus('Frame cancellation requested');}},'Cancel extraction');coverBox.append(cancel);}catch(e){setStatus(e.message,'bad');}}},'Extract cover frame'));}
    strip.append(coverBox);return strip;
  }

  async function swapAsset(index, m) {
    const v = variant();
    await save.flush();
    openAssetPicker(ctx, {
      title: `Swap image ${index + 1}`, currentId: m.asset,
      onPick: async (asset) => {
        if (asset.id === m.asset) return;
        const before = v.queue_status;
        try {
          const res = await ctx.api.post(`posts/${postId}/swap_asset`, { variant: v.id, expected_rev: S.rev, index, asset: asset.id });
          S.post = res.post; S.rev = res.post.rev;
          await loadAssets(); await loadServerPreview();
          const nv = variant();
          if ((before === "approved" || before === "awaiting_approval") && nv.queue_status === "awaiting_approval" && before === "approved") {
            S.notice = `Approval removed: the image changed (${m.asset} to ${asset.id}). It is back in review.`;
          } else S.notice = null;
          drawAll();
          say(ctx, S.notice || `Swapped to ${asset.id}.`);
          const alt = form.querySelector(`[data-path$="media.${index}.alt"]`);
          if (alt && !String(alt.value).trim()) alt.focus();
        } catch (ex) {
          if (ex.latest) { S.post = ex.latest; S.rev = ex.latest.rev; drawAll(); }
          say(ctx, `Could not swap: ${ex.message}`, { error: true });
        }
      },
    });
  }

  function drawPreview() {
    if (!S.post || !S.cfg) return;
    const v = variant();
    clear(previewBox);
    if (!v) return;
    const model = previewOf(S.post, S.dirty, v, { assets: S.assets, accounts: S.accounts, server: S.server[v.id], showHidden: S.showHidden });
    const toggle = h("label.st-check.ed-fold", h("input", { type: "checkbox", checked: S.showHidden, onchange: (e) => { S.showHidden = e.target.checked; drawPreview(); } }), "Show the text readers only see after tapping more");
    previewBox.append(h("h2", `${PLATFORM_NAMES[v.platform] || v.platform} preview`), toggle, buildPreview(model, S.cfg));
    const stats = captionStats(model, platformCfg(S.cfg, v.platform));
    previewBox.dataset.state = stats.state;
  }

  function drawSide() {
    clear(side);
    const v = variant();
    if (!v) return;
    const q = S.queue[v.id] || {};
    const approved = v.queue_status === "approved";
    const ap = v.approval;
    const bound = ["media file bytes", "caption", "hashtags", "first comment", "link", "alt text", "account"];
    side.append(
      h("section.ed-approval", { "aria-label": "Approval" }, h("h2", "Approval"),
        h("p", statusChip(v.status,v), " ", h("span.st-hint", window.aveNative&&v.delivery_operation?.recoverable?"Inspect the destination, then use Review publication outcome for this exact operation.":v.status_reason || "")),
        S.notice ? h("p.st-alert.warn", { role: "note" }, S.notice) : null,
        approved && ap ? h("p", `Approved by ${ap.by}${q.expires_at ? `, valid until ${fmtWhen(q.expires_at)}` : ""}.`) : null,
        (v.flags || []).filter((f) => FLAG_ORDER.includes(f)).map((f) => h("p.st-alert." + (f === "approval_expired" || f === "approval_stale" ? "bad" : "warn"), { role: "note", dataset: { flag: f } }, h("strong", flagInfo(f)[0] + ". "), flagInfo(f)[1])),
        h("p.st-hint", "Approval covers: ", bound.join(", "), ". It does not cover the time."),
        v.queue_sha ? h("p.st-hint", "Bytes: ", h("code", shortSha(v.queue_sha, 12))) : null,
        v.status === "in_review" ? h("a.st-btn.primary", { href: `/app/#/workspace/approvals`, dataset: { act: "to-review" } }, "Review and approve in the Workspace") : null),
      h("section.ed-license", { "aria-label": "Licences" }, h("h2", "Licences"), licenseList(v)),
      h("section.ed-disc", h("h2", "Disclosure"), h("p", S.post.disclosure && S.post.disclosure.ai_generated ? "Posts with an AI disclosure: this uses AI-generated media." : "No AI-generated media is used.")),
      h("details.ed-history", h("summary", `History (${S.events.length})`), deliveryReceipt(v), h("ol.st-timeline", S.events.slice().reverse().slice(0, 12).map((e) => h("li", h("time", fmtWhen(e.ts)), ` ${e.actor && e.actor.name || "?"} · ${e.op}`)))));
  }
  /** The time, with the two ways to change it that need no dragging: a date and time picker (with the next free slots) and "next free slot". */
  function whenSection(v) {
    const acct = S.accounts[v.account] || {};
    const locked = ["published", "publishing", "dropped"].includes(v.status);
    const item = () => ({ post: postId, variant: v.id, rev: S.rev, title: postTitle(S.post, v), at: v.schedule ? v.schedule.local : null, tz: (v.schedule && v.schedule.tz) || acct.tz || "UTC",
      account: v.account, status: v.status, flags: v.flags || [] });
    const refresh = async () => { await load(); };
    return h("section.ed-when", { "aria-label": "Time" }, h("h2", "When"),
      h("p.ed-whentext", v.schedule ? [h("strong", fmtWhen(v.schedule.local, v.schedule.tz)), h("span.st-hint", ` ${v.schedule.tz} time`)] : "Not scheduled yet."),
      locked ? h("p.st-hint", v.status === "dropped" ? "Dropped posts have no time." : "A post that is published or being sent cannot be moved.")
        : h("div.ed-whenbtns",
          h("button", { type: "button", dataset: { act: "change-time" }, onclick: async () => { await save.flush(); openReschedule(ctx, [item()], { onDone: refresh }); } }, "Change time…"),
          h("button", { type: "button", dataset: { act: "next-slot" }, onclick: async () => { await save.flush(); try { await nextFreeSlot(ctx, item(), { onChange: refresh }); } catch (ex) { say(ctx, ex.message, { error: true }); } } }, "Next free slot"),
          v.schedule ? h("button.ghost", { type: "button", dataset: { act: "unschedule" }, onclick: async () => { await save.flush(); try { const m = await import("./bulk.js"); await m.runBulk(ctx, { op: "unschedule", items: [{ post: postId, variant: v.id, expected_rev: S.rev }] }, { titles: { [v.id]: postTitle(S.post, v) }, onChange: refresh }); } catch (ex) { say(ctx, ex.message, { error: true }); } } }, "Move to Unscheduled") : null));
  }
  function licenseList(v) {
    const list = effectiveMedia(S.post, v);
    if (!list.length) return h("p.st-hint", "No media, nothing to license.");
    return h("ul.st-plain", list.map((m) => {
      const a = S.assets[m.asset] || {};
      const cov = a.coverage || {};
      const lic = a.license || {};
      return h("li", h("code", m.asset), " ", cov.covered === false ? h("span.bad", "No licence entry") : h("span.ok", lic.summary || "own work"),
        lic.attribution && lic.attribution.required ? h("span.st-alert.warn", ` Credit required: ${lic.attribution.text}`) : null);
    }));
  }

  // ---- send back, polling, lifecycle -----------------------------------------------------------------------------------------------
  async function sendBack() {
    await save.flush();
    const sel = S.lastSelection;
    let copy = null;
    if (sel && /\.copy\.(caption|first_comment|hashtags)$/.test(sel.path)) {
      const el = sel.el;
      copy = { field: pathInfo(sel.path).key, before: el.value, selection: el.selectionStart !== el.selectionEnd ? [el.selectionStart, el.selectionEnd] : null };
    }
    openSendBack(ctx, {
      post: S.post, variant: variant(), rev: S.rev, copy, asset: (effectiveMedia(S.post, variant())[0] || {}).asset || null,
      onSent: async () => { await load(); },
    });
  }

  async function poll() {
    if(S.frameJob){try{const j=await ctx.api.get(`workbench/jobs/${S.frameJob}`);if(["done","failed","interrupted"].includes(j.status)){S.frameJob=null;setStatus(j.diagnostics||j.result?.note||"Cover frame ready",j.status==="done"?"saved":"bad");}}catch{}}
    if (!S.alive || S.saving || S.conflict || document.visibilityState === "hidden") return;
    try {
      const res = await ctx.api.get(`posts/${postId}`);
      if (res.post.rev === S.rev) return;
      if (S.dirty.size) {
        S.remote = res;
        banner.textContent = "Another editor changed this post while you were typing. Your next save will show both versions.";
        return;
      }
      adopt(res); await loadAssets(); await loadServerPreview();
      banner.textContent = "";
      drawAll();
      announce(ctx, "Another editor updated this post.");
    } catch { /* offline for a moment: the next poll tries again */ }
  }
  const unregister=registerDraft({dirty:()=>S.dirty.size>0||S.saving,flush:()=>save.flush(),discard:async()=>{save.cancel();clearTimeout(S.rebaseTimer);if(S.saveTask)await S.saveTask;save.cancel();clearTimeout(S.rebaseTimer);const latest=await ctx.api.get(`posts/${postId}`);adopt(latest);S.dirty.clear();S.conflict=null;S.refreshMedia=false;S.undo=null;clear(conflictBox);clear(banner);await loadAssets();setStatus('Up to date','saved');}});
  const timer = setInterval(poll, POLL_MS);
  const onVis = () => { if (document.visibilityState === "hidden") save.flush(); };
  document.addEventListener("visibilitychange", onVis);
  const onKey = (e) => {
    if (e.defaultPrevented || e.metaKey || e.ctrlKey || e.altKey) return;
    const t = e.target;
    if (t && (t.tagName === "TEXTAREA" || t.tagName === "INPUT" || t.tagName === "SELECT" || t.isContentEditable)) return;
    if (document.querySelector("dialog[open]")) return;
    if (e.key === "s") { e.preventDefault(); sendBack(); }
  };
  document.addEventListener("keydown", onKey);

  load(true);
  return {
    state: S,
    flush: () => save.flush(),
    destroy() { mediaDnd.cancel();mediaDnd.reset();unregister(); S.alive = false; clearInterval(timer); document.removeEventListener("visibilitychange", onVis); document.removeEventListener("keydown", onKey); if (S.dirty.size && !S.conflict) save.flush(); },
  };
}

/** Convenience for a shell route handler: "#/post/<id>?variant=<v>". */
export function mountEditorFromHash(root, ctx, hash = location.hash) {
  const m = /^#\/post\/([^?/]+)/.exec(hash);
  if (!m) { clear(root).append(empty("No post selected.")); return { destroy() {} }; }
  return mountEditor(root, ctx, { postId: decodeURIComponent(m[1]), variantId: hashQuery(hash).variant || null });
}
