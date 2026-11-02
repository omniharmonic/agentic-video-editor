"""Frozen due-work rehearsal in a new temporary workspace; no authority or keys."""
from pathlib import Path
from datetime import datetime,timedelta
from zoneinfo import ZoneInfo
from ave.studio.project import StudioProject
from ave.studio import model,store,state
from ave.studio.sync import sync_queue
from ave.studio.events import AGENT
from ave.platforms.accounts import save_account
from ave.desktop_service import heartbeat
from ave.schedule.slots import make_schedule
import subprocess,json,hashlib,os,sys

def main(executable,destination):
    out=Path(destination);out.mkdir(exist_ok=False)
    sp=StudioProject(out/'studio');sp.create(title='Synthetic frozen due-work rehearsal')
    save_account(sp,{'id':'local','platform':'linkedin','mode':'manual','label':'Synthetic local profile'})
    now=datetime.now(ZoneInfo('UTC'))
    p=model.new_post(idea='Synthetic due queue',copy={'base':'Unapproved synthetic text. No real destination or credentials.'})
    v=model.new_variant(account='local',platform='linkedin',post_type='text');v['delivery']={'method':'manual'}
    v['schedule']=make_schedule(now-timedelta(seconds=15),slot=None,set_by='agent');p['variants']=[v]
    p=store.save_post(sp,p,expected_rev=None,actor=AGENT,op='fixture')
    result=sync_queue(sp,p['id']);p=store.load_post(sp,p['id']);v=p['variants'][0];q=state.load_queue_item(sp,v)
    assert q and not q['approvals'],result
    expected=state.explain(v,q,[],now,sp)
    protected={str(x.relative_to(sp.root)):hashlib.sha256(x.read_bytes()).hexdigest() for folder in (sp.posts_dir,sp.publish/"queue") for x in folder.glob('*.json')}
    exe=Path(executable);runs=[]
    for _ in range(2):
        r=subprocess.run([str(exe),'--scheduler','--studio',str(sp.root),'--mode','rehearsal'],capture_output=True,text=True,timeout=45)
        runs.append({'exit':r.returncode,'stdout':r.stdout,'stderr':r.stderr})
        assert r.returncode==0 and json.loads(r.stdout)['blocked']==1 and json.loads(r.stdout)['posted']==0,runs
    assert all(hashlib.sha256((sp.root/name).read_bytes()).hexdigest()==sha for name,sha in protected.items())
    assert not (sp.root/'send.enabled').exists()
    h=json.loads(heartbeat(sp).read_text());sha=hashlib.sha256(exe.read_bytes()).hexdigest();assert h['executable_sha']==sha
    report={'ok':True,'executable':str(exe),'sha256':sha,'agent_marker_preserved':bool(os.environ.get('AVE_AGENT') or os.environ.get('CODEX_THREAD_ID')),'runs':runs,
        'candidate':{'post':p['id'],'variant':v['id'],'queue':q['id'],'expected_status':expected['status'],'expected_reason':expected['reason'],'queue_approvals':len(q['approvals'])},
        'protected_artifact_hashes':protected,'queue_and_post_unchanged':True,'heartbeat':h,'send_enabled':False,'real_launchagent_installed':False}
    (out/'verification.json').write_text(json.dumps(report,indent=2));print(json.dumps(report));return 0
if __name__=='__main__':raise SystemExit(main(*sys.argv[1:]))
