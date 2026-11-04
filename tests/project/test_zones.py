"""One sync policy: zone_of, the generated .stignore and the doctor check all read the same rules."""

from __future__ import annotations

import fnmatch
import json
import re

import pytest

from ave import doctor
from ave.episode import PRESETS
from ave.project import zones
from ave.project.zones import Rule, rules, stignore, zone_of

FLAT = PRESETS["flat"]
NUMBERED = PRESETS["numbered"]
STUDIO = {"preset": "flat", **PRESETS["flat"], "raw_dirs": ["inbox"]}
FULL, PROXIES = {"media": "full"}, {"media": "proxies"}


@pytest.mark.parametrize("path,zone", [
    (".ave/log/ABCD.jsonl", "log"), (".ave/project.json", "settings"), (".ave/local/index.sqlite", "local"),
    ("02_FOOTAGE/a.mov", "permanent"), ("01_PROJECT/edits/rough.v1.json", "permanent"),
    ("01_PROJECT/work/render/a.wav", "local"), ("01_PROJECT/publish/queue/x.json", "local"),
    ("anything/new.bin", "local"),
    ("episode.json", "settings"), ("01_PROJECT/captions/a.srt", "permanent"), ("01_PROJECT/work/transcripts/a.json", "permanent"),
    ("03_AUDIO/a.wav", "permanent"), ("06_EXPORTS/final.mp4", "permanent"), ("PROGRESS.md", "local"),
    (".ave/log/.ABCD.jsonl.tmp", "local"), (".ave/local/locks/ABCD.jsonl.lock", "local"),
    ("02_FOOTAGE/._a.mov", "local"), ("01_PROJECT/edits/.DS_Store", "local"),
    ("01_PROJECT/edits/rough.v1.json.sync-conflict-20261102-ABCD.json", "permanent"),
])
def test_zones(path, zone):
    assert zone_of(path, rules(NUMBERED, FULL)) == zone


def test_proxies_member_excludes_originals_but_keeps_proxies():
    full, prox = rules(NUMBERED, FULL), rules(NUMBERED, PROXIES)
    for raw in ("02_FOOTAGE", "03_AUDIO"):
        assert zone_of(f"{raw}/a.mov", full) == "permanent"
        assert zone_of(f"{raw}/a.mov", prox) == "local"
        assert zone_of(f"{raw}/sub/b.wav", prox) == "local"
        assert zone_of(f"{raw}/proxies/a.mp4", prox) == "permanent"
        assert zone_of(f"{raw}/proxies/a.mp4", full) == "permanent"
    assert zone_of("01_PROJECT/edits/rough.v1.json", prox) == "permanent"
    assert zone_of("06_EXPORTS/final.mp4", prox) == "permanent"


def test_bad_media_policy_is_refused():
    with pytest.raises(ValueError):
        rules(FLAT, {"media": "everything"})


def test_flat_and_numbered_layouts_both_work():
    flat, num = rules(FLAT, FULL), rules(NUMBERED, FULL)
    assert zone_of("raw/a.mov", flat) == "permanent" and zone_of("raw/a.mov", num) == "local"
    assert zone_of("edits/rough.v1.json", flat) == "permanent" and zone_of("edits/rough.v1.json", num) == "local"
    assert zone_of("captions/a.srt", flat) == "permanent"
    assert zone_of("work/transcripts/a.json", flat) == "permanent"
    assert zone_of("work/render/a.wav", flat) == "local"
    assert zone_of("publish/queue/x.json", flat) == "local"
    assert zone_of("exports/a.mp4", flat) == "permanent"
    assert zone_of("work/gfx/lower.png", flat) == "permanent" and zone_of("04_GFX/lower.png", num) == "permanent"
    assert zone_of("05_MUSIC/bed.wav", num) == "permanent"
    custom = {**FLAT, "raw_dirs": ["cams", "mics"]}
    assert zone_of("mics/a.wav", rules(custom, FULL)) == "permanent"
    assert zone_of("mics/a.wav", rules(custom, PROXIES)) == "local"


