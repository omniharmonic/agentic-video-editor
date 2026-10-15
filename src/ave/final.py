"""Final cut: rough cut + music + captions + loudness, rendered and verified, with exact-match subtitles.

Stages (each recorded; nothing is overwritten, every step writes the next EditDoc version):
  1. final EditDoc = latest rough (assembly with a warning if cleanup was skipped) + optional music (license-gated)
  2. captions attached (SRT + styled ASS) via ave.captions
  3. render with ave.compile (verified by ffprobe + measured loudness)
  4. RE-TRANSCRIBE the rendered file so the shipped SRT matches exactly what is heard, after every edit
Motion graphics and B-roll are placed beforehand (`ave motion place`, B-roll overlays) and ride along in the doc.
"""

from __future__ import annotations

from pathlib import Path

from . import editdoc
from .episode import Episode


def _license_gate(ep: Episode, doc: dict, allow_unlicensed: bool) -> list[str]:
    """Every music/B-roll file must have a license-log entry for exactly these bytes. Returns warnings; raises otherwise."""
    from .assets import licenses
    files = [m["file"] for m in doc["tracks"].get("music") or []]
    files += [o["file"] for o in doc["tracks"].get("overlays") or [] if o.get("kind") == "broll"]
    if not files:
        return []
    cov = licenses.coverage(ep, files)
    bad = [m["file"] for m in cov["missing"]] + list(cov["unreadable"])
    if bad and not allow_unlicensed:
        raise SystemExit("These files have no license-log entry, so the final cannot use them:\n  - " + "\n  - ".join(bad) +
                         "\nLog each one with `ave assets log-license` (or fetch it with `ave assets stock-get`), or pass --allow-unlicensed "
                         "if you own the rights and accept the risk.")
    return [f"UNLICENSED asset allowed by flag: {f}" for f in bad]


def _base_stage(ep: Episode, name: str | None, from_final: bool) -> str:
    """Which edit the final is built from. `from_final` continues the latest final.<name> (its motion overlays, e.g. lower thirds
    placed with `ave motion place --stage final`) instead of cloning the rough cut again, which would drop them."""
    if from_final:
        if not editdoc.list_versions(ep, "final", name):
            raise SystemExit(f"--from-final needs an existing final{' ' + name if name else ''} edit (run `ave final --no-render` first, then place graphics with --stage final).")
        return "final"
    return "rough" if editdoc.list_versions(ep, "rough", name) else "assembly"


