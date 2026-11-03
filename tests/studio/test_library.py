"""The asset library (WS1): add_file, registry, license log, coverage over derived_from, search/reindex, verify, facade hooks."""

import json

import pytest

import ave.library as lib
from ave.assets import licenses
from ave.library import registry
from ave.studio import model
from ave.studio.events import AGENT
from ave.studio.model import new_post
from ave.studio import store
from studio_helpers import make_png

STOCK = {"source": "pexels", "asset_id": "1", "url": "https://www.pexels.com/photo/1/", "license": "Pexels License",
         "license_url": "https://www.pexels.com/license/", "license_text": "free to use",
         "attribution": {"required": True, "text": "Photo by A on Pexels"}}


def png(sp, name, color):
    return make_png(sp.work / "src" / name, size=(40, 30), color=color)


def test_add_own_file_copies_by_sha_logs_once_and_is_idempotent(bare):
    src = png(bare, "a.png", (1, 2, 3))
    before = src.read_bytes()
    rec = lib.add_file(bare, src, source={"type": "own"}, by="maren", tags=["Bread", "bread", " "], alt="A tiny square.")
    assert model.validate("asset", rec) == []
    assert rec["id"] == "a_" + rec["sha256"][:12]
    assert rec["file"] == f"library/assets/{rec['sha256'][:2]}/{rec['sha256']}.png"
    assert (bare.root / rec["file"]).read_bytes() == before and src.read_bytes() == before     # copied, source untouched
    assert (rec["w"], rec["h"], rec["kind"], rec["tags"], rec["added_by"]) == (40, 30, "image", ["Bread", "bread"], "maren")
    entries = registry.license_log(bare).entries()
    assert len(entries) == 1 and entries[0]["source"] == "own-footage" and entries[0]["approved_by"] == "maren"
    assert rec["license"]["entry"] == licenses.entry_hash(entries[0])
    again = lib.add_file(bare, src, source={"type": "own"}, by="maren")
    assert again == rec and len(registry.license_log(bare).entries()) == 1
    assert len((bare.library_dir / "assets.jsonl").read_text().splitlines()) == 1
    assert registry.license_log(bare).verify() == []


def test_stock_needs_a_license_entry_and_nothing_is_written_without_one(bare):
    src = png(bare, "s.png", (9, 9, 9))
    with pytest.raises(ValueError, match="license"):
        lib.add_file(bare, src, source={"type": "stock", "provider": "pexels"}, by="maren")
    with pytest.raises(ValueError, match="license_text"):
        lib.add_file(bare, src, source={"type": "stock"}, license_entry={**STOCK, "license_text": ""}, by="maren")
    with pytest.raises(ValueError, match="by is required"):
        lib.add_file(bare, src, source={"type": "own"}, by=" ")
    with pytest.raises(ValueError, match="unsupported"):
        lib.add_file(bare, bare.meta_path, source={"type": "own"}, by="maren")
    assert not list((bare.library_dir / "assets").rglob("*.*"))
    assert not (bare.library_dir / "assets.jsonl").exists() and not (bare.library_dir / "licenses.jsonl").exists()


def test_log_failure_after_the_copy_removes_the_copy(bare, monkeypatch):
    src = png(bare, "f.png", (5, 5, 5))

    def boom(self, entry):
        raise OSError("disk full")
    monkeypatch.setattr(licenses.LicenseLog, "append", boom)
    with pytest.raises(OSError):
        lib.add_file(bare, src, source={"type": "own"}, by="maren")
    assert not [p for p in (bare.library_dir / "assets").rglob("*") if p.is_file()]
    assert not (bare.library_dir / "assets.jsonl").exists()


def test_derived_from_must_name_registered_assets(bare):
    with pytest.raises(ValueError, match="unknown assets"):
        lib.add_file(bare, png(bare, "r.png", (7, 7, 7)), source={"type": "render"}, by="claude", derived_from=["a_000000000000"])


