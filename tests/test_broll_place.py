"""`ave broll place`: library B-roll into an EditDoc, rendered by the real compiler and measured with ffprobe/pixels/samples.

Everything is synthetic (the two-camera demo episode, B-roll made of known colour blocks and a tone) so every frame and sample of a render can be
traced to where it came from. No network, no real keychain (publish_helpers isolates it), no footage.
"""

from __future__ import annotations

import json
import subprocess
from fractions import Fraction

import numpy as np
import pytest
import soundfile as sf
from PIL import Image

import broll_place_helpers as H
from ave import cli, editdoc, ffutil, library
from ave import compile as comp
from ave.broll import place
from ave.episode import Episode
from conftest import needs_ffmpeg
from publish_helpers import isolated_secrets  # noqa: F401  (autouse: tmp HOME, in-memory keyring)

pytestmark = needs_ffmpeg
FPS = Fraction(30)
SR = 48000


# ---- a small world: a 10 s rough cut, a studio, licensed and unlicensed B-roll ------------------------------------------

@pytest.fixture
def world(synth_episode, tmp_path):
    ep = synth_episode
    base = editdoc.load_latest(ep, "assembly")
    rough = editdoc.slice_doc(base, 0.0, 10.0, stage="rough")
    editdoc.save_new_version(ep, rough)
    sp = H.make_studio(tmp_path)
    src = tmp_path / "src"
    video = H.make_broll_video(src / "clay.mp4")
    silent = H.make_broll_video(src / "wind.mp4", audio=False, size=(160, 90))
    still = H.make_still(src / "bench.png")
    w = type("W", (), {})()
    w.ep, w.sp, w.tmp = ep, sp, tmp_path
    w.video = H.add_stock(sp, video)
    w.silent = H.add_stock(sp, silent, {**H.STOCK, "asset_id": "993", "url": "https://www.pexels.com/video/993/"})
    w.still = H.add_stock(sp, still, {**H.STOCK, "asset_id": "994", "url": "https://www.pexels.com/photo/994/"})
    w.credited = H.add_stock(sp, H.make_broll_video(src / "hands.mp4", blocks=8), H.CREDIT_REQUIRED)
    unatt = tmp_path / "src" / "mine.mp4"
    H.make_broll_video(unatt, blocks=6)
    w.unattested = H.add_unattested(sp, unatt)
    return w


def kw(w, asset, **over):
    return {"asset": asset["id"], "studio": w.sp.root, "by": "Maren", **over}


def render(w, name="out.mp4", preset="master", doc=None):
    doc = doc or editdoc.load_latest(w.ep, "rough")
    out = w.ep.exports / name
    rep = comp.render(w.ep, doc, out, preset=preset, overwrite=True)
    return out, rep


def audio_wav(w, name, doc=None):
    doc = doc or editdoc.load_latest(w.ep, "rough")
    p = w.ep.work / f"{name}.wav"
    comp.compile_audio(w.ep, doc, p)
    return p


def colors(video, a, b):
    return [H.nearest_color(f) for f in H.decode(video)[a:b]]


# ---- suggest-only, apply, history, reversibility ---------------------------------------------------------------------------

def test_place_without_apply_writes_a_proposal_and_leaves_the_edit_alone(world):
    w = world
    v1 = editdoc.latest_path(w.ep, "rough")
    before = v1.read_bytes()
    r = place.place(w.ep, at=2.0, dur=1.0, **kw(w, w.video))
    assert r["applied"] is False and "suggest-only" in r["note"]
    p = r["proposal"]
    assert (w.ep.root / p["file"]).is_file() and p["id"].startswith("bp_")
    assert editdoc.list_versions(w.ep, "rough") == [1] and v1.read_bytes() == before        # the edit did not move
    assert p["placement"]["mode"] == "cutaway" and p["placement"]["audio"] == "mute"
    assert p["placement"]["library"]["asset"] == w.video["id"] and p["checks"]["license"]["ok"] is True
    assert [x["id"] for x in place.list_proposals(w.ep)] == [p["id"]]


