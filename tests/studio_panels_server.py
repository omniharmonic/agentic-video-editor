"""A fixture-backed stand-in for the studio server, for developing and testing the WS6 panels (spec 11, WS5/WS6 "develop against committed JSON").

It serves, on 127.0.0.1 only:
  /static/<file>          src/ave/studio/ui/static/ (the panels), falling back to src/ave/ui/static/ (style.css, the shared tokens)
  /harness/<file>         tests/studio_ui_harness/ (a minimal hash-router shell that mounts the panels; the real shell is WS5's)
  /files/<path>           generated placeholder images sized from the asset records (no binary fixtures in git)
  /api/studio/...         the 7.3 endpoints, answering with the SHAPES committed in tests/data_studio/api/*.json, but stateful in memory:
                          compare-and-swap edits (409 + latest), swap_asset demotes approval, feedback lines, view events, PIN approval,
                          library upload by sha, B-roll search/pick.
  /__test/...             agent_edit (an edit "by Claude" that bumps rev), requests (the log), reset

It is test scaffolding, not the product server (WS4 owns that). Where it must guess a detail the fixtures do not show, the guess is listed in
HARNESS_ASSUMPTIONS and in the WS6 report so WS4 can confirm or correct it.
"""

from __future__ import annotations

import copy
import hashlib
import io
import json
import threading
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from PIL import Image, ImageDraw

ROOT = Path(__file__).resolve().parents[1]
API = ROOT / "tests" / "data_studio" / "api"
STATIC = ROOT / "src" / "ave" / "studio" / "ui" / "static"
BASE_STATIC = ROOT / "src" / "ave" / "ui" / "static"
HARNESS = ROOT / "tests" / "studio_ui_harness"
PIN = "123456"
VIEW_MS = 2000

HARNESS_ASSUMPTIONS = [
    "POST approve with a wrong PIN answers 403 {code: 'pin.wrong', attempts_left}; after 5 wrong PINs 423 {code: 'pin.locked'}",
    "POST approve without a view event refuses the ITEM (200 with refused[]), as in POST_approve.json",
    "POST posts/<id>/view answers 400 {code: 'view.too_short'} under 2000 ms and 409 {code: 'view.sha_mismatch'} for another sha",
    "POST posts/<id>/feedback takes {variant, kinds[], note, structured, rev?, asset?} and answers {feedback[], post}, as in POST_feedback.json",
    "GET summary may carry approval_mode ('pin_after_review' | 'typed')",
    "broll candidates carry a local `thumb` path once the server has cached the preview (preview_url is never loaded by the browser)",
    "POST alerts/<id>/ack exists (the spec lists no route for it; the page falls back to the CLI line on 404)",
]


def _load(name: str) -> dict:
    return json.loads((API / f"{name}.json").read_text())["response"]


def _now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _sha(seed: str) -> str:
    return hashlib.sha256(seed.encode()).hexdigest()


def _asset(aid: str, w: int, h: int, *, kind="image", ext=".png", alt="", source_type="own", provider=None, license_ok=True, tags=(), usage=()) -> dict:
    sha = _sha(aid)
    rec = {
        "v": 1, "id": aid, "sha256": sha, "kind": kind, "ext": ext, "file": f"library/assets/{sha[:2]}/{sha}{ext}", "bytes": 120000 + w, "w": w, "h": h,
        "duration": None, "fps": None, "source": {"type": source_type, "provider": provider, "asset_id": None, "url": None},
        "license": {"entry": "c" * 64 if license_ok else None, "summary": "Pexels License" if provider == "pexels" else "own work" if license_ok else "none",
                    "attribution": {"required": False, "text": None}},
        "ai_generated": False, "derived_from": [], "tags": list(tags), "alt": alt, "people": {"faces": 0, "release": None}, "origin": None,
        "added": "2026-10-19T09:00:00-0600", "added_by": "claude", "usage": list(usage), "license_ok": license_ok,
    }
    rec["url"] = "/files/" + rec["file"]
    return rec


