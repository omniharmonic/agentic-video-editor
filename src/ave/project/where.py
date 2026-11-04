"""Where a project file is, in plain words, for the Workspace's Sources view.

The zone rules (zones.py) decide whether a file syncs, and each member's media choice ("full" or "proxies") decides whether a
raw footage file reaches that member's computer or only its review proxy does. This module turns those two facts into the
words the person reads: "Shared with Tomas", "Only on this computer", "Proxies only on Tomas's computer", and, on a computer
that keeps proxies only, "Proxy on this computer; original on Maren's computer". People are named, never their device ids.

    w = Where.of(root, device_id, person)    # reads .ave/project.json and the layout once (zones.project_policy)
    w.describe("raw/a.mov")                  # {"zone": "permanent", "text": "Shared with Tomas", "detail": "...", "people": [...]}
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path, PurePosixPath

from .zones import PROXY_DIRNAME, Rule, project_policy, rules, zone_of

DETAIL = {
    "permanent": "Kept as it is: new work is saved as a new file, so copies never disagree.",
    "settings": "Shared settings: if two people change it while apart, both are kept and you choose.",
    "log": "Part of the project's shared history.",
    "local": "Never leaves this computer.",
}
RULE_NOTE = " This is what the project's sharing rules say; the sync status shows whether a copy has arrived yet."


def names(people: list[str]) -> str:
    """"Tomas", "Tomas and Sam", "Tomas, Sam and Lee"."""
    people = [p for p in people if p]
    if len(people) <= 1:
        return "".join(people)
    return ", ".join(people[:-1]) + " and " + people[-1]


def possessive(people: list[str]) -> str:
    """"Tomas's computer", "Tomas's and Sam's computers"."""
    owned = [f"{p}'" if p.endswith("s") else f"{p}'s" for p in people if p]
    return f"{names(owned)} computer{'s' if len(owned) > 1 else ''}"


@dataclass
class Where:
    layout: dict
    studio: bool
    mine: list[Rule]
    media: str                    # this computer's own choice: "full" or "proxies" (an unlisted computer keeps proxies)
    others: list[dict]            # the other people on the project: {"person", "media"}, one entry per person
    raw_dirs: list[str]

    @classmethod
    def of(cls, root: Path, device_id: str | None = None, person: str | None = None) -> "Where":
        """`person` is who this computer belongs to (Project.person). On a computer not listed yet (a second Mac before it is
        added), that person is still "you", never someone the file is shared with."""
        pol = project_policy(Path(root), device_id)
        own = (pol.me or {}).get("media")
        media = own if own in ("full", "proxies") else ("full" if pol.me is not None and own is None else "proxies")
        me_person = str((pol.me or {}).get("person") or person or "")
        others: dict[str, dict] = {}
        for m in pol.members:
            who = str(m.get("person") or "")
            if not who or m is pol.me or who == me_person:
                continue
            choice = m.get("media", "full") if m.get("media", "full") in ("full", "proxies") else "proxies"
            prev = others.get(who)                    # one person on two computers: they have it if either computer does
            others[who] = {"person": who, "media": "full" if choice == "full" or (prev and prev["media"] == "full") else "proxies"}
        raw = [str(PurePosixPath(d)).strip("/") for d in pol.layout["raw_dirs"]]
        return cls(layout=pol.layout, studio=pol.studio, mine=rules(pol.layout, {"media": media}, studio=pol.studio), media=media,
                   others=list(others.values()), raw_dirs=raw)

    def _raw_rel(self, rel: str) -> tuple[str, str] | None:
        """(raw dir, path inside it) when `rel` is original footage (not a proxy), else None."""
        for d in self.raw_dirs:
            if rel == d or not rel.startswith(d + "/"):
                continue
            inner = rel[len(d) + 1:]
            if inner.split("/")[0] == PROXY_DIRNAME:
                return None
            return d, inner
        return None

    def proxy_of(self, rel: str) -> str | None:
        """The project-relative path of the review proxy of a raw footage file (proxies.proxy_path's rule), else None."""
        raw = self._raw_rel(rel)
        if raw is None:
            return None
        d, inner = raw
        return f"{d}/{PROXY_DIRNAME}/{PurePosixPath(inner).with_suffix('.mov')}"

    def original_dir_of(self, proxy_rel: str) -> tuple[str, str] | None:
        """For a proxy `<raw>/proxies/<sub>/<name>.mov`: (`<raw>/<sub>`, the original's stem), else None."""
        for d in self.raw_dirs:
            pre = f"{d}/{PROXY_DIRNAME}/"
            if proxy_rel.startswith(pre):
                inner = PurePosixPath(proxy_rel[len(pre):])
                parent = f"{d}/{inner.parent}".rstrip("/.") if str(inner.parent) != "." else d
                return parent, inner.stem
        return None

    def describe(self, rel: str, *, has_proxy: bool | None = None) -> dict:
        """Who else has this file, in words. `has_proxy` says whether its review proxy exists (only asked for raw footage)."""
        rel = str(PurePosixPath(rel.replace("\\", "/"))).strip("/")
        zone = zone_of(rel, self.mine)
        base = {"zone": zone, "detail": DETAIL.get(zone, DETAIL["local"]), "people": []}
        if zone == "local":
            return {**base, "text": "Only on this computer", "reach": "local"}
        if not self.others:
            return {**base, "text": "Not shared yet", "reach": "solo",
                    "detail": "Only you are on this project. It is shared with the people you add, as soon as you add them."}
        full, proxies, missing = [], [], []
        for m in self.others:
            theirs = rules(self.layout, {"media": m["media"]}, studio=self.studio)
            if zone_of(rel, theirs) != "local":
                full.append(m["person"])
            elif m["media"] == "proxies" and self._raw_rel(rel) is not None:
                (proxies if has_proxy is not False else missing).append(m["person"])
            else:
                missing.append(m["person"])
        parts = []
        if full:
            parts.append(f"Shared with {names(full)}")
        if proxies:
            parts.append(f"Proxies only on {possessive(proxies)}")
        if missing:
            parts.append(f"Not on {possessive(missing)}" + (": no proxy yet" if has_proxy is False else ""))
        reach = "shared" if full and not (proxies or missing) else "proxies" if proxies and not missing else "partial" if full or proxies else "none"
        people = [{"person": p, "has": "all"} for p in full] + [{"person": p, "has": "proxy"} for p in proxies] + \
                 [{"person": p, "has": "none"} for p in missing]
        detail = base["detail"]
        if proxies or missing:
            who = names(proxies + missing)
            detail += f" {who} {'keeps' if len(proxies + missing) == 1 else 'keep'} review proxies plus what they work on, not every original."
        return {**base, "text": " · ".join(parts), "reach": reach, "people": people, "detail": detail + RULE_NOTE}

    def describe_proxy(self, proxy_rel: str) -> dict:
        """A review proxy on a computer that keeps proxies only, standing in for an original that is not here."""
        holders = [m["person"] for m in self.others if m["media"] == "full"]
        text = f"Proxy on this computer; original on {possessive(holders)}" if holders else "Proxy on this computer; the original is on no one else's computer"
        people = [{"person": p, "has": "all"} for p in holders] + [{"person": m["person"], "has": "proxy"} for m in self.others if m["media"] != "full"]
        return {"zone": zone_of(proxy_rel, self.mine), "text": text, "reach": "proxy-here", "people": people,
                "detail": "This computer keeps review proxies plus what you work on, not every original. The proxy is a smaller copy for "
                          "watching and reviewing." + RULE_NOTE}
