import json

import pytest
from publish_helpers import *  # noqa: F401,F403
from publish_helpers import RecordingPublisher

from ave.cli import main
from ave.publish import queue, safety


def run_cli(capsys, *argv):
    rc = main(list(argv))
    cap = capsys.readouterr()
    return rc, cap.out, cap.err


@pytest.fixture
def fakes(monkeypatch):
    made = {}

    def gp(name, **kw):
        made.setdefault(name, RecordingPublisher(name))
        return made[name]
    monkeypatch.setattr(queue, "get_publisher", gp)
    monkeypatch.setattr("ave.commands.publish.get_publisher", gp)
    return made


def test_full_flow_dry_run_default_then_yes(ep, wide, capsys, fakes, monkeypatch):
    rc, out, _ = run_cli(capsys, "publish", "queue", str(ep.root), "--video", str(wide), "--platforms", "youtube",
                         "--title", "Ep 1", "--ai-disclosure", "no")
    item = json.loads(out)[0]
    assert rc == 0 and item["status"] == "awaiting_approval"
    iid = item["id"]
    # approve non-interactively without a hash confirmation is refused
    with pytest.raises(SystemExit, match="needs a human"):
        main(["publish", "approve", str(ep.root), iid])
    # an agent must not be able to approve its own post: --confirm-sha is dead without the explicit unattended opt-in
    monkeypatch.delenv("AVE_ALLOW_UNATTENDED_APPROVAL", raising=False)
    with pytest.raises(SystemExit, match="--confirm-sha is disabled"):
        main(["publish", "approve", str(ep.root), iid, "--confirm-sha", item["sha256"][:10]])
    assert queue.load(ep, iid)["status"] == "awaiting_approval"        # the refused attempt changed nothing
    monkeypatch.setenv("AVE_ALLOW_UNATTENDED_APPROVAL", "1")      # CI/test opt-in only
    with pytest.raises(SystemExit, match="does not match"):
        main(["publish", "approve", str(ep.root), iid, "--confirm-sha", "deadbeefdead"])
    rc, out, _ = run_cli(capsys, "publish", "approve", str(ep.root), iid, "--by", "ben", "--confirm-sha", item["sha256"][:10])
    assert rc == 0 and "approved" in out
    # default run is a dry run
    rc, out, err = run_cli(capsys, "publish", "run", str(ep.root), iid)
    assert rc == 0 and json.loads(out)["mode"] == "dry-run" and "DRY RUN" in err
    assert fakes["youtube"].publish_calls == 0
    # --dry-run wins over --yes
    run_cli(capsys, "publish", "run", str(ep.root), iid, "--yes", "--dry-run")
    assert fakes["youtube"].publish_calls == 0
    rc, out, _ = run_cli(capsys, "publish", "run", str(ep.root), iid, "--yes")
    assert rc == 0 and fakes["youtube"].publish_calls == 1
    rc, out, _ = run_cli(capsys, "publish", "run", str(ep.root), iid, "--yes")
    assert fakes["youtube"].publish_calls == 1
    rc, out, _ = run_cli(capsys, "publish", "list", str(ep.root))
    assert json.loads(out)[0]["status"] == "published"


def test_run_without_approval_exits_nonzero(ep, wide, capsys, fakes):
    _, out, _ = run_cli(capsys, "publish", "queue", str(ep.root), "--video", str(wide), "--platforms", "youtube", "--title", "t",
                        "--ai-disclosure", "no")
    iid = json.loads(out)[0]["id"]
    rc, out, _ = run_cli(capsys, "publish", "run", str(ep.root), iid, "--yes")
    assert rc == 2 and fakes["youtube"].publish_calls == 0


def test_unknown_platform_and_missing_args(ep, wide, capsys):
    with pytest.raises(SystemExit, match="unknown platform"):
        main(["publish", "queue", str(ep.root), "--video", str(wide), "--platforms", "myspace", "--title", "t"])
    with pytest.raises(SystemExit, match="--video and --title"):
        main(["publish", "queue", str(ep.root), "--platforms", "youtube"])


def test_status_shows_readiness_but_no_secrets(ep, capsys, monkeypatch):
    monkeypatch.setenv("AVE_FACEBOOK_PAGE_TOKEN", "EAASECRETTOKEN1234567890xyz")
    rc, out, _ = run_cli(capsys, "publish", "status", str(ep.root))
    assert rc == 0 and "EAASECRET" not in out and "youtube" in out


def test_stage_publish_detects_queue(ep, wide, capsys, fakes):
    from ave import state
    assert state._done(ep, "publish") is False
    run_cli(capsys, "publish", "queue", str(ep.root), "--video", str(wide), "--platforms", "youtube", "--title", "t", "--ai-disclosure", "no")
    assert state._done(ep, "publish") is True


def test_auth_facebook_stores_in_keychain_only(ep, capsys, monkeypatch, isolated_secrets):
    monkeypatch.setattr("getpass.getpass", lambda prompt="": "EAAPAGETOKEN1234567890abc")
    rc, out, _ = run_cli(capsys, "publish", "auth", "facebook", "--page-id", "999")
    assert rc == 0 and "EAAPAGE" not in out
    assert isolated_secrets.d[("ave-publish", "facebook_page_token")] == "EAAPAGETOKEN1234567890abc"


def test_no_secret_or_queue_files_in_repo_config():
    """Guard: nothing credential-shaped is committed in config/ or docs/."""
    from pathlib import Path
    import re
    root = Path(__file__).parents[1]
    pat = re.compile(r"(ya29\.|EAA[A-Za-z0-9]{20}|GOCSPX-|1//[A-Za-z0-9_-]{30}|-----BEGIN)")
    for d in ("config", "docs", "skills"):
        for f in (root / d).rglob("*"):
            if f.is_file() and f.suffix in (".yaml", ".yml", ".md", ".json"):
                assert not pat.search(f.read_text()), f
