"""Explicit isolated acceptance harness. Never authenticates or sends."""
from pathlib import Path
import argparse,hashlib,json,os,subprocess,time
p=argparse.ArgumentParser();p.add_argument('--executable',type=Path,required=True);p.add_argument('--output',type=Path,required=True);p.add_argument('--media',type=Path,required=True);p.add_argument('--resume-render',action='store_true')
a=p.parse_args();a.output.mkdir(parents=True,exist_ok=a.resume_render)
os.environ.update(XDG_DATA_HOME=str(a.output/'data'),XDG_CONFIG_HOME=str(a.output/'config'),AVE_APPROVAL_KEY='synthetic-desktop-acceptance-only')
from ave.studio.project import StudioProject
from ave.studio.service import open_studio
from ave.studio import workbench as W, manual_export, store
from ave.desktop_files import import_selected,save_verified
from ave.desktop_runtime import digest
sp=StudioProject(a.output/'studio');sp.create(title='Frozen synthetic acceptance')
studio=open_studio(sp.root,publishers={},host=None);studio.native_authoring=True
# Restrict actual frozen children, not this fixture-construction harness.
repo=Path(__file__).resolve().parents[1]
profile=a.output/'worker.sb';profile.write_text('''(version 1)
(allow default)
(deny network*)
(allow network* (local ip "localhost:*"))
(allow network* (remote ip "localhost:*"))
'''+''.join('(deny file-read* file-write* (subpath '+json.dumps(str(x))+'))\n' for x in [repo,Path('/opt/homebrew'),Path.home()/'Library/Caches/ms-playwright',Path.home()/'Library/Application Support/FluidAudio/Models']))
W.worker_argv=lambda root,jid:['/usr/bin/sandbox-exec','-f',str(profile),str(a.executable),'--workbench-worker',str(root),jid]
original_env=W.child_env
W.child_env=lambda:{**original_env(),'PATH':'/usr/bin:/bin','AVE_HOME':'/does-not-exist','PLAYWRIGHT_BROWSERS_PATH':'/does-not-exist','AVE_FLUIDAUDIO':'/does-not-exist'}
wb=W.Workbench(studio);results=json.loads((a.output/'verification.json').read_text()) if a.resume_render else {'executable':str(a.executable),'source_sha256':digest(a.media),'jobs':[]}

def wait(job):
    end=time.monotonic()+180
    while time.monotonic()<end:
        data=wb.tail(job['id'])
        if data['status'] not in ('queued','running'):
            data['acceptance_executable']=str(a.executable)
            data['acceptance_executable_sha256']=digest(a.executable)
            results['jobs'].append(data);(a.output/'verification.json').write_text(json.dumps(results,indent=2))
            assert data['status']=='done',data
            return data
        time.sleep(.1)
    raise RuntimeError('Frozen worker timeout')
try:
    if not a.resume_render:
        imported=import_selected(wb,[a.media]);results['import']=imported
        imp=wait(imported[0]['job']);src=imported[0]['source_id']
        job=wait(wb.start_transcribe({'source_id':src,'speakers':2}))
        assert job['result']['words']>350,job
    writing=a.output/'selection.md';writing.write_text('We leave room for one another, and we listen before we answer.\n')
    import_selected(wb,[writing]);text=next(s for s in wb.overview()['sources'] if s['kind']=='writing')
    args={'source_id':text['source_id'],'sentence_id':text['sentences'][0]['id'],'recipe':'quote-post','style':'paper','format':'4x5'}
    first=wait(wb.start_render(args));second=wait(wb.start_render(args))
    assert first['result']['critic']['ok'] and second['result']['critic']['ok']
    assert first['result']['assets'][0]['sha256']==second['result']['assets'][0]['sha256']
    from PIL import Image
    rendered=sp.root/first['result']['assets'][0]['file'];assert Image.open(rendered).size==(1080,1350)
    post=store.load_post(sp,first['post'])
    pack=manual_export.build(sp,post['id'],draft=True,expected_rev=post['rev'])
    source=Path(pack['zip']);saved=a.output/('saved-package-'+str(len(results['jobs']))+'.zip');save_verified(source,saved,digest(source))
    results.update(latest_render_executable=str(a.executable),saved_package=str(saved),source_unchanged=digest(a.media)==results['source_sha256'],rendered=str(rendered),render_sha256=digest(rendered),package_sha256=digest(saved),native_intake_bytes=a.media.stat().st_size,claims='600 MiB sparse-padded 134-second decodable recording: transport boundary, not long-duration editing. Frozen workers under denied checkout/Homebrew/global caches/external network.')
    assert results['source_unchanged']
finally:
    wb.close();(a.output/'verification.json').write_text(json.dumps(results,indent=2))
print(json.dumps({k:v for k,v in results.items() if k not in ('jobs','import')},indent=2))
