"""Safety primitives for publishing: file hashing, approval signatures, secrets, and log redaction.

WHY this module exists: publishing posts to a real person's public accounts. The only things standing between an
agent and a public post are (1) an approval bound to the exact bytes and metadata the human reviewed, (2) credentials
that never touch the repo or logs, and (3) redaction as a last line of defence. Keep these small and testable.
"""

from __future__ import annotations

import datetime
import hashlib
import hmac
import json
import logging
import os
import re
import secrets
import time
from pathlib import Path
from .._verification_cancel import check as _check_verification_cancelled
from typing import Any

SERVICE = "ave-publish"
APPROVAL_KEY_NAME = "approval_key"
_CHUNK = 1024 * 1024
_MIN_SECRET_LEN = 6

# Secret values seen this process (from keyring/env). Anything that equals one of them is masked in output.
_KNOWN_SECRETS: set[str] = set()

_PATTERNS = [
    # a presigned object-store URL IS a credential for its lifetime: mask the whole URL, not just its signature
    re.compile(r"(?i)\bhttps?://[^\s\"'<>]*[?&]X-Amz-(?:Signature|Credential|Security-Token)=[^\s\"'<>]*"),
    re.compile(r"(?i)(\b(?:x-amz-(?:signature|credential|security-token)|signature|credential)=)[^\s&\"'#,}]+"),
    re.compile(r"(?i)(bearer|oauth|basic)\s+[A-Za-z0-9._~+/=-]{8,}"),
    re.compile(r"(?i)((?:access_token|refresh_token|client_secret|api[_-]?key|token|password|secret|authorization)"
               r"[\"']?\s*[:=]\s*[\"']?)[^\s\"'&,}]+"),
    re.compile(r"(?i)([?&;]\s*(?:key|api_?key|apikey|sig|signature|auth|access_key)=)[^\s&\"'#,}]+"),   # generic query-string credentials
    re.compile(r"(?i)((?:set-)?cookie\s*[:=]\s*)[^\r\n]+"),                                      # Cookie / Set-Cookie header values (whole line)
    re.compile(r"\bya29\.[A-Za-z0-9._-]{10,}"),          # Google access tokens
    re.compile(r"\b1//[A-Za-z0-9._-]{20,}"),              # Google refresh tokens
    re.compile(r"\bEAA[A-Za-z0-9]{20,}"),                 # Meta access tokens
    re.compile(r"\bGOCSPX-[A-Za-z0-9_-]{10,}"),           # Google client secrets
]
MASK = "[REDACTED]"


# --------------------------------------------------------------------------- hashing

def sha256_file(path: str | Path) -> str:
    """Streaming SHA-256 of a file (videos are gigabytes; never read them whole)."""
    _check_verification_cancelled()
    h = hashlib.sha256()
    with open(path, "rb") as f:
        while True:
            _check_verification_cancelled()
            chunk = f.read(_CHUNK)
            if not chunk:
                break
            h.update(chunk)
    _check_verification_cancelled()
    return h.hexdigest()


def canonical(obj: Any) -> str:
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


META_FIELDS = ("platforms", "title", "description", "tags", "caption", "schedule", "visibility", "ai_disclosure",
               "made_for_kids", "format")


# meta_v 2 (studio items): a retime must not void a human's approval, so `schedule` is NOT bound; the account, the post type, the
# first comment and the link ARE (a human reviewed which account says what). Items without `meta_v` hash exactly META_FIELDS, forever.
META_FIELDS_V2 = ("platforms", "accounts", "account", "post_type", "title", "description", "tags", "caption", "first_comment",
                  "link_url", "visibility", "ai_disclosure", "made_for_kids", "format")