def test_apply_writes_a_new_version_with_a_history_entry_and_never_edits_in_place(world):
    w = world
    v1 = editdoc.latest_path(w.ep, "rough")
    before = v1.read_bytes()
    p = place.place(w.ep, at=2.0, dur=1.0, **kw(w, w.video, reason="hands match the line"))["proposal"]
    res = place.apply_proposal(w.ep, p["id"], by="Maren")
    assert res["version"] == 2 and editdoc.list_versions(w.ep, "rough") == [1, 2]
    assert v1.read_bytes() == before                                                       # v1 is untouched
    doc = editdoc.load_latest(w.ep, "rough")
    assert editdoc.validate(doc) == []
    ov = [o for o in doc["tracks"]["overlays"] if o["kind"] == "broll"]
    assert len(ov) == 1 and ov[0]["library"]["asset"] == w.video["id"] and (ov[0]["o0"], ov[0]["o1"]) == (2.0, 3.0)
    h = doc["history"][-1]
    assert (h["op"], h["by"], h["reason"], h["asset"], h["mode"]) == ("broll.place", "Maren", "hands match the line", w.video["id"], "cutaway")
    assert h["from"] == {"stage": "rough", "name": None, "version": 1} and h["result_version"] == 2 and "ave broll undo" in h["inverse"]
    with pytest.raises(SystemExit, match="already applied"):
        place.apply_proposal(w.ep, p["id"])


def test_apply_refuses_when_the_edit_moved_on_since_the_proposal(world):
    w = world
    p = place.place(w.ep, at=2.0, dur=1.0, **kw(w, w.video))["proposal"]
    other = editdoc.clone(editdoc.load_latest(w.ep, "rough"))
    other["notes"] = "someone else saved"
    editdoc.save_new_version(w.ep, other)
    with pytest.raises(SystemExit, match="changed since proposal"):
        place.apply_proposal(w.ep, p["id"])


def test_remove_and_undo_are_new_versions_and_restore_exactly(world):
    w = world
    base = editdoc.load_latest(w.ep, "rough")
    v1 = editdoc.latest_path(w.ep, "rough").read_bytes()
    place.place(w.ep, at=2.0, dur=1.0, apply=True, **kw(w, w.video))
    place.place(w.ep, at=5.0, dur=1.0, apply=True, **kw(w, w.silent))
    assert [o["id"] for o in place.placements(editdoc.load_latest(w.ep, "rough"))] == ["b1", "b2"]
    r = place.remove(w.ep, "b1", by="Maren", reason="too early")
    d4 = editdoc.load_latest(w.ep, "rough")
    assert r["version"] == 4 and [o["id"] for o in place.placements(d4)] == ["b2"] and d4["history"][-1]["op"] == "broll.remove"
    u = place.undo(w.ep, by="Maren")                       # undoes the remove: b1 is back
    d5 = editdoc.load_latest(w.ep, "rough")
    assert u["version"] == 5 and [o["id"] for o in place.placements(d5)] == ["b1", "b2"] and d5["history"][-1]["op"] == "broll.undo"
    with pytest.raises(SystemExit, match="does not end in a B-roll step"):
        place.undo(w.ep)                                 # an undo is not itself undone; going further back is `remove`
    place.remove(w.ep, "b1")
    place.remove(w.ep, "b2")
    assert place.placements(editdoc.load_latest(w.ep, "rough")) == []
    assert editdoc.path_for(w.ep, "rough", 1).read_bytes() == v1       # history is append-only: v1 is still exactly what it was
    assert editdoc.load(editdoc.path_for(w.ep, "rough", 1))["tracks"] == base["tracks"]


def test_undo_refuses_when_something_else_was_saved_since(world):
    w = world
    place.place(w.ep, at=2.0, dur=1.0, apply=True, **kw(w, w.video))
    other = editdoc.clone(editdoc.load_latest(w.ep, "rough"))
    other["notes"] = "a later edit"
    editdoc.save_new_version(w.ep, other)
    with pytest.raises(SystemExit, match="saved after the B-roll step"):
        place.undo(w.ep)


def test_undo_of_a_placement_restores_the_cut_before_it(world):
    w = world
    base = editdoc.load_latest(w.ep, "rough")
    place.place(w.ep, at=2.0, dur=1.0, apply=True, **kw(w, w.video))
    place.undo(w.ep, by="Maren")
    back = editdoc.load_latest(w.ep, "rough")
    assert back["tracks"] == base["tracks"] and back["duration"] == base["duration"] and back["history"][-1]["op"] == "broll.undo"
    assert editdoc.list_versions(w.ep, "rough") == [1, 2, 3]


# ---- timing: frame-accurate, measured on the render ---------------------------------------------------------------------------

