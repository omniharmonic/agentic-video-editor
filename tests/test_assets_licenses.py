"""The license log is the legal record: append-only, complete, tamper-evident."""
import json

import pytest

from ave.assets import licenses


def entry(**kw):
    base = dict(source="pexels", asset_id="1", url="https://www.pexels.com/video/x-1/", file="work/assets/downloads/pexels-1.mp4",
                sha256="ab" * 32, license="Pexels License", license_url="https://www.pexels.com/license/",
                license_text="Free to use...", query="hands clay", approved_by="ben")
    base.update(kw)
    return base


def test_append_adds_timestamp_chain_and_persists(tmp_path):
    log = licenses.LicenseLog(tmp_path / "licenses.jsonl")
    a = log.append(entry())
    b = log.append(entry(asset_id="2", sha256="cd" * 32))
    lines = (tmp_path / "licenses.jsonl").read_text().splitlines()
    assert len(lines) == 2
    assert a["at"].endswith("Z") and a["prev"] == licenses.GENESIS
    assert b["prev"] == licenses.entry_hash(a)
    assert log.verify() == []


def test_append_only_never_rewrites_earlier_lines(tmp_path):
    log = licenses.LicenseLog(tmp_path / "l.jsonl")
    log.append(entry())
    first = (tmp_path / "l.jsonl").read_text()
    log.append(entry(asset_id="2", sha256="cd" * 32))
    assert (tmp_path / "l.jsonl").read_text().startswith(first)


@pytest.mark.parametrize("missing", ["source", "url", "license", "license_url", "license_text", "sha256", "approved_by", "file"])
def test_incomplete_entries_are_refused(tmp_path, missing):
    log = licenses.LicenseLog(tmp_path / "l.jsonl")
    e = entry()
    e.pop(missing)
    with pytest.raises(ValueError, match=missing):
        log.append(e)
    assert not (tmp_path / "l.jsonl").exists() or (tmp_path / "l.jsonl").read_text() == ""


def test_blank_approver_is_refused(tmp_path):
    with pytest.raises(ValueError, match="approved_by"):
        licenses.LicenseLog(tmp_path / "l.jsonl").append(entry(approved_by="  "))


def test_verify_detects_edited_and_deleted_history(tmp_path):
    p = tmp_path / "l.jsonl"
    log = licenses.LicenseLog(p)
    log.append(entry())
    log.append(entry(asset_id="2", sha256="cd" * 32))
    log.append(entry(asset_id="3", sha256="ef" * 32))
    rows = [json.loads(x) for x in p.read_text().splitlines()]
    edited = [dict(rows[0], license="CC0"), rows[1], rows[2]]
    p.write_text("\n".join(json.dumps(r) for r in edited) + "\n")
    assert log.verify()  # problems reported
    p.write_text("\n".join(json.dumps(r) for r in [rows[0], rows[2]]) + "\n")
    assert log.verify()


def test_find_by_sha_and_coverage(tmp_path):
    log = licenses.LicenseLog(tmp_path / "l.jsonl")
    log.append(entry())
    assert log.find(sha256="ab" * 32)["asset_id"] == "1"
    assert log.find(sha256="00" * 32) is None
    assert log.find(source="pexels", asset_id="1")["file"].endswith("pexels-1.mp4")


def test_urls_never_store_api_keys(tmp_path):
    log = licenses.LicenseLog(tmp_path / "l.jsonl")
    e = log.append(entry(url="https://pixabay.com/api/videos/?key=SECRET123&q=x"))
    assert "SECRET123" not in json.dumps(e)


def test_attribution_lines_for_entries_that_require_them():
    es = [dict(entry(), attribution={"required": False, "text": "Video by Ada on Pexels"}),
          dict(entry(source="x", asset_id="9"), attribution={"required": True, "text": "Track by Y (CC BY 4.0)"})]
    assert licenses.attribution_lines(es, include_optional=False) == ["Track by Y (CC BY 4.0)"]
    assert len(licenses.attribution_lines(es, include_optional=True)) == 2


def test_provider_terms_snapshots_exist_and_are_dated():
    for name in ("pexels", "pixabay"):
        t = licenses.PROVIDER_TERMS[name]
        assert t["license_url"].startswith("https://") and t["text"] and t["snapshot_date"] and t["verified"] is False
