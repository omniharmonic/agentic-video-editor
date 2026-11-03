"""Studio accounts (spec 2.5): `accounts/<id>.yaml`, parsed and validated. NO secret VALUES ever live here.

A secret is named `<platform>_<field>@<ref>` (`instagram_token@ig-main`); `ave.publish.safety.get_secret` resolves it from the env
(`AVE_INSTAGRAM_TOKEN_IG_MAIN`) and then the OS keychain. Non-secret identifiers an adapter needs (an Instagram user id, a Bluesky handle,
a Pinterest board id) may sit in the YAML under `credentials:` next to `ref:`; everything else under `credentials:` is refused so a token
pasted there cannot be saved.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

from ..publish import safety
from ..studio import model

MODES = ("graph", "aggregator", "inbox", "manual", "direct", "native")
# `credentials:` keys that are plain identifiers, not secrets. Anything else there is refused.
PUBLIC_FIELDS = ("ref", "handle", "pds", "ig_user_id", "threads_user_id", "page_id", "author", "board_id", "sandbox", "user_id", "did", "channel_id", "client_id")


class AccountError(ValueError):
    """The account file is missing, malformed, or holds something that looks like a secret."""


def secret_name(platform: str, field_: str, ref: str | None) -> str:
    """`bluesky_app_password@bsky-main`. Without a ref (an unbound adapter) the legacy un-suffixed name."""
    return f"{platform}_{field_}@{ref}" if ref else f"{platform}_{field_}"


@dataclass(frozen=True)
class Account:
    id: str
    platform: str
    mode: str = "graph"
    label: str = ""
    tz: str = "UTC"
    post_types: tuple[str, ...] = ()
    cred_ref: str | None = None
    public: dict = field(default_factory=dict)         # non-secret ids from credentials:
    limits: dict = field(default_factory=dict)
    quiet_hours: tuple = ()
    catch_up: bool = False
    audited: bool = False
    connection: dict = field(default_factory=dict)
    approval: dict = field(default_factory=dict)
    accounts_dir: Path | None = None                   # where accounts/<id>.live.json lives; None = unbound (never live)

    # ---- construction
    @classmethod
    def from_config(cls, cfg: dict, accounts_dir: Path | None = None) -> "Account":
        problems = model.validate("account", cfg)
        creds = cfg.get("credentials") or {}
        bad = [k for k in creds if k not in PUBLIC_FIELDS]
        if bad:
            problems.append(f"credentials may only hold {', '.join(PUBLIC_FIELDS)}; remove {', '.join(map(str, bad))} "
                            "(secrets go in the keychain: `ave studio account auth`)")
        problems += _secret_like(cfg)
        if problems:
            raise AccountError(f"account {cfg.get('id', '?')!r}: " + "; ".join(problems))
        public = {k: v for k, v in creds.items() if k != "ref"}
        return cls(id=cfg["id"], platform=cfg["platform"], mode=cfg.get("mode") or "graph", label=str(cfg.get("label") or ""),
                   tz=cfg.get("tz") or "UTC", post_types=tuple(cfg.get("post_types") or ()), cred_ref=creds.get("ref") or cfg["id"],
                   public=public, limits=dict(cfg.get("limits") or {}), quiet_hours=tuple(cfg.get("quiet_hours") or ()),
                   catch_up=bool(cfg.get("catch_up", False)), audited=bool(cfg.get("audited", False)),
                   approval=dict(cfg.get("approval") or {}), connection=dict(cfg.get("connection") or {}), accounts_dir=accounts_dir)

    @classmethod
    def unbound(cls, platform: str) -> "Account":
        """An account that is not any studio account: always dry-run (never live), legacy un-suffixed secret names."""
        return cls(id=f"unbound-{platform}", platform=platform, mode="graph", cred_ref=None)

    @property
    def bound(self) -> bool:
        return self.accounts_dir is not None

    @property
    def studio_root(self) -> Path | None:
        return self.accounts_dir.parent if self.accounts_dir else None

    # ---- credentials (values are looked up on demand and never stored on the object)
    def secret_name(self, field_: str) -> str:
        return secret_name(self.platform, field_, self.cred_ref if self.bound else None)

    def secret(self, field_: str) -> str | None:
        return safety.get_secret(self.secret_name(field_))

    def credential(self, field_: str) -> str | None:
        """A public id from the YAML if given there, else the secret of that name (some people keep ids in the keychain too)."""
        v = self.public.get(field_)
        return str(v) if v not in (None, "") else self.secret(field_)

    def missing(self, fields: tuple[str, ...]) -> list[str]:
        return [self.secret_name(f) for f in fields if not self.credential(f)]

    # ---- live switch
    def live_status(self):
        from . import attest
        if not self.bound:
            return attest.LiveStatus(False, "not a studio account (unbound adapters are always dry-run)")
        return attest.verify_live(self.accounts_dir, self.id)

    def live_ok(self) -> bool:
        return self.live_status().ok

    def paused(self) -> bool:
        return bool(self.accounts_dir and ((self.accounts_dir / f"{self.id}.paused").exists() or (self.accounts_dir.parent / "PAUSE").exists()))


def _secret_like(cfg: Any) -> list[str]:
    """Strings in the account file that match a token pattern (safety._PATTERNS). The file must name secrets, never hold them."""
    out: list[str] = []

    def walk(v: Any, path: str) -> None:
        if isinstance(v, dict):
            for k, x in v.items():
                walk(x, f"{path}.{k}" if path else str(k))
        elif isinstance(v, list):
            for i, x in enumerate(v):
                walk(x, f"{path}[{i}]")
        elif isinstance(v, str) and safety.redact(v) != v:
            out.append(f"{path} looks like a secret; accounts/*.yaml must not hold credentials")

    walk(cfg, "")
    return out


# --------------------------------------------------------------------------- files

_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,40}$")


def account_path(sp, account_id: str) -> Path:
    if not _ID.match(account_id or ""):
        raise AccountError(f"bad account id {account_id!r}")
    return sp.accounts_dir / f"{account_id}.yaml"


def load_account(sp, account_id: str) -> Account:
    p = account_path(sp, account_id)
    try:
        cfg = yaml.safe_load(p.read_text())
    except OSError:
        raise AccountError(f"no account {account_id!r} (expected {p})")
    except yaml.YAMLError as e:
        raise AccountError(f"{p.name} is not valid YAML: {e}")
    if not isinstance(cfg, dict):
        raise AccountError(f"{p.name} must hold a mapping")
    if cfg.get("id") != account_id:
        raise AccountError(f"{p.name}: id {cfg.get('id')!r} does not match the file name")
    return Account.from_config(cfg, sp.accounts_dir)


def list_accounts(sp) -> list[Account]:
    """Every valid account; a broken file is skipped here and reported by `check_accounts`."""
    out = []
    for p in sorted(sp.accounts_dir.glob("*.yaml")) if sp.accounts_dir.is_dir() else []:
        try:
            out.append(load_account(sp, p.stem))
        except AccountError:
            continue
    return out


def check_accounts(sp) -> dict[str, str]:
    """{account file stem: problem} for each broken account file (doctor)."""
    bad = {}
    for p in sorted(sp.accounts_dir.glob("*.yaml")) if sp.accounts_dir.is_dir() else []:
        try:
            load_account(sp, p.stem)
        except AccountError as e:
            bad[p.stem] = str(e)
    return bad


def save_account(sp, cfg: dict) -> Account:
    """Validate then write accounts/<id>.yaml. Refuses secrets. Does not touch the live attestation."""
    acct = Account.from_config(cfg, sp.accounts_dir)
    sp.accounts_dir.mkdir(parents=True, exist_ok=True)
    account_path(sp, acct.id).write_text(yaml.safe_dump(cfg, sort_keys=False, allow_unicode=True))
    return acct


_BINARY = (".png", ".jpg", ".jpeg", ".mp4", ".mov", ".gif", ".webp", ".sqlite", ".pdf")


def find_secrets(root: Path, *, max_bytes: int = 1_000_000) -> list[str]:
    """Doctor helper: files under `root` that contain a token-shaped value (safety._PATTERNS). Names the file, never the value."""
    hits = []
    for p in sorted(Path(root).rglob("*")):
        if not p.is_file() or p.stat().st_size > max_bytes or p.suffix.lower() in _BINARY:
            continue
        try:
            text = p.read_text(errors="ignore")
        except OSError:
            continue
        if safety.redact(text) != text:
            hits.append(str(p))
    return hits
