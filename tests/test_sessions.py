"""N1: a recording day has several SESSIONS (takes). Cameras of one session share creation_time and length."""
import json
import subprocess

import pytest

from ave import media
from ave.episode import Episode

from conftest import needs_ffmpeg


def item(name, ct, dur, kind="video", ch=2):
    return {"file": f"raw/{name}", "kind": kind, "duration": dur, "creation_time": ct,
            "video": {"fps": 30.0} if kind == "video" else None,
            "audio": [{"index": 1, "codec": "pcm", "channels": ch, "sample_rate": 44100}]}


S1, S2 = "2026-10-12T22:07:45.000000Z", "2026-10-13T00:32:47.000000Z"


def real_like():
    return [item("IMG_0002.mov", S1, 7030.7), item("IMG_0002 2.mov", S1, 7031.0),
            item("IMG_0003.mov", S2, 1314.9), item("IMG_0003 2.mov", "2026-10-13T00:32:48.000000Z", 1315.5),
            item("master.wav", None, 8437.6, kind="audio")]


def test_clusters_cameras_by_creation_time_and_duration():
    s = media.cluster_sessions(real_like())
    assert [x["id"] for x in s] == ["s1", "s2"]
    assert s[0]["files"] == ["raw/IMG_0002 2.mov", "raw/IMG_0002.mov"] or sorted(s[0]["files"]) == ["raw/IMG_0002 2.mov", "raw/IMG_0002.mov"]
    assert sorted(s[1]["files"]) == ["raw/IMG_0003 2.mov", "raw/IMG_0003.mov"]
    assert s[0]["duration"] == pytest.approx(7031.0) and s[0]["label"] is None
    assert s[0]["creation_time"] == S1


def test_same_creation_time_but_different_length_is_not_one_session():
    s = media.cluster_sessions([item("a.mov", S1, 100.0), item("b.mov", S1, 160.0)])
    assert len(s) == 2


def test_missing_creation_time_falls_back_to_duration():
    s = media.cluster_sessions([item("a.mov", None, 30.0), item("b.mov", None, 29.0), item("c.mov", None, 90.0)])
    assert [len(x["files"]) for x in s] == [2, 1]


def test_audio_longer_than_a_session_warns_and_reports_sums():
    items = real_like()
    rep = media.session_report(items, media.cluster_sessions(items))
    assert [r["file"] for r in rep["recorders"]] == ["raw/master.wav"]
    r = rep["recorders"][0]
    assert r["spans_sessions"] is True
    assert r["sum_session_durations"] == pytest.approx(7031.0 + 1315.5)
    assert r["difference_s"] == pytest.approx(8437.6 - 8346.5)
    assert any("may span" in w for w in rep["warnings"])


def test_audio_shorter_than_every_session_is_quiet():
    items = [item("a.mov", S1, 100.0), item("r.wav", None, 99.0, kind="audio")]
    rep = media.session_report(items, media.cluster_sessions(items))
    assert not rep["warnings"] and rep["recorders"][0]["spans_sessions"] is False


@needs_ffmpeg
def test_ingest_writes_sessions_and_keeps_human_labels(tmp_path):
    ep = Episode(tmp_path / "ep"); ep.create()

    def vid(name, ct, dur):
        subprocess.run(["ffmpeg", "-nostdin", "-v", "error", "-y", "-f", "lavfi", "-i", f"color=c=black:s=32x18:r=10:d={dur}",
                        "-f", "lavfi", "-i", f"sine=f=300:d={dur}", "-metadata", f"creation_time={ct}", "-shortest",
                        "-c:v", "libx264", "-preset", "ultrafast", str(ep.raw / name)], check=True)
    vid("a1.mp4", "2026-10-12T22:07:45Z", 2); vid("a2.mp4", "2026-10-12T22:07:45Z", 2)
    vid("b1.mp4", "2026-10-13T00:32:47Z", 1); vid("b2.mp4", "2026-10-13T00:32:47Z", 1)
    subprocess.run(["ffmpeg", "-nostdin", "-v", "error", "-y", "-f", "lavfi", "-i", "sine=f=200:d=5", str(ep.raw / "master.wav")], check=True)
    r = media.ingest(ep)
    assert [s["id"] for s in r["sessions"]] == ["s1", "s2"]
    assert [len(s["files"]) for s in r["sessions"]] == [2, 2]
    assert r["recorders"][0]["spans_sessions"] is True and any("may span" in w for w in r["warnings"])
    # a human label written into media.json survives a re-ingest
    p = ep.artifact("media.json")
    d = json.loads(p.read_text()); d["sessions"][1]["label"] = "experimental"; p.write_text(json.dumps(d))
    r2 = media.ingest(ep)
    assert r2["sessions"][1]["label"] == "experimental"
