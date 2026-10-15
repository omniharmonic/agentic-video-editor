from ave.cutplan import CutPolicy, plan, to_frames

COV = {"A": (0.0, 100.0), "B": (0.0, 100.0)}
MAP = {"S1": "A", "S2": "B"}


def u(spk, a, b, text="word " * 6):
    return {"speaker": spk, "start": a, "end": b, "text": text.strip()}


def test_alternates_cameras_with_lead_and_is_contiguous():
    shots = plan([u("S1", 0, 10), u("S2", 10.5, 20), u("S1", 21, 30)], MAP, COV, 30.0)
    assert [s["camera"] for s in shots] == ["A", "B", "A"]
    assert shots[1]["start"] == 10.5 - 0.25                      # lead-in before the new speaker
    assert all(a["end"] == b["start"] for a, b in zip(shots, shots[1:]))
    assert shots[0]["start"] == 0.0 and shots[-1]["end"] == 30.0


def test_min_shot_length_delays_a_too_early_cut():
    p = CutPolicy(min_shot_s=3.0)
    shots = plan([u("S1", 0, 1.0), u("S2", 1.1, 8)], MAP, COV, 8.0, p)
    assert shots[1]["start"] >= 3.0


def test_default_policy_ignores_short_interjection():
    shots = plan([u("S1", 0, 10), u("S2", 5, 5.6, "yeah"), u("S1", 10, 20)], MAP, COV, 20.0)
    assert [s["camera"] for s in shots] == ["A"]


def test_falls_back_when_camera_not_recording():
    cov = {"A": (0.0, 100.0), "B": (12.0, 100.0)}                # B started 12 s late
    shots = plan([u("S1", 0, 5), u("S2", 6, 20)], MAP, cov, 20.0)
    assert [s["camera"] for s in shots] == ["A"]                 # S2's line is covered by A, merged into one shot


def test_frames_are_contiguous_and_use_sync_offsets():
    shots = plan([u("S1", 0, 10), u("S2", 10.5, 20)], MAP, COV, 20.0)
    f = to_frames(shots, 30.0, {"A": 0.0, "B": 4.25}, {"A": "a.mov", "B": "b.mov"})
    assert f[0]["record_frame"] == 0
    assert f[1]["record_frame"] == f[0]["record_frame"] + f[0]["frames"]
    t = f[1]["start_s"]
    assert f[1]["source_start_frame"] == round((t - 4.25) * 30)   # camera B's own clock
    assert f[1]["source_end_frame"] - f[1]["source_start_frame"] + 1 == f[1]["frames"]


def test_adjacent_same_camera_shots_are_merged():
    cov = {"A": (0.0, 100.0), "B": (12.0, 100.0)}
    shots = plan([u("S2", 0, 5), u("S1", 6, 20)], MAP, cov, 20.0)   # S2->B falls back to A, then S1 is A
    assert len(shots) == 1 and shots[0]["camera"] == "A"


def test_frames_follow_the_cameras_drift():
    shots = plan([u("S1", 0, 10), u("S2", 600, 700)], MAP, {"A": (0.0, 1000.0), "B": (0.0, 1000.0)}, 700.0)
    f = to_frames(shots, 30.0, {"A": 0.0, "B": 4.0}, {"A": "a.mov", "B": "b.mov"}, {"B": 80.0})
    t = f[1]["start_s"]
    assert f[1]["source_start_frame"] == round(((t - 4.0) / 1.00008) * 30)
    assert f[1]["source_start_frame"] != round((t - 4.0) * 30)             # 80 ppm at 600 s is 48 ms: more than a frame
