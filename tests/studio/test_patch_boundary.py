"""Replacing a container must not smuggle scheduler-owned fields into a patch."""
import pytest
from ave.studio.model import apply_patch, new_post, new_variant


@pytest.mark.parametrize('path,value', [('variants', []), ('variants.v_test', {}), ('variants.0', {}), ('variants.v_test.queue', {'root': '/outside', 'id': 'q'}), ('variants.v_test.schedule', None)])
def test_operational_variant_containers_cannot_be_replaced(path, value):
    post = new_post(idea='Keep it grounded')
    post['variants'] = [new_variant(account='main', platform='manual', post_type='text', variant_id='v_test')]
    with pytest.raises(ValueError):
        apply_patch(post, {path: value})


def test_editorial_variant_copy_can_still_be_changed():
    post = new_post(idea='Keep it grounded')
    post['variants'] = [new_variant(account='main', platform='manual', post_type='text', variant_id='v_test')]
    out = apply_patch(post, {'variants.v_test.copy.caption': 'A quieter version'})
    assert out['variants'][0]['copy']['caption'] == 'A quieter version'
    assert post['variants'][0]['copy']['caption'] is None


@pytest.mark.parametrize('sidecar',['posts/p_guard.lock','posts/p_guard.json.tmp','ledger/.lock','ledger/events.jsonl'])
def test_store_does_not_follow_write_sidecars(tmp_path,sidecar):
    from ave.studio import store
    from ave.studio.project import StudioProject
    from ave.studio.events import AGENT
    sp=StudioProject(tmp_path/'studio');sp.create()
    victim=tmp_path/'raw-recording';victim.write_bytes(b'ORIGINAL RAW BYTES')
    target=sp.root/sidecar
    target.symlink_to(victim)
    post=new_post(idea='A harmless draft',post_id='p_guard')
    with pytest.raises(ValueError,match='symlink'):
        store.save_post(sp,post,expected_rev=None,actor=AGENT,op='create')
    assert victim.read_bytes()==b'ORIGINAL RAW BYTES'
    assert not (sp.posts_dir/'p_guard.json').exists()
