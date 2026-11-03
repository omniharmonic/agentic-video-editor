from pathlib import Path

from browser_cache import playwright_cache_dir

HOME = Path("/home/runner")


def test_linux_uses_dot_cache_never_the_macos_library_path():
    assert playwright_cache_dir(HOME, "linux", {}) == HOME / ".cache/ms-playwright"
    assert "Library" not in str(playwright_cache_dir(HOME, "linux", {}))
    assert playwright_cache_dir(HOME, "linux", {"XDG_CACHE_HOME": "/x"}) == Path("/x/ms-playwright")


def test_macos_uses_library_caches_and_the_env_override_wins():
    assert playwright_cache_dir(Path("/Users/a"), "darwin", {}) == Path("/Users/a/Library/Caches/ms-playwright")
    assert playwright_cache_dir(HOME, "linux", {"PLAYWRIGHT_BROWSERS_PATH": "/b"}) == Path("/b")
