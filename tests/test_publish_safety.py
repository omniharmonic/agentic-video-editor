import logging

import pytest
from publish_helpers import *  # noqa: F401,F403

from ave.publish import safety


def test_sha256_streaming(tmp_path):
    p = tmp_path / "f"
    p.write_bytes(b"abc" * 1000)
    import hashlib
    assert safety.sha256_file(p) == hashlib.sha256(b"abc" * 1000).hexdigest()


def test_redact_patterns_and_known_values():
    safety.register_secret("supersecretvalue123")
    s = safety.redact("failed https://g/x?access_token=EAAB1234567890abcdefghij&a=1 using supersecretvalue123 "
                      "Authorization: Bearer abcdef123456789 ya29.a0AfH6SMBxxxxxxxxxxxxxx")
    for leak in ("EAAB1234567890abcdefghij", "supersecretvalue123", "abcdef123456789", "ya29.a0AfH6SMB"):
        assert leak not in s
    assert "a=1" in s


def test_redact_obj_masks_sensitive_keys():
    out = safety.redact_obj({"refresh_token": "plainvalue", "nested": [{"client_secret": "x1"}], "title": "ok"})
    assert out["refresh_token"] == safety.MASK and out["nested"][0]["client_secret"] == safety.MASK and out["title"] == "ok"


def test_get_secret_env_then_keychain_and_registers(isolated_secrets, monkeypatch):
    safety.set_secret("facebook_page_token", "keychain-token-123456")
    assert safety.get_secret("facebook_page_token") == "keychain-token-123456"
    monkeypatch.setenv("AVE_FACEBOOK_PAGE_TOKEN", "env-token-abcdef")
    assert safety.get_secret("facebook_page_token") == "env-token-abcdef"
    assert "env-token-abcdef" not in safety.redact("boom env-token-abcdef")


def test_json_credential_fields_are_registered(isolated_secrets):
    safety.set_secret("youtube_credentials", '{"refresh_token": "1//refreshvalueXYZ123456789012", "client_secret": "sec-value-abc"}')
    safety.get_secret("youtube_credentials")
    assert "sec-value-abc" not in safety.redact("client said sec-value-abc")


def test_logger_redacts(caplog):
    safety.register_secret("tok-ABCDEF-123")
    log = safety.get_logger("ave.publish.test")
    with caplog.at_level(logging.INFO, logger="ave.publish.test"):
        log.info("sending with %s", "tok-ABCDEF-123")
    assert "tok-ABCDEF-123" not in caplog.text and safety.MASK in caplog.text


def test_approval_signature_and_tamper(isolated_secrets):
    item = {"id": "a", "status": "approved", "sha256": "h1", "platforms": ["youtube"], "title": "t"}
    item["approvals"] = [safety.make_approval(item, "h1", "ben", "2026-01-01T00:00:00+0000")]
    assert safety.approval_problems(item, "h1") == []
    # file changed
    assert any("sha256" in p for p in safety.approval_problems(item, "h2"))
    # metadata changed
    item["title"] = "other"
    assert any("changed since approval" in p for p in safety.approval_problems(item, "h1"))
    item["title"] = "t"
    # forged approval (no valid signature)
    item["approvals"][0]["sig"] = "0" * 64
    assert any("signature" in p for p in safety.approval_problems(item, "h1"))
    # wrong status
    item["approvals"] = [safety.make_approval(item, "h1", "ben", "t")]
    item["status"] = "awaiting_approval"
    assert any("not approved" in p for p in safety.approval_problems(item, "h1"))
    # hand-written approval with no sig key at all
    item["status"] = "approved"
    item["approvals"] = [{"by": "me", "at": "x", "sha256": "h1", "meta_sha256": safety.meta_hash(item)}]
    assert safety.approval_problems(item, "h1")


def test_approval_key_file_fallback_is_0600(isolated_secrets, monkeypatch):
    import keyring
    class Broken(keyring.backend.KeyringBackend):
        priority = 2
        def get_password(self, *a): raise RuntimeError("no keychain")
        def set_password(self, *a): raise RuntimeError("no keychain")
        def delete_password(self, *a): raise RuntimeError("no keychain")
    keyring.set_keyring(Broken())
    k = safety.approval_key()
    kf = safety._key_file()
    assert kf.exists() and (kf.stat().st_mode & 0o777) == 0o600
    assert safety.approval_key() == k