def meta_hash(item: dict) -> str:
    """Hash of everything a human reviews besides the file. Editing the title after approval must invalidate it.

    Image and carousel items also bind their alt texts (a human reviewed what the image is described as) and their media type.
    A video item hashes exactly the fields it always did, so approvals made before images existed stay valid.
    An item with `meta_v == 2` (studio) hashes META_FIELDS_V2 instead: the schedule is excluded; account and copy extras are bound."""
    fields = {k: item.get(k) for k in (META_FIELDS_V2 if item.get("meta_v") in (2, 3) else META_FIELDS)}
    if (item.get("media_type") or "video") != "video":
        fields["media_type"] = item["media_type"]
        fields["alt_texts"] = item.get("alt_texts")
    if item.get("meta_v") == 3:
        fields.update({k: item.get(k) for k in ("delivery", "cover", "presentation")})
        fields["meta_v"] = 3
    return hashlib.sha256(canonical(fields).encode()).hexdigest()


def media_sha256(paths: list[str | Path], media_type: str = "video") -> str:
    """The hash an approval is bound to. One file (video or image): that file's sha256, so a human can check it with `shasum -a 256`.
    A carousel: the sha256 of the ORDERED list of every slide's sha256, so changing, adding, removing or reordering any slide
    changes it."""
    shas = [sha256_file(p) for p in paths]
    if media_type == 'text':
        if shas: raise ValueError('text items have no media')
        return hashlib.sha256(b'ave:text:v1').hexdigest()
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
    return isinstance(k, str) and bool(re.search(r"(?i)(token|secret|password|api_?key|authorization|cookie)", k))


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


_KEY_CACHE: dict = {}
_FALLBACK_WARNED = False
_log = logging.getLogger("ave.publish.safety")


def reset_approval_key_cache() -> None:
    """Forget the per-process key (tests that swap the keyring or HOME call this; production never needs to)."""
    global _FALLBACK_WARNED
    _KEY_CACHE.clear()
    _FALLBACK_WARNED = False


def _cache_id():
    try:
        import keyring
        kr = (keyring.get_password, keyring.set_password)
    except ImportError:
        kr = (None, None)
    return (os.environ.get("AVE_APPROVAL_KEY"), os.environ.get("HOME"), *kr)


def _created_record(kf: Path) -> Path:
    return kf.with_name(kf.name + ".created")


