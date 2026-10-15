"""The content quote picker and the quote reel (creator feedback 2026-10-14): quotes come from complete statements only, and the
quote card is its own looping product, not an opening."""

import json
import subprocess

import pytest

from ave import cli, quote_reel
from ave.content import sources as S
from ave.episode import read_json, write_json
from content_helpers import CLIP_TEXT, cep, content_env, pick_quote  # noqa: F401

pytestmark = pytest.mark.skipif(not __import__("shutil").which("ffmpeg"), reason="ffmpeg not installed")


def edit_clip(ep, cid="c01", **fields):
    d = read_json(ep.artifact("clips.json"))
    c = next(x for x in d["clips"] if x["id"] == cid)
    c.update(fields)
    write_json(ep.artifact("clips.json"), d)
    return c


# ---- the picker refuses or uses the complete boundary -----------------------------------------------------------------

def test_clip_source_refuses_an_incomplete_clip(cep):
    edit_clip(cep, start_complete=False, complete_statement=False, completeness_note="starts mid-sentence: lowercase 'and'")
    with pytest.raises(ValueError, match="not a complete statement"):
        S.clip_source(cep, "c01")


def test_clip_source_refuses_a_clip_that_was_never_assessed(cep):
    d = read_json(cep.artifact("clips.json"))
    for c in d["clips"]:
        for k in ("start_complete", "end_complete", "complete_statement"):
            c.pop(k, None)
    write_json(cep.artifact("clips.json"), d)
    with pytest.raises(ValueError, match="never assessed"):
        S.clip_source(cep, "c01")


def test_clip_source_uses_the_context_variant_when_the_clip_is_incomplete(cep):
    edit_clip(cep, start_complete=False, complete_statement=False,
              context_variant={"sentence_ids": ["s001"], "start": 5.0, "end": 41.0, "duration": 36.0, "added_text": "Okay so.", "note": "one sentence earlier"})
    src = S.clip_source(cep, "c01")
    assert src["completeness"]["boundary"] == "context_variant" and src["text"].startswith("Okay so.")


def test_clip_source_override_is_explicit_and_recorded(cep):
    edit_clip(cep, end_complete=False, complete_statement=False)
    src = S.clip_source(cep, "c01", allow_incomplete=True)
    assert src["completeness"]["override"] is True and src["completeness"]["complete"] is False


def test_plan_lists_only_complete_clips_and_says_which_it_skipped(cep):
    edit_clip(cep, "c02", status="approved")
    edit_clip(cep, "c02", start_complete=False, complete_statement=False)
    out = S.plan(cep)
    assert [c["clip"] for c in out["approved_clips"]] == ["c01"]
    assert any(x["clip"] == "c02" and x["problems"] for x in out["skipped_incomplete"])


def test_plan_offers_only_quotes_that_start_and_end_on_a_complete_phrase(cep):
    out = S.plan(cep)
    quotes = [q["quote"] for c in out["approved_clips"] for q in c["quote_candidates"]]
    assert quotes and all(q[:1].isupper() for q in quotes)
    assert any(x.get("quote") == "we tend it for a generation" for x in out["skipped_incomplete"])     # the lowercase hook/end quote fragment


@pytest.mark.parametrize("quote,why", [("and the ground remembers what we do to it", "continues the previous thought|lowercase"),
                                       ("the ground remembers what we do to it", "lowercase"),
                                       ("A goal is a wish and a system is a,", "comma"),
                                       ("We tend it for the", "needs something after")])
def test_quote_completeness_flags_a_mid_sentence_start_or_end(cep, quote, why):
    import re
    probs = S.quote_completeness(cep, quote)
    assert probs and re.search(why, " ".join(probs))


def test_whole_sentences_and_clause_pull_quotes_pass(cep):
    assert S.quote_completeness(cep, "We do not fix a field in a season, we tend it for a generation.") == []
    assert S.quote_completeness(cep, "A goal is a wish and a system is a habit") == []


def test_propose_rejects_a_quote_that_starts_mid_sentence_and_the_override_is_explicit(cep, tmp_path, capsys):
    pick = pick_quote(slots={"quote": "the ground remembers what we do to it", "attribution": "Maren Okafor"})
    pf = tmp_path / "picks.json"
    pf.write_text(json.dumps([pick]))
    code = cli.main(["content", "propose", str(cep.root), str(pf)])
    out = json.loads(capsys.readouterr().out)
    assert code == 2 and "complete sentence" in " ".join(out["rejected"][0]["problems"])
    assert cli.main(["content", "propose", str(cep.root), str(pf), "--allow-incomplete"]) == 0


def test_the_ui_can_never_pass_the_override():
    from ave.ui import jobs
    assert "allow-incomplete" in jobs.FORBIDDEN_WORDS and "allow_incomplete" in jobs.FORBIDDEN_WORDS


# ---- the quote reel is its own product -------------------------------------------------------------------------------

QUOTE = "A goal is a wish and a system is a habit, and the ground remembers what we do to it long after we have stopped paying attention."
QUOTE2 = "We do not fix a field in a season, we tend it for a generation."


def test_reel_checks_verbatim_and_complete_starts(cep):
    assert quote_reel.check_quotes(cep, [{"quote": QUOTE}, {"quote": QUOTE2}]) == []
    bad = quote_reel.check_quotes(cep, [{"quote": "Goals are only wishes, systems are habits."}, {"quote": "the ground remembers what we do to it"}, {"quote": ""}])
    assert any("verbatim" in p for p in bad) and any("lowercase" in p for p in bad) and any("empty" in p for p in bad)
    with pytest.raises(SystemExit, match="not built"):
        quote_reel.build(cep, [{"quote": "a paraphrase of nothing the speakers said"}], cep.root / "r.mp4")


def test_reel_is_a_silent_seamless_loop_never_overwritten_and_documents_itself(cep, tmp_path, capsys):
    pytest.importorskip("playwright")
    qf = tmp_path / "quotes.json"
    qf.write_text(json.dumps([{"quote": QUOTE, "attribution": "Maren Okafor", "title": "CO-HOST"}, {"quote": QUOTE2, "attribution": "Maren Okafor"}]))
    out = cep.root / "reels" / "quote-reel.v1.mp4"
    assert cli.main(["motion", "quote-reel", str(cep.root), "--quotes", str(qf), "--card-s", "1.2", "--canvas", "540x960", "--loops", "2",
                     "--out", str(out)]) == 0
    a = json.loads(capsys.readouterr().out)
    assert a["audio"] == "none" and a["streams"] == ["video"] and a["loops_seamlessly"] and a["cards"] == 2
    assert a["duration"] == pytest.approx(2 * 2 * 1.2, abs=0.15)
    side = json.loads(out.with_suffix(".json").read_text())
    assert side["draft"] and side["kind"] == "quote_reel" and "never prepended" in side["note"]
    assert quote_reel.next_path(out.parent, "quote-reel").name == "quote-reel.v2.mp4"
    with pytest.raises(SystemExit, match="never overwritten"):
        cli.main(["motion", "quote-reel", str(cep.root), "--quotes", str(qf), "--out", str(out)])
    # the first and last frames are the plate (each card fades in from and out to it), so the file loops without a seam
    def px(t):
        return subprocess.run(["ffmpeg", "-v", "error", "-ss", str(t), "-i", str(out), "-frames:v", "1", "-vf", "crop=20:20:260:60,scale=1:1", "-f", "rawvideo",
                               "-pix_fmt", "rgb24", "-"], capture_output=True, check=True).stdout
    first, last = px(0.0), px(a["duration"] - 0.05)
    assert max(abs(x - y) for x, y in zip(first, last)) < 10
