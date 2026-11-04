import os
import re
import shutil
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent))
import fixtures  # noqa: E402

needs_ffmpeg = pytest.mark.skipif(not shutil.which("ffmpeg"), reason="ffmpeg not installed")
# Shared CI runners are slower and have no hardware video encoder: time budgets and long waits stretch by this much there.
CI_SLOW = 3 if os.environ.get("CI") else 1


def _ffmpeg_has_libass() -> bool:
    if not shutil.which("ffmpeg"):
        return False
    from ave import ffutil
    return ffutil.has_filter("ass")


# Burned-in captions need the `ass` filter (libass). Homebrew's plain `ffmpeg` formula is built without it; `brew install ffmpeg-full` has it.
needs_libass = pytest.mark.skipif(not _ffmpeg_has_libass(), reason="this ffmpeg has no libass (`ass` filter); see `ave doctor`")


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
def _approval_age_follows_fake_time(request, monkeypatch):
    """Fixtures approve at FakeClock time and the 14-day check reads the real clock, so they would expire by calendar date."""
    if "studio" not in request.path.parts:      # elsewhere the real clock is the point, and FakeClock.latest may be another test's
        return
    import time
    from datetime import datetime
    from ave.publish import safety
    from studio_helpers import FakeClock
    age = safety.approval_age_days

    def aged(at, now=None):
        fake = FakeClock.latest
        if now is None and fake is not None:
            try:
                when = datetime.fromisoformat(str(at).replace("Z", "+00:00"))
                if when <= fake.now() or when.timestamp() < time.time() - 3600:     # a test may wind its clock back past the approval
                    now = max(fake.now(), when).timestamp()
            except (TypeError, ValueError):
                pass
        return age(at, now)
    monkeypatch.setattr(safety, "approval_age_days", aged)


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


@pytest.fixture
def human(monkeypatch):
    """Pretend a person is at a terminal (the one seam: ave.humanpresence.is_human_terminal) for tests of human-only commands."""
    from ave import humanpresence
    monkeypatch.setattr(humanpresence, "is_human_terminal", lambda: True)


# ---- optional extras --------------------------------------------------------------------------------------------------
# `setup.sh --minimal` installs only the core dependencies. A test that needs an optional extra (OpenCV, librosa, Playwright,
# keyring + the Google client) then reports a SKIP naming the extra, instead of failing. The full suite needs `uv sync --all-extras`.
# Only a genuinely missing module is converted: any other failure in these tests is still a failure.
_EXTRAS = {"cv2": "vision", "librosa": "audio", "playwright": "motion", "keyring": "publish", "httplib2": "publish",
           "googleapiclient": "publish", "google_auth_oauthlib": "publish", "requests": "publish"}


def _missing_extra(exc: BaseException) -> str | None:
    import importlib.util
    if isinstance(exc, ModuleNotFoundError):
        mod = (exc.name or "").split(".")[0]
    elif isinstance(exc, SystemExit) and "Playwright is not installed" in str(exc.code):
        mod = "playwright"
    elif isinstance(exc, SystemExit) and (m := re.search(r"No module named '([\w.]+)'", str(exc.code))):
        mod = m.group(1).split(".")[0]      # a command that wrapped the ImportError in its own message
    else:
        return None
    if mod in _EXTRAS and importlib.util.find_spec(mod) is None:
        return f"needs the '{_EXTRAS[mod]}' extra ({mod} not installed): uv sync --extra {_EXTRAS[mod]}, or --all-extras"
    return None


def _skip_when_extra_missing():
    try:
        yield
    except (ModuleNotFoundError, SystemExit) as e:
        why = _missing_extra(e)
        if why is None:
            raise
        pytest.skip(why)


@pytest.hookimpl(wrapper=True)
def pytest_runtest_setup(item):
    return (yield from _skip_when_extra_missing())


@pytest.hookimpl(wrapper=True)
def pytest_runtest_call(item):
    return (yield from _skip_when_extra_missing())


# Modules whose MODULE-scoped fixtures render through Playwright/Chromium. pytest itself trips over a module fixture that dies in setup
# (an internal assertion on re-setup), so these are skipped up front rather than converted after the fact.
_NEEDS_PLAYWRIGHT_MODULES = {"test_content_byline", "test_content_render", "test_sequence"}


def pytest_collection_modifyitems(config, items):
    import importlib.util
    if importlib.util.find_spec("playwright") is not None:
        return
    skip = pytest.mark.skip(reason="needs the 'motion' extra (playwright not installed): uv sync --extra motion, or --all-extras")
    for item in items:
        if item.module.__name__.split(".")[-1] in _NEEDS_PLAYWRIGHT_MODULES:
            item.add_marker(skip)


@pytest.fixture(autouse=True)
def _never_touch_the_real_keychain(request, monkeypatch):
    """No test may read or write the developer's login keychain (approval key, platform tokens): macOS answers a failed write from a
    background or parallel process with a "a keychain cannot be found to store ..." dialog, repeatedly. An in-memory keyring
    stands in, and a fixed test approval key is set unless a test sets its own."""
    if "isolated_secrets" in request.fixturenames:      # tests that bring their own keyring stand-in keep it
        return
    from ave.publish import safety as _safety
    _safety.reset_approval_key_cache()
    store: dict = {}
    try:
        import keyring
    except ImportError:
        keyring = None
    if keyring is not None:
        monkeypatch.setattr(keyring, "get_password", lambda svc, name: store.get((svc, name)))
        monkeypatch.setattr(keyring, "set_password", lambda svc, name, v: store.__setitem__((svc, name), v))
        monkeypatch.setattr(keyring, "delete_password", lambda svc, name: store.pop((svc, name), None))
    monkeypatch.setenv("AVE_APPROVAL_KEY", "test-approval-key-not-secret")