def _file_key() -> str:
    """The file fallback, used only when the keychain is unusable. It is NEVER trusted blindly: a key file that already exists must look
    like one WE made (owner is this user, mode 0600, 64 hex characters, and a creation record holding its sha256), else we refuse: a planted
    file would otherwise become the HMAC key and let whoever planted it forge approvals."""
    global _FALLBACK_WARNED
    kf, rec = _key_file(), _created_record(_key_file())
    if kf.exists():
        st = kf.stat()
        v = kf.read_text().strip()
        problems = []
        if st.st_uid != os.getuid():
            problems.append("it is not owned by you")
        if st.st_mode & 0o077:
            problems.append(f"its mode is {st.st_mode & 0o777:o}, not 600")
        if not re.fullmatch(r"[0-9a-f]{64}", v):
            problems.append("it is not a 64-hex key")
        try:
            made = json.loads(rec.read_text())
        except (OSError, ValueError):
            made = {}
        if made.get("created_by") != "ave" or made.get("sha256") != hashlib.sha256(v.encode()).hexdigest():
            problems.append("it has no matching creation record (it was not created by ave)")
        if problems:
            raise RuntimeError(f"refusing to use the approval key file {kf}: " + "; ".join(problems) +
                               ". Delete it only if you are sure it is yours (that invalidates every approval made with it), or make the "
                               "OS keychain usable.")
    else:
        kf.parent.mkdir(parents=True, exist_ok=True)
        v = secrets.token_hex(32)
        fd = os.open(kf, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, "w") as f:
            f.write(v + "\n")
        fd = os.open(rec, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w") as f:
            f.write(json.dumps({"created_by": "ave", "at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
                                "sha256": hashlib.sha256(v.encode()).hexdigest()}) + "\n")
    if not _FALLBACK_WARNED:
        _FALLBACK_WARNED = True
        _log.warning("the OS keychain is not usable: the approval HMAC key is read from %s. That file is readable by anything running as "
                     "you, so it protects against accidents and hand edits, not against a hostile program.", kf)
    return v


_KEYS_FORBIDDEN = False


def forbid_approval_keys() -> None:
    """Irreversible per-process guard for delegated mechanical episode workers."""
    global _KEYS_FORBIDDEN
    _KEYS_FORBIDDEN = True


def approval_key() -> bytes:
    """HMAC key for approval records, kept in the keychain (or env). Hand-editing a queue JSON to add an approval
    then fails the signature check; forging one requires deliberately reading the key.

    Read ONCE per process (a keychain read can raise a macOS dialog; repeated prompts teach people to click "Always Allow"). The file under
    ~/.config/ave is a last resort for a machine with no usable keychain, validated by `_file_key`, and announced once."""
    if _KEYS_FORBIDDEN:raise RuntimeError('Mechanical episode workers cannot access approval keys')
    cid = _cache_id()
    hit = _KEY_CACHE.get(cid)
    if hit is not None:
        return hit
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
        v = _file_key()
    register_secret(v)
    key = v.encode()
    _KEY_CACHE.clear()
    _KEY_CACHE[cid] = key
    return key


def sign_approval(item_id: str, file_sha: str, meta_sha: str, by: str, at: str) -> str:
    msg = "|".join([item_id, file_sha, meta_sha, by, at]).encode()
    return hmac.new(approval_key(), msg, hashlib.sha256).hexdigest()


def renewal_signature(item_id: str, approval: dict) -> str:
    """Explicit v2 approval identity. The legacy sign_approval domain is unchanged."""
    body={k:approval.get(k) for k in ('by','at','sha256','meta_sha256','approval_v','nonce','renewed_from')}
    return hmac.new(approval_key(),('approval-renewal-v2|'+item_id+'|'+canonical(body)).encode(),hashlib.sha256).hexdigest()


def make_renewed_approval(item: dict, file_sha: str, by: str, at: str, previous: str) -> dict:
    approval=make_approval(item,file_sha,by,at)
    approval.update(approval_v=2,nonce=secrets.token_hex(32),renewed_from=previous)
    approval['sig']=renewal_signature(item['id'],approval)
    return approval


def make_approval(item: dict, file_sha: str, by: str, at: str) -> dict:
    m = meta_hash(item)
    return {"by": by, "at": at, "sha256": file_sha, "meta_sha256": m,
            "sig": sign_approval(item["id"], file_sha, m, by, at)}


def norm_slot(value: Any) -> str | None:
    """A schedule (ISO string, with or without offset/Z) as a canonical UTC string, or None."""
    if not value:
        return None
    try:
        t = datetime.datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None
    if t.tzinfo is None:
        t = t.replace(tzinfo=datetime.timezone.utc)
    return t.astimezone(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def sign_bind(approval_sig: str, bind: dict) -> str:
    return hmac.new(approval_key(), ("bind|" + approval_sig + "|" + canonical(bind)).encode(), hashlib.sha256).hexdigest()


def attach_bind(approval: dict, item: dict, extra: dict | None = None) -> dict:
    """Studio items (`meta_v` 2): sign, together with the approval, WHAT the human approved beyond bytes and copy: the account, the slot
    (the schedule is deliberately not in the metadata hash, so a retime does not void an approval; this lets the sender tell a sanctioned
    retime from a hand edit) and the approval-lifetime policy in force (so raising a TTL afterwards is visible)."""
    bind = {"account": item.get("account"), "slot": norm_slot(item.get("schedule")),
            "ttl": {"publish_days": approval_ttl_days()}, **(extra or {})}
    if extra and "ttl" in extra:
        bind["ttl"] = {"publish_days": approval_ttl_days(), **extra["ttl"]}
    bind["ttl_hash"] = hashlib.sha256(canonical(bind["ttl"]).encode()).hexdigest()[:16]        # the approval-lifetime policy in force, as a fingerprint
    approval["bind"] = bind
    approval["bind_sig"] = sign_bind(approval["sig"], bind)
    return approval


def bind_problem(approval: dict) -> str | None:
    """None when the approval's binding is present and genuine, else why not."""
    bind = approval.get("bind")
    if not isinstance(bind, dict):
        return "the approval carries no binding (account, slot, lifetime policy); approve it again"
    if not hmac.compare_digest(str(approval.get("bind_sig", "")), sign_bind(str(approval.get("sig", "")), bind)):
        return "the approval's binding signature is invalid (edited by hand?)"
    return None


DEFAULT_APPROVAL_TTL_DAYS = 14


def approval_ttl_days() -> float:
    """`publish.approval_ttl_days` from ~/.config/ave/publish.yaml (a human-owned file), else config/publish.default.yaml, else 14."""
    import yaml

    from .. import resources
    paths = [Path(os.environ.get("AVE_CONFIG_DIR", Path(os.environ.get("HOME", "~")).expanduser() / ".config/ave")) / "publish.yaml"]
    try:
        paths.append(resources.config_file("publish.default.yaml"))
    except Exception:
        pass
    for p in paths:
        try:
            v = ((yaml.safe_load(Path(p).read_text()) or {}).get("publish") or {}).get("approval_ttl_days")
            if isinstance(v, (int, float)) and not isinstance(v, bool) and v > 0:
                return float(v)
        except Exception:
            continue
    return float(DEFAULT_APPROVAL_TTL_DAYS)


def approval_age_days(at: str, now: float | None = None) -> float | None:
    try:
        t = datetime.datetime.strptime(at, "%Y-%m-%dT%H:%M:%S%z").timestamp()
    except (ValueError, TypeError):
        return None
    return ((time.time() if now is None else now) - t) / 86400.0


def approval_problems(item: dict, current_file_sha: str, now: float | None = None) -> list[str]:
    """Why this item may NOT be sent right now. Empty list means an unexpired, matching, signed approval exists.

    Checks: a latest approval exists with a valid signature; it is younger than `publish.approval_ttl_days` (default 14);
    the approved file hash equals the file's hash NOW; the approved metadata hash equals the metadata now.
    """
    problems: list[str] = []
    if item.get("status") not in ("approved", "publishing", "failed"):
        problems.append(f"status is {item.get('status')!r}, not approved")
    approvals = item.get("approvals") or []
    if not approvals:
        return problems + ["no approval recorded"]
    a = approvals[-1]
    if 'approval_v' in a:
        if a.get('approval_v')!=2 or not all(isinstance(a.get(k),str) and re.fullmatch(r'[0-9a-f]{64}',a[k]) for k in ('nonce','renewed_from')):
            problems.append('invalid renewed approval identity')
        expect=renewal_signature(item['id'],a)
    else:
        if any(k in a for k in ("nonce","renewed_from")):
            problems.append("renewal metadata requires explicit approval_v=2")
        expect = sign_approval(item["id"], a.get("sha256", ""), a.get("meta_sha256", ""), a.get("by", ""), a.get("at", ""))
    if not a.get("by") or not hmac.compare_digest(str(a.get("sig", "")), expect):
        problems.append("approval signature invalid (queue file was edited by hand?)")
    age = approval_age_days(str(a.get("at", "")), now)
    ttl = approval_ttl_days()
    if age is None:
        problems.append("approval has no readable timestamp; approve again")
    elif age > ttl:
        problems.append(f"approval expired ({age:.0f} days old, limit {ttl:g}); re-review and approve again")
    if a.get("sha256") != current_file_sha:
        problems.append(("media files" if (item.get("media_type") or "video") != "video" else "video file") +
                        " changed since approval (sha256 mismatch); re-review and approve again")
    if a.get("meta_sha256") != meta_hash(item):
        problems.append("title/description/platforms/etc. changed since approval; approve again")
    if item.get("sha256") != current_file_sha:
        problems.append("queued sha256 differs from the file on disk")
    return problems