def build_final(ep: Episode, *, style: str = "podcast", music: str | None = None, music_in: float = 0.0,
                music_gain_db: float = -20.0, duck_db: float = -8.0, preset: str = "master", render: bool = True,
                burn: bool = False, do_retranscribe: bool = True, allow_unlicensed: bool = False,
                asr: str = "fluidaudio", name: str | None = None, opening: bool | None = None, closing: bool | None = None,
                draft_sequences: bool = False, from_final: bool = False) -> dict:
    from . import captions, compile as comp

    warnings: list[str] = []
    from . import sessions
    base_stage = _base_stage(ep, name, from_final)
    if not editdoc.list_versions(ep, base_stage, name):
        raise SystemExit(f"No rough or assembly edit{' ' + name if name else ''} to build the final from. Run: ave assemble {ep.root}" + sessions.hint(ep, name))
    if base_stage == "assembly":
        warnings.append("No rough cut: the final is built from the assembly, so nothing has been cleaned up.")
    doc = editdoc.clone(editdoc.load_latest(ep, base_stage, name))
    doc["stage"] = "final"
    if base_stage != "final":      # a continued final keeps its overlays; captions are regenerated below either way
        doc["captions"] = {"srt": None, "ass": None, "burn": False}
    d = doc["duration"]
    if music:
        mp = Path(music)
        rel = str(mp.relative_to(ep.root)) if mp.is_absolute() and mp.is_relative_to(ep.root) else music
        doc["tracks"]["music"] = [{"id": "music1", "file": rel, "o0": 0.0, "o1": d, "in": music_in, "gain_db": music_gain_db,
                                   "duck_db": duck_db, "fade_in_s": 1.5, "fade_out_s": min(3.0, d / 4)}]
    warnings += _license_gate(ep, doc, allow_unlicensed)
    from . import sequence     # opening/closing sequences: isolated hook (policy `sequences:`); a no-op unless wanted
    seq_ctx = sequence.final_prepare(ep, doc, name=name, opening=opening, closing=closing, preset=preset, build_now=render, burn=burn,
                                     draft=draft_sequences, allow_unlicensed=allow_unlicensed)
    path = editdoc.save_new_version(ep, doc, name)

    cap = captions.make_captions(ep, "final", style, name=name)
    final_doc = editdoc.load_latest(ep, "final", name)
    report: dict = {"name": name, "base": base_stage, "edit": cap["edit"], "captions": {"srt": cap["srt"], "cues": cap["cues"],
                    "problems": cap["problems"], "style": style}, "warnings": warnings + list(cap.get("warnings") or [])}
    # Post-pass checks on the compiled edit (read-only; they warn): zoom-state rule (no in-out-in jumpiness) and cuts that slice a word.
    try:
        from . import cleanup
        chk = cleanup.check_doc(ep, final_doc)
        report["checks"] = {"zoom": {k: v for k, v in chk["zoom"].items() if k != "violations"} | {"violations": chk["zoom"]["violations"][:10]},
                            "word_cuts": ({"inside_word": len(chk["cutcheck"]["inside_word"]), "stubs": len(chk["cutcheck"]["stubs"]),
                                           "hot_edges": len(chk["cutcheck"]["hot_edges"]), "first": (chk["cutcheck"]["inside_word"] + chk["cutcheck"]["hot_edges"])[:5]}
                                          if chk["cutcheck"] else None)}
        report["warnings"] += chk["warnings"]
    except Exception as e:      # a validator must never stop a final cut
        report["warnings"].append(f"post-pass checks did not run: {type(e).__name__}: {e}")
    if not render:
        return report

    n = editdoc.list_versions(ep, "final", name)[-1]
    tag = f"final.{name}" if name else "final"
    out = ep.exports / f"{tag}.v{n}.{preset}.mp4"
    out.parent.mkdir(parents=True, exist_ok=True)
    body_out = out.with_name(out.stem + ".body.mp4") if seq_ctx else out     # with sequences the body renders aside, then is joined to `out`
    rr = comp.render(ep, final_doc, body_out, preset=preset, burn_captions=burn, overwrite=True)
    report["render"] = {k: rr[k] for k in ("out", "duration", "lufs", "true_peak_db", "preset", "captions_burned") if k in rr}
    report["warnings"] += rr.get("warnings", [])
    if seq_ctx:
        sequence.final_finish(ep, seq_ctx, final_doc, body_out, out, preset, f"final.{name}" if name else "final", report)

    if do_retranscribe:
        # The SRT that ships must match what is actually heard in the final file, not what we planned.
        t = captions.retranscribe(ep, out, asr=asr)
        style_obj = captions.get_style(style, ep=ep)
        cues = captions.cues_from_words(t["words"], style_obj, captions.episode_glossary(ep))
        srt = captions.write_srt(cues, ep.exports / f"{tag}.v{n}.srt")
        report["retranscribed_srt"] = {"path": str(srt), "cues": len(cues), "problems": captions.check(cues, style_obj),
                                       "engine": (t.get("engine") or {}).get("asr")}
        # The whisper.cpp fallback is NOT frame-accurate: on the real episode its word times ran 0.5 s (p10: 1.4 s) early and it
        # drops case/punctuation. Parakeet matched the planned captions within 0.13 s. Say so instead of dropping the warning.
        report["warnings"] += list(t.get("warnings") or [])
        if (t.get("engine") or {}).get("asr") == "whisper":
            report["warnings"].append("retranscribed SRT came from whisper.cpp: cue times can be ~0.5-2 s off and text is lowercase without "
                                      "punctuation. Build FluidAudio (scripts/build-fluidaudio.sh) and rerun `ave captions retranscribe` before shipping it.")
    return report
