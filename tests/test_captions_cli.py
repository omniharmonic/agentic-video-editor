"""retranscribe (ASR mocked, audio extraction real) and the `ave captions` CLI."""

import json
import subprocess
import wave

import pytest

import fixtures
from ave import captions, cli, editdoc, transcribe
from ave.episode import read_json
from conftest import needs_ffmpeg
from test_captions import make_edit

pytestmark = needs_ffmpeg


def run_cli(*argv, capsys):
    rc = cli.main(list(map(str, argv)))
    out = capsys.readouterr().out
    return rc, out


def fake_asr(seen):
    def _asr(wav, out, language):
        with wave.open(str(wav)) as w:
            seen.update(rate=w.getframerate(), channels=w.getnchannels(), seconds=w.getnframes() / w.getframerate())
        return [{"w": "Hello", "start": 0.5, "end": 0.9, "conf": 0.9}, {"w": "world.", "start": 1.0, "end": 1.6, "conf": 0.9},
                {"w": "Thanks", "start": 4.0, "end": 4.4, "conf": 0.9}, {"w": "everyone", "start": 4.5, "end": 5.2, "conf": 0.9}]
    return _asr


def test_retranscribe_extracts_16k_mono_and_reuses_asr(synth_episode, monkeypatch):
    seen = {}
    monkeypatch.setattr(transcribe, "run_fluidaudio_asr", fake_asr(seen))
    monkeypatch.setattr(transcribe.config, "fluidaudio_cli", lambda: "/fake")
    ep = synth_episode
    res = captions.retranscribe(ep, ep.raw / "camA.mp4")
    assert (seen["rate"], seen["channels"]) == (16000, 1)
    assert seen["seconds"] == pytest.approx(30.0, abs=0.1)
    assert [w["w"] for w in res["words"]] == ["Hello", "world.", "Thanks", "everyone"]
    assert res["engine"]["asr"] == "fluidaudio" and res["source"].endswith("camA.mp4")
    assert res["duration"] == pytest.approx(30.0, abs=0.1)
    assert res["utterances"] and res["speakers"]
    cues = captions.cues_from_transcript(res)
    assert [c.text for c in cues] == ["Hello world.", "Thanks everyone"]   # 3 s pause splits them
    assert read_json(ep.work / "retranscribe" / "camA.transcript.json")["words"]


def test_retranscribe_falls_back_to_whisper_when_fluidaudio_missing(synth_episode, monkeypatch):
    seen = {}
    monkeypatch.setattr(transcribe.config, "fluidaudio_cli", lambda: None)
    monkeypatch.setattr(transcribe, "run_whisper_asr", lambda wav, prefix, lang: fake_asr(seen)(wav, prefix, lang))
    res = captions.retranscribe(synth_episode, synth_episode.raw / "camA.mp4")
    assert res["engine"]["asr"] == "whisper" and res["warnings"]


def test_retranscribe_rejects_missing_video(synth_episode):
    with pytest.raises(FileNotFoundError):
        captions.retranscribe(synth_episode, synth_episode.root / "nope.mp4")


def test_cli_captions_writes_files_and_new_version(synth_episode, capsys):
    ep = synth_episode
    editdoc.save_new_version(ep, make_edit(ep, [(0.0, 6.0), (8.0, 30.0)]))
    rc, out = run_cli("captions", ep.root, "--stage", "rough", "--style", "podcast", capsys=capsys)
    assert rc == 0
    res = json.loads(out)
    assert res["srt"] == "captions/rough.srt" and res["style"] == "podcast" and res["problems"] == []
    assert (ep.root / "captions/rough.ass").exists()
    assert editdoc.load_latest(ep, "rough")["captions"]["ass"] == "captions/rough.ass"
    assert read_json(ep.meta_path)["stages"]["captions"]["done"]


def test_cli_check_reports_problems_and_exit_code(synth_episode, tmp_path, capsys):
    good = tmp_path / "good.srt"
    captions.write_srt(captions.cues_from_words(fixtures.words(), captions.get_style("podcast")["rules"]), good)
    rc, out = run_cli("captions", "check", synth_episode.root, good, capsys=capsys)
    assert rc == 0 and json.loads(out)["problems"] == []
    bad = tmp_path / "bad.srt"
    bad.write_text("1\n00:00:00,000 --> 00:00:00,300\n" + "x" * 80 + "\n\n2\n00:00:00,200 --> 00:00:02,000\nfine\n")
    rc, out = run_cli("captions", "check", synth_episode.root, bad, capsys=capsys)
    assert rc == 1
    assert {"line_too_long", "overlap"} <= {p["kind"] for p in json.loads(out)["problems"]}


def test_cli_check_reads_ass_too(synth_episode, tmp_path, capsys):
    p = tmp_path / "x.ass"
    style = captions.get_style("podcast")
    captions.write_ass(captions.cues_from_words(fixtures.words(), style["rules"]), style, (1920, 1080), p)
    rc, out = run_cli("captions", "check", synth_episode.root, p, capsys=capsys)
    assert rc == 0, out


def test_cli_burn_renders_video_with_captions(synth_episode, capsys):
    ep = synth_episode
    doc = make_edit(ep, [(0.0, 6.0), (8.0, 12.0)])
    editdoc.save_new_version(ep, doc)
    vid = ep.root / "exports" / "rough.mp4"
    vid.parent.mkdir(exist_ok=True)
    subprocess.run(["ffmpeg", "-nostdin", "-v", "error", "-y", "-i", str(ep.raw / "camA.mp4"), "-t", "10", "-c", "copy", str(vid)], check=True)
    rc, out = run_cli("captions", ep.root, "--stage", "rough", "--burn", "--video", vid, capsys=capsys)
    assert rc == 0
    res = json.loads(out)
    assert (ep.root / res["burned"]).exists()
    assert editdoc.load_latest(ep, "rough")["captions"]["burn"] is True


def test_cli_retranscribe_writes_srt(synth_episode, monkeypatch, capsys, tmp_path):
    monkeypatch.setattr(transcribe, "run_fluidaudio_asr", fake_asr({}))
    monkeypatch.setattr(transcribe.config, "fluidaudio_cli", lambda: "/fake")
    out_srt = tmp_path / "final.srt"
    rc, out = run_cli("captions", "retranscribe", synth_episode.root, synth_episode.raw / "camA.mp4", "--srt-out", out_srt, capsys=capsys)
    assert rc == 0
    assert [c.text for c in captions.parse_srt(out_srt)] == ["Hello world.", "Thanks everyone"]
    assert json.loads(out)["srt"] == str(out_srt)
