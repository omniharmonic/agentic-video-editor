"""Manual handoff packs preserve full media bytes and never cross posting gates."""
from datetime import datetime,timezone
import hashlib
import json
from pathlib import Path
import zipfile

import pytest
from PIL import Image

from ave.studio import manual_export as export,store
from ave.studio.events import AGENT
from ave.studio.model import new_post,new_variant,RevConflict
from ave.studio.project import StudioProject
from studio_helpers import add_asset,write_account,RecordingPublisher


@pytest.fixture
def studio(tmp_path):
    sp=StudioProject(tmp_path/'studio');sp.create(title='Sofia’s launch')
    return sp


def make_post(sp,tmp_path,*,variant=False,reviewed=False,source_type='render',count=2):
    media=[]
    for i in range(count):
        src=tmp_path/f'slide{i}.jpg'
        Image.new('RGB',(1080,1350) if reviewed else (2160+i*10,2700),(80+i*30,110,150)).save(src,quality=96)
        asset=add_asset(sp,src,source_type=source_type,alt=f'Book launch slide {i+1}')
        media.append({'slot':count-i,'asset':asset['id'],'role':'image','alt':f'Slide {i+1} alt'})
    post=new_post(idea='A book about attention',copy={'base':'A thought to carry with you.','hashtags':['Attention','BookLaunch'],
                  'link':'https://example.com/book','first_comment':'A question for your reading group.'},media=media)
    if variant:
        write_account(sp,'sofia-ig','instagram')
        post['variants']=[new_variant(account='sofia-ig',platform='instagram',post_type='carousel' if count>1 else 'feed')]
    post=store.save_post(sp,post,expected_rev=None,actor=AGENT,op='fixture')
    if reviewed:
        from ave.studio.sync import sync_queue
        from ave.publish import queue as pq
        from ave.studio.dispatch import approval_bind
        sync_queue(sp,post['id'],publishers={'instagram':RecordingPublisher('instagram')})
        post=store.load_post(sp,post['id'])
        item=pq.load(sp,post['variants'][0]['queue']['id'])
        pq.approve(sp,item['id'],'Sofia',item['sha256'][:12],publishers={'instagram':RecordingPublisher('instagram')},
                   bind=approval_bind(sp,post['variants'][0]))
    return store.load_post(sp,post['id'])


def authority(sp):
    return {str(p.relative_to(sp.root)):p.read_bytes() for d in (sp.posts_dir,sp.accounts_dir,sp.publish,sp.ledger_dir)
            for p in d.rglob('*') if p.is_file()}


def test_draft_pack_keeps_original_resolution_order_copy_and_state(studio,tmp_path):
    p=make_post(studio,tmp_path)
    before=authority(studio)
    result=export.build(studio,p['id'],draft=True,expected_rev=p['rev'])
    assert authority(studio)==before
    assert result['published'] is False and result['mode']=='draft'
    manifest=result['manifest']
    assert not manifest['reviewed'] and manifest['media'][0]['width']==2170
    assert '#Attention #BookLaunch' in manifest['caption'] and 'https://example.com/book' in manifest['caption']
    assert manifest['first_comment']=='A question for your reading group.'
    with zipfile.ZipFile(result['zip']) as z:
        assert {'index.html','caption.txt','first-comment.txt','alt-text.txt','manifest.json'} <= set(z.namelist())
        for row in manifest['media']:
            data=z.read(row['filename'])
            assert hashlib.sha256(data).hexdigest()==row['sha256']
            original=studio.root/store.read_assets(studio)[row['asset']]['file']
            assert data==original.read_bytes()
        assert 'DRAFT' in z.read('index.html').decode()
        assert b'window.open' not in z.read('index.html') and b'<script' not in z.read('index.html')
        assert z.read('caption.txt').decode()==manifest['caption']
    assert not list(studio.accounts_dir.iterdir())


def test_reviewed_pack_needs_exact_approval_not_live_credentials(studio,tmp_path):
    p=make_post(studio,tmp_path,variant=True,reviewed=True)
    before=authority(studio)
    description=export.describe(studio,p['id'])
    assert description['ok'],description
    result=export.build(studio,p['id'],expected_rev=p['rev'])
    assert result['manifest']['reviewed'] and result['published'] is False
    after=authority(studio)
    assert {key:after[key] for key in before}==before
    added=set(after)-set(before)
    assert added=={studio.rel(studio.ledger_dir/'manual_reviews'/(result['package_id']+'.json'))}
    assert export.load_review_snapshot(studio,result['manifest'])['approvals']
    assert 'reviewed_snapshot' not in result['manifest']