def make_state() -> dict:
    posts = {p["id"]: p for p in copy.deepcopy(_load("GET_posts")["posts"])}
    lib = {a["id"]: a for a in copy.deepcopy(_load("GET_library")["assets"])}
    # the fixture's two assets keep their ids/urls; more shapes for the grid crop and safe zones
    for rec in (
        _asset("a_square000001", 1080, 1080, alt="A loaf on a board, square.", tags=["bread"], usage=[]),
        _asset("a_tall00000001", 1080, 1920, alt="Flour on a dark counter, vertical.", tags=["flour"]),
        _asset("a_wide00000001", 1200, 628, alt="The bakery window at dawn.", tags=["shop"]),
        _asset("a_nolicense001", 1080, 1350, alt="", source_type="stock", provider="pexels", license_ok=False, tags=["stock"]),
    ):
        lib[rec["id"]] = rec
    posts["p_h_social"] = _social_post()
    return {
        "posts": posts, "library": lib, "feedback": copy.deepcopy(_load("GET_feedback")["feedback"]), "events": copy.deepcopy(_load("GET_post")["events"]), "viewed": {}, "wrong_pins": 0,
        "alerts": copy.deepcopy(_load("GET_alerts")["alerts"]), "accounts": copy.deepcopy(_load("GET_accounts")["accounts"]), "paused": False,
        "requests": [], "fb_seq": 2, "search": copy.deepcopy(_load("GET_broll")), "mode": "pin_after_review", "uploads": 0,
    }


def _variant(vid, account, platform, ptype, *, caption=None, status="in_review", media=None):
    return {
        "id": vid, "account": account, "platform": platform, "post_type": ptype, "copy": {"caption": caption, "first_comment": None, "hashtags": None}, "media": media,
        "schedule": {"local": "2026-11-05T09:00", "tz": "America/Denver", "utc": "2026-11-05T16:00:00Z", "slot": "r1", "locked": False, "set_by": "agent"},
        "queue": {"root": ".", "id": f"q-{vid}"}, "queue_sha": _sha("q-" + vid), "flags": [], "attempts": [], "result": None, "dropped": False,
        "status": status, "status_reason": "awaiting the human's approval", "queue_status": "awaiting_approval", "approval": None,
    }


def _social_post() -> dict:
    base = ("Day 3: the starter smells like pears, and that is normal. It means the wild yeast is waking up and doing its slow, honest work. "
            "Keep feeding it every twelve hours, keep it warm, and do not panic about the smell. \U0001F35E\U0001F468‍\U0001F469‍\U0001F467 日本語のテスト")
    return {
        "v": 1, "kind": "studio.post", "id": "p_h_social", "rev": 1, "stage": "active", "campaign": None,
        "idea": {"text": "Starter day 3, everywhere", "origin": {"type": "idea", "ref": None, "passages": []}, "reason": "harness", "confidence": 0.5},
        "copy": {"base": base, "link": "https://acme.example/starter", "first_comment": "", "hashtags": ["sourdough", "bread"], "cta": ""},
        "media": [{"slot": 0, "asset": "a_square000001", "role": "image", "alt": "A loaf on a board, square.", "crop": {"grid_focus": [0.5, 0.5]}},
                  {"slot": 1, "asset": "a_wide00000001", "role": "image", "alt": "", "crop": None}],
        "variants": [
            _variant("v_x", "x-main", "x", "text"), _variant("v_bsky", "bsky-main", "bluesky", "text"), _variant("v_th", "threads-main", "threads", "text"),
            _variant("v_li", "li-main", "linkedin", "feed"), _variant("v_tt", "tt-main", "tiktok", "video", media=[{"slot": 0, "asset": "a_tall00000001", "role": "image", "alt": "Flour.", "crop": None}]),
            _variant("v_fb2", "fb-page", "facebook", "feed"), _variant("v_ig3", "ig-main", "instagram", "feed", media=[{"slot": 0, "asset": "a_square000001", "role": "image", "alt": "Loaf.", "crop": {"grid_focus": [0.1, 0.9]}}]),
            _variant("v_story", "ig-main", "instagram", "story", media=[{"slot": 0, "asset": "a_tall00000001", "role": "image", "alt": "Flour.", "crop": None}]),
        ],
        "disclosure": {"ai_generated": False}, "created": "2026-10-19T10:15:00-0600", "updated": "2026-10-19T10:15:00-0600", "by": "claude",
    }


