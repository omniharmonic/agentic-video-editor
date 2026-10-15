"""Safety primitives for publishing: file hashing, approval signatures, secrets, and log redaction.

WHY this module exists: publishing posts to a real person's public accounts. The only things standing between an
agent and a public post are (1) an approval bound to the exact bytes and metadata the human reviewed, (2) credentials
that never touch the repo or logs, and (3) redaction as a last line of defence. Keep these small and testable.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import logging
import os
import re
import secrets
from pathlib import Path
from typing import Any

SERVICE = "ave-publish"
APPROVAL_KEY_NAME = "approval_key"
_CHUNK = 1024 * 1024
_MIN_SECRET_LEN = 6

# Secret values seen this process (from keyring/env). Anything that equals one of them is masked in output.
_KNOWN_SECRETS: set[str] = set()

_PATTERNS = [
    re.compile(r"(?i)(bearer|oauth|basic)\s+[A-Za-z0-9._~+/=-]{8,}"),
    re.compile(r"(?i)((?:access_token|refresh_token|client_secret|api[_-]?key|token|password|secret|authorization)"
               r"[\"']?\s*[:=]\s*[\"']?)[^\s\"'&,}]+"),
    re.compile(r"\bya29\.[A-Za-z0-9._-]{10,}"),          # Google access tokens
    re.compile(r"\b1//[A-Za-z0-9._-]{20,}"),              # Google refresh tokens
    re.compile(r"\bEAA[A-Za-z0-9]{20,}"),                 # Meta access tokens
    re.compile(r"\bGOCSPX-[A-Za-z0-9_-]{10,}"),           # Google client secrets
]
MASK = "[REDACTED]"


# --------------------------------------------------------------------------- hashing

def sha256_file(path: str | Path) -> str:
    """Streaming SHA-256 of a file (videos are gigabytes; never read them whole)."""
    h = hashlib.sha256()
    with open(path, "rb") as f:
        while chunk := f.read(_CHUNK):
            h.update(chunk)
    return h.hexdigest()


def canonical(obj: Any) -> str:
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


META_FIELDS = ("platforms", "title", "description", "tags", "caption", "schedule", "visibility", "ai_disclosure",
               "made_for_kids", "format")


def meta_hash(item: dict) -> str:
    """Hash of everything a human reviews besides the file. Editing the title after approval must invalidate it.

    Image and carousel items also bind their alt texts (a human reviewed what the image is described as) and their media type.
    A video item hashes exactly the fields it always did, so approvals made before images existed stay valid."""
    fields = {k: item.get(k) for k in META_FIELDS}
    if (item.get("media_type") or "video") != "video":
        fields["media_type"] = item["media_type"]
        fields["alt_texts"] = item.get("alt_texts")
    return hashlib.sha256(canonical(fields).encode()).hexdigest()


def media_sha256(paths: list[str | Path], media_type: str = "video") -> str:
    """The hash an approval is bound to. One file (video or image): that file's sha256, so a human can check it with `shasum -a 256`.
    A carousel: the sha256 of the ORDERED list of every slide's sha256, so changing, adding, removing or reordering any slide
    changes it."""
    shas = [sha256_file(p) for p in paths]
    if media_type == "carousel":
        return hashlib.sha256(canonical(shas).encode()).hexdigest()
    if len(shas) != 1:
        raise ValueError(f"a {media_type} item has exactly one file (got {len(shas)}); use media_type carousel for several")
    return shas[0]


# --------------------------------------------------------------------------- secrets

def _env_name(name: str) -> str:
    return "AVE_" + re.sub(r"[^A-Za-z0-9]", "_", name).upper()


def register_secret(value: str | None) -> None:
    if value and len(value) >= _MIN_SECRET_LEN:
        _KNOWN_SECRETS.add(value)


def _register_nested(value: str) -> None:
    register_secret(value)
    try:
        data = json.loads(value)
    except Exception:
        return
    if isinstance(data, dict):
        for v in data.values():
            if isinstance(v, str):
                register_secret(v)


def get_secret(name: str) -> str | None:
    """Environment variable AVE_<NAME> first, then the OS keychain. Never reads files in the repo."""
    v = os.environ.get(_env_name(name))
    if not v:
        try:
            import keyring
            v = keyring.get_password(SERVICE, name)
        except Exception:
            v = None
    if v:
        _register_nested(v)
    return v or None


def set_secret(name: str, value: str) -> None:
    """Store in the OS keychain only. Raises if no keychain is usable rather than falling back to a file."""
    import keyring
    keyring.set_password(SERVICE, name, value)
    _register_nested(value)


def has_secret(name: str) -> bool:
    return get_secret(name) is not None


# --------------------------------------------------------------------------- redaction

def redact(text: Any) -> str:
    s = text if isinstance(text, str) else str(text)
    for secret in sorted(_KNOWN_SECRETS, key=len, reverse=True):
        s = s.replace(secret, MASK)
    for pat in _PATTERNS:
        if pat.groups:
            s = pat.sub(lambda m: m.group(1) + MASK, s)
        else:
            s = pat.sub(MASK, s)
    return s


def redact_obj(obj: Any) -> Any:
    """Redact every string inside a JSON-like structure. Used before anything is persisted or printed."""
    if isinstance(obj, str):
        return redact(obj)
    if isinstance(obj, dict):
        return {k: (MASK if _sensitive_key(k) and isinstance(v, str) else redact_obj(v)) for k, v in obj.items()}
    if isinstance(obj, (list, tuple)):
        return [redact_obj(v) for v in obj]
    return obj


def _sensitive_key(k: Any) -> bool:
    return isinstance(k, str) and bool(re.search(r"(?i)(token|secret|password|api_?key|authorization)", k))


class RedactingFilter(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        record.msg = redact(record.getMessage())
        record.args = ()
        return True


def get_logger(name: str) -> logging.Logger:
    """A logger whose records are redacted before any handler sees them."""
    log = logging.getLogger(name)
    if not any(isinstance(f, RedactingFilter) for f in log.filters):
        log.addFilter(RedactingFilter())
    return log


# --------------------------------------------------------------------------- approval signatures

def _key_file() -> Path:
    return Path(os.environ.get("HOME", "~")).expanduser() / ".config" / "ave" / "approval.key"


def approval_key() -> bytes:
    """HMAC key for approval records, kept in the keychain (or env). Hand-editing a queue JSON to add an approval
    then fails the signature check; forging one requires deliberately reading the key."""
    v = os.environ.get("AVE_APPROVAL_KEY")
    if not v:
        try:
            import keyring
            v = keyring.get_password(SERVICE, APPROVAL_KEY_NAME)
            if not v:
                v = secrets.token_hex(32)
                keyring.set_password(SERVICE, APPROVAL_KEY_NAME, v)
        except Exception:
            v = None
    if not v:
        kf = _key_file()
        if kf.exists():
            v = kf.read_text().strip()
        else:
            kf.parent.mkdir(parents=True, exist_ok=True)
            v = secrets.token_hex(32)
            fd = os.open(kf, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            with os.fdopen(fd, "w") as f:
                f.write(v + "\n")
    register_secret(v)
    return v.encode()


def sign_approval(item_id: str, file_sha: str, meta_sha: str, by: str, at: str) -> str:
    msg = "|".join([item_id, file_sha, meta_sha, by, at]).encode()
    return hmac.new(approval_key(), msg, hashlib.sha256).hexdigest()


def make_approval(item: dict, file_sha: str, by: str, at: str) -> dict:
    m = meta_hash(item)
    return {"by": by, "at": at, "sha256": file_sha, "meta_sha256": m,
            "sig": sign_approval(item["id"], file_sha, m, by, at)}


def approval_problems(item: dict, current_file_sha: str) -> list[str]:
    """Why this item may NOT be sent right now. Empty list means an unexpired, matching, signed approval exists.

    Checks: a latest approval exists with a valid signature; the approved file hash equals the file's hash NOW;
    the approved metadata hash equals the metadata now.
    """
    problems: list[str] = []
    if item.get("status") not in ("approved", "publishing", "failed"):
        problems.append(f"status is {item.get('status')!r}, not approved")
    approvals = item.get("approvals") or []
    if not approvals:
        return problems + ["no approval recorded"]
    a = approvals[-1]
    expect = sign_approval(item["id"], a.get("sha256", ""), a.get("meta_sha256", ""), a.get("by", ""), a.get("at", ""))
    if not a.get("by") or not hmac.compare_digest(str(a.get("sig", "")), expect):
        problems.append("approval signature invalid (queue file was edited by hand?)")
    if a.get("sha256") != current_file_sha:
        problems.append(("media files" if (item.get("media_type") or "video") != "video" else "video file") +
                        " changed since approval (sha256 mismatch); re-review and approve again")
    if a.get("meta_sha256") != meta_hash(item):
        problems.append("title/description/platforms/etc. changed since approval; approve again")
    if item.get("sha256") != current_file_sha:
        problems.append("queued sha256 differs from the file on disk")
    return problems