STUDIO_ZONES = {
    "studio.json": "settings", "posts/p1.json": "settings", "campaigns/c1.json": "settings", "recipes/r.yaml": "settings",
    "schedule/proposals/a.json": "settings", "schedule/acct1.yaml": "settings",
    "schedule/state/acct1.json": "local", "schedule/heartbeat.json": "local", "library/assets/a.png": "permanent",
    "accounts/acct1.yaml": "local", "accounts/acct1.live.json": "local", "ledger/events.jsonl": "local",
    "ledger/.lock": "local", "policy/publish.yaml": "local", "publish/queue/q.json": "local", "PAUSE": "local",
    "work/x.json": "local", "exports/a.mp4": "permanent", "inbox/a.mov": "permanent", "edits/e.json": "permanent",
}


@pytest.mark.parametrize("path,zone", sorted(STUDIO_ZONES.items()))
def test_studio_folders(path, zone):
    assert zone_of(path, rules(STUDIO, FULL, studio=True)) == zone


STUDIO_DIR_ZONES = {
    "posts": "settings", "campaigns": "settings", "accounts": "local", "library/assets": "permanent",
    "schedule/proposals": "settings", "schedule/state": "local", "ledger": "local", "policy": "local", "recipes": "settings",
    "publish/queue": "local", "work": "local", "exports": "permanent", "inbox": "permanent",
}


def test_studio_dirs_all_decided():
    """A new studio folder fails here until someone decides its zone."""
    from ave.studio.project import DIRS
    assert set(DIRS) == set(STUDIO_DIR_ZONES)
    rl = rules(STUDIO, FULL, studio=True)
    for d, zone in STUDIO_DIR_ZONES.items():
        assert zone_of(d + "/x", rl) == zone, d

# ---------------------------------------------------------------------------------------------------------------- tree

def _tree(layout: dict, studio: bool) -> list[str]:
    raw = layout["raw_dirs"]
    p = [".ave/project.json", ".ave/log/ABCD-1.jsonl", ".ave/log/EFGH-2.jsonl", ".ave/log/.ABCD-1.jsonl.tmp",
         ".ave/local/index.sqlite", ".ave/local/locks/ABCD-1.jsonl.lock", ".ave/local/room.json", ".ave/local/sync.json",
         ".ave/stray.txt", "PROGRESS.md", "notes.txt", "anything/new.bin", ".DS_Store", ".env", "token.json", "x/approval.key", "x/y/server.pem", "._PROGRESS.md",
         "studio.json" if studio else "episode.json", "episode.json.sync-conflict-20261102-ABCD.json"]
    for d in raw:
        p += [f"{d}/a.mov", f"{d}/sub/b.wav", f"{d}/._a.mov", f"{d}/.DS_Store", f"{d}/proxies/a.mp4", f"{d}/proxies/sub/b.mp4"]
    for k in ("edits", "captions", "exports", "gfx", "music"):
        p += [f"{layout[k]}/x.json", f"{layout[k]}/deep/y.json", f"{layout[k]}/._x.json", f"{layout[k]}/x.json.sync-conflict-1.json"]
    p += [f"{layout['work']}/render/a.wav", f"{layout['work']}/transcripts/a.json", f"{layout['work']}/transcripts/s/b.json",
          f"{layout['work']}/cache/c.bin", f"{layout['work']}/transcripts.tmp",
          f"{layout['publish']}/queue/x.json", f"{layout['publish']}/log.txt"]
    if studio:
        p += list(STUDIO_ZONES) + ["posts/deep/p.json", "library/assets/sub/a.png", "library/other.txt", "schedule/proposals/p/q.json",
                                   "accounts/x.paused", "policy/secrets.yaml", "unknown/dir/file.txt"]
    return p


def _seg_match(pat: list[str], path: list[str], start: int) -> bool:
    return len(path) - start >= len(pat) and all(fnmatch.fnmatchcase(path[start + i], s) for i, s in enumerate(pat))


