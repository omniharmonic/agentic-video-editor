"""The opt-in Canva renderer: job files, approval gate, a fake client driving a job, import into the ContentDoc, failure fallback.
No network, no Canva: every Canva operation goes through a fake `CanvaClient`."""

import json

import pytest
import yaml
from PIL import Image

from ave import cli
from ave.content import manifest as M
from ave.content import registry as R
from ave.content import render_canva as C
from ave.content import settings
from content_helpers import apply_env, cep, make_episode, pick_quote  # noqa: F401
from publish_helpers import isolated_secrets  # noqa: F401
from test_content_flow import picks_file, run


@pytest.fixture(autouse=True)
def content_env(isolated_secrets, monkeypatch, tmp_path):
    apply_env(monkeypatch, tmp_path)


CANVA_CFG = {"renderers": {"canva": {"enabled": True, "kit_id": "KIT1", "limits": {"export_per_minute": 2, "export_per_day": 500, "autofill_per_minute": 60},
                                     "templates": {"quote-post": {"brand_template_id": "BT-QUOTE", "native_format": "4x5",
                                                                  "field_map": {"quote": "QUOTE", "attribution": "BY", "role": "ROLE", "show": "SHOW"}}}}}}


@pytest.fixture
def canva_on(tmp_path):
    p = tmp_path / "cfg" / "content.yaml"
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(yaml.safe_dump(CANVA_CFG))


class FakeClient:
    """Implements CanvaClient with no network. Records every call so tests can assert what was (not) called."""

    def __init__(self, dataset=None, fail_on=None, size=(1080, 1350)):
        self.calls, self.fail_on, self.size = [], fail_on, size
        self._dataset = dataset or {"QUOTE": "text", "BY": "text", "ROLE": "text", "SHOW": "text"}

    def _do(self, name, *a):
        self.calls.append((name, *a))
        if self.fail_on == name:
            raise RuntimeError(f"{name} blew up")

    def dataset(self, brand_template_id):
        self._do("dataset", brand_template_id)
        return self._dataset

    def autofill(self, brand_template_id, fields, title):
        self._do("autofill", brand_template_id)
        return {"design_id": "DAF1"}

    def resize(self, design_id, width, height):
        self._do("resize", design_id, width, height)
        self.size = (width, height)
        return {"design_id": "DAF1-resized"}

    def export(self, design_id, fmt):
        self._do("export", design_id, fmt)
        return {"url": "https://export.example/expiring"}

    def download(self, url, dest):
        self.calls.append(("download", url))
        Image.new("RGB", self.size, (27, 24, 20)).save(dest, format="PNG")
        return dest


@pytest.fixture
def planned(cep, tmp_path, capsys):
    run(capsys, "propose", cep.root, picks_file(tmp_path, [pick_quote()]))
    return cep


def make_job(ep, capsys, *args):
    _, rep = run(capsys, "render", ep.root, "--renderer", "canva", *args)
    return rep


def approve(ep, job_id, monkeypatch, capsys):
    monkeypatch.setenv("AVE_ALLOW_UNATTENDED_APPROVAL", "1")
    code = cli.main(["content", "canva-approve", str(ep.root), job_id, "--by", "ben", "--confirm", C.load_job(ep, job_id)["fields_sha256"][:8]])
    assert code == 0, capsys.readouterr().out
    capsys.readouterr()


def test_canva_is_off_by_default_and_the_inhouse_renderer_is_untouched(planned, capsys):
    ep = planned
    code = cli.main(["content", "render", str(ep.root), "--renderer", "canva", "--item", "quote-c01-4x5"])
    out = json.loads(capsys.readouterr().out)
    assert code == 2 and out["jobs"] == [] and "Canva renderer is off" in out["failed"][0]["error"]
    assert M.load_latest(ep)["items"][0]["renderer"] == "motion-kit" and not C.jobs_dir(ep).exists()


def test_render_canva_writes_a_job_and_calls_nothing(planned, canva_on, capsys):
    ep = planned
    rep = make_job(ep, capsys)
    assert rep["canva_calls_made"] == 0 and len(rep["jobs"]) == 1
    assert rep["jobs"][0]["status"] == "pending-approval" and rep["jobs"][0]["writes"][0] == "autofill-design"
    again = make_job(ep, capsys)                                   # same plan -> same job, not a duplicate
    assert again["jobs"][0]["reused"] and len(list(C.jobs_dir(ep).glob("*.json"))) == 1


