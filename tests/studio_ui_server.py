"""A tiny stand-in for the studio server, for developing and testing the UI shell (WS5) without WS4's real one.

It serves `src/ave/studio/ui/static/` as the page, answers `/api/studio/*` from the committed fixtures in `tests/data_studio/api/`
(with the real CSP header the real server sends, so the page is proven to run under it), and keeps just enough state that a drag
changes what the next request returns: move, swap, unschedule, accept-proposal, and an agent-side edit injected by the test.
`demo=True` adds more posts (several accounts, statuses, image shapes) so screenshots look like a lived-in studio; `demo=False`
returns exactly the fixture content. Loopback only; nothing here talks to any other host.

    srv = FixtureStudioServer(); srv.start(); page.goto(srv.url + "/#/calendar"); ...; srv.stop()
"""

from __future__ import annotations

import copy
import io
import json
import re
import threading
from datetime import date, timedelta
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

from PIL import Image, ImageDraw

from ave.ui.server import PAGE_CSP

ROOT = Path(__file__).resolve().parents[1]
STATIC = ROOT / "src" / "ave" / "studio" / "ui" / "static"
API = Path(__file__).parent / "data_studio" / "api"
THUMB_FIXTURE = "/files/library/assets/9f/9f2c1d4e7b10a3b5c6d7e8f901234567890abcdef1234567890abcdef1234567.jpg"
STUBS = {"editor.js", "review.js", "library.js", "feedback.js", "alerts.js", "accounts.js", "campaigns.js"}   # other workstreams' pages
CTYPE = {".js": "text/javascript; charset=utf-8", ".css": "text/css; charset=utf-8", ".html": "text/html; charset=utf-8",
         ".json": "application/json; charset=utf-8", ".svg": "image/svg+xml"}
RULES = {"ig-main": {"dow": (0, 2, 4), "time": "09:00", "rule": "r1"}, "fb-page": {"dow": (1, 3), "time": "12:30", "rule": "r3"}}


def fixture(name: str) -> dict:
    return json.loads((API / f"{name}.json").read_text())