def test_draft_explicit_when_no_approval_and_exports_never_overwrite(studio,tmp_path):
    p=make_post(studio,tmp_path)
    assert not export.describe(studio,p['id'])['ok']
    with pytest.raises(ValueError,match='not ready'):
        export.build(studio,p['id'])
    a=export.build(studio,p['id'],draft=True)
    b=export.build(studio,p['id'],draft=True)
    assert a['package_id']!=b['package_id']
    assert Path(a['zip']).is_file() and Path(b['zip']).is_file()


def test_unlicensed_source_warns_in_draft_blocks_reviewed(studio,tmp_path):
    p=make_post(studio,tmp_path,source_type='own')
    d=export.describe(studio,p['id'],draft=True)
    assert d['ok'] and any('license' in s for s in d['warnings'])
    with pytest.raises(ValueError,match='license'):
        export.build(studio,p['id'])


def test_changed_bytes_and_missing_assets_refused_even_for_draft(studio,tmp_path):
    p=make_post(studio,tmp_path,count=1)
    rec=store.read_assets(studio)[p['media'][0]['asset']]
    path=studio.root/rec['file']
    path.write_bytes(b'changed')
    with pytest.raises(ValueError,match='changed'):
        export.build(studio,p['id'],draft=True)
    path.unlink()
    with pytest.raises(FileNotFoundError):export.build(studio,p['id'],draft=True)


@pytest.mark.parametrize('which',['asset','index','post','output','output-parent'])
def test_symlink_paths_never_read_or_written(studio,tmp_path,which):
    p=make_post(studio,tmp_path,count=1)
    victim=tmp_path/'outside-private';victim.write_bytes(b'PRIVATE ORIGINAL')
    kwargs={}
    if which=='asset': target=studio.root/store.read_assets(studio)[p['media'][0]['asset']]['file']
    elif which=='index':target=studio.library_dir/'assets.jsonl'
    elif which=='post':target=studio.posts_dir/(p['id']+'.json')
    else:
        outside=tmp_path/'outside';outside.mkdir()
        target=studio.work/'manual_exports'
        target.symlink_to(outside,target_is_directory=True)
        if which=='output-parent':kwargs['output']=target/'nested'
        with pytest.raises(ValueError,match='symlink'):export.build(studio,p['id'],draft=True,**kwargs)
        assert not list(outside.iterdir())
        return
    target.unlink();target.symlink_to(victim)
    with pytest.raises(ValueError,match='symlink'):export.build(studio,p['id'],draft=True)
    assert victim.read_bytes()==b'PRIVATE ORIGINAL'


def test_raw_output_refused_and_revision_checked(studio,tmp_path):
    p=make_post(studio,tmp_path)
    with pytest.raises(ValueError,match='raw'):
        export.build(studio,p['id'],draft=True,output=studio.raw/'exports')
    with pytest.raises(RevConflict):export.build(studio,p['id'],draft=True,expected_rev=p['rev']+1)
    with pytest.raises(ValueError):export.build(studio,'../../outside',draft=True)


def test_html_escapes_source_content_and_manifest_has_no_operational_fields(studio,tmp_path):
    p=make_post(studio,tmp_path,count=1)
    p['copy']['base']='</textarea><script>alert("x")</script>'
    p=store.save_post(studio,p,expected_rev=p['rev'],actor=AGENT,op='edit')
    r=export.build(studio,p['id'],draft=True)
    page=(Path(r['directory'])/'index.html').read_text()
    assert '<script>alert' not in page and '&lt;script&gt;' in page
    assert not {'approvals','queue','credentials','schedule'} & set(r['manifest'])


def test_media_copy_race_leaves_no_final_archive(studio,tmp_path,monkeypatch):
    p=make_post(studio,tmp_path,count=1)
    orig=export._asset_path;calls=0
    def changed(sp,rec):
        nonlocal calls
        calls+=1
        path=orig(sp,rec)
        if calls==3:path.write_bytes(b'changed during copying')
        return path
    monkeypatch.setattr(export,'_asset_path',changed)
    with pytest.raises(ValueError,match='changed'):
        export.build(studio,p['id'],draft=True)
    assert not list((studio.work/'manual_exports').glob('manual-*.zip'))