def _ignored_by_stignore(text: str, rel: str, ci: bool = False) -> bool:
    """Evaluate a .stignore the way Syncthing does: first matching line wins, `!` includes, a pattern also matches whatever is
    beneath the path it names, no match means synced. Written separately from zones._regex on purpose."""
    path = (rel.lower() if ci else rel).split("/")
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("(?d)"):
            line = line[4:]
        line = line.lower() if ci else line
        include = line.startswith("!")
        pat = line.lstrip("!")
        assert "**" not in pat
        rooted = pat.startswith("/")
        segs = [re.sub(r"\\(.)", r"[\1]", g) for g in re.split(r"(?<!\\)/", pat.strip("/"))]
        starts = [0] if rooted else range(len(path))
        if any(_seg_match(segs, path, s) for s in starts):
            return not include
    return False


@pytest.mark.parametrize("layout,studio", [(FLAT, False), (NUMBERED, False), (STUDIO, True)], ids=["flat", "numbered", "studio"])
@pytest.mark.parametrize("member", [FULL, PROXIES], ids=["full", "proxies"])
def test_stignore_matches_zone_of_for_every_path(layout, studio, member):
    rl = rules(layout, member, studio=studio)
    text = stignore(rl)
    tree = _tree(layout, studio)
    assert len(tree) > 40
    for rel in tree:
        syncs = zone_of(rel, rl) != "local"
        assert _ignored_by_stignore(text, rel) == (not syncs), f"{rel}: zone {zone_of(rel, rl)}\n{text}"
    zs = {zone_of(r, rl) for r in tree}
    assert zs == {"log", "permanent", "settings", "local"}      # the tree really exercises every zone


def test_stignore_shape():
    text = stignore(rules(NUMBERED, FULL))
    lines = [x for x in text.splitlines() if not x.startswith("#")]
    assert lines[0] == "*.pem" and lines[-1] == "*"
    assert lines.index("(?d).DS_Store") < lines.index("!/.ave/project.json")
    assert "!/.ave/project.json" in lines and "/.ave" in lines and "/01_PROJECT/work" in lines
    assert lines.index("!/01_PROJECT/work/transcripts") < lines.index("/01_PROJECT/work")
    assert "sync-conflict" not in text and "syncthing" not in text.lower().replace("# generated", "")  # never hide conflicts
    prox = stignore(rules(NUMBERED, PROXIES)).splitlines()
    assert prox.index("!/02_FOOTAGE/proxies") < prox.index("/02_FOOTAGE")


def test_first_match_wins_and_default_is_local():
    rl = [Rule("/a/b", "permanent"), Rule("/a", "settings")]
    assert zone_of("a/b/c", rl) == "permanent" and zone_of("a/x", rl) == "settings" and zone_of("z", rl) == "local"
    assert zone_of("ab/c", rl) == "local"            # a glob names a folder, not a string prefix


# ---------------------------------------------------------------------------------------------------------------- doctor

def _make(root, layout="numbered", media="full", device="ABCD"):
    (root / ".ave").mkdir(parents=True, exist_ok=True)
    (root / ".ave" / "project.json").write_text(json.dumps({"v": 1, "project_id": "p", "layout": layout, "members": [
        {"person": "maren", "devices": ["ABCD"], "media": media}]}))
    (root / "episode.json").write_text(json.dumps({"layout": layout}))   # the writers' layout, which the rules follow
    from ave.project.model import device_file
    device_file().parent.mkdir(parents=True, exist_ok=True)
    device_file().write_text(json.dumps({"device_id": device}))


def _touch(root, rels):
    for rel in rels:
        (root / rel).parent.mkdir(parents=True, exist_ok=True)
        (root / rel).write_text("x")


UNAMBIGUOUS = [".env", ".env.local", "a/server.pem", "a/b.p12", "a/b.pfx", "a/b.jks", ".netrc", ".npmrc", "a/approval.key",
               "a/id_rsa", "a/id_rsa.pub", "a/id_ed25519", "a/client_secret_1.json", "a/credentials.json", "a/.env.production", "a/.env.dev.local",
               "a/token.json", "a/google_token.json"]