def test_cutaway_is_frame_exact_and_starts_at_the_in_point(world):
    w = world
    place.place(w.ep, at=2.0, dur=1.0, apply=True, **kw(w, w.video, src_in=0.8))
    out, rep = render(w)
    info = ffutil.probe(out)
    # ffprobe: the delivered file has exactly round(10 s * 30 fps) frames and the doc's duration
    assert ffutil.count_video_packets(out) == 300 and abs(info["duration"] - 10.0) < 1 / 30
    c = colors(out, 0, 300)
    # source block k covers 0.4k..0.4k+0.4 s: --in 0.8 starts on block 2 (yellow); 12 output frames per 0.4 s block
    assert c[59] == "other" and c[60:72] == ["yellow"] * 12 and c[72:84] == ["cyan"] * 12 and c[84:90] == ["lime"] * 6
    assert c[90] == "other" and set(c[:60]) == {"other"} and set(c[90:]) == {"other"}
    assert rep["broll"]["assets"] == [w.video["id"]] and rep["broll"]["derived_from"] == [w.video["id"]]


def test_cutaway_covers_the_whole_frame_and_only_inside_its_window(world):
    w = world
    place.place(w.ep, at=1.0, dur=0.5, apply=True, **kw(w, w.silent))      # a 160x90 asset on a 640x360 canvas: covered, not letterboxed
    out, _ = render(w)
    frame = H.decode(out, size=(64, 36))[20]                                # frame 20 is inside 30..45? no: before it
    assert H.nearest_color(frame) == "other"
    inside = H.decode(out, size=(64, 36))[33]
    assert H.nearest_color(inside) == "lime"
    # every pixel of the frame is B-roll (cover fills the canvas), corners included
    assert (np.abs(inside.astype(int) - np.array([0, 255, 0])).max() < 70)


def test_placement_with_a_different_frame_rate_is_conformed_to_the_edit(world):
    w = world
    # the asset is 25 fps; the cut is 30 fps: the render still has exactly the cut's frames and the cutaway lasts exactly dur
    place.place(w.ep, at=3.0, dur=0.9, apply=True, **kw(w, w.video))
    out, _ = render(w)
    assert ffutil.probe(out)["r_fps"] == pytest.approx(30.0)
    c = colors(out, 0, 300)
    assert c[90:117].count("lime") + c[90:117].count("magenta") + c[90:117].count("yellow") == 27 and c[117] == "other"


def test_overlay_is_a_box_and_the_speaker_stays_visible_around_it(world):
    w = world
    place.place(w.ep, mode="overlay", corner="br", scale=0.25, at=2.0, dur=1.0, apply=True, **kw(w, w.video))
    ov = place.placements(editdoc.load_latest(w.ep, "rough"))[0]
    assert ov["w"] == 160 and ov["h"] == 90 and (ov["x"], ov["y"]) == (640 - 160 - 26, 360 - 90 - 26)
    out, _ = render(w)
    f = H.decode(out, size=(640, 360))[75]
    inside = f[ov["y"] + 10: ov["y"] + ov["h"] - 10, ov["x"] + 10: ov["x"] + ov["w"] - 10].reshape(-1, 3).mean(axis=0)
    assert np.linalg.norm(inside - np.array([255, 0, 255])) < 60                  # frame 75 = block 1 (magenta)
    outside = f[10:100, 10:100].reshape(-1, 3).mean(axis=0)
    assert np.linalg.norm(outside - np.array([255, 0, 255])) > 150                # the camera picture, not B-roll


def test_a_short_asset_is_refused_unless_the_last_frame_may_be_held(world):
    w = world
    with pytest.raises(SystemExit, match="allow-hold"):
        place.place(w.ep, at=1.0, dur=6.0, **kw(w, w.video))                       # the asset is 4.8 s
    r = place.place(w.ep, at=1.0, dur=6.0, apply=True, allow_hold=True, **kw(w, w.video))
    assert any("last frame is held" in x for x in r["warnings"])
    out, rep = render(w)
    c = colors(out, 0, 300)
    assert c[30:210].count("other") == 0 and c[209] != "other" and c[210] == "other"      # no flash of the speaker once the footage ends
    assert any("last frame is held" in x for x in rep["warnings"])


def test_still_with_ken_burns_pushes_in_over_exactly_its_frames(world):
    w = world
    place.place(w.ep, at=1.0, dur=2.0, apply=True, **kw(w, w.still, ken_burns="push_in"))
    out, _ = render(w)
    assert ffutil.count_video_packets(out) == 300
    fr = H.decode(out, size=(160, 90))
    first, last = fr[30].astype(int), fr[89].astype(int)
    assert H.nearest_color(fr[29]) == "other" and H.nearest_color(fr[90]) == "other"
    # the still is a left-to-right red gradient: pushing in crops the dark left edge, so the left column gets brighter and the right darker
    assert last[:, :6, 0].mean() > first[:, :6, 0].mean() + 8
    assert last[:, -6:, 0].mean() < first[:, -6:, 0].mean() - 8
    mid = fr[60].astype(int)
    assert first[:, :6, 0].mean() < mid[:, :6, 0].mean() < last[:, :6, 0].mean()       # a steady move, not a jump


