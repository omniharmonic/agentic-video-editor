"""Sourcing from the creator's Markdown: sentence-addressable passages, verbatim quotes, drift detection."""

import json

import pytest

from ave import cli
from ave.content import manifest as M
from ave.content import render_motionkit as RM
from ave.content import sources as S
from content_helpers import cep, content_env, make_episode, pick_quote  # noqa: F401

ESSAY = """---
title: What the soil keeps
date: 2026-09-02
url: https://example.org/soil
---

# What the soil keeps

I used to think a field was a thing you *fixed*. Dr. Okafor taught me otherwise. She said a field is **a relationship**, and you tend it.

## Small repairs

```python
print("not prose")
```

- Mend the fence before the storm.
- Leave the [hedgerow](https://example.org/h) alone.

<!-- a comment that is not prose -->

We do not repair once. We repair again, and again, and the repairing is the thing.
"""


@pytest.fixture
def writing(cep, tmp_path):
    d = tmp_path / "writing"
    d.mkdir()
    (d / "soil.md").write_text(ESSAY)
    (d / "empty.md").write_text("---\ntitle: x\n---\n")
    S.ingest_writing(cep, d)
    return cep, d


def test_ingest_splits_into_passages_and_sentences_with_front_matter(writing):
    ep, d = writing
    w = S.load_writing(ep)
    assert [f["file"] for f in w["files"]] == ["soil.md"]                  # a file with no prose is dropped
    f = w["files"][0]
    assert f["title"] == "What the soil keeps" and f["date"] == "2026-09-02" and f["url"] == "https://example.org/soil"
    texts = [p["text"] for p in f["passages"]]
    assert texts[0].startswith("I used to think a field was a thing you fixed.")          # emphasis markers gone
    assert "**" not in " ".join(texts) and "print(" not in " ".join(texts) and "comment" not in " ".join(texts)
    assert "Leave the hedgerow alone." in " ".join(texts) and "https://" not in " ".join(texts)
    sents = [s["text"] for s in f["passages"][0]["sentences"]]
    assert sents[1] == "Dr. Okafor taught me otherwise."                  # an abbreviation does not end a sentence
    assert f["passages"][0]["sentences"][0]["id"] == "p001.s1" and f["passages"][0]["heading"] == "What the soil keeps"
    assert w["totals"]["files"] == 1 and w["totals"]["sentences"] >= 7


def test_ingest_reads_only_and_command_prints_a_summary(cep, tmp_path, capsys):
    d = tmp_path / "w"
    d.mkdir()
    (d / "a.md").write_text(ESSAY)
    before = (d / "a.md").read_bytes()
    assert cli.main(["content", "ingest-writing", str(cep.root), str(d)]) == 0
    out = json.loads(capsys.readouterr().out)
    assert out["files"][0]["file"] == "a.md" and (d / "a.md").read_bytes() == before
    with pytest.raises(SystemExit, match="not a folder"):
        S.ingest_writing(cep, tmp_path / "nope")


def test_quote_from_writing_is_verbatim_checked_against_the_passage(writing, tmp_path, capsys):
    ep, d = writing
    good = pick_quote(id="w-good", source={"writing": "soil.md", "sentences": ["p001.s3"]},
                      slots={"quote": "She said a field is a relationship, and you tend it.", "attribution": "Maren Okafor"})
    cheat = pick_quote(id="w-cheat", source={"writing": "soil.md", "sentences": ["p001.s3"]},
                       slots={"quote": "A field is a relationship that you tend", "attribution": "Maren Okafor"})
    far = pick_quote(id="w-far", source={"writing": "soil.md", "sentences": ["p001.s3"]},
                     slots={"quote": "Mend the fence before the storm.", "attribution": "Maren Okafor"})      # real writing, but not the chosen sentence
    pf = tmp_path / "p.json"
    pf.write_text(json.dumps([good, cheat, far]))
    code = cli.main(["content", "propose", str(ep.root), str(pf)])
    out = json.loads(capsys.readouterr().out)
    assert code == 2 and [a["id"] for a in out["accepted"]] == ["w-good"]
    assert {r["id"] for r in out["rejected"]} == {"w-cheat", "w-far"}
    doc = M.load_latest(ep)
    s = doc["sources"]["w1"]
    assert s["type"] == "markdown" and s["sentences"] == ["p001.s3"] and s["url"] == "https://example.org/soil"
    assert doc["items"][0]["provenance"]["quotes"][0]["located"] is False                  # writing has no timestamp


def test_unknown_file_or_ids_and_missing_ingest_are_clear_errors(writing, cep):
    ep, d = writing
    with pytest.raises(ValueError, match="not in the ingested writing"):
        S.writing_source(ep, "ghost.md", passages=["p001"])
    with pytest.raises(ValueError, match="unknown sentence ids"):
        S.writing_source(ep, "soil.md", sentences=["p009.s1"])
    with pytest.raises(ValueError, match="exactly one"):
        S.writing_source(ep, "soil.md")


def test_no_writing_ingested_is_a_clear_error(cep):
    with pytest.raises(ValueError, match="no writing ingested"):
        S.writing_source(cep, "soil.md", passages=["p001"])


def test_editing_the_file_after_quoting_blocks_rendering(writing, tmp_path, capsys):
    ep, d = writing
    pf = tmp_path / "p.json"
    pf.write_text(json.dumps([pick_quote(id="w-good", source={"writing": "soil.md", "sentences": ["p001.s3"]},
                                         slots={"quote": "She said a field is a relationship, and you tend it.", "attribution": "Maren Okafor"})]))
    cli.main(["content", "propose", str(ep.root), str(pf)])
    capsys.readouterr()
    doc = M.load_latest(ep)
    assert S.verify_sources(ep, doc) == []
    (d / "soil.md").write_text(ESSAY.replace("a relationship", "a debt"))
    drift = S.verify_sources(ep, doc)
    assert drift and "changed on disk since it was quoted" in drift[0]
    with pytest.raises(SystemExit, match="Sources changed"):
        RM.render_doc(ep, doc)
