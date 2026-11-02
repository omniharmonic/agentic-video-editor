"""License coverage over the derived_from closure (spec 2.4, 5.3 step 4). Every leaf must have a license-log entry for exactly its bytes."""

from __future__ import annotations

from ..assets import licenses
from . import derive, registry


def coverage(sp, asset_ids) -> dict:
    """{"covered", "missing", "unknown", "attributions", "ok"} over the closure of `asset_ids`.

    A leaf is covered when the license log holds an entry whose sha256 equals the bytes on disk (the same match `licenses.coverage` makes,
    against the studio's own log). A `render` with no inputs contains nothing third-party by construction and is covered as "made here".
    A derived asset is covered when all of its inputs are. A missing file, or bytes that no longer match their name, count as missing: we
    cannot vouch for them."""
    from .ingest import attestation_events, attestation_for
    events = attestation_events(sp)
    recs = registry.read_all(sp)
    order, unknown = derive.closure(recs, asset_ids)
    log = registry.license_log(sp)
    covered, missing, attributions = [], [], []
    for a in order:
        rec = recs[a]
        if rec.get('untrusted_import'):
            # A sender's render claim (including a zero-parent render) is not
            # local authorship or license authority. Require receiver attestation
            # for this occurrence even when its parents already have coverage.
            entry = log.find(sha256=rec['sha256'])
            local_state = attestation_for(events, a, rec['sha256'], licenses.entry_hash(entry) if entry else None)
            if not entry or local_state not in ('attested', 'legacy_attested'):
                missing.append({'asset': a, 'why': 'untrusted imported occurrence needs local human license attestation'})
                continue
        if rec.get("derived_from"):
            continue
        p = registry.asset_file(sp, rec)
        if not p.is_file():
            missing.append({"asset": a, "why": f"file missing ({rec['file']})"})
            continue
        if licenses.sha256_file(p) != rec["sha256"]:
            missing.append({"asset": a, "why": "bytes do not match the recorded sha256"})
            continue
        e = log.find(sha256=rec["sha256"])
        state=attestation_for(events,a,rec['sha256'],licenses.entry_hash(e) if e else None)
        if state in ('withdrawn','stale'):
            missing.append({"asset":a,"why":"license attestation was withdrawn or does not match the current license entry"})
        elif e and e.get("attested") is False and state not in ('legacy_attested','attested'):
            missing.append({"asset": a, "why": "the license was claimed by an agent-reachable path (library add / adopt) and is not attested by a human: "
                                               "`ave studio library attest`"})
        elif e:
            covered.append({"asset": a, "license": e["license"], "source": e["source"], "entry": licenses.entry_hash(e)})
            att = e.get("attribution") or {}
            if att.get("required") and att.get("text") and att["text"] not in attributions:
                attributions.append(att["text"])
        elif (rec.get("source") or {}).get("type") == "render":
            covered.append({"asset": a, "license": "made here (no inputs)", "source": "render", "entry": None})
        else:
            missing.append({"asset": a, "why": "no license-log entry for these bytes"})
    for u in unknown:
        missing.append({"asset": u, "why": "unknown asset id (not in the registry)"})
    return {"covered": covered, "missing": missing, "unknown": unknown, "attributions": attributions, "ok": not missing}