def test_job_contents_fields_resize_and_show_dedup(cep, tmp_path, canva_on, capsys):
    run(capsys, "propose", cep.root, picks_file(tmp_path, [pick_quote(format="1x1", id="q1x1")]))
    job = C.load_job(cep, make_job(cep, capsys)["jobs"][0]["id"])
    assert job["fields"] == {"QUOTE": {"type": "text", "text": "A goal is a wish and a system is a habit"},
                             "BY": {"type": "text", "text": "Maren Okafor"}, "ROLE": {"type": "text", "text": "Soil ecologist"},
                             "SHOW": {"type": "text", "text": "The Long Table"}}
    assert job["target"] == {"w": 1080, "h": 1080} and job["brand_template_id"] == "BT-QUOTE" and job["kit_id"] == "KIT1"
    assert [s["op"] for s in job["steps"]] == ["dataset", "autofill", "resize", "export"]          # authored at 4x5, item is 1x1
    assert job["steps"][2]["args"] == {"design_id": "<from step 2>", "width": 1080, "height": 1080}
    assert [s["writes"] for s in job["steps"]] == [False, True, True, True]
    assert job["approval"]["by"] is None and job["tools_verified"] is False and job["download_to"].endswith(".png")


def test_job_blanks_a_slot_that_repeats_the_show(cep, canva_on):
    it = M.new_item("q", "quote-post", "ink", "4x5", {"quote": "A goal is a wish and a system is a habit", "role": "The Long Table", "show": "The Long Table"},
                    reason="t", confidence=0.5)
    job = C.build_job(cep, M.new_doc(cep), it, R.load_registry(), settings.load())
    assert job["fields"]["ROLE"]["text"] == "" and job["fields"]["SHOW"]["text"] == "The Long Table"
    assert [s["op"] for s in job["steps"]] == ["dataset", "autofill", "export"]                     # native 4x5 = item 4x5: no resize


def test_carousel_and_image_slots_are_refused(cep, canva_on):
    cfg = json.loads(json.dumps(settings.load()))
    reg, doc = R.load_registry(), M.new_doc(cep)
    cfg["renderers"]["canva"]["templates"]["carousel"] = {"brand_template_id": "BT-C", "field_map": {"show": "SHOW"}}
    car = M.new_item("c", "carousel", "ink", "4x5", {"show": "x", "cover": {"title": "t"}, "slides": [], "closing": {"line": "l"}}, reason="t", confidence=0.5)
    with pytest.raises(C.CanvaError, match="carousels"):
        C.build_job(cep, doc, car, reg, cfg)
    cfg["renderers"]["canva"]["templates"]["thumbnail"] = {"brand_template_id": "BT-T", "field_map": {"image": "PHOTO", "title": "TITLE"}}
    th = M.new_item("t", "thumbnail", "ink", "16x9", {"title": "Soil", "image": "a.jpg"}, reason="t", confidence=0.5, assets=["a.jpg"])
    with pytest.raises(C.CanvaError, match="only text slots"):
        C.build_job(cep, doc, th, reg, cfg)


def test_unmapped_slot_with_a_value_warns(cep, canva_on):
    cfg = json.loads(json.dumps(settings.load()))
    del cfg["renderers"]["canva"]["templates"]["quote-post"]["field_map"]["role"]
    it = M.new_item("q", "quote-post", "ink", "4x5", {"quote": "A goal is a wish and a system is a habit", "title": "Soil ecologist"}, reason="t", confidence=0.5)
    job = C.build_job(cep, M.new_doc(cep), it, R.load_registry(), cfg)
    assert any("title" in w and "NOT appear" in w for w in job["warnings"])


def test_writes_are_refused_until_the_human_approves(planned, canva_on, capsys, monkeypatch):
    ep = planned
    job_id = make_job(ep, capsys)["jobs"][0]["id"]
    with pytest.raises(C.CanvaError, match="has not approved"):
        C.log_call(ep, job_id, "autofill-design", True)
    C.log_call(ep, job_id, "get-brand-template-dataset", True, "a read needs no approval")
    fake = FakeClient()
    with pytest.raises(C.CanvaError, match="must approve"):
        C.run_job(fake, ep, job_id)
    assert fake.calls == []
    with pytest.raises(SystemExit, match="terminal"):                                  # no tty, no opt-in: an agent cannot approve
        cli.main(["content", "canva-approve", str(ep.root), job_id])
    monkeypatch.setenv("AVE_ALLOW_UNATTENDED_APPROVAL", "1")
    with pytest.raises(SystemExit, match="does not match"):
        cli.main(["content", "canva-approve", str(ep.root), job_id, "--confirm", "00000000"])
    approve(ep, job_id, monkeypatch, capsys)
    assert C.load_job(ep, job_id)["approval"]["by"] == "ben"


