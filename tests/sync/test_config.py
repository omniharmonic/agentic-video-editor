"""The Syncthing configuration built from project.json: private network paths, exFAT-safe folders, members only."""

from __future__ import annotations

import pytest

from ave.syncthing import config as C

from .conftest import SID


def test_public_paths_are_off(cfg):
    o = cfg["options"]
    assert not (o["relaysEnabled"] or o["globalAnnounceEnabled"] or o["localAnnounceEnabled"] or o["natEnabled"])
    assert o["urAccepted"] == -1 and o["autoUpgradeIntervalH"] == 0 and not o["startBrowser"] and not o["crashReportingEnabled"]
    assert o["listenAddresses"] == ["tcp://100.64.0.1:22417"]


def test_folder_uses_trashcan_and_exfat_settings(cfg):
    f = cfg["folders"][0]
    assert f["ignorePerms"] and f["modTimeWindowS"] == 2 and f["versioning"]["type"] == "trashcan"
    assert f["versioning"]["params"] == {"cleanoutDays": "0"} and f["markerName"] == ".stfolder"
    assert f["id"] == "prj_TEST" and f["type"] == "sendreceive"


def test_folders_never_auto_accept(cfg):
    assert not any(d["autoAcceptFolders"] for d in cfg["devices"])
    assert not any(d["introducer"] for d in cfg["devices"])


def test_only_listed_members_get_a_folder(cfg):
    assert [d["deviceID"] for d in cfg["folders"][0]["devices"]] == [SID["A"], SID["B"]]
    assert SID["C"] not in {d["deviceID"] for d in cfg["devices"]}
    tomas = next(d for d in cfg["devices"] if d["deviceID"] == SID["B"])
    assert tomas["addresses"] == ["tcp://100.64.0.2:22417"]
    assert tomas["allowedNetworks"] == ["100.64.0.0/10", "fd7a:115c:a1e0::/48"]


@pytest.mark.parametrize("bad", ["0.0.0.0", "::", "dynamic", "default", "tcp://0.0.0.0:22000", "quic://100.64.0.1:22000",
                                 "example.com", "192.168.1.5", "8.8.8.8", "127.0.0.1", "fe80::1"])
def test_wildcard_or_dynamic_listen_is_refused(bad):
    with pytest.raises(C.ConfigError):
        C.listen_url(bad)


def test_prepare_folder_writes_this_devices_ignores_and_marker(two_member_project):
    text = C.prepare_folder(two_member_project, "dev_B")          # tomas keeps proxies only
    assert (two_member_project / ".stfolder").is_dir()
    assert (two_member_project / ".stignore").read_text() == text
    assert "!/raw/proxies" in text and "\n/raw\n" in text and text.rstrip().endswith("*")


def test_prepare_folder_refuses_a_missing_drive(tmp_path):
    with pytest.raises(C.ConfigError):
        C.prepare_folder(tmp_path / "gone", "dev_A")


def test_loopback_only_when_allowed():
    assert C.listen_url("127.0.0.1", 23000, allow_loopback=True) == "tcp://127.0.0.1:23000"
    assert C.listen_url("fd7a:115c:a1e0::5") == "tcp://[fd7a:115c:a1e0::5]:22417"


@pytest.mark.parametrize("pid", ["", "../x", "prj_a/b", "prj_ ", None, 7, "default"])
def test_a_bad_project_id_is_refused(two_member_project, pid):
    import json
    meta = json.loads((two_member_project / ".ave" / "project.json").read_text())
    with pytest.raises(C.ConfigError):
        C.folder_entry(two_member_project, {**meta, "project_id": pid}, SID["A"], [])
