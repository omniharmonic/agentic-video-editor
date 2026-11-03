"""One small seam for every human-only CLI act (approve a post, approve an opening/closing plan, set a trim, apply taste weights).

`require_human(action, run_line)` refuses unless a person plausibly is at the keyboard:

  * stdin AND stdout are both a terminal, and
  * none of the agent markers is set in the environment (`CLAUDECODE`, `CLAUDE_CODE_ENTRYPOINT`, `AVE_AGENT`).

HONEST LIMIT. This is a speed bump and a clear message, NOT a security boundary. An agent that can run shell commands as the same
OS user can spawn a pty (`script`, `expect`, `python -c "import pty"`) and unset the markers, and it can read the HMAC approval key.
The real control is the harness: permission deny rules for these commands (see SECURITY.md and `.claude/settings.example.json`).
The human runs them in their own terminal window. The harness's `! ave ...` escape runs inside the agent session, which carries the agent markers and no real tty, so it is refused too.

Tests monkeypatch `is_human_terminal` (the single named seam) or call the library function (`ave.publish.queue.approve`, ...) directly.
"""

from __future__ import annotations

import os
import sys

AGENT_MARKERS = ("CLAUDECODE", "CLAUDE_CODE_ENTRYPOINT", "AVE_AGENT")


def agent_markers_set() -> list[str]:
    return [k for k in AGENT_MARKERS if os.environ.get(k)]


def is_human_terminal() -> bool:
    """True when both ends are a terminal and no agent marker is present. The one function tests replace."""
    try:
        tty = sys.stdin.isatty() and sys.stdout.isatty()
    except (AttributeError, ValueError):
        tty = False
    return tty and not agent_markers_set()


def provenance() -> str:
    """`tty` or `notty`, recorded on decisions that stay open to agents (clips/content decide) so the ledger shows who really typed it."""
    return "tty" if is_human_terminal() else "notty"


def require_human(action: str, run_line: str) -> None:
    """Exit with the exact `! ave ...` line for the human unless a human terminal is present."""
    if is_human_terminal():
        return
    why = ("an agent marker is set in the environment (" + ", ".join(agent_markers_set()) + "); run it in your own terminal, not through the agent"
           if agent_markers_set() else "stdin and stdout must both be a terminal")
    raise SystemExit(f"{action} is a human act and was not run: {why}.\n"
                     f"Ask the human to run this in their own terminal window (a separate Terminal tab, not the agent's `!` prefix, which carries the agent markers):\n  {run_line}")