def test_coverage_walks_the_derived_from_closure(bare):
    stock = lib.add_file(bare, png(bare, "bg.png", (10, 10, 10)), source={"type": "stock", "provider": "pexels", "asset_id": "1"},
                         license_entry=STOCK, by="maren")
    render = lib.add_file(bare, png(bare, "card.png", (20, 20, 20)), source={"type": "render"}, by="claude", derived_from=[stock["id"]])
    cov = lib.coverage(bare, [render["id"]])
    assert cov["ok"] and [c["asset"] for c in cov["covered"]] == [stock["id"]] and cov["missing"] == []
    assert cov["attributions"] == ["Photo by A on Pexels"]
    assert render["license"]["entry"] is None and render["license"]["attribution"]["text"] is None

    # an input with no log entry (registered by hand, as an older tool might) makes the render uncovered
    raw = png(bare, "raw.png", (30, 30, 30))
    rec = lib.add_file(bare, raw, source={"type": "own"}, by="maren")
    log = bare.library_dir / "licenses.jsonl"
    kept = [ln for ln in log.read_text().splitlines() if json.loads(ln)["sha256"] != rec["sha256"]]
    log.write_text("\n".join(kept) + "\n")
    render2 = lib.add_file(bare, png(bare, "card2.png", (40, 40, 40)), source={"type": "render"}, by="claude", derived_from=[rec["id"]])
    cov2 = lib.coverage(bare, [render2["id"]])
    assert not cov2["ok"] and cov2["missing"][0]["asset"] == rec["id"] and "no license-log entry" in cov2["missing"][0]["why"]
    assert lib.coverage(bare, ["a_deadbeef0000"])["missing"][0]["why"].startswith("unknown asset")


def test_a_render_from_nothing_is_covered_and_a_tampered_input_is_not(bare):
    flat = lib.add_file(bare, png(bare, "flat.png", (50, 50, 50)), source={"type": "render"}, by="claude")
    assert lib.coverage(bare, [flat["id"]])["ok"]
    own = lib.add_file(bare, png(bare, "own.png", (60, 60, 60)), source={"type": "own"}, by="maren")
    (bare.root / own["file"]).write_bytes(b"not the same bytes")
    cov = lib.coverage(bare, [own["id"]])
    assert not cov["ok"] and "do not match" in cov["missing"][0]["why"]
    assert not lib.verify(bare)["ok"]


def test_verify_detects_history_edits(bare):
    lib.add_file(bare, png(bare, "v1.png", (1, 1, 1)), source={"type": "own"}, by="maren")
    lib.add_file(bare, png(bare, "v2.png", (2, 2, 2)), source={"type": "own"}, by="maren")
    assert lib.verify(bare) == {"ok": True, "problems": [], "assets": 2, "pending_attestation": []}
    log = bare.library_dir / "licenses.jsonl"
    lines = log.read_text().splitlines()
    e = json.loads(lines[0])
    e["license_text"] = "rewritten history"
    log.write_text("\n".join([json.dumps(e)] + lines[1:]) + "\n")
    res = lib.verify(bare)
    assert not res["ok"] and any("chain broken" in p for p in res["problems"])


def test_search_filters_and_reindex_rebuilds_from_the_registry_alone(bare):
    a = lib.add_file(bare, png(bare, "a.png", (1, 0, 0)), source={"type": "own"}, by="maren", tags=["bread", "kitchen"], alt="Warm loaf of bread.")
    b = lib.add_file(bare, png(bare, "b.png", (0, 1, 0)), source={"type": "own"}, by="maren", tags=["garden"], alt="Tomato plants in rain.")
    assert [r["id"] for r in lib.search(bare, "bread")] == [a["id"]]
    assert [r["id"] for r in lib.search(bare, "tomat")] == [b["id"]]               # prefix match
    assert lib.search(bare, "bread tomato") == []                                  # every word must match
    assert {r["id"] for r in lib.search(bare, tag="garden")} == {b["id"]}
    assert {r["id"] for r in lib.search(bare, kind="image")} == {a["id"], b["id"]} and lib.search(bare, kind="video") == []
    assert len(lib.search(bare, limit=1)) == 1

    idx = bare.library_dir / "index.sqlite"
    idx.unlink()                                                                    # derived: deleting it loses nothing
    assert lib.reindex(bare) == 2 and idx.is_file()
    idx.unlink()
    assert [r["id"] for r in lib.search(bare, "loaf")] == [a["id"]]                 # search rebuilds a missing index itself
    c = lib.add_file(bare, png(bare, "c.png", (0, 0, 1)), source={"type": "own"}, by="maren", alt="Fresh sourdough loaf.")
    assert {r["id"] for r in lib.search(bare, "loaf")} == {a["id"], c["id"]}        # and a stale one


