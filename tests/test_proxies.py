"""`ave proxies`: proxies land next to the footage, originals are never touched, the run is resumable and refuses a full disk."""
import hashlib
from argparse import Namespace

import pytest

from ave import editdoc, ffutil, proxies
from ave.commands import proxies as cmd_mod
from conftest import needs_ffmpeg

pytestmark = needs_ffmpeg


def _hashes(paths):
    return {str(p): hashlib.sha1(p.read_bytes()).hexdigest() for p in paths}


def _doc(ep):
    return editdoc.load_latest(ep, "assembly")


@pytest.mark.parametrize("codec", sorted(proxies.PROFILES))
def test_proxies_are_made_next_to_footage_and_originals_are_untouched(layout_episode, codec):
    ep = layout_episode
    files = proxies.footage_used(ep, _doc(ep))
    assert files
    before = _hashes(files)
    mtimes = {f: f.stat().st_mtime_ns for f in files}
    res = proxies.run(ep, files, codec, 1080, hw=False, log=lambda m: None)
    assert not res["failed"] and len(res["made"]) == len(files)
    for f in files:
        out = proxies.proxy_path(ep, f)
        assert out.parent == ep.raw_dirs[0].resolve() / "proxies" and out.suffix == ".mov" and out.exists()
        assert proxies.verify(f, out, codec) == (True, "")
        assert ffutil.probe(out)["codec"] == proxies.PROFILES[codec]["codec_name"]
    assert _hashes(files) == before and {f: f.stat().st_mtime_ns for f in files} == mtimes   # originals byte-identical
    assert not list(ep.root.rglob("*.partial"))
    assert ep.artifact("proxies.json").exists()


def test_proxies_are_not_treated_as_footage_by_ingest(synth_episode):
    ep = synth_episode
    n = len(ep.media_files())
    proxies.run(ep, proxies.footage_used(ep, _doc(ep)), "dnxhr-lb", 1080, hw=False, log=lambda m: None)
    assert len(ep.media_files()) == n


def test_rerun_skips_verified_proxies_and_rebuilds_a_truncated_one(synth_episode):
    ep = synth_episode
    files = proxies.footage_used(ep, _doc(ep))
    proxies.run(ep, files, "prores-proxy", 1080, hw=False, log=lambda m: None)
    again = proxies.run(ep, files, "prores-proxy", 1080, hw=False, log=lambda m: None)
    assert again["made"] == [] and len(again["kept"]) == len(files)
    victim = proxies.proxy_path(ep, files[0])
    victim.write_bytes(victim.read_bytes()[:2000])                      # a cut-off copy that kept its name
    fixed = proxies.run(ep, files, "prores-proxy", 1080, hw=False, log=lambda m: None)
    assert fixed["made"] == [str(victim)] and proxies.verify(files[0], victim, "prores-proxy")[0]


def test_stale_partial_file_is_replaced(synth_episode):
    ep = synth_episode
    f = proxies.footage_used(ep, _doc(ep))[0]
    out = proxies.proxy_path(ep, f)
    out.parent.mkdir(parents=True)
    (out.parent / (out.name + ".partial")).write_bytes(b"junk")
    res = proxies.run(ep, [f], "dnxhr-lb", 1080, hw=False, log=lambda m: None)
    assert res["made"] == [str(out)] and not list(out.parent.glob("*.partial"))


def test_dry_run_prints_estimate_and_writes_nothing(synth_episode):
    ep = synth_episode
    files = proxies.footage_used(ep, _doc(ep))
    snap = sorted(p for p in ep.root.rglob("*") if p.is_file())
    lines = []
    res = proxies.run(ep, files, "prores-proxy", 1080, hw=False, dry_run=True, log=lines.append)
    assert sorted(p for p in ep.root.rglob("*") if p.is_file()) == snap
    assert res["estimate_bytes"] > 0 and "estimate" in lines[0] and "free" in lines[0]


def test_refuses_when_free_space_is_below_one_and_a_half_times_the_estimate(synth_episode, monkeypatch):
    ep = synth_episode
    files = proxies.footage_used(ep, _doc(ep))
    est = proxies.plan_proxies(ep, files, "prores-proxy", 1080)["estimate_bytes"]
    Usage = type("U", (), {})

    def fake_usage(path):
        u = Usage()
        u.free = int(est * 1.4)                                           # enough for the estimate, not for 1.5x
        return u
    monkeypatch.setattr(proxies.shutil, "disk_usage", fake_usage)
    with pytest.raises(SystemExit, match="Refusing"):
        proxies.run(ep, files, "prores-proxy", 1080, hw=False, log=lambda m: None)
    assert not proxies.proxy_path(ep, files[0]).parent.exists()            # nothing was written before the refusal


def test_never_upscales_and_estimate_scales_with_codec(synth_episode):
    ep = synth_episode
    f = proxies.footage_used(ep, _doc(ep))[0]
    facts = proxies._source_facts(f, 1080)
    assert facts["h"] <= ffutil.probe(f)["h"]
    assert proxies.estimate_bytes(facts, "prores-proxy") > proxies.estimate_bytes(facts, "dnxhr-lb") > 0


def test_command_dry_run_and_real_run(synth_episode, capsys):
    ep = synth_episode
    args = dict(episode=str(ep.root), stage="assembly", name=None, codec="dnxhr-lb", height=1080, no_hw=True, force=False, dry_run=True)
    assert cmd_mod.cmd(Namespace(**args)) == 0
    assert '"dry_run": true' in capsys.readouterr().out
    assert cmd_mod.cmd(Namespace(**{**args, "dry_run": False})) == 0
    found, missing = proxies.proxy_map(ep, _doc(ep))
    assert found and not missing