@pytest.mark.parametrize("name", UNAMBIGUOUS)
@pytest.mark.parametrize("layout", [FLAT, NUMBERED])
def test_unambiguous_credential_names_never_sync_in_any_zone(name, layout):
    for member in (FULL, PROXIES):
        rl = rules(layout, member, studio=True)
        for prefix in ("", "02_FOOTAGE/", "raw/", "01_PROJECT/edits/", "edits/", ".ave/log/", "posts/", "exports/proxies/"):
            assert zone_of(prefix + name, rl) == "local", prefix + name
        assert _ignored_by_stignore(stignore(rl), "01_PROJECT/edits/" + name)
    assert stignore(rules(layout, FULL)).splitlines()[1:4] == list(zones.CREDENTIAL_LOCAL_GLOBS[:3])   # first, before any syncing rule


def test_credential_like_files_are_reported_by_name_wherever_they_sit(tmp_path):
    _make(tmp_path)
    reported = ["02_FOOTAGE/token.txt", "01_PROJECT/edits/client_secret_1.json", "01_PROJECT/captions/approval.key", ".env",
                "06_EXPORTS/server.pem", "01_PROJECT/edits/sub/notes.env", "01_PROJECT/edits/my_secret.txt",
                "01_PROJECT/work/token.json", "01_PROJECT/edits/credentials-prod.json", "01_PROJECT/Token.JSON"]    # work/ is walked because work/transcripts syncs
    quiet = ["01_PROJECT/publish/client_secret.json", ".ave/local/approval.key", ".ave/local/deep/api.key",
             "02_FOOTAGE/clip.mov", "02_FOOTAGE/secret_garden.mov", "02_FOOTAGE/token_walk.wav", "06_EXPORTS/the_secret.mp4",
             "01_PROJECT/captions/secret.srt", "01_PROJECT/work/render/x.pem"]
    _touch(tmp_path, reported + quiet)
    assert zones.credential_warnings(tmp_path, zones.project_rules(tmp_path)) == sorted(reported)


def test_broad_names_are_warn_only_so_they_still_sync():
    rl = rules(NUMBERED, FULL)
    assert zone_of("01_PROJECT/edits/my_secret.txt", rl) == "permanent"      # broad name: reported, not blocked
    assert zone_of("01_PROJECT/edits/token.json", rl) == "local"             # unambiguous name: blocked


def test_doctor_check_warns_only_for_projects_with_leaks(tmp_path):
    assert doctor.project_sync_checks(tmp_path) == []
    _make(tmp_path)
    (tmp_path / "02_FOOTAGE").mkdir()
    [c] = doctor.project_sync_checks(tmp_path)
    assert c.status == doctor.OK
    _touch(tmp_path, ["02_FOOTAGE/id_token.txt"])
    [c] = doctor.project_sync_checks(tmp_path)
    assert c.status == doctor.WARN and "02_FOOTAGE/id_token.txt" in c.detail and c.fix


def test_doctor_follows_the_members_media_policy(tmp_path):
    _make(tmp_path, media="proxies")
    _touch(tmp_path, ["02_FOOTAGE/secret.txt", "02_FOOTAGE/proxies/secret_note.txt"])   # raw dir is local for a proxies member
    [c] = doctor.project_sync_checks(tmp_path)
    assert c.status == doctor.WARN and "proxies/secret_note.txt" in c.detail and "02_FOOTAGE/secret.txt" not in c.detail


def test_unlisted_device_gets_proxies(tmp_path):
    _make(tmp_path, device="SOMEONE-ELSE")
    rl = zones.project_rules(tmp_path)
    assert zone_of("02_FOOTAGE/a.mov", rl) == "local" and zone_of("02_FOOTAGE/proxies/a.mp4", rl) == "permanent"
    assert zone_of("02_FOOTAGE/a.mov", zones.project_rules(tmp_path, "ABCD")) == "permanent"