class Harness:
    def __init__(self):
        self.state = make_state()
        self.lock = threading.RLock()
        self.server: ThreadingHTTPServer | None = None
        self.thread: threading.Thread | None = None
        self.url = ""

    def reset(self):
        with self.lock:
            self.state = make_state()

    # ---- domain ---------------------------------------------------------------------------------------------------------------------
    def find_variant(self, post, vid):
        return next((v for v in post["variants"] if v["id"] == vid), None)

    def event(self, actor, post, variant, op, detail=None):
        self.state["events"].append({"ts": _now(), "actor": {"kind": actor, "name": "maren" if actor == "human" else "claude"}, "post": post["id"], "variant": variant,
                                     "op": op, "rev": post["rev"], "before_sha": None, "after_sha": None, "detail": detail or {}})

    @staticmethod
    def _set(obj, path, value):
        parts = path.split(".")
        cur = obj
        for p in parts[:-1]:
            if isinstance(cur, list):
                nxt = next((x for x in cur if isinstance(x, dict) and x.get("id") == p), None)
                cur = nxt if nxt is not None else cur[int(p)]
            else:
                if cur.get(p) is None:
                    cur[p] = {}
                cur = cur[p]
        last = parts[-1]
        if isinstance(cur, list):
            cur[int(last)] = value
        else:
            cur[last] = value

    @staticmethod
    def _get(obj, path):
        cur = obj
        for p in path.split("."):
            if isinstance(cur, list):
                nxt = next((x for x in cur if isinstance(x, dict) and x.get("id") == p), None)
                cur = nxt if nxt is not None else cur[int(p)]
            else:
                cur = cur.get(p)
        return cur

    def demote(self, post, variant, why):
        if variant["queue_status"] == "approved":
            variant["queue_status"] = "awaiting_approval"
            variant["approval"] = None
            variant["status"] = "in_review"
            variant["status_reason"] = why
            variant["queue_sha"] = _sha(variant["queue_sha"] + why)
            self.event("human", post, variant["id"], "approval_invalidated", {"why": why})

    def edit(self, post_id, body, actor="human"):
        post = self.state["posts"][post_id]
        if body.get("expected_rev") != post["rev"]:
            return 409, {"error": f"post {post_id} is at rev {post['rev']}, not {body.get('expected_rev')}", "code": "conflict.rev", "latest": copy.deepcopy(post)}
        touched = set()
        for path, value in body["patch"].items():
            before = self._get(post, path)
            self._set(post, path, value)
            if actor == "human":
                self.state["fb_seq"] += 1
                self.state["feedback"].append({"v": 1, "op": "feedback", "id": f"fb_{self.state['fb_seq']:04d}", "ts": _now(), "post": post_id, "variant": path.split(".")[1] if path.startswith("variants.") else None,
                                               "rev": post["rev"] + 1, "asset": None, "kind": "human_edit", "note": None, "structured": {"copy": {"field": path, "before": before, "after": value, "selection": None}},
                                               "by": "maren", "source": "ui", "resolved": {"by": "maren", "ts": _now()}, "claim": None})
            if path.startswith("variants."):
                touched.add(path.split(".")[1])
            else:
                touched.update(v["id"] for v in post["variants"])
        post["rev"] += 1
        for vid in touched:
            v = self.find_variant(post, vid)
            if v:
                self.demote(post, v, "the words changed, so it needs a fresh look")
        self.event(actor, post, None, "edit", {"paths": sorted(body["patch"])})
        return 200, {"post": copy.deepcopy(post)}

    def swap(self, post_id, body):
        post = self.state["posts"][post_id]
        if body.get("expected_rev") != post["rev"]:
            return 409, {"error": "stale", "code": "conflict.rev", "latest": copy.deepcopy(post)}
        v = self.find_variant(post, body["variant"])
        new = self.state["library"].get(body["asset"])
        if not v or not new:
            return 404, {"error": "unknown variant or asset", "code": "not_found"}
        media = copy.deepcopy(v["media"] if v["media"] else post["media"])
        old = media[body["index"]]
        media[body["index"]] = {**old, "asset": new["id"], "alt": old.get("alt") or new.get("alt") or ""}
        v["media"] = media
        post["rev"] += 1
        self.demote(post, v, f"the image changed ({old['asset']} to {new['id']})")
        self.event("human", post, v["id"], "swap_asset", {"from": old["asset"], "to": new["id"]})
        return 200, {"post": copy.deepcopy(post)}

    def feedback(self, post_id, body):
        post = self.state["posts"][post_id]
        v = self.find_variant(post, body.get("variant"))
        lines = []
        for kind in body.get("kinds", []):
            self.state["fb_seq"] += 1
            lines.append({"v": 1, "op": "feedback", "id": f"fb_{self.state['fb_seq']:04d}", "ts": _now(), "post": post_id, "variant": body.get("variant"), "rev": post["rev"],
                          "asset": body.get("asset"), "kind": kind, "note": body.get("note"), "structured": {k: val for k, val in (body.get("structured") or {}).items() if k == {"rework_copy": "copy", "rework_visual": "visual", "retime": "retime", "drop": "drop"}[kind]},
                          "by": "maren", "source": "ui", "resolved": None, "claim": None})
        self.state["feedback"].extend(lines)
        if v:
            if "drop" in body.get("kinds", []):
                v.update(dropped=True, status="dropped", status_reason="dropped by the human", queue_status="rejected", approval=None)
            else:
                v.update(status="needs_rework", status_reason="open rework feedback", queue_status="awaiting_approval", approval=None)
            post["rev"] += 1
            self.event("human", post, v["id"], "feedback", {"kinds": body.get("kinds")})
        return 201, {"feedback": lines, "post": copy.deepcopy(post)}

    def view(self, post_id, body):
        post = self.state["posts"][post_id]
        v = self.find_variant(post, body["variant"])
        if body.get("displayed_ms", 0) < VIEW_MS:
            return 400, {"error": "shown for less than 2 seconds", "code": "view.too_short"}
        if body["sha"] != v["queue_sha"]:
            return 409, {"error": "the bytes changed", "code": "view.sha_mismatch"}
        self.state["viewed"][(post_id, body["variant"])] = body["sha"]
        return 200, {"ok": True, "viewed": {"post": post_id, "variant": body["variant"], "sha": body["sha"], "at": _now()}}

    def approve(self, body):
        if self.state["wrong_pins"] >= 5:
            return 423, {"error": "approval is locked until the server restarts", "code": "pin.locked"}
        if self.state["mode"] != "typed" and body.get("pin") != PIN:
            self.state["wrong_pins"] += 1
            return 403, {"error": "wrong PIN", "code": "pin.wrong", "attempts_left": max(0, 5 - self.state["wrong_pins"])}
        approved, refused = [], []
        for it in body["items"]:
            post = self.state["posts"].get(it["post"])
            v = self.find_variant(post, it["variant"]) if post else None
            if not v:
                refused.append({**it, "reason": "unknown variant"})
            elif self.state["viewed"].get((it["post"], it["variant"])) != it["sha"] or it["sha"] != v["queue_sha"]:
                refused.append({"post": it["post"], "variant": it["variant"], "reason": "not viewed: open the exact media for at least 2 seconds first"})
            else:
                v.update(queue_status="approved", status="scheduled" if v.get("schedule") else "approved", status_reason="approved, unexpired, has a slot",
                         approval={"by": body["by"], "at": _now()})
                post["rev"] += 1
                self.event("human", post, v["id"], "approved", {"source": "studio-ui"})
                approved.append({"post": it["post"], "variant": it["variant"], "queue_id": v["queue"]["id"], "sha": it["sha"], "by": body["by"], "at": _now(), "source": "studio-ui"})
        return 200, {"approved": approved, "refused": refused}

    def library_list(self, q):
        out = []
        for a in self.state["library"].values():
            if q.get("kind") and a["kind"] != q["kind"]:
                continue
            if q.get("source") and a["source"]["type"] != q["source"]:
                continue
            if q.get("license") == "ok" and not a["license_ok"] or q.get("license") == "missing" and a["license_ok"]:
                continue
            if q.get("tag") and q["tag"] not in a["tags"]:
                continue
            if q.get("q") and q["q"].lower() not in (" ".join(a["tags"]) + " " + a["alt"] + " " + a["id"]).lower():
                continue
            rec = copy.deepcopy(a)
            rec["usage"] = self.usage(a["id"])
            if q.get("unused") and rec["usage"]:
                continue
            out.append(rec)
        return {"assets": out, "total": len(out)}

    def usage(self, aid):
        use = []
        for p in self.state["posts"].values():
            for v in p["variants"]:
                media = v["media"] if v["media"] else p["media"]
                if any(m["asset"] == aid for m in media):
                    use.append({"post": p["id"], "variant": v["id"]})
        return use

    def preview(self, post_id, vid):
        post = self.state["posts"][post_id]
        v = self.find_variant(post, vid)
        media = v["media"] if v["media"] else post["media"]
        out = []
        for m in media:
            a = self.state["library"][m["asset"]]
            ratio = a["w"] / a["h"]
            keep_x = min(1.0, 0.75 / ratio)
            keep_y = min(1.0, ratio / 0.75)
            lost_x, lost_y = round((1 - keep_x) * 100), round((1 - keep_y) * 100)
            out.append({"asset": a["id"], "url": a["url"], "w": a["w"], "h": a["h"], "alt": m.get("alt") or "",
                        "crop_overlay": {"cell_aspect": "3:4", "lost": {"left_pct": lost_x // 2, "right_pct": lost_x - lost_x // 2, "top_pct": lost_y // 2, "bottom_pct": lost_y - lost_y // 2}}, "text_cut": False})
        caption = (v["copy"]["caption"] if v["copy"]["caption"] is not None else post["copy"]["base"])
        return {"post": post_id, "variant": vid, "platform": v["platform"], "kind": "profile_grid_cell", "approximate": True,
                "caption": {"text": caption, "fold_at": 125, "counter": {"unit": "chars", "used": len(caption), "limit": 2200}}, "media": out, "warnings": []}

    def broll_pick(self, body):
        cand = next((c for c in self.state["search"]["candidates"] if f"{c['provider']}:{c['asset_id']}" == body["candidate"]), None)
        if not cand or not body.get("by"):
            return 400, {"error": "a named human must approve the download", "code": "broll.approver"}
        rec = _asset("a_br" + cand["asset_id"].rjust(9, "0"), 1080, 1350, source_type="stock", provider=cand["provider"], alt=cand["title"], tags=cand["tags"])
        self.state["library"][rec["id"]] = rec
        return 201, {"asset": rec, "license_entry": "d" * 64}

    def upload(self, name, data):
        sha = hashlib.sha256(data).hexdigest()
        aid = "a_" + sha[:12]
        if aid in self.state["library"]:
            return 200, {"asset": self.state["library"][aid], "duplicate": True}
        try:
            im = Image.open(io.BytesIO(data))
            w, h = im.size
            ext = "." + (im.format or "png").lower().replace("jpeg", "jpg")
        except Exception:
            w = h = None
            ext = Path(name).suffix or ".bin"
        rec = _asset(aid, w or 0, h or 0, ext=ext, kind="image" if w else "document", source_type="import", tags=[Path(name).stem])
        rec["sha256"], rec["file"], rec["bytes"], rec["url"] = sha, f"library/assets/{sha[:2]}/{sha}{ext}", len(data), f"/files/library/assets/{sha[:2]}/{sha}{ext}"
        rec["alt"], rec["license"] = "", {"entry": None, "summary": "own work", "attribution": {"required": False, "text": None}}
        self.state["library"][aid] = rec
        self.state["uploads"] += 1
        return 201, {"asset": rec, "duplicate": False}

    def image_for(self, rel: str) -> bytes | None:
        for a in self.state["library"].values():
            if a["file"] == rel or a["url"] == "/" + rel or a["url"] == "/files/" + rel:
                return _render(a)
        if rel.startswith("work/broll/"):
            return _render({"id": rel, "w": 400, "h": 300, "tags": ["broll"]})
        return None

    # ---- routing --------------------------------------------------------------------------------------------------------------------
    def api(self, method, path, q, body, raw):
        s = self.state
        parts = path.split("/")                                  # after /api/studio/
        head = parts[0]
        if method == "GET":
            if head == "summary":
                sm = copy.deepcopy(_load("GET_summary"))
                sm["paused"], sm["approval_mode"] = s["paused"], s["mode"]
                return 200, sm
            if head == "connections":
                from ave.desktop_tools import fixture_connections
                return 200, fixture_connections(s["accounts"])
            if head == "accounts":
                return 200, {"accounts": s["accounts"]}
            if head == "campaigns":
                return 200, {"campaigns": [], "total": 0}
            if head == "alerts":
                return 200, {"alerts": s["alerts"]}
            if head == "changes":
                return 200, {"cursor": "ev:%d" % len(s["events"]), "events": []}
            if head == "library" and len(parts) == 1:
                return 200, self.library_list(q)
            if head == "library":
                a = s["library"].get(parts[1])
                if not a:
                    return 404, {"error": "no such asset", "code": "not_found"}
                covered = a["license_ok"]
                return 200, {"asset": {k: v for k, v in a.items() if k not in ("usage", "license_ok", "url")}, "usage": self.usage(a["id"]), "url": a["url"],
                             "license": {"covered": covered, "entries": [{"entry": a["license"]["entry"], "provider": a["source"]["provider"], "summary": a["license"]["summary"]}] if covered else [], "missing": [] if covered else [a["id"]]}}
            if head == "feedback":
                st = q.get("status", "open")
                lines = [f for f in s["feedback"] if f["kind"] != "human_edit"]
                if st == "open":
                    lines = [f for f in lines if not f["resolved"] and not f["claim"]]
                elif st == "claimed":
                    lines = [f for f in lines if f["claim"] and not f["resolved"]]
                else:
                    lines = [f for f in lines if f["resolved"]]
                return 200, {"feedback": lines}
            if head == "broll":
                return 200, s["search"]
            if head == "posts" and len(parts) == 1:
                posts = list(s["posts"].values())
                if q.get("status"):
                    posts = [p for p in posts if any(v["status"] == q["status"] for v in p["variants"])]
                if q.get("stage"):
                    posts = [p for p in posts if p["stage"] == q["stage"]]
                return 200, {"posts": copy.deepcopy(posts), "total": len(posts)}
            if head == "posts" and len(parts) == 2:
                p = s["posts"].get(parts[1])
                if not p:
                    return 404, {"error": "no such post", "code": "not_found"}
                queue = {v["id"]: {"id": v["queue"]["id"], "root": ".", "status": v["queue_status"], "sha256": v["queue_sha"], "approved_by": (v["approval"] or {}).get("by"),
                                   "approved_at": (v["approval"] or {}).get("at"), "expires_at": "2026-11-02T11:30:00-0600" if v["approval"] else None, "preflight_problems": []} for v in p["variants"]}
                fb = [f for f in s["feedback"] if f["post"] == p["id"] and not f["resolved"] and f["kind"] != "human_edit"]
                return 200, {"post": copy.deepcopy(p), "feedback": fb, "events": [e for e in s["events"] if e["post"] == p["id"]], "queue": queue}
            if head == "posts" and len(parts) == 4 and parts[2] == "preview":
                return 200, self.preview(parts[1], parts[3])
        if method == "POST":
            if head == "posts" and len(parts) == 3 and parts[1] in s["posts"]:
                pid, act = parts[1], parts[2]
                if act == "edit":
                    return self.edit(pid, body)
                if act == "media" and body.get("op") == "replace":
                    status,result=self.swap(pid, {**body,"index":int(body["item"].split("_")[-1])})
                    return self.api("GET",f"posts/{pid}",{},None,None) if status==200 else (status,result)
                if act == "swap_asset":
                    return self.swap(pid, body)
                if act == "feedback":
                    return self.feedback(pid, body)
                if act == "view":
                    return self.view(pid, body)
            if head == "approve":
                return self.approve(body)
            if head == "broll" and parts[1] == "search":
                return 201, {"search": copy.deepcopy(s["search"])}
            if head == "broll" and parts[1] == "pick":
                return self.broll_pick(body)
            if head == "pause":
                if body.get("account"):
                    for a in s["accounts"]:
                        if a["id"] == body["account"]:
                            a["paused"] = True
                else:
                    s["paused"] = True
                return 200, {"paused": True, "scope": "account" if body.get("account") else "studio"}
            if head == "resume":
                if body.get("pin") != PIN:
                    return 403, {"error": "wrong PIN", "code": "pin.wrong"}
                s["paused"] = False
                return 200, {"paused": False}
            if head == "alerts" and len(parts) == 3 and parts[2] == "ack":
                for a in s["alerts"]:
                    if a["id"] == parts[1]:
                        a["ack"] = {"by": "maren", "ts": _now()}
                return 200, {"ok": True}
        if method == "PUT" and head == "library" and parts[1] == "upload":
            return self.upload(q.get("name", "upload.bin"), raw)
        return 404, {"error": f"harness has no {method} {path}", "code": "not_found"}

    # ---- http -----------------------------------------------------------------------------------------------------------------------
    def start(self) -> str:
        outer = self

        class H(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def log_message(self, *a):  # quiet
                pass

            def _send(self, code, data: bytes, ctype: str, extra=None):
                self.send_response(code)
                self.send_header("Content-Type", ctype)
                self.send_header("Content-Length", str(len(data)))
                self.send_header("Cache-Control", "no-store")
                for k, v in (extra or {}).items():
                    self.send_header(k, v)
                self.end_headers()
                self.wfile.write(data)

            def _json(self, code, obj):
                self._send(code, json.dumps(obj).encode(), "application/json")

            def _file(self, root: Path, rel: str, fallback: Path | None = None):
                for base in (root, fallback):
                    if base is None:
                        continue
                    p = (base / rel).resolve()
                    if base.resolve() in p.parents and p.is_file():
                        ctype = {".js": "text/javascript", ".css": "text/css", ".html": "text/html; charset=utf-8", ".json": "application/json"}.get(p.suffix, "application/octet-stream")
                        return self._send(200, p.read_bytes(), ctype)
                self._send(404, b"not found", "text/plain")

            def do(self, method):
                u = urlparse(self.path)
                n = int(self.headers.get("Content-Length") or 0)
                raw = self.rfile.read(n) if n else b""
                with outer.lock:
                    outer.state["requests"].append({"method": method, "path": u.path, "query": u.query})
                    path = u.path
                    q = {k: v[0] for k, v in parse_qs(u.query).items()}
                    if path == "/":
                        self.send_response(302); self.send_header("Location", "/harness/index.html"); self.send_header("Content-Length", "0"); self.end_headers(); return
                    if path.startswith("/static/"):
                        return self._file(STATIC, path[len("/static/"):], BASE_STATIC)
                    if path.startswith("/harness/"):
                        return self._file(HARNESS, path[len("/harness/"):])
                    if path.startswith("/files/"):
                        img = outer.image_for(path[len("/files/"):])
                        return self._send(200, img, "image/png") if img else self._send(404, b"", "text/plain")
                    if path.startswith("/__test/"):
                        if path == "/__test/requests":
                            return self._json(200, outer.state["requests"])
                        if path == "/__test/state":
                            return self._json(200, {"posts": outer.state["posts"], "feedback": outer.state["feedback"], "events": outer.state["events"], "library": list(outer.state["library"]), "uploads": outer.state["uploads"]})
                        if path == "/__test/reset":
                            outer.reset()
                            return self._json(200, {"ok": True})
                        if path == "/__test/agent_edit":
                            body = json.loads(raw or b"{}")
                            post = outer.state["posts"][body["post"]]
                            code, res = outer.edit(body["post"], {"expected_rev": post["rev"], "patch": body["patch"]}, actor="agent")
                            return self._json(code, res)
                        if path == "/__test/mode":
                            outer.state["mode"] = json.loads(raw)["mode"]
                            return self._json(200, {"ok": True})
                    if path.startswith("/api/studio/"):
                        body = {}
                        if raw and method == "POST":
                            try:
                                body = json.loads(raw)
                            except ValueError:
                                return self._json(400, {"error": "bad JSON", "code": "bad_request"})
                        code, res = outer.api(method, path[len("/api/studio/"):].strip("/"), q, body, raw)
                        return self._json(code, res)
                    self._send(404, b"not found", "text/plain")

            do_GET = lambda self: self.do("GET")      # noqa: E731
            do_POST = lambda self: self.do("POST")    # noqa: E731
            do_PUT = lambda self: self.do("PUT")      # noqa: E731

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), H)
        self.server.daemon_threads = True
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.url = f"http://127.0.0.1:{self.server.server_address[1]}"
        return self.url

    def stop(self):
        if self.server:
            self.server.shutdown()
            self.server.server_close()


def _render(a: dict) -> bytes:
    """A deterministic placeholder picture: a warm gradient with the id and a plus at the centre of the frame (so crops are visible)."""
    w, h = a["w"], a["h"]
    k = min(1.0, 600 / max(w, h))
    w, h = max(8, int(w * k)), max(8, int(h * k))
    seed = int(hashlib.sha256(a["id"].encode()).hexdigest()[:6], 16)
    base = (200 - seed % 60, 150 - seed % 40, 90 + seed % 50)
    im = Image.new("RGB", (w, h), base)
    d = ImageDraw.Draw(im)
    for y in range(h):
        f = y / max(1, h - 1)
        d.line([0, y, w, y], fill=(int(base[0] * (1 - f * .4)), int(base[1] * (1 - f * .45)), int(base[2] * (1 - f * .3))))
    d.rectangle([2, 2, w - 3, h - 3], outline=(255, 255, 255), width=2)
    d.line([w // 2, 0, w // 2, h], fill=(255, 255, 255), width=1)
    d.line([0, h // 2, w, h // 2], fill=(255, 255, 255), width=1)
    d.text((10, 10), f"{a['id'][:14]}  {a['w']}x{a['h']}", fill=(255, 255, 255))
    out = io.BytesIO()
    im.save(out, "PNG")
    return out.getvalue()