def test_full_handshake_with_a_fake_client_then_normal_review_and_approval(planned, canva_on, capsys, monkeypatch, tmp_path):
    ep = planned
    job_id = make_job(ep, capsys)["jobs"][0]["id"]
    approve(ep, job_id, monkeypatch, capsys)
    fake = FakeClient()
    res = C.run_job(fake, ep, job_id)
    assert [c[0] for c in fake.calls] == ["dataset", "autofill", "export", "download"]
    job = C.load_job(ep, job_id)
    assert job["status"] == "running" and [c["tool"] for c in job["log"]] == ["get-brand-template-dataset", "autofill-design", "export-design"]
    assert "export.example" not in json.dumps(job)                                      # the expiring URL is never stored
    _, out = run(capsys, "canva-import", ep.root, job_id, res["file"])
    assert out["version"] == 2 and out["design_id"] == "DAF1" and out["w"] == 1080 and out["h"] == 1350
    doc = M.load_latest(ep)
    it = doc["items"][0]
    assert it["renderer"] == "canva" and it["review"]["status"] == "draft" and it["review"]["critic"]["measured"] is False
    o = it["outputs"][0]
    assert (ep.root / o["file"]).is_file() and o["file"].endswith(".canva.png") and o["alt_text"]
    pc = it["provenance"]["canva"]
    assert pc["design_id"] == "DAF1" and pc["template_id"] == "BT-QUOTE" and pc["kit_id"] == "KIT1" and pc["sha256"] == o["sha256"]
    assert pc["license"]["source"] == "canva-brand-template" and pc["license"]["ai_generated"] is False
    assert C.load_job(ep, job_id)["status"] == "imported"
    with pytest.raises(C.CanvaError, match="already imported"):
        C.import_export(ep, doc, job_id, res["file"], R.load_registry())
    # review page renders (unmeasured chip), and the normal human decide flow applies
    _, rv = run(capsys, "review", ep.root)
    assert "not measured" in open(rv["index"]).read()
    d = ep.root / "decisions.json"
    d.write_text(json.dumps([{"id": it["id"], "decision": "approve", "note": "looked at it"}]))
    run(capsys, "decide", ep.root, d, "--by", "ben")
    assert M.load_latest(ep)["items"][0]["review"]["status"] == "approved"
    _, rep = run(capsys, "render", ep.root)                                             # default render never touches a Canva item
    assert rep["rendered"] == [] and "approved" in rep["skipped"][0]["why"]


def test_resize_step_runs_for_a_non_native_format(cep, tmp_path, canva_on, capsys, monkeypatch):
    run(capsys, "propose", cep.root, picks_file(tmp_path, [pick_quote(format="1x1", id="q1x1")]))
    job_id = make_job(cep, capsys)["jobs"][0]["id"]
    approve(cep, job_id, monkeypatch, capsys)
    fake = FakeClient()
    res = C.run_job(fake, cep, job_id)
    assert ("resize", "DAF1", 1080, 1080) in fake.calls and ("export", "DAF1-resized", "png") in fake.calls
    _, out = run(capsys, "canva-import", cep.root, job_id, res["file"])
    assert (out["w"], out["h"]) == (1080, 1080)


def test_import_refusals(planned, canva_on, capsys, monkeypatch, tmp_path):
    ep = planned
    job_id = make_job(ep, capsys)["jobs"][0]["id"]
    good = tmp_path / "x.png"
    Image.new("RGB", (1080, 1350)).save(good)
    doc, reg = M.load_latest(ep), R.load_registry()
    with pytest.raises(C.CanvaError, match="needs an approved job"):
        C.import_export(ep, doc, job_id, good, reg)
    approve(ep, job_id, monkeypatch, capsys)
    wrong = tmp_path / "wrong.png"
    Image.new("RGB", (1080, 1080)).save(wrong)
    with pytest.raises(C.CanvaError, match="needs 1080x1350"):
        C.import_export(ep, doc, job_id, wrong, reg)
    (tmp_path / "x.mp4").write_bytes(b"x")
    with pytest.raises(C.CanvaError, match="extension"):
        C.import_export(ep, doc, job_id, tmp_path / "x.mp4", reg)
    with pytest.raises(C.CanvaError, match="not found"):
        C.import_export(ep, doc, job_id, tmp_path / "nope.png", reg)
    changed = M.clone(doc)
    changed["items"][0]["slots"]["attribution"] = "Someone Else"
    with pytest.raises(C.CanvaError, match="changed since job"):
        C.import_export(ep, changed, job_id, good, reg)
    assert C.load_job(ep, job_id)["status"] == "approved"                              # nothing half-imported


