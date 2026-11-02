"""Atomic no-clobber publication on native and legacy file-only platforms."""
import errno
import pytest
from ave import atomic


def test_native_publication_does_not_need_hardlinks(tmp_path,monkeypatch):
    source=tmp_path/'staged';target=tmp_path/'published';source.write_bytes(b'complete')
    monkeypatch.setattr(atomic.os,'link',lambda *a,**k:(_ for _ in ()).throw(OSError(errno.ENOTSUP,'no hardlinks')))
    atomic.publish(source,target)
    assert not source.exists() and target.read_bytes()==b'complete'
    source.write_bytes(b'another')
    with pytest.raises(FileExistsError):atomic.publish(source,target)
    assert source.read_bytes()==b'another' and target.read_bytes()==b'complete'


def test_file_only_fallback_preserves_supported_platforms(tmp_path,monkeypatch):
    monkeypatch.setattr(atomic.sys,'platform','legacy-test-platform')
    source=tmp_path/'staged';target=tmp_path/'published';source.write_bytes(b'complete')
    atomic.publish(source,target);assert target.read_bytes()==b'complete' and not source.exists()
    source.write_bytes(b'another')
    with pytest.raises(FileExistsError):atomic.publish(source,target)
    assert target.read_bytes()==b'complete' and source.read_bytes()==b'another'
    directory=tmp_path/'directory';directory.mkdir()
    atomic.publish(directory,tmp_path/'new-directory')
    assert not directory.exists() and (tmp_path/'new-directory').is_dir()


def _no_primitives(monkeypatch):
    monkeypatch.setattr(atomic.sys,'platform','legacy-test-platform')
    monkeypatch.setattr(atomic.os,'link',lambda *a,**k:(_ for _ in ()).throw(OSError(errno.ENOTSUP,'no hardlinks')))


def test_exfat_style_storage_publishes_without_replacing(tmp_path,monkeypatch):
    _no_primitives(monkeypatch)
    source=tmp_path/'staged';target=tmp_path/'published';source.write_bytes(b'complete')
    atomic.publish(source,target)
    assert target.read_bytes()==b'complete' and not source.exists() and sorted(p.name for p in tmp_path.iterdir())==['published']
    source.write_bytes(b'second')
    with pytest.raises(FileExistsError):atomic.publish(source,target)
    assert target.read_bytes()==b'complete' and source.read_bytes()==b'second'


def test_a_live_claim_blocks_and_a_stale_claim_clears(tmp_path,monkeypatch):
    import os
    _no_primitives(monkeypatch)
    source=tmp_path/'staged';target=tmp_path/'published';source.write_bytes(b'complete');claim=tmp_path/'.published.claim';claim.write_bytes(b'')
    with pytest.raises(FileExistsError):atomic.publish(source,target)
    assert not target.exists()
    os.utime(claim,(1,1));atomic.publish(source,target)
    assert target.read_bytes()==b'complete' and not claim.exists()