def test_required_license_credit_is_part_of_manual_caption(studio,tmp_path):
    from ave.library.registry import license_log
    p=make_post(studio,tmp_path,count=1,source_type='own')
    rec=store.read_assets(studio)[p['media'][0]['asset']]
    license_log(studio).append({'source':'synthetic','url':'https://example.invalid/photo','license':'Synthetic test license',
        'license_url':'https://example.invalid/license','license_text':'Synthetic fixture only','sha256':rec['sha256'],
        'approved_by':'test','file':rec['file'],'attested':True,
        'attribution':{'required':True,'text':'Synthetic photo credit'}})
    d=export.describe(studio,p['id'],draft=True)
    assert 'Synthetic photo credit' in d['manifest']['caption']
    assert d['manifest']['caption'].index('Synthetic photo credit') < d['manifest']['caption'].index('#Attention')


def test_final_caption_change_requires_new_review(studio,tmp_path):
    p=make_post(studio,tmp_path,variant=True,reviewed=True,count=1)
    p['copy']['base']='Changed after the human review'
    p=store.save_post(studio,p,expected_rev=p['rev'],actor=AGENT,op='edit')
    d=export.describe(studio,p['id'])
    assert not d['ok'] and any('differs' in w for w in d['warnings'])
    with pytest.raises(ValueError,match='not ready'):export.build(studio,p['id'])


def test_open_rework_prevents_final_manual_export(studio,tmp_path):
    from ave.studio import ledger
    p=make_post(studio,tmp_path,variant=True,reviewed=True,count=1)
    ledger.append_feedback(studio,{'post':p['id'],'variant':p['variants'][0]['id'],'rev':p['rev'],
        'kind':'rework_copy','note':'A softer opening please','by':'Sofia','source':'ui'})
    assert not export.describe(studio,p['id'])['ok']


def test_metadata_and_media_limits_refuse_without_partial_archives(studio,tmp_path,monkeypatch):
    p=make_post(studio,tmp_path,count=1)
    monkeypatch.setattr(export,'MAX_FILE',1)
    with pytest.raises(ValueError,match='size limit'):export.build(studio,p['id'],draft=True)
    assert not (studio.work/'manual_exports').exists()


def test_raw_source_record_never_becomes_downloadable(studio,tmp_path):
    p=make_post(studio,tmp_path,count=1)
    records=store.read_assets(studio)
    rec=records[p['media'][0]['asset']]
    original=studio.root/rec['file']
    raw=studio.raw/'private.jpg';raw.write_bytes(original.read_bytes())
    rec['file']='inbox/private.jpg'
    (studio.library_dir/'assets.jsonl').write_text(json.dumps(rec)+'\n')
    with pytest.raises(ValueError,match='library media'):export.build(studio,p['id'],draft=True)


def test_copy_failure_does_not_leave_final_zip(studio,tmp_path,monkeypatch):
    p=make_post(studio,tmp_path,count=1)
    monkeypatch.setattr(export,'_publish_archive',lambda *a,**k:(_ for _ in ()).throw(OSError('simulated finalization failure')))
    with pytest.raises(OSError,match='simulated'):export.build(studio,p['id'],draft=True)
    assert not list((studio.work/'manual_exports').glob('manual-*'))
    assert not [p for p in (studio.work/'manual_exports').glob('.manual-*') if p.name!='.manual-export.lock']


@pytest.mark.parametrize('bad',['false',1,None])
def test_draft_mode_must_be_boolean(studio,tmp_path,bad):
    p=make_post(studio,tmp_path,count=1)
    with pytest.raises(ValueError,match='boolean'):export.build(studio,p['id'],draft=bad)


def test_time_and_operational_output_boundaries(studio,tmp_path):
    p=make_post(studio,tmp_path,count=1)
    with pytest.raises(ValueError,match='timezone'):export.describe(studio,p['id'],draft=True,now=datetime(2026,10,28))
    for directory in (studio.accounts_dir/'export',studio.library_dir/'assets/pack',studio.posts_dir/'pack'):
        with pytest.raises(ValueError,match='operational'):export.build(studio,p['id'],draft=True,output=directory)


def test_export_works_on_filesystems_without_hardlinks(studio,tmp_path,monkeypatch):
    p=make_post(studio,tmp_path,count=1)
    def unsupported(*a,**kw):raise OSError('hardlinks are not supported on ExFAT')
    monkeypatch.setattr(export.os,'link',unsupported)
    result=export.build(studio,p['id'],draft=True)
    assert zipfile.is_zipfile(result['zip'])
    assert not (Path(result['directory'])/'.incomplete').exists()


