import subprocess

import pytest

from ave import editdoc, final
from conftest import needs_ffmpeg

pytestmark = needs_ffmpeg


def _music(path):
    subprocess.run(["ffmpeg", "-nostdin", "-v", "error", "-y", "-f", "lavfi", "-i", "sine=frequency=110:duration=40",
                    "-ar", "48000", str(path)], check=True)


def test_final_builds_captions_and_renders_with_verification(synth_episode):
    rep = final.build_final(synth_episode, render=True, do_retranscribe=False, preset="proxy")
    assert rep["base"] == "assembly" and any("nothing has been cleaned" in w for w in rep["warnings"])
    assert rep["render"]["duration"] == pytest.approx(editdoc.load_latest(synth_episode, "final")["duration"], abs=0.1)
    doc = editdoc.load_latest(synth_episode, "final")
    assert doc["stage"] == "final" and doc["captions"]["srt"] and (synth_episode.root / doc["captions"]["srt"]).exists()
    assert editdoc.list_versions(synth_episode, "final") == [1, 2]       # doc, then doc+captions; nothing overwritten


def test_unlicensed_music_is_refused_unless_explicitly_allowed(synth_episode):
    m = synth_episode.work / "bed.wav"; _music(m)
    with pytest.raises(SystemExit, match="no license-log entry"):
        final.build_final(synth_episode, music=str(m), render=False)
    rep = final.build_final(synth_episode, music=str(m), render=False, allow_unlicensed=True)
    assert any("UNLICENSED" in w for w in rep["warnings"])
    music = editdoc.load_latest(synth_episode, "final")["tracks"]["music"]
    assert music and music[0]["duck_db"] == -8.0 and music[0]["gain_db"] == -20.0


def test_whisper_fallback_retranscription_is_reported_not_swallowed(synth_episode, monkeypatch):
    """Real episode: FluidAudio was missing, retranscribe silently fell back to whisper (times 0.5-2 s early, no punctuation) and
    `ave final` printed no warning. The final report must carry the fallback warning."""
    from ave import captions
    words = [{"w": "hello", "start": 0.1, "end": 0.5, "conf": 1.0, "speaker": "S1"}]
    monkeypatch.setattr(captions, "retranscribe", lambda ep, out, asr="fluidaudio": {
        "words": words, "engine": {"asr": "whisper"}, "warnings": ["FluidAudio is not built; used whisper.cpp instead"]})
    rep = final.build_final(synth_episode, render=True, preset="proxy")
    assert rep["retranscribed_srt"]["engine"] == "whisper"
    assert any("FluidAudio is not built" in w for w in rep["warnings"]) and any("0.5-2 s off" in w for w in rep["warnings"])


def test_from_final_keeps_overlays_placed_after_the_first_final(synth_episode):
    """Real episode: `ave final` always cloned the rough cut, so lower thirds placed on final.vN were silently dropped on the next
    `ave final`. --from-final continues the latest final (overlays kept, captions regenerated)."""
    from ave import motion
    final.build_final(synth_episode, render=False)
    motion.place(synth_episode, "lower-third", {"name": "Tomas", "title": "CO-HOST"}, at=1.0, dur=1.5, stage="final", fps=10)
    # the default path starts from rough/assembly again: overlay gone (documented behaviour)
    final.build_final(synth_episode, render=False)
    assert not editdoc.load_latest(synth_episode, "final")["tracks"]["overlays"]
    motion.place(synth_episode, "lower-third", {"name": "Tomas", "title": "CO-HOST"}, at=1.0, dur=1.5, stage="final", fps=10)
    rep = final.build_final(synth_episode, render=False, from_final=True)
    doc = editdoc.load_latest(synth_episode, "final")
    assert [o["props"]["name"] for o in doc["tracks"]["overlays"]] == ["Tomas"] and doc["captions"]["srt"] and rep["edit"]
    with pytest.raises(SystemExit, match="from-final"):
        final.build_final(synth_episode, render=False, from_final=True, name="nope")