@pytest.mark.parametrize("text", ["[]", "[1]", '"x"', "null", "{nope", '{"members": 3}'])
def test_odd_project_json_never_crashes_the_doctor(tmp_path, text):
    (tmp_path / ".ave").mkdir()
    (tmp_path / ".ave" / "project.json").write_text(text)
    [c] = doctor.project_sync_checks(tmp_path)
    assert c.status in (doctor.WARN, doctor.OK)
    if text != '{"members": 3}':
        assert c.status == doctor.WARN


def test_local_folders_are_pruned_unless_a_syncing_rule_lives_inside(tmp_path, monkeypatch):
    _make(tmp_path)
    _touch(tmp_path, ["01_PROJECT/work/cache/a.txt", "01_PROJECT/work/transcripts/my_secret.txt", ".ave/log/my_secret.txt"])
    visited = []
    real = type(tmp_path).iterdir

    def spy(self):
        visited.append(self.relative_to(tmp_path).as_posix())
        return real(self)
    monkeypatch.setattr(type(tmp_path), "iterdir", spy)
    hits = zones.credential_warnings(tmp_path, zones.project_rules(tmp_path))
    assert "01_PROJECT/work/cache" not in visited and ".ave/local" not in visited
    assert "01_PROJECT/work/transcripts" in visited and ".ave/log" in visited and "01_PROJECT/work" in visited
    assert hits == ["01_PROJECT/work/transcripts/my_secret.txt"]


# ---------------------------------------------------------------------------------------------------------- odd names

ODD = {**NUMBERED, "work": "01 [PROJECT]/work", "publish": "01 [PROJECT]/publish", "edits": "01 [PROJECT]/ed{its}",
       "captions": "01 [PROJECT]/cap*s", "raw_dirs": ["02 FOOTAGE?"], "exports": "!out", "music": "#music"}


@pytest.mark.parametrize("member", [FULL, PROXIES], ids=["full", "proxies"])
def test_layout_names_with_pattern_characters_are_escaped(member):
    rl = rules(ODD, member)
    text = stignore(rl)
    assert "01 \\[PROJECT\\]/work" in text and "ed\\{its\\}" in text and "cap\\*s" in text and "02 FOOTAGE\\?" in text
    cases = {"01 [PROJECT]/ed{its}/a.json": "permanent", "01 [PROJECT]/cap*s/a.srt": "permanent", "01 [PROJECT]/capXs/a.srt": "local",
             "01 [PROJECT]/work/transcripts/a.json": "permanent", "01 [PROJECT]/work/render/a.wav": "local",
             "01 [PROJECT]/publish/q.json": "local", "01 P/work/x": "local", "01 R/ed{its}/a.json": "local",
             "02 FOOTAGE?/a.mov": "local" if member is PROXIES else "permanent", "02 FOOTAGE?/proxies/a.mp4": "permanent",
             "02 FOOTAGEX/a.mov": "local", "!out/a.mp4": "permanent", "#music/a.wav": "permanent"}
    for rel, zone in cases.items():
        assert zone_of(rel, rl) == zone, rel
        assert _ignored_by_stignore(text, rel) == (zone == "local"), rel


@pytest.mark.parametrize("bad", ["", ".", "/abs", "../up", "a/../../b", "C:/x", "\\x"])
def test_layout_values_outside_the_project_are_refused(bad):
    with pytest.raises(ValueError, match="layout"):
        rules({**FLAT, "work": bad}, FULL)
    with pytest.raises(ValueError, match="layout"):
        rules({**FLAT, "raw_dirs": [bad]}, FULL)


def test_matching_is_case_insensitive_where_syncthing_is():
    rl = rules(NUMBERED, FULL)
    assert zone_of("02_footage/A.MOV", rl, ci=True) == "permanent" and zone_of("02_footage/A.MOV", rl, ci=False) == "local"
    assert zone_of("x/APPROVAL.KEY", rl, ci=True) == "local"