def test_same_package_name_refuses_without_overwriting(studio,tmp_path,monkeypatch):
    from types import SimpleNamespace
    p=make_post(studio,tmp_path,count=1)
    monkeypatch.setattr(export.uuid,'uuid4',lambda:SimpleNamespace(hex='a'*32))
    first=export.build(studio,p['id'],draft=True)
    saved=Path(first['zip']).read_bytes()
    with pytest.raises(ValueError,match='never overwrite'):
        export.build(studio,p['id'],draft=True)
    assert Path(first['zip']).read_bytes()==saved


@pytest.mark.parametrize('layout,raw_folder',[
    ('flat','raw'),('numbered','02_FOOTAGE'),('numbered','03_AUDIO'),
    ({'raw_dirs':['recorded-material']},'recorded-material'),
])
def test_sibling_episode_raw_output_refused_without_creating_raw(studio,tmp_path,layout,raw_folder):
    p=make_post(studio,tmp_path,count=1)
    episode=tmp_path/'sibling-episode';episode.mkdir()
    marker=episode/'episode.json'
    marker.write_text(json.dumps({'title':'Synthetic sibling episode','layout':layout}))
    before={str(f.relative_to(episode)):f.read_bytes() for f in episode.rglob('*') if f.is_file()}
    with pytest.raises(ValueError,match='raw folders'):
        export.build(studio,p['id'],draft=True,output=episode/raw_folder/'manual-packages')
    assert {str(f.relative_to(episode)):f.read_bytes() for f in episode.rglob('*') if f.is_file()}==before
    assert not (episode/raw_folder).exists()


def test_sibling_studio_raw_output_and_oversized_marker_are_refused(studio,tmp_path):
    p=make_post(studio,tmp_path,count=1)
    sibling=tmp_path/'other-studio';sibling.mkdir()
    marker=sibling/'studio.json'
    marker.write_text(json.dumps({'layout':{'raw_dirs':['inbox']}}))
    with pytest.raises(ValueError,match='raw folders'):
        export.build(studio,p['id'],draft=True,output=sibling/'inbox'/'export')
    assert not (sibling/'inbox').exists()
    marker.write_text(' '*262145)
    with pytest.raises(ValueError,match='marker is too large'):
        export.build(studio,p['id'],draft=True,output=sibling/'exports')
    assert not (sibling/'exports').exists()


def test_repeated_carousel_asset_slots_count_toward_payload_limit(studio,tmp_path,monkeypatch):
    p=make_post(studio,tmp_path,count=1)
    first=p['media'][0]
    p['media']=[{**first,'slot':0},{**first,'slot':1}]
    p=store.save_post(studio,p,expected_rev=p['rev'],actor=AGENT,op='fixture_duplicate_slot')
    asset=store.read_assets(studio)[first['asset']]
    size=(studio.root/asset['file']).stat().st_size
    monkeypatch.setattr(export,'MAX_TOTAL',size+size//2)
    with pytest.raises(ValueError,match='aggregate size limit'):
        export.build(studio,p['id'],draft=True)
    assert not (studio.work/'manual_exports').exists()


@pytest.mark.parametrize('policy_name',['studio_days','publish_days'])
def test_raising_ttl_cannot_resurrect_reviewed_export(studio,tmp_path,monkeypatch,policy_name):
    from datetime import timedelta
    from ave.publish import queue as pq,safety
    start=datetime(2026,10,20,12,tzinfo=timezone.utc)
    monkeypatch.setattr(pq,'now_iso',lambda:start.strftime('%Y-%m-%dT%H:%M:%S%z'))
    publish_ttl={'days':1 if policy_name=='publish_days' else 14}
    monkeypatch.setattr(safety,'approval_ttl_days',lambda:publish_ttl['days'])
    if policy_name=='studio_days':
        meta=json.loads(studio.meta_path.read_text());meta['approval']['ttl_days']=1
        studio.meta_path.write_text(json.dumps(meta))
    p=make_post(studio,tmp_path,variant=True,reviewed=True,count=1)
    later=start+timedelta(days=2)
    assert not export.describe(studio,p['id'],now=later)['ok']
    if policy_name=='studio_days':
        meta=json.loads(studio.meta_path.read_text());meta['approval']['ttl_days']=14
        studio.meta_path.write_text(json.dumps(meta))
    else:publish_ttl['days']=14
    refused=export.describe(studio,p['id'],now=later)
    assert not refused['ok'] and any('lifetime settings were raised' in w and policy_name in w for w in refused['warnings'])
    with pytest.raises(ValueError,match='lifetime settings were raised'):
        export.build(studio,p['id'],now=later)
    assert export.describe(studio,p['id'],draft=True,now=later)['ok']