def test_still_pan_moves_across_the_picture(world):
    w = world
    place.place(w.ep, at=1.0, dur=2.0, apply=True, **kw(w, w.still, ken_burns="pan_right"))
    out, _ = render(w)
    fr = H.decode(out, size=(160, 90)).astype(int)
    assert fr[89][:, :, 0].mean() > fr[30][:, :, 0].mean() + 15                        # the window slides toward the bright edge


def test_still_without_ken_burns_is_held_steady(world):
    w = world
    place.place(w.ep, at=1.0, dur=1.0, apply=True, **kw(w, w.still))
    out, _ = render(w)
    fr = H.decode(out, size=(160, 90)).astype(int)
    assert np.abs(fr[31] - fr[58]).max() < 8


# ---- audio: dialogue untouched ---------------------------------------------------------------------------------------------------

def test_silent_broll_leaves_the_dialogue_master_bit_identical(world):
    w = world
    base_doc = editdoc.load_latest(w.ep, "rough")
    before = audio_wav(w, "before")
    place.place(w.ep, at=2.0, dur=1.0, apply=True, **kw(w, w.video))                       # the asset HAS sound; the placement is mute
    place.place(w.ep, mode="overlay", at=5.0, dur=1.0, apply=True, **kw(w, w.silent))
    after = audio_wav(w, "after")
    # libsndfile's floating WAV PEAK chunk includes the current second. Compare the
    # actual float sample bytes, which must stay exact even across that timestamp.
    a, _ = sf.read(str(before), dtype="float32", always_2d=True)
    b, _ = sf.read(str(after), dtype="float32", always_2d=True)
    assert a.tobytes() == b.tobytes()
    assert a.shape == b.shape and np.array_equal(a, b)
    # and the delivered files carry the same sound
    o1, _ = render(w, "after.mp4")
    o0, _ = render(w, "before.mp4", doc=base_doc)

    def pcm(p):
        return subprocess.run(["ffmpeg", "-nostdin", "-v", "error", "-i", str(p), "-vn", "-f", "s16le", "-"], capture_output=True, check=True).stdout
    assert pcm(o0) and pcm(o0) == pcm(o1)


def test_audio_keep_mixes_the_brolls_sound_only_inside_its_frames_and_ducks_under_speech(world):
    w = world
    base = audio_wav(w, "base")
    ref, _ = sf.read(str(base), dtype="float32", always_2d=True)
    # find a window the dialogue is speaking in, so the ducking is measurable
    loud = max(range(0, 8), key=lambda s: float(np.sqrt((ref[s * SR:(s + 1) * SR] ** 2).mean())))
    place.place(w.ep, at=float(loud), dur=1.0, apply=True, **kw(w, w.video, audio="keep", duck_db=0.0))
    flat = audio_wav(w, "flat")
    place.undo(w.ep)
    place.place(w.ep, at=float(loud), dur=1.0, apply=True, **kw(w, w.video, audio="keep", duck_db=-30.0))
    ducked = audio_wav(w, "ducked")
    a, _ = sf.read(str(flat), dtype="float32", always_2d=True)
    d, _ = sf.read(str(ducked), dtype="float32", always_2d=True)
    n0, n1 = loud * SR, (loud + 1) * SR
    for mix in (a, d):
        diff = mix - ref
        assert np.all(diff[:n0] == 0) and np.all(diff[n1:] == 0)                           # nothing outside the placement's frames moves
    rms = lambda x: float(np.sqrt((x ** 2).mean()))  # noqa: E731
    assert rms((a - ref)[n0:n1]) > 0.005                                                      # the B-roll's tone is in
    assert rms((d - ref)[n0:n1]) < 0.3 * rms((a - ref)[n0:n1])                                # ... and ducked while the dialogue speaks
    place_doc = editdoc.load_latest(w.ep, "rough")
    assert place.placements(place_doc)[0]["audio"] == "keep"