def _thumb(seed: str, w: int, h: int) -> bytes:
    """A deterministic 'photograph': warm light, a loaf, bright marks near every edge so a crop is visible at a glance."""
    n = sum(ord(c) for c in seed)
    s = 0.5
    W, H = max(120, int(w * s * 0.5)), max(120, int(h * s * 0.5))
    img = Image.new("RGB", (W, H))
    px = img.load()
    top = (233 - n % 30, 196 - n % 40, 150 - n % 50)
    bot = (96 + n % 40, 62 + n % 20, 48 + n % 30)
    for y in range(H):
        t = y / H
        c = tuple(int(top[i] * (1 - t) + bot[i] * t) for i in range(3))
        for x in range(W):
            px[x, y] = c
    d = ImageDraw.Draw(img)
    cx, cy = W * (0.35 + (n % 5) * 0.08), H * 0.55
    rx, ry = W * 0.34, H * 0.2
    d.ellipse([cx - rx, cy - ry, cx + rx, cy + ry], fill=(176 - n % 20, 112, 62))
    for k in range(4):                                                  # scoring marks
        d.arc([cx - rx * 0.6 + k * rx * 0.3, cy - ry * 0.7, cx - rx * 0.1 + k * rx * 0.3, cy + ry * 0.2], 200, 330, fill=(240, 214, 170), width=max(2, W // 90))
    d.ellipse([W * 0.7, H * 0.08, W * 0.7 + W * 0.18, H * 0.08 + W * 0.18], fill=(255, 238, 190))     # window light
    m = max(4, W // 28)
    for (x, y) in ((m, m), (W - 6 * m, m), (m, H - 4 * m), (W - 6 * m, H - 4 * m)):                    # corner marks: what a crop would eat
        d.rectangle([x, y, x + 5 * m, y + 3 * m], fill=(24, 55, 86) if (x + y) % 2 else (240, 244, 247))
    buf = io.BytesIO()
    img.save(buf, "JPEG", quality=80)
    return buf.getvalue()


class FixtureStudioServer:
    def __init__(self, *, now: str = "2026-11-02T17:00:00Z", demo: bool = True):
        self.now = now
        self.demo = demo
        self.log: list[tuple[str, str]] = []
        self.moves: list[dict] = []
        self.swaps: list[dict] = []
        self.unscheduled_calls: list[dict] = []
        self.accepted: list[str] = []
        self.feed_orders: dict[str, list[str]] = {}
        self.feed_revs: dict[str, int] = {}
        self.events: list[dict] = []
        self.lock = threading.Lock()
        self._seed()
        outer = self

        class H(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"
            def log_message(self, *a): pass
            def do_GET(self): outer._handle(self, "GET")
            def do_POST(self): outer._handle(self, "POST")
        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), H)
        self.httpd.daemon_threads = True
        self.port = self.httpd.server_address[1]
        self.url = f"http://127.0.0.1:{self.port}"
        self.thread = threading.Thread(target=self.httpd.serve_forever, daemon=True)

    def start(self): self.thread.start(); return self
    def stop(self): self.httpd.shutdown(); self.httpd.server_close()
    def __enter__(self): return self.start()
    def __exit__(self, *a): self.stop()

    # ---------- state
    def _seed(self):
        cal = fixture("GET_calendar")["response"]
        self.cursor = 18273
        self.proposals = copy.deepcopy(cal["proposals"])
        self.items: list[dict] = []
        for day in cal["days"].values():
            for it in day:
                it = copy.deepcopy(it)
                grid = next((c for c in fixture("GET_grid")["response"]["cells"] if c["variant"] == it["variant"]), {})
                it.update(w=grid.get("w", 1080), h=grid.get("h", 1350), account=it.get("account", "ig-main"))
                self.items.append(it)
        for u in cal["unscheduled"]:
            u = copy.deepcopy(u); u.update(at=None, w=1080, h=1350); self.items.append(u)
        if self.demo:
            for it in self.items:                      # distinct pictures so the demo does not show one photo twenty times
                it["thumb"] = f"/files/library/assets/de/{it['post']}_{it['w']}x{it['h']}.jpg"
            D = self._mk
            self.items += [
                D("p_pub1", "v_pub1", "ig-main", "2026-10-30T09:00", "published", "Flour, water, salt: the whole recipe", 9, locked=True, w=1080, h=1080),
                D("p_pub2", "v_pub2", "ig-main", "2026-10-28T09:00", "published", "Why the crumb opens", 9, locked=True, w=1080, h=1350),
                D("p_d1", "v_d1", "ig-main", "2026-11-09T09:00", "in_review", "Saturday market table, mapped", 7, w=1080, h=1080),
                D("p_d2", "v_d2", "ig-main", "2026-11-11T09:00", "approved", "Pre-order cutoff is November 15", 5, w=1080, h=1920),
                D("p_d3", "v_d3", "ig-main", "2026-11-13T09:00", "scheduled", "Four hands, one dough", 6, w=1080, h=1350),
                D("p_d4", "v_d4", "ig-main", "2026-11-16T09:00", "draft", "Rye starter, week two", 2, w=1080, h=1350, text_cut=True),
                D("p_d5", "v_d5", "ig-main", None, "approved", "Scoring the loaf", 2, w=1080, h=1350),
                D("p_f1", "v_f1", "fb-page", "2026-11-03T12:30", "in_review", "Day 3: the starter smells like pears", 7, w=1200, h=630),
                D("p_f2", "v_f2", "fb-page", "2026-11-05T12:30", "scheduled", "Market day: what we are bringing", 4, w=1200, h=630),
                D("p_f3", "v_f3", "fb-page", "2026-11-10T12:30", "failed", "The oven was down on Tuesday", 3, w=1200, h=630),
            ]
        by_post = {i["post"]: i for i in self.items}
        for it in self.items:
            it.setdefault("slot", None)
        self.by_post = by_post

    def _mk(self, post, variant, account, at, status, title, rev, *, locked=False, w=1080, h=1350, text_cut=False):
        thumb = f"/files/library/assets/de/{post}_{w}x{h}.jpg"
        return {"post": post, "variant": variant, "account": account, "at": (at + ":00-07:00") if at else None, "slot": "r1" if at else None, "status": status,
                "title": title, "thumb": thumb, "rev": rev, "locked": locked, "flags": [], "w": w, "h": h, "text_cut": text_cut}

    def _event(self, op, it, actor="human", detail=None):
        self.cursor += 1
        self.events.append({"cursor": self.cursor, "ts": "2026-11-02T17:01:00Z", "actor": {"kind": actor, "name": "maren" if actor == "human" else "claude"},
                            "post": it["post"], "variant": it["variant"], "op": op, "rev": it["rev"], "before_sha": None, "after_sha": None, "detail": detail or {}})

    # ---------- the agent side (tests call this to simulate `ave studio move` from a CLI)
    def agent_move(self, variant: str, local: str):
        with self.lock:
            it = next(i for i in self.items if i["variant"] == variant)
            it["at"] = local + ":00-07:00"; it["rev"] += 1
            self._event("move", it, actor="agent", detail={"to": local})

    # ---------- calendar math
    def _slots(self, frm: str, to: str, accounts: list[str]):
        out = {}
        d, end = date.fromisoformat(frm), date.fromisoformat(to)
        taken = {(i["account"], i["at"][:16]) for i in self.items if i["at"]}
        while d <= end:
            for acc in accounts:
                r = RULES.get(acc)
                if r and d.weekday() in r["dow"] and (self.demo or acc == "ig-main"):
                    at = f"{d.isoformat()}T{r['time']}"
                    out.setdefault(d.isoformat(), []).append({"account": acc, "at": at + ":00-07:00", "rule": r["rule"], "free": (acc, at) not in taken})
            d += timedelta(days=1)
        return out

    def calendar(self, frm: str, to: str, accounts: list[str]):
        days = {}
        for it in self.items:
            if it["at"] and frm <= it["at"][:10] <= to and it["account"] in accounts:
                days.setdefault(it["at"][:10], []).append(self._public(it))
        slots = self._slots(frm, to, accounts)
        un = [self._public(i) for i in self.items if not i["at"] and (i["account"] in accounts or i["account"] is None)]
        return {"tz": "America/Denver", "cursor": f"ev:{self.cursor}", "accounts": [{"id": a, "platform": "instagram" if a == "ig-main" else "facebook", "live": False, "paused": False} for a in accounts],
                "days": days, "slots": slots, "unscheduled": un, "proposals": self.proposals}

    def _detail(self, its):
        it0 = its[0]
        variants = []
        for it in its:
            if not it.get("variant"):
                continue
            sched = {"local": it["at"][:16], "tz": "America/Denver", "utc": it["at"], "slot": it.get("slot"), "locked": False, "set_by": "human"} if it.get("at") else None
            variants.append({"id": it["variant"], "account": it["account"], "platform": "instagram" if it["account"] == "ig-main" else "facebook", "post_type": "feed",
                             "copy": {"caption": None, "first_comment": None, "hashtags": None}, "media": None, "schedule": sched, "queue": None, "queue_sha": None,
                             "flags": it.get("flags") or [], "attempts": [], "result": None, "dropped": False, "status": it["status"], "status_reason": "", "queue_status": None, "approval": None})
        post = {"v": 1, "kind": "studio.post", "id": it0["post"], "rev": it0["rev"], "stage": "active", "campaign": None, "copy": {"base": it0["title"], "link": None, "first_comment": None, "hashtags": [], "cta": None},
                "media": [], "variants": variants, "disclosure": {"ai_generated": False}, "created": "2026-10-30T09:00:00-0600"}
        return {"post": post, "feedback": [], "events": [], "queue": {}}

    @staticmethod
    def _public(it):
        keys = ("post", "variant", "at", "slot", "status", "title", "thumb", "rev", "locked", "flags", "account", "tz", "campaign")
        out = {k: it[k] for k in keys if k in it}
        if out.get("at") is None: out.pop("at", None); out.pop("slot", None)
        return out

    def grid(self, account: str):
        base = fixture("GET_grid")["response"]
        acc = next(a for a in fixture("GET_accounts")["response"]["accounts"] if a["id"] == account)
        layout = base["layout"] if account == "ig-main" else {"kind": "page_timeline", "columns": 1, "approximate": True, "verified": False}
        cells = []
        for it in sorted([i for i in self.items if i["account"] == account and i["at"]], key=lambda i: i["at"], reverse=True):
            c = {k: it[k] for k in ("post", "variant", "at", "status", "thumb", "w", "h", "rev", "locked", "title") if k in it}
            c["crop"] = {"v":1,"x":0.5,"y":0.4,"aspect":"3:4","purpose":"preview"}
            if it.get("text_cut"): c["text_cut"] = True
            cells.append(c)
        return {"account": acc, "layout": layout, "cells": cells}

    def feed_plan(self, account):
        account = account or "ig-main"
        result = self.grid(account)
        cells = result["cells"]
        order = self.feed_orders.get(account, [c["variant"] for c in cells if not c.get("locked")])
        by_id = {c["variant"]: c for c in cells}
        result["cells"] = [{**by_id[k], "key": k} for k in order if k in by_id] + [{**c, "key": c["variant"]} for c in cells if c.get("locked")]
        result.update(rev=self.feed_revs.get(account, 0), snapshot=str(self.cursor), read_only=False)
        return result

    # ---------- mutations
    def _find(self, post, variant):
        return next((i for i in self.items if i["post"] == post and i["variant"] == variant), None)

    def _move(self, post, body):
        it = self._find(post, body["variant"])
        if not it: return 404, {"error": "no such variant", "code": "not_found"}
        if it["rev"] != body["expected_rev"]: return 409, {"error": "changed since you loaded it", "code": "conflict.rev", "latest": {"id": post, "rev": it["rev"]}}
        if it.get("locked"): return 409, {"error": "published posts cannot be moved", "code": "conflict.locked", "latest": {"id": post, "rev": it["rev"]}}
        local = body["to"]["local"]
        r = RULES.get(it["account"])
        if body.get("snap") == "slot" and r and date.fromisoformat(local[:10]).weekday() in r["dow"]:
            local = f"{local[:10]}T{r['time']}"
        clash = next((o for o in self.items if o is not it and o["account"] == it["account"] and o["at"] and o["at"][:16] == local), None)
        if clash: return 409, {"error": f"slot taken by {clash['post']}", "code": "conflict.slot", "latest": {"id": post, "rev": it["rev"]}}
        before = it["at"]
        it["at"] = local + ":00-07:00"; it["rev"] += 1
        if it["status"] in ("approved", "scheduled") and "moved_after_approval" not in it["flags"]: it["flags"].append("moved_after_approval")
        self.moves.append({"post": post, "variant": it["variant"], "from": before, "to": local, "snap": body.get("snap")})
        self._event("move", it, detail={"from": before, "to": local})
        return 200, {"post": {"id": post, "rev": it["rev"]}, "warnings": []}

    def _swap(self, post, body):
        a = self._find(post, body["variant"]); b = next((i for i in self.items if i["variant"] == body["with"]), None)
        if not a or not b: return 404, {"error": "no such variant", "code": "not_found"}
        if a["rev"] != body["expected_rev"]: return 409, {"error": "changed since you loaded it", "code": "conflict.rev"}
        if a.get("locked") or b.get("locked"): return 409, {"error": "a published post keeps its place", "code": "conflict.locked"}
        a["at"], b["at"] = b["at"], a["at"]; a["rev"] += 1; b["rev"] += 1
        self.swaps.append({"a": a["variant"], "b": b["variant"]})
        self._event("swap", a, detail={"with": b["variant"]})
        return 200, {"post": {"id": post, "rev": a["rev"]}, "warnings": []}

    def _unschedule(self, post, body):
        it = self._find(post, body["variant"])
        if not it: return 404, {"error": "no such variant", "code": "not_found"}
        if it["rev"] != body["expected_rev"]: return 409, {"error": "changed since you loaded it", "code": "conflict.rev"}
        it["at"] = None; it["rev"] += 1
        self.unscheduled_calls.append({"post": post, "variant": it["variant"]})
        self._event("unschedule", it)
        return 200, {"post": {"id": post, "rev": it["rev"]}, "warnings": []}

    def _accept(self, pid):
        pr = next((p for p in self.proposals if p["id"] == pid), None)
        if not pr: return 404, {"error": "no such proposal", "code": "not_found"}
        applied, conflicts = [], []
        for mv in pr["moves"]:
            it = next(i for i in self.items if i["variant"] == mv["variant"])
            st, _ = self._move(mv["post"], {"variant": mv["variant"], "expected_rev": it["rev"], "to": mv["to"], "snap": "slot"})
            (applied if st == 200 else conflicts).append({"post": mv["post"], "variant": mv["variant"], "status": "moved" if st == 200 else "conflict"})
        self.proposals = [p for p in self.proposals if p["id"] != pid]
        self.accepted.append(pid)
        return 200, {"applied": applied, "conflicts": conflicts}

    # ---------- http
    def _handle(self, h: BaseHTTPRequestHandler, method: str):
        u = urlsplit(h.path)
        q = {k: v[0] for k, v in parse_qs(u.query).items()}
        self.log.append((method, h.path))
        body = {}
        if method == "POST":
            n = int(h.headers.get("Content-Length") or 0)
            body = json.loads(h.rfile.read(n) or b"{}")
        path = u.path
        with self.lock:
            if path.startswith("/api/studio/"):
                return self._api(h, method, path[len("/api/studio/"):], q, body)
        if path.startswith("/files/"):
            m = re.search(r"_(\d+)x(\d+)\.jpg$", path)
            w, hh = (int(m.group(1)), int(m.group(2))) if m else (1080, 1350)
            return self._send(h, 200, _thumb(path, w, hh), "image/jpeg")
        if path in ("/", "/index.html"):
            return self._send(h, 200, (STATIC / "index.html").read_bytes(), CTYPE[".html"])
        if path.startswith("/static/"):
            name = path[len("/static/"):]
            f = (STATIC / name).resolve()
            if name == "library.js":
                return self._send(h, 200, b'export function mount(root){const p=document.createElement("p");p.textContent="stub library";root.append(p);} export function openAssetPicker(){throw new Error("asset picker is outside this shell fixture");}', CTYPE[".js"])
            if name == "editor.js":
                return self._send(h, 200, b'export function mount(root){const p=document.createElement("p");p.textContent="stub editor";root.append(p);} export function mountEditor(root,ctx,options){root.textContent="stub composer";return {destroy(){root.replaceChildren();},flush:async()=>{},state:{}};}', CTYPE[".js"])
            if name in STUBS:   # the shell tests exercise the shell alone: stand-in pages win, so they never depend on another page's API calls
                return self._send(h, 200, b'export function mount(root){ const p = document.createElement("p"); p.textContent = "stub page"; root.append(p); }', CTYPE[".js"])
            if f.is_file() and STATIC.resolve() in f.parents:
                return self._send(h, 200, f.read_bytes(), CTYPE.get(f.suffix, "application/octet-stream"))
            if name in STUBS:   # a page another workstream ships: a stand-in so the router has something to import
                return self._send(h, 200, b'export function mount(root){ const p = document.createElement("p"); p.textContent = "stub page"; root.append(p); }', CTYPE[".js"])
        return self._send(h, 404, b'{"error":"not found"}', CTYPE[".json"])

    def _api(self, h, method, path, q, body):
        def out(status, obj): return self._send(h, status, json.dumps(obj).encode(), CTYPE[".json"])
        if method == "GET":
            if path == "summary":
                s = fixture("GET_summary")["response"]; s["now"] = self.now; s["cursor"] = f"ev:{self.cursor}"
                return out(200, s)
            if path == "accounts": return out(200, fixture("GET_accounts")["response"])
            if path == "campaigns": return out(200, {"campaigns": [], "total": 0})
            if path == "calendar":
                accs = [a for a in (q.get("accounts") or "ig-main,fb-page").split(",") if a]
                return out(200, self.calendar(q["from"], q["to"], accs))
            if path.startswith("grid/"): return out(200, self.grid(path.split("/", 1)[1]))
            if path == "feed-plan": return out(200, self.feed_plan(q.get("account", "")))
            if path == "changes":
                since = int((q.get("cursor") or "ev:0").split(":")[1])
                evs = [e for e in self.events if e["cursor"] > since]
                return out(200, {"cursor": f"ev:{self.cursor}", "events": [{k: v for k, v in e.items() if k != "cursor"} for e in evs]})
            m = re.fullmatch(r"posts/([^/]+)(?:/preview/([^/]+))?", path)
            if m:                                                      # what the side preview asks for: the post, and its (empty) server preview
                its = [i for i in self.items if i["post"] == m.group(1)]
                if not its:
                    return out(404, {"error": "no such post", "code": "not_found"})
                if m.group(2):
                    return out(200, {"media": [], "warnings": []})
                return out(200, self._detail(its))
            return out(404, {"error": "not in the fixture server", "code": "not_found"})
        if path == "feed-plan":
            account = body.get("account") or "ig-main"
            current = self.feed_plan(account)
            if body.get("expected_rev") != current["rev"] or body.get("snapshot") != current["snapshot"]:
                return out(409, {"error": "The feed changed in another window", "code": "conflict.feed_plan"})
            order = [c["key"] for c in current["cells"] if not c.get("locked")]
            key, before = body.get("key"), body.get("before")
            if key not in order or before is not None and before not in order:
                return out(409, {"error": "Published posts keep their place", "code": "conflict.feed_plan"})
            order.remove(key)
            order.insert(order.index(before) if before else len(order), key)
            self.feed_orders[account] = order
            self.feed_revs[account] = current["rev"] + 1
            return out(200, self.feed_plan(account))
        m = re.fullmatch(r"posts/([^/]+)/(move|swap|unschedule)", path)
        if m:
            st, obj = {"move": self._move, "swap": self._swap, "unschedule": self._unschedule}[m.group(2)](m.group(1), body)
            return out(st, obj)
        m = re.fullmatch(r"schedule/proposals/([^/]+)/accept", path)
        if m:
            st, obj = self._accept(m.group(1))
            return out(st, obj)
        return out(404, {"error": "not in the fixture server", "code": "not_found"})

    def _send(self, h, status, data: bytes, ctype: str):
        h.send_response(status)
        h.send_header("Content-Type", ctype)
        h.send_header("Content-Length", str(len(data)))
        h.send_header("Cache-Control", "no-store")
        h.send_header("Content-Security-Policy", PAGE_CSP)
        h.end_headers()
        h.wfile.write(data)