# ----------------------------------------------------------------------------------------------- media is never a credential

MEDIA_LOOKALIKES = ["footage/client_secret_interview.mov", "edits/id_rsa_walk.mp4", "footage/.env-shots/a.mov",
                    "footage/.env-shots/sub/b.wav", "footage/credentials_scene.json", "footage/credentials-take2.json",
                    "footage/id_ed25519_closeup.mov", "footage/token_walk.wav", "footage/.environment.mov", "footage/.envelope.png",
                    "footage/client_secret.mp4", "footage/key.pem.mov", "footage/credentials.mov", "footage/.env.mov"]


def test_media_lookalikes_sync_in_the_raw_folder():
    layout = {**FLAT, "raw_dirs": ["footage"], "edits": "edits"}
    rl = rules(layout, FULL)
    text = stignore(rl)
    for rel in MEDIA_LOOKALIKES:
        if rel.startswith("edits/"):
            continue
        assert zone_of(rel, rl) == "permanent", rel
        assert not _ignored_by_stignore(text, rel), rel
    assert zone_of("edits/id_rsa_walk.mp4", rl) == "permanent" and zone_of("edits/client_secret_interview.mov", rl) == "permanent"
    assert zone_of("footage/.env-shots/a.mov", rules(layout, PROXIES)) == "local"            # raw is local for proxies, not credentials


REAL = ["footage/client_secret_123.json", "footage/id_rsa", "footage/id_rsa.pub", "footage/.env", "footage/.env.local",
        "footage/credentials.json", "footage/token.json", "footage/google_token.json", "footage/Token.JSON", "footage/x.pem",
        "edits/credentials-prod.json", "edits/credentials_prod.json", "captions/Credentials_prod.json", "exports/approval.key"]


def test_real_credential_files_stay_local():
    layout = {**FLAT, "raw_dirs": ["footage"]}
    rl = rules(layout, FULL)
    text = stignore(rl)
    for rel in REAL:
        if rel.startswith("footage/credentials_") or rel.startswith("footage/credentials-"):
            continue
        assert zone_of(rel, rl, ci=True) == "local", rel
        assert _ignored_by_stignore(text, rel, ci=True), rel


def test_no_credential_glob_can_match_a_media_extension():
    for g in (*zones.CREDENTIAL_LOCAL_GLOBS, *zones.CREDENTIAL_LATE_GLOBS):
        ext = "." + g.rsplit(".", 1)[-1] if "." in g.lstrip(".") else ""
        assert ext.lower() not in zones._MEDIA_EXTS, g
        for m in sorted(zones._MEDIA_EXTS):
            probe = re.escape(g).replace(r"\*", ".*")
            assert not re.fullmatch(probe, "x" + m, re.I), (g, m)
            assert not re.fullmatch(probe, "id_rsa" + m, re.I) and not re.fullmatch(probe, ".env" + m, re.I), (g, m)
    from ave.episode import MEDIA_EXTS
    assert MEDIA_EXTS <= zones._MEDIA_EXTS


def test_credential_globs_match_files_not_folder_lookalikes():
    rl = rules(FLAT, FULL)
    assert zone_of("raw/.env-shots/a.mov", rl) == "permanent" and zone_of("raw/.environment/a.mov", rl) == "permanent"


def test_doctor_reports_a_folder_a_credential_rule_swallows(tmp_path):
    _make(tmp_path)
    _touch(tmp_path, ["02_FOOTAGE/.env/a.mov", "02_FOOTAGE/.env-shots/a.mov", "02_FOOTAGE/credentials_scene.json",
                      "01_PROJECT/edits/credentials-prod.json"])
    assert zones.credential_warnings(tmp_path, zones.project_rules(tmp_path)) == [
        "01_PROJECT/edits/credentials-prod.json", "02_FOOTAGE/.env/", "02_FOOTAGE/credentials_scene.json"]   # last: warn-only, still syncs
    assert zone_of("02_FOOTAGE/credentials_scene.json", zones.project_rules(tmp_path)) == "permanent"