def test_a_source_reads_the_same_samples_whether_or_not_the_read_starts_in_the_first_second(world):
    src = world.ep.root / editdoc.load_latest(world.ep, "rough")["sources"]["A"]["file"]
    r = comp._Reader(src, [], "A")
    whole = r.read(0, 3 * SR)
    for start in (SR // 2, SR, SR + 7, 2 * SR + 1234):
        assert np.array_equal(r.read(start, SR // 2), whole[start:start + SR // 2])


def test_keep_needs_an_asset_with_sound(world):
    w = world
    with pytest.raises(SystemExit, match="no sound"):
        place.place(w.ep, at=1.0, dur=1.0, audio="keep", **kw(w, w.silent))
    with pytest.raises(SystemExit, match="only make sense for a video"):
        place.place(w.ep, at=1.0, dur=1.0, audio="keep", **kw(w, w.still))


# ---- insert: the cut gets longer, the dialogue holds still ------------------------------------------------------------------------

def test_insert_ripples_the_cut_and_the_dialogue_holds_in_exact_silence(world):
    w = world
    base_doc = editdoc.load_latest(w.ep, "rough")
    base = audio_wav(w, "base")
    ref, _ = sf.read(str(base), dtype="float32", always_2d=True)
    r = place.place(w.ep, mode="insert", at=4.0, dur=1.2, apply=True, **kw(w, w.video))
    doc = editdoc.load_latest(w.ep, "rough")
    assert editdoc.validate(doc) == [] and doc["duration"] == pytest.approx(11.2)
    assert r["history"]["ripple"]["dialogue_gap"] and any(s.get("fill") == "silence" for s in doc["tracks"]["dialogue"])
    wav = audio_wav(w, "ins")
    got, _ = sf.read(str(wav), dtype="float32", always_2d=True)
    a, b, c = 4 * SR, int(5.2 * SR), int(11.2 * SR)
    assert got.shape[0] == c
    assert np.array_equal(got[:a], ref[:a])                                       # before the insert: the dialogue as it was
    assert np.all(got[a:b] == 0)                                                  # exact silence while the B-roll plays
    assert got[b:c].shape == ref[a:int(10 * SR)].shape and np.array_equal(got[b:c], ref[a:int(10 * SR)])   # after it: the same dialogue, 1.2 s later, sample for sample (FFmpeg 9 once put reads after a seek one AAC frame off those from the start of the file)
    out, rep = render(w)
    assert ffutil.count_video_packets(out) == 336 and abs(rep["duration"] - 11.2) < 1 / 30
    cs = colors(out, 0, 336)
    assert set(cs[120:156]) <= {"lime", "magenta", "yellow", "cyan"} and cs[119] == "other" and cs[156] == "other"
    # the picture after the insert is the picture that was there before it, 36 frames later
    ref_out, _ = render(w, "never.mp4", doc=base_doc)
    f_new, f_old = H.decode(out, size=(64, 36)).astype(int), H.decode(ref_out, size=(64, 36)).astype(int)
    assert np.abs(f_new[200] - f_old[164]).mean() < 6 and np.abs(f_new[100] - f_old[100]).mean() < 6
    assert not (doc.get("captions") or {}).get("srt")


def test_insert_is_reversed_by_undo_not_remove(world):
    w = world
    base = editdoc.load_latest(w.ep, "rough")
    place.place(w.ep, mode="insert", at=4.0, dur=1.2, apply=True, **kw(w, w.video))
    with pytest.raises(SystemExit, match="is an insert"):
        place.remove(w.ep, "b1")
    place.undo(w.ep)
    back = editdoc.load_latest(w.ep, "rough")
    assert back["tracks"] == base["tracks"] and back["duration"] == base["duration"]


def test_insert_warns_when_it_would_cut_a_spoken_word(world):
    w = world
    words = json.loads((w.ep.work / "transcript.json").read_text())["words"]
    mid = next(x for x in words if x["end"] < 9 and x["end"] - x["start"] > 0.2)
    t = (mid["start"] + mid["end"]) / 2
    r = place.place(w.ep, mode="insert", at=t, dur=1.0, **kw(w, w.video))["proposal"]
    assert any("inside the spoken word" in x for x in r["checks"]["warnings"])


# ---- licenses: closure, attestation, attribution -----------------------------------------------------------------------------------

def test_an_unattested_asset_cannot_be_placed_and_the_message_says_what_to_do(world):
    w = world
    with pytest.raises(SystemExit) as e:
        place.place(w.ep, at=1.0, dur=1.0, **kw(w, w.unattested))
    msg = str(e.value.code)
    assert w.unattested["id"] in msg and "not attested by a human" in msg and "ave studio library attest" in msg
    library.attest_license(w.sp, w.unattested["id"], "Maren")                       # the human's act (here: the library primitive it calls)
    r = place.place(w.ep, at=1.0, dur=1.0, apply=True, **kw(w, w.unattested))
    assert r["applied"] is True


def test_an_unknown_asset_and_a_non_picture_are_refused(world, tmp_path):
    w = world
    with pytest.raises(SystemExit, match="no library asset"):
        place.place(w.ep, at=1.0, dur=1.0, asset="a_000000000000", studio=w.sp.root)
    wav = tmp_path / "tone.wav"
    H.ffm("-f", "lavfi", "-i", "sine=frequency=300:duration=2", str(wav))
    rec = library.add_file(w.sp, wav, source={"type": "own"}, by="Maren")
    with pytest.raises(SystemExit, match="video or an image"):
        place.place(w.ep, at=1.0, dur=1.0, **kw(w, rec))


def _hand_placed(w, asset):
    """An overlay written straight into the doc, bypassing `place` (a hand edit, an old doc, another tool)."""
    import os
    doc = editdoc.clone(editdoc.load_latest(w.ep, "rough"))
    f = library.registry.asset_file(w.sp, asset)
    doc["tracks"]["overlays"].append({"id": "b9", "file": os.path.relpath(f, w.ep.root), "o0": 1.0, "o1": 2.0, "x": 0, "y": 0, "w": 640, "h": 360,
                                      "layer": 0, "kind": "broll", "mode": "cutaway", "fit": "cover", "src_in": 0.0, "audio": "mute",
                                      "library": {"studio": place.studio_ref(w.ep, w.sp), "asset": asset["id"], "sha256": asset["sha256"],
                                                  "kind": "video"}})
    return doc


def test_the_render_is_blocked_for_an_unattested_asset_and_leaves_nothing(world):
    w = world
    doc = _hand_placed(w, w.unattested)
    out = w.ep.exports / "blocked.mp4"
    with pytest.raises(comp.RenderError) as e:
        comp.render(w.ep, doc, out)
    msg = str(e.value)
    assert "render is blocked" in msg and w.unattested["id"] in msg and "not attested by a human" in msg
    assert not out.exists() and not list(w.ep.exports.glob("blocked*"))
    attested_doc = _hand_placed(w, w.video)
    assert comp.render(w.ep, attested_doc, out, preset="proxy")["ok"]


def test_the_render_is_blocked_when_the_placed_file_is_not_the_licensed_bytes(world):
    w = world
    place.place(w.ep, at=1.0, dur=1.0, apply=True, **kw(w, w.video))
    f = library.registry.asset_file(w.sp, w.video)
    f.write_bytes(f.read_bytes() + b"\0")                                              # edited after it was licensed
    with pytest.raises(comp.RenderError, match="no longer has the bytes"):
        render(w)


def test_the_render_is_blocked_when_the_studio_cannot_be_found(world, tmp_path):
    w = world
    place.place(w.ep, at=1.0, dur=1.0, apply=True, **kw(w, w.video))
    import shutil
    shutil.move(str(w.sp.root), str(tmp_path / "moved"))
    with pytest.raises(comp.RenderError, match="studio .* was not found"):
        render(w)


def test_attribution_is_collected_and_shown_when_the_license_requires_it(world):
    w = world
    r = place.place(w.ep, at=2.0, dur=1.0, apply=True, **kw(w, w.credited))
    doc = editdoc.load_latest(w.ep, "rough")
    credit = next(o for o in doc["tracks"]["overlays"] if o["kind"] == "credit")
    assert credit["text"] == "Clay hands by A. Maker on Pexels" and credit["credit_for"] == "b1" and r["credit"]["id"] == "b1c"
    assert (credit["o0"], credit["o1"]) == (2.0, 4.5)                                       # at least credit.min_s on screen
    assert credit["x"] + credit["w"] <= 640 and credit["y"] + credit["h"] <= 360                  # the bar sits fully inside the picture
    png = Image.open(w.ep.root / credit["file"])
    assert png.mode == "RGBA" and png.size == (credit["w"], credit["h"])
    out, rep = render(w)
    assert rep["broll"]["attributions"] == ["Clay hands by A. Maker on Pexels"] and rep["broll"]["derived_from"] == [w.credited["id"]]
    # the caption bar is drawn in the credit's frames and in no others: compare with the same cut rendered without the credit overlay
    bare = editdoc.clone(doc)
    bare["tracks"]["overlays"] = [o for o in bare["tracks"]["overlays"] if o["kind"] != "credit"]
    out2, _ = render(w, "nocredit.mp4", doc=bare)
    a, b = H.decode(out, size=(640, 360)).astype(int), H.decode(out2, size=(640, 360)).astype(int)
    box = (slice(credit["y"], credit["y"] + credit["h"]), slice(credit["x"], credit["x"] + credit["w"]))
    inside = np.abs(a[70][box] - b[70][box]).mean()                                       # frame 70 = 2.33 s: the credit is up
    after = np.abs(a[140][box] - b[140][box]).mean()                                      # frame 140 = 4.67 s: it is gone (2.0 + 2.5 s = 4.5 s)
    assert inside > 20 and after < 3
    assert place.check(w.ep)["credits"] == ["Clay hands by A. Maker on Pexels"]


def test_no_credit_overlay_unless_required_or_asked(world):
    w = world
    place.place(w.ep, at=1.0, dur=1.0, apply=True, **kw(w, w.video))                          # attribution is optional for this license
    assert not [o for o in editdoc.load_latest(w.ep, "rough")["tracks"]["overlays"] if o["kind"] == "credit"]
    place.place(w.ep, at=4.0, dur=1.0, apply=True, credit="always", **kw(w, w.video))
    assert len([o for o in editdoc.load_latest(w.ep, "rough")["tracks"]["overlays"] if o["kind"] == "credit"]) == 1
    place.place(w.ep, at=7.0, dur=1.0, apply=True, credit="never", **kw(w, w.credited))
    assert len([o for o in editdoc.load_latest(w.ep, "rough")["tracks"]["overlays"] if o["kind"] == "credit"]) == 1
    c = place.check(w.ep)
    assert c["ok"] and c["courtesy_credits"] and c["credits"] == ["Clay hands by A. Maker on Pexels"]      # required credit is listed even if not on screen


def test_derived_from_covers_every_placed_asset_once(world):
    w = world
    place.place(w.ep, at=1.0, dur=1.0, apply=True, **kw(w, w.video))
    place.place(w.ep, at=3.0, dur=1.0, apply=True, **kw(w, w.still))
    place.place(w.ep, at=5.0, dur=1.0, apply=True, **kw(w, w.video))
    place.place(w.ep, mode="overlay", at=7.0, dur=1.0, apply=True, **kw(w, w.silent))
    out, rep = render(w, preset="proxy")
    assert sorted(rep["broll"]["derived_from"]) == sorted({w.video["id"], w.still["id"], w.silent["id"]})
    assert len(rep["broll"]["derived_from"]) == 3


def test_a_doc_with_no_library_broll_renders_exactly_as_before(world):
    w = world
    out, rep = render(w, preset="proxy")
    assert "broll" not in rep


def test_the_other_license_gates_know_library_placements(world):
    w = world
    from ave import final, shorts
    from ave.package import collect
    place.place(w.ep, at=2.0, dur=1.0, apply=True, **kw(w, w.credited))
    doc = editdoc.load_latest(w.ep, "rough")
    assert shorts.licensed_files(doc) == []                                                  # not the episode log's business
    rep = shorts.license_report(w.ep, doc)
    assert rep["ok"] and rep["derived_from"] == [w.credited["id"]] and "Clay hands by A. Maker on Pexels" in rep["attribution"]
    assert final._license_gate(w.ep, doc, False) == []
    assert collect.credits_for(w.ep, doc)["lines"] == ["Clay hands by A. Maker on Pexels"] and collect.credits_for(w.ep, doc)["missing"] == []
    bad = _hand_placed(w, w.unattested)
    with pytest.raises(SystemExit, match="not attested"):
        final._license_gate(w.ep, bad, False)
    assert not shorts.license_report(w.ep, bad)["ok"] and collect.credits_for(w.ep, bad)["missing"]


# ---- refusals and advice -------------------------------------------------------------------------------------------------------------

@pytest.mark.parametrize("over,msg", [
    ({"at": 9.5, "dur": 1.0}, "only 10"),
    ({"at": -1.0, "dur": 1.0}, "--at must be"),
    ({"at": 1.0, "dur": 0.01}, "shorter than one frame"),
    ({"at": 1.0, "dur": 1.0, "mode": "wipe"}, "mode must be"),
    ({"at": 1.0, "dur": 1.0, "audio": "loud"}, "audio must be"),
    ({"at": 1.0, "dur": 1.0, "ken_burns": "push_in"}, "stills"),
    ({"at": 1.0, "dur": 1.0, "fit": "zoom"}, "--fit"),
    ({"at": 1.0, "dur": 1.0, "mode": "overlay", "scale": 3.0}, "--scale"),
    ({"at": 1.0, "dur": 1.0, "mode": "overlay", "box": [600, 300, 100, 100]}, "not inside"),
])
def test_refusals_say_why(world, over, msg):
    w = world
    with pytest.raises(SystemExit, match=msg):
        place.place(w.ep, **{**kw(w, w.video), **over})
    assert editdoc.list_versions(w.ep, "rough") == [1]


def test_a_cutaway_may_not_overlap_another(world):
    w = world
    place.place(w.ep, at=2.0, dur=2.0, apply=True, **kw(w, w.video))
    with pytest.raises(SystemExit, match="overlaps B-roll b1"):
        place.place(w.ep, at=3.0, dur=1.0, **kw(w, w.silent))
    assert place.place(w.ep, at=4.0, dur=1.0, **kw(w, w.silent))["applied"] is False       # edge to edge is fine


def test_pacing_is_advice_unless_strict(world):
    w = world
    p = place.place(w.ep, at=2.0, dur=0.5, **kw(w, w.video))["proposal"]                      # under the 1.5 s minimum hold
    assert p["checks"]["pacing"]["ok"] is False and p["checks"]["pacing"]["violations"][0]["rule"] == "length"
    with pytest.raises(SystemExit, match="strict-pacing"):
        place.place(w.ep, at=2.0, dur=0.5, strict_pacing=True, **kw(w, w.video))
    beats = [{"t0": 2.2, "t1": 2.8, "label": "laugh"}]
    p2 = place.place(w.ep, at=2.0, dur=2.0, beats=beats, **kw(w, w.video))["proposal"]
    assert any(v["rule"] == "emotional_beat" for v in p2["checks"]["pacing"]["violations"])


def test_a_named_short_is_a_target(world):
    w = world
    base = editdoc.load_latest(w.ep, "rough")
    sh = editdoc.slice_doc(base, 0.0, 6.0, stage="short")
    sh["canvas"] = {"w": 1080, "h": 1920}
    for s in sh["tracks"]["video"]:
        s["crop"] = [0, 0, 202, 360]
    editdoc.save_new_version(w.ep, sh, name="clip-a")
    r = place.place(w.ep, stage="short", name="clip-a", at=1.0, dur=1.5, apply=True, credit="always", **kw(w, w.credited))
    assert r["stage"] == "short" and r["name"] == "clip-a" and r["version"] == 2
    d = editdoc.load_latest(w.ep, "short", "clip-a")
    ov = place.placements(d)[0]
    assert (ov["w"], ov["h"]) == (1080, 1920)
    cr = next(o for o in d["tracks"]["overlays"] if o["kind"] == "credit")
    assert cr["y"] == int(1920 * 0.12)                                                      # portrait: high, clear of the caption line
    assert editdoc.list_versions(w.ep, "rough") == [1]


# ---- the command ---------------------------------------------------------------------------------------------------------------------------

def _run(capsys, *argv):
    rc = cli.main(list(argv))
    out = capsys.readouterr().out
    return rc, json.loads(out) if out.strip() else None


def test_cli_place_apply_list_check_remove(world, capsys):
    w = world
    common = [str(w.ep.root)]
    rc, res = _run(capsys, "broll", "place", *common, "--asset", w.video["id"], "--studio", str(w.sp.root), "--at", "2", "--dur", "1.5", "--by", "Maren")
    assert rc == 0 and res["applied"] is False and editdoc.list_versions(w.ep, "rough") == [1]
    rc, res = _run(capsys, "broll", "apply", *common, res["proposal"]["id"])
    assert rc == 0 and res["version"] == 2
    rc, res = _run(capsys, "broll", "list", *common)
    assert rc == 0 and res["placements"][0]["asset"] == w.video["id"] and res["closure"]["ok"] and res["history"][0]["op"] == "broll.place"
    rc, res = _run(capsys, "broll", "check", *common)
    assert rc == 0 and res["ok"] and res["derived_from"] == [w.video["id"]]
    rc, res = _run(capsys, "broll", "place", *common, "--asset", w.still["id"], "--studio", str(w.sp.root), "--at", "5", "--dur", "2",
                   "--ken-burns", "pull_out", "--apply")
    assert rc == 0 and res["applied"] is True and res["placement"]["ken_burns"]["zoom_from"] == 1.12
    rc, res = _run(capsys, "broll", "remove", *common, "--id", "b1")
    assert rc == 0 and res["removed"] == ["b1"]
    rc, res = _run(capsys, "broll", "undo", *common)
    assert rc == 0 and res["undid"] == "broll.remove"
    rc, res = _run(capsys, "broll", "proposals", *common)
    assert [p["applied"] is not None for p in res["proposals"]] == [True, True]


def test_cli_check_exits_1_on_an_unlicensed_placement(world, capsys):
    w = world
    doc = _hand_placed(w, w.unattested)
    editdoc.save_new_version(w.ep, doc)
    rc, res = _run(capsys, "broll", "check", str(w.ep.root))
    assert rc == 1 and res["ok"] is False and res["license"]["missing"]


def test_cli_refusal_exits_nonzero_with_the_reason(world):
    w = world
    with pytest.raises(SystemExit) as e:
        cli.main(["broll", "place", str(w.ep.root), "--asset", w.unattested["id"], "--studio", str(w.sp.root), "--at", "1", "--dur", "1"])
    assert "attest" in str(e.value.code)
