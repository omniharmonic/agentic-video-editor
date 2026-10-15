---
name: ave-canva-render
description: Render content items through the creator's Canva (Pro) Brand Templates, as an OPT-IN second renderer. Use only when the human asks for Canva output and `renderers.canva.enabled` is on; the in-house renderer stays the default. Covers the two-phase handshake, the exact MCP calls, the approval gate before any write, import, and the recorded fallback.
---

# Canva render: a handshake, not a command

The CLI cannot call Canva; its MCP lives in your harness (`mcp__claude_ai_Canva__*`). So the work is split:
`ave` writes a **job file** and later **imports** the file you bring back; you make the MCP calls in between, **only after the human approves**.
The in-house renderer (`ave-content-render`) is the default and the fallback. Canva never publishes anything; its output enters the same review and
publish approval as every other image.

## Hard rules (the creator's account is real)

1. **No write to Canva before `! ave content canva-approve <ep> <job>`** has been run by the human. You cannot run it (it needs their terminal); `canva-log` refuses
   write tools on an unapproved job. Reads (`get-brand-template-dataset`, search, list) are always allowed.
2. **Only the steps in the job.** Never edit, delete, rename, move, share or publish an existing design or a Brand Template. Autofill creates a NEW design from the
   template; that is the only thing written, plus the export. Never use `generate-design` or AI generation here (taste: generic output; the brief is authentic, not AI-polished).
3. **One way.** Our tokens go into Canva by hand (`ave content canva-brief`); never read Brand Kit or template contents out for use elsewhere (Canva's policy). Reading a template's
   field names is the only read this flow needs.
4. **Log every call** (`ave content canva-log`). On any failure stop and `canva-fail`; never retry a write blindly and never swap in the in-house render without recording it.
5. Respect limits: export 20/min and 500/day, autofill 60/min (from Canva docs; unverified live). A bulk run is not what this is for.

## Steps

```bash
ave content canva-brief <ep>                         # FINAL palette, bundled fonts (files, OFL licenses), names (Maren / Tomas, CO-HOST) and card layout to enter in Canva's Brand Kit by hand
                                                     # (the creator does this in the Canva UI and will add more brand kit later; the agent never writes the Brand Kit)
                                                     # map Canva fields QUOTE, BY (name), TITLE (CO-HOST, under the name) and SHOW (once); `role` in a field_map still works for TITLE
ave content render <ep> --renderer canva [--item ID] [--export png|jpg|mp4]
                                                     # writes <work>/content/canva_jobs/<job>.json for items whose template is mapped; calls nothing
```

1. **Show the human each job**: item, exact field values, `brand_template_id`, target size, the `steps` that write (`autofill-design`, maybe `resize-design`, `export-design`),
   and `warnings` (a slot with no field mapping will not appear in the design). Ask them to run `! ave content canva-approve <ep> <job>`.
2. **Confirm the tools first.** The job's `tools_verified` is false: run ToolSearch for `Canva`, load the schemas of the tools named in `steps`, and compare argument names. If a name or
   argument differs from the job, log the difference (`canva-log ... --summary "tool X takes Y"`) and use the real schema; if something would write more than the job says, stop and ask.
3. **Run the steps in order** (read job, `status` must be `approved`):
   - `get-brand-template-dataset`: every key of the job's `fields` must exist as a text field. If not: `canva-fail` ("template lacks field X"). The creator fixes the template.
   - `autofill-design` with the job's `fields` (data) and title. Note the returned `design_id`; log it with `--design-id`.
   - `resize-design` only if the job lists it (the Brand Template is authored at another format). Use the resized design's id afterwards.
   - `export-design` as `png`/`jpg`/`mp4`. **Download the result immediately** to the job's `download_to` path (the URL expires in 24 h; never store it).
   After each call: `ave content canva-log <ep> <job> --tool <name> [--design-id ID] [--summary "..."] [--failed]`.
4. **Import:** `ave content canva-import <ep> <job> <downloaded-file> [--design-id ID]`. It checks the pixel size, that the words in the item still match the job, hashes the file and writes
   a new ContentDoc version as a `draft` with `provenance.canva` (design id, template id, kit id, hash, calls, license note). The critic cannot measure Canva output: **Read the PNG yourself**
   at phone size (fit, contrast, orphan words, fonts actually applied) and say what you see.
5. Next is the normal flow: `ave content review`, the human decides, `ave content queue`, `! ave publish approve`. Tell the human the Canva design id/link so they can tweak and re-export;
   a changed export is a new hash and needs a new import and approval.

## Failure: fall back, on the record

`ave content canva-fail <ep> <job> --reason "..."` marks the job failed and writes `provenance.canva.fallback` on the item. Then `ave content render <ep> --item <id>` renders it in-house.
Tell the human which items fell back and why. Common causes to expect (all unverified until a live run): missing field in the template, premium-element `license_required` on export,
rate limit, expired sign-in (`mcp__claude_ai_Canva__authenticate`: send the human the URL; you cannot finish OAuth for them), the reported `perform-editing-operations` bug (not used here).

## Not supported yet

Carousels (multi-page autofill unverified), image/asset fields (an upload is a separate approved write), chart fields, `brand_template_ids` beyond what the creator has tagged, Brand Kit writes.