def test_rate_limit_is_enforced_and_logged(planned, canva_on, capsys, monkeypatch):
    ep = planned
    job_id = make_job(ep, capsys)["jobs"][0]["id"]
    approve(ep, job_id, monkeypatch, capsys)
    now = [1000.0]
    lim = C.RateLimiter({"export_per_minute": 2}, clock=lambda: now[0])
    lim.record("export"), lim.record("export")
    with pytest.raises(C.RateLimited) as e:
        C.run_job(FakeClient(), ep, job_id, limiter=lim)
    assert "export per minute" in str(e.value)
    assert C.load_job(ep, job_id)["log"][-1]["ok"] is False
    now[0] += 61
    lim.check("export")                                                                 # the window slid


def test_missing_template_field_or_client_error_fails_loudly_and_falls_back_recorded(planned, canva_on, capsys, monkeypatch):
    ep = planned
    job_id = make_job(ep, capsys)["jobs"][0]["id"]
    approve(ep, job_id, monkeypatch, capsys)
    fake = FakeClient(dataset={"QUOTE": "text"})
    with pytest.raises(C.CanvaError, match="lacks text field"):
        C.run_job(fake, ep, job_id)
    assert [c[0] for c in fake.calls] == ["dataset"]                                    # stopped before any write
    fake2 = FakeClient(fail_on="export")
    with pytest.raises(C.CanvaError, match="export-design failed"):
        C.run_job(fake2, ep, job_id)
    _, out = run(capsys, "canva-fail", ep.root, job_id, "--reason", "export failed: license_required")
    assert C.load_job(ep, job_id)["status"] == "failed" and "falls back" in out["next"]
    it = M.load_latest(ep)["items"][0]
    assert it["provenance"]["canva"]["fallback"]["reason"] == "export failed: license_required" and it["renderer"] == "motion-kit"
    _, rep = run(capsys, "render", ep.root, "--item", it["id"])                         # the in-house fallback keeps the record
    it2 = M.load_latest(ep)["items"][0]
    assert rep["rendered"] and it2["renderer"] == "motion-kit" and it2["provenance"]["canva"]["fallback"]["job"] == job_id


def test_named_inhouse_render_after_a_canva_import_records_the_switch(planned, canva_on, capsys, monkeypatch):
    ep = planned
    job_id = make_job(ep, capsys)["jobs"][0]["id"]
    approve(ep, job_id, monkeypatch, capsys)
    res = C.run_job(FakeClient(), ep, job_id)
    run(capsys, "canva-import", ep.root, job_id, res["file"])
    _, rep = run(capsys, "render", ep.root)
    assert rep["rendered"] == [] and "rendered by Canva" in rep["skipped"][0]["why"]
    run(capsys, "render", ep.root, "--item", "quote-c01-4x5")
    it = M.load_latest(ep)["items"][0]
    assert it["renderer"] == "motion-kit" and it["provenance"]["canva"]["fallback"]["was"] == "DAF1" and it["outputs"][0]["file"].endswith("quote-c01-4x5.png")


def test_registry_only_accepts_the_canva_renderer_when_this_install_mapped_the_template(planned, canva_on):
    reg = R.load_registry()
    item = {**M.load_latest(planned)["items"][0], "renderer": "canva"}
    assert reg.check_item_shape(item) == []
    assert R.canva_configured(settings.load(), "thumbnail") is False
    assert any("renderer 'canva'" in p for p in reg.check_item_shape({**item, "template": "thumbnail", "style": "ink", "format": "16x9",
                                                                          "slots": {"title": "t", "show": "x"}}))


def test_brand_brief_and_status_are_local_only(planned, canva_on, capsys):
    _, brief = run(capsys, "canva-brief")
    assert brief["palette"]["clay"] == "#b9825a" and "ONE WAY" in brief["direction"] and brief["fonts"]["serif"]["family"] == "Newsreader"
    make_job(planned, capsys)
    _, st = run(capsys, "canva-status", planned.root)
    assert st["canva_enabled"] and st["mapped_templates"] == ["quote-post"] and st["jobs"][0]["status"] == "pending-approval"


def test_protocol_is_satisfied_by_the_fake():
    assert isinstance(FakeClient(), C.CanvaClient)