def test_unused_filter_uses_post_media(bare):
    a = lib.add_file(bare, png(bare, "u1.png", (3, 0, 0)), source={"type": "own"}, by="maren")
    b = lib.add_file(bare, png(bare, "u2.png", (0, 3, 0)), source={"type": "own"}, by="maren")
    post = new_post(idea="x", post_id="p_use", media=[{"slot": 0, "asset": a["id"], "role": "image", "alt": "x"}])
    store.save_post(bare, post, expected_rev=None, actor=AGENT, op="create")
    assert [r["id"] for r in lib.search(bare, unused=True)] == [b["id"]]
    assert lib.usage(bare, a["id"]) == [{"post": "p_use", "variant": None}]


def test_update_asset_appends_and_freezes_provenance(bare):
    a = lib.add_file(bare, png(bare, "t.png", (4, 4, 4)), source={"type": "own"}, by="maren", tags=["x"])
    new = lib.update_asset(bare, a["id"], tags=["x", "y"], alt="Better alt text.")
    assert lib.get(bare, a["id"])["tags"] == ["x", "y"] == new["tags"]
    assert len((bare.library_dir / "assets.jsonl").read_text().splitlines()) == 2        # history kept, last line wins
    with pytest.raises(ValueError, match="cannot change"):
        lib.update_asset(bare, a["id"], license={"entry": None})
    with pytest.raises(KeyError):
        lib.get(bare, "a_nope00000000")


def test_generated_assets_are_ai_and_need_a_license_entry(bare):
    entry = {**STOCK, "source": "gen:fake", "license": "Generated, tier terms"}
    rec = lib.add_file(bare, png(bare, "g.png", (8, 8, 8)), source={"type": "generated", "provider": "fake"}, license_entry=entry, by="maren")
    assert rec["ai_generated"] is True


def test_facade_hooks(built):
    studio = built.studio
    lib.register(studio)
    rec = lib.add_file(studio.sp, png(studio.sp, "h.png", (6, 6, 6)), source={"type": "own"}, by="maren", tags=["bread"], alt="A loaf.")
    out = studio.library(q="loaf", tag="bread")
    assert out["total"] == 1 and out["assets"][0]["id"] == rec["id"]
    assert out["assets"][0]["license_ok"] is True and out["assets"][0]["url"] == "/files/" + rec["file"] and out["assets"][0]["usage"] == []
    one = studio.library_asset(rec["id"])
    assert one["license"]["covered"] is True and one["license"]["entries"][0]["summary"] == "Owned" and one["license"]["missing"] == []
    up = studio.upload("card.png", png(studio.sp, "up.png", (7, 8, 9)).read_bytes(), by="maren", alt="A card.")
    assert up["duplicate"] is False and up["asset"]["source"]["type"] == "own"
    assert studio.upload("card.png", png(studio.sp, "up.png", (7, 8, 9)).read_bytes(), by="maren")["duplicate"] is True
    with pytest.raises(ValueError):
        studio.upload("x.png", b"", source_type="stock")
    # the built studio's older assets have no license entry: the library says so rather than hiding it
    old = built.assets[0]
    assert studio.library_asset(old["id"])["license"]["covered"] is False
