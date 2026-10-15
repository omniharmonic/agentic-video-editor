import shutil
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent))
import fixtures  # noqa: E402

needs_ffmpeg = pytest.mark.skipif(not shutil.which("ffmpeg"), reason="ffmpeg not installed")


@pytest.fixture(scope="session")
def synth_root(tmp_path_factory):
    """Session-wide pristine synthetic episode at stage 'assembly'. Tests must COPY it (use `synth_episode`)."""
    root = tmp_path_factory.mktemp("synth") / "episode"
    fixtures.build_episode(root, upto="assembly")
    return root


@pytest.fixture
def synth_episode(synth_root, tmp_path):
    """A private, writable copy of the synthetic episode for one test."""
    from ave.episode import Episode
    dst = tmp_path / "episode"
    shutil.copytree(synth_root, dst)
    return Episode(dst)


LAYOUTS = ["flat", "numbered"]


@pytest.fixture(scope="session")
def layout_roots(tmp_path_factory):
    """Pristine synthetic episodes (stage 'assembly') for each layout, built lazily and once per session."""
    cache: dict[str, Path] = {}

    def get(layout: str) -> Path:
        if layout not in cache:
            root = tmp_path_factory.mktemp(f"synth-{layout}") / "episode"
            fixtures.build_episode(root, upto="assembly", layout=layout)
            cache[layout] = root
        return cache[layout]
    return get


@pytest.fixture(params=LAYOUTS)
def layout_episode(request, layout_roots, tmp_path):
    """A private writable copy of the synthetic episode in the flat layout AND the numbered layout (the test runs twice)."""
    from ave.episode import Episode
    dst = tmp_path / "episode"
    shutil.copytree(layout_roots(request.param), dst)
    return Episode(dst)


@pytest.fixture(scope="session")
def ms_roots(tmp_path_factory):
    """Pristine multi-session episodes (2 sessions x 2 cameras + one stereo recorder with a dead channel per session), built once per
    stage on request: 'media' (ingested, analysis audio), 'sync' (+ real sync.json), 'map' (+ cameras/speakers)."""
    cache: dict[str, Path] = {}

    def get(upto: str) -> Path:
        if upto not in cache:
            root = tmp_path_factory.mktemp(f"ms-{upto}") / "episode"
            fixtures.build_multisession(root, upto=upto)
            cache[upto] = root
        return cache[upto]
    return get


@pytest.fixture
def ms_copy(ms_roots, tmp_path):
    """ms_copy('map') -> a private writable copy of the multi-session episode at that stage ('media' | 'sync' | 'map')."""
    from ave.episode import Episode
    n = [0]

    def make(upto: str = "map"):
        n[0] += 1
        dst = tmp_path / f"episode{n[0]}"
        shutil.copytree(ms_roots(upto), dst)
        return Episode(dst)
    return make


@pytest.fixture(autouse=True)
def _isolated_show_config(tmp_path_factory, monkeypatch):
    """Never read the developer's real taste profile or clip-reason overrides: each test gets empty ones."""
    d = tmp_path_factory.mktemp("showcfg")
    monkeypatch.setenv("AVE_TASTE_DIR", str(d / "taste"))
    monkeypatch.setenv("AVE_CONFIG_DIR", str(d / "cfg"))


@pytest.fixture(autouse=True)
def _fast_title_overlay(request, monkeypatch):
    """The title opening renders a motion template in Chromium (seconds per call). Tests that are not about the picture of the
    title get a tiny transparent ProRes .mov of the right size and length instead. Mark a test `real_motion` to render for real."""
    if request.node.get_closest_marker("real_motion"):
        return
    import subprocess
    from ave import opening

    def fake(ep, props, duration, canvas, fps):
        import json
        import hashlib
        key = hashlib.sha256(json.dumps([props, duration, list(canvas), fps], sort_keys=True).encode()).hexdigest()[:10]
        rel = ep.work.relative_to(ep.root) / "motion" / f"short-title-stub-{key}.mov"
        target = ep.root / rel
        if not target.exists():
            target.parent.mkdir(parents=True, exist_ok=True)
            w, h = canvas
            subprocess.run(["ffmpeg", "-v", "error", "-y", "-f", "lavfi", "-i", f"color=c=black:s={w}x{h}:r={fps}:d={duration}",
                            "-vf", "format=rgba,colorchannelmixer=aa=0,format=yuva444p10le", "-c:v", "prores_ks", "-profile:v", "4444", "-pix_fmt", "yuva444p10le", str(target)], check=True)
        return rel
    monkeypatch.setattr(opening, "render_overlay", fake)


@pytest.fixture(autouse=True)
def _fake_resolve_mcp(tmp_path_factory, monkeypatch):
    """Handoff tests run the Resolve script through a stub; they only need `resolve_mcp_python()` to find *something*. Without this they
    passed only on machines that happen to have the MCP server installed (CI and fresh clones do not)."""
    d = tmp_path_factory.mktemp("fake-resolve-mcp")
    (d / "venv/bin").mkdir(parents=True)
    (d / "venv/bin/python").write_text("")
    monkeypatch.setenv("AVE_RESOLVE_MCP", str(d))
