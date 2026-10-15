"""The creator's real failure, reproduced: a ContentDoc stored before the fix (role == show, surname attribution, clip ids that were
later renumbered) must re-render clean into a NEW versioned folder, leaving every earlier file untouched."""

import hashlib
import json

from ave.content import manifest as M
from ave.content import render_motionkit as RM
from ave.content import sources as S
from content_helpers import cep, content_env  # noqa: F401
from test_content_byline import SHOW, SPEAKERS, _set_speakers

# ---- the real failure: a stored doc from before the fix --------------------------------------------------

def _legacy_doc(cep):
    """The shape of the creator's main.v2.json: role == show, a surname attribution, sources bound to clip ids."""
    from content_helpers import pick_quote
    from ave.content import propose as P
    out = P.propose(cep, [pick_quote()], allow_unapproved=True)
    assert not out["rejected"], out
    # propose now refuses a repeated show, so write the old shape by hand: the next version carries role == show
    path = M.latest_path(cep)
    doc = M.load(path)
    doc["items"][0]["slots"].update(attribution="Maren Okafor", role=SHOW)
    doc["items"][0]["slots"].pop("title", None)
    new = M.clone(doc)
    p2 = M.save_new_version(cep, new)
    return M.load(p2), out


def test_a_legacy_doc_rerenders_clean_into_a_new_versioned_folder_and_old_files_stay(cep):
    doc, _ = _legacy_doc(cep)
    assert doc["items"][0]["slots"]["role"] == SHOW
    _set_speakers(cep, SPEAKERS)
    v_before = M.list_versions(cep)[-1]
    new1, rep1 = RM.render_doc(cep, doc, force=True)
    assert rep1["out_dir"].endswith(f"renders/v{v_before + 1}") and rep1["rendered"][0]["ok"], rep1
    assert rep1["normalized"][0]["to"]["title"] == "CO-HOST"
    it = new1["items"][0]
    assert it["slots"]["attribution"] == "Maren" and it["slots"]["title"] == "CO-HOST" and "role" not in it["slots"]
    first = cep.root / it["outputs"][0]["file"]
    sha1 = hashlib.sha256(first.read_bytes()).hexdigest()
    M.save_new_version(cep, new1)
    # a second pass lands in its own folder; nothing from the first is overwritten
    new2, rep2 = RM.render_doc(cep, new1, force=True)
    second = cep.root / new2["items"][0]["outputs"][0]["file"]
    assert second != first and first.is_file() and hashlib.sha256(first.read_bytes()).hexdigest() == sha1
    assert second.parent.name == f"v{v_before + 2}"


def test_renumbered_clips_are_followed_by_identical_text_and_recorded(cep):
    doc, _ = _legacy_doc(cep)
    clips = json.loads((cep.work / "clips.json").read_text())
    for c in clips["clips"]:
        c["id"] = "s9-" + c["id"]
        c["slug"] = "s9-" + c["slug"]
    (cep.work / "clips.json").write_text(json.dumps(clips))
    assert S.verify_sources(cep, doc)                                           # stale: the old ids are gone
    new = M.clone(doc)
    changes = S.rebind_clip_sources(cep, new)
    assert changes and all(c["to"].startswith("s9-") for c in changes)
    assert not S.verify_sources(cep, new)
    src = next(iter(new["sources"].values()))
    assert src["rebound_from"] and src["text_sha256"] == next(iter(doc["sources"].values()))["text_sha256"]
    # text that no longer exists anywhere is NOT rebound
    clips["clips"][0]["text"] = "completely different words now"
    (cep.work / "clips.json").write_text(json.dumps(clips))
    stale = M.clone(doc)
    assert S.rebind_clip_sources(cep, stale) == [] and S.verify_sources(cep, stale)


