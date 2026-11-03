"""E1: meta_hash v2. Legacy items (no `meta_v`) must keep their hashes and signatures byte-for-byte (golden fixture captured BEFORE the
change); a `meta_v == 2` item excludes `schedule` and binds account / post_type / first_comment / link_url."""

import copy
import json
from datetime import datetime, timezone
from pathlib import Path

import pytest

from ave.publish import safety

GOLDEN = json.loads((Path(__file__).parent.parent / "data_studio" / "golden" / "meta_hash_legacy.json").read_text())


@pytest.fixture
def golden_key(monkeypatch):
    monkeypatch.setenv("AVE_APPROVAL_KEY", GOLDEN["approval_key"])


NOW = datetime(2026, 1, 2, tzinfo=timezone.utc).timestamp()      # one day after the golden approvals, inside the 14-day TTL


@pytest.mark.parametrize("case", GOLDEN["cases"], ids=[c["item"]["id"] for c in GOLDEN["cases"]])
def test_legacy_items_hash_and_sign_exactly_as_before(case, golden_key):
    item = case["item"]
    assert "meta_v" not in item
    assert safety.meta_hash(item) == case["meta_hash"]
    again = safety.make_approval(item, item["sha256"], "ben", "2026-01-01T01:00:00-0700")
    assert again == case["approval"]
    stored = {**item, "approvals": [case["approval"]], "status": "approved"}
    assert safety.approval_problems(stored, item["sha256"], now=NOW) == []         # a pre-change approval is still valid


def base_v2(**kw):
    item = copy.deepcopy(GOLDEN["cases"][1]["item"])      # the image item
    item.update(meta_v=2, account="ig-main", post_type="feed", first_comment="", link_url=None, status="approved")
    item.update(kw)
    return item


def test_v2_excludes_schedule_but_legacy_binds_it():
    a, b = base_v2(schedule="2026-11-02T16:00:00+00:00"), base_v2(schedule="2026-11-09T16:00:00+00:00")
    assert safety.meta_hash(a) == safety.meta_hash(b)
    legacy = copy.deepcopy(GOLDEN["cases"][0]["item"])
    moved = {**legacy, "schedule": "2030-01-01T00:00:00+00:00"}
    assert safety.meta_hash(legacy) != safety.meta_hash(moved)


@pytest.mark.parametrize("field,value", [
    ("account", "ig-alt"), ("post_type", "carousel"), ("first_comment", "link in bio"), ("link_url", "https://acme.example/x"),
    ("title", "Another title"), ("caption", "Another caption"), ("alt_texts", ["different alt"]), ("platforms", ["instagram"]),
    ("tags", ["x"]), ("visibility", "public"), ("ai_disclosure", True), ("description", "d2"), ("made_for_kids", True),
])
def test_v2_binds_these_fields(field, value):
    assert safety.meta_hash(base_v2()) != safety.meta_hash(base_v2(**{field: value}))


def test_v2_hash_differs_from_legacy_hash_of_the_same_item():
    item = base_v2()
    legacy = {k: v for k, v in item.items() if k != "meta_v"}
    assert safety.meta_hash(item) != safety.meta_hash(legacy)


def test_retime_keeps_a_v2_approval_valid_and_a_content_edit_voids_it(golden_key):
    item = base_v2()
    item["approvals"] = [safety.make_approval(item, item["sha256"], "ben", "2026-01-01T01:00:00-0700")]
    assert safety.approval_problems(item, item["sha256"], now=NOW) == []
    item["schedule"] = "2031-05-05T05:05:00+00:00"
    assert safety.approval_problems(item, item["sha256"], now=NOW) == []
    item["account"] = "ig-alt"
    assert any("changed since approval" in p for p in safety.approval_problems(item, item["sha256"], now=NOW))


def test_renewal_identity_is_explicit_and_distinct_under_same_clock(golden_key):
    item=base_v2();at='2026-01-01T01:00:00-0700';old=safety.make_approval(item,item['sha256'],'ben',at)
    a=safety.make_renewed_approval(item,item['sha256'],'ben',at,old['sig'])
    b=safety.make_renewed_approval(item,item['sha256'],'ben',at,old['sig'])
    assert len({old['sig'],a['sig'],b['sig']})==3
    assert a['at']==b['at']==old['at'] and a['approval_v']==2
    assert safety.approval_problems({**item,'approvals':[a]},item['sha256'],now=NOW)==[]
    assert safety.make_approval(item,item['sha256'],'ben',at)==old


@pytest.mark.parametrize('mutation',['unknown_version','missing_version','missing_nonce','wrong_nonce','wrong_parent','legacy_with_nonce','legacy_with_version','different_item'])
def test_renewal_metadata_never_falls_back_to_legacy_verification(golden_key,mutation):
    item=base_v2();at='2026-01-01T01:00:00-0700';old=safety.make_approval(item,item['sha256'],'ben',at)
    a=safety.make_renewed_approval(item,item['sha256'],'ben',at,old['sig'])
    if mutation=='unknown_version':a['approval_v']=99
    elif mutation=='missing_version':a.pop('approval_v')
    elif mutation=='missing_nonce':a.pop('nonce')
    elif mutation=='wrong_nonce':a['nonce']='a'*64
    elif mutation=='wrong_parent':a['renewed_from']='b'*64
    elif mutation=='legacy_with_nonce':a={**old,'nonce':'a'*64}
    elif mutation=='legacy_with_version':a={**old,'approval_v':2,'nonce':'a'*64,'renewed_from':old['sig']}
    elif mutation=='different_item':item['id']='different-item'
    assert safety.approval_problems({**item,'approvals':[a]},item['sha256'],now=NOW)
