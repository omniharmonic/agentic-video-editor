"""Explicit slow synthetic media acceptance provisioner; never run by ordinary pytest.

Requires two caller-supplied original/licensed speech WAVs (>=6s); generates a repeated
hour from the first and records every input hash. Uses only existing local app engines.
"""
import argparse
import hashlib
import json
import shutil
import subprocess
import time
import uuid
from pathlib import Path
from ave.config import binary
from ave.studio.project import StudioProject
from ave.studio.service import open_studio
from ave.studio.workbench import Workbench
from ave.studio import video_workshop as V


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--speech-one',type=Path,required=True)
    parser.add_argument('--speech-two',type=Path,required=True)
    parser.add_argument('--output',type=Path,required=True)
    args=parser.parse_args()
    root=args.output.resolve()
    root.mkdir(parents=True,exist_ok=False)
    inputs=root/'inputs';inputs.mkdir()
    for index,path in enumerate((args.speech_one,args.speech_two),1):
        shutil.copyfile(path,inputs/f'turn-0{index}.wav')
    hour=inputs/'synthetic-speech-60min.wav'
    subprocess.run([binary('ffmpeg'),'-v','error','-stream_loop','-1','-i',str(inputs/'turn-01.wav'),'-t','3600','-ar','16000','-ac','1','-c:a','pcm_s16le',str(hour)],check=True)
    video=inputs/'synthetic-speech-60min.mp4'
    subprocess.run([binary('ffmpeg'),'-v','error','-f','lavfi','-i','color=c=0x335978:s=320x180:r=24:d=3600','-i',str(hour),'-t','3600','-c:v','libx264','-preset','ultrafast','-c:a','aac',str(video)],check=True)
    manifest=[{'path':str(p),'sha256':hashlib.sha256(p.read_bytes()).hexdigest(),'bytes':p.stat().st_size} for p in inputs.iterdir()]
    (root/'input-manifest.json').write_text(json.dumps({'synthetic_repetition':True,'files':manifest},indent=2))
    sp=StudioProject(root/'studio');sp.create(title='Synthetic explicit media acceptance',tz='UTC')
    studio=open_studio(sp.root);workbench=Workbench(studio,by='synthetic fixture');jobs=[]
    def wait(job):
        deadline=time.monotonic()+14400
        while job['status'] in ('queued','running'):
            if time.monotonic()>deadline:raise RuntimeError('Acceptance job deadline exceeded')
            time.sleep(.25);job=workbench.tail(job['id'])
        jobs.append(job)
        if job['status']!='done':raise RuntimeError(job['diagnostics'])
        return job
    def mutate(p,operation):
        return V.mutate(studio,dict(project=p['id'],expected_revision=p['revision'],expected_version=p['version'],expected_sha256=p['sha256'],request_id=str(uuid.uuid4()),operation=operation))
    try:
        ids={}
        for name in ('turn-01.wav','turn-02.wav',hour.name,video.name):
            source=inputs/name;stage=sp.work/('input-'+name);shutil.copyfile(source,stage)
            result=workbench.upload(stage,name,source.stat().st_size,hashlib.sha256(source.read_bytes()).hexdigest());wait(result['job']);ids[name]=result['source_id']
            wait(workbench.start_transcribe({'source_id':result['source_id'],'speakers':2}))
        projects=[]
        for title,mode,files,ranges in [('Independent synthetic speech','audio',['turn-01.wav','turn-02.wav'],[('turn-02.wav',1,5),('turn-01.wav',0,4),('turn-01.wav',0,4)]),('Last minute synthetic video','audiovisual',[video.name],[(video.name,3590,3598)])]:
            p=V.create(studio,{'title':title,'mode':mode,'request_id':str(uuid.uuid4())})
            for name in files:
                wait(workbench.start_media('video-source',dict(project=p['id'],source_id=ids[name],expected_revision=p['revision'],request_id=str(uuid.uuid4()))));p=V.load(sp,p['id'])
            for name,a,b in ranges:p=mutate(p,dict(kind='insert',source=ids[name],**{'in':a,'out':b}))
            projects.append(p['id'])
        (root/'real-workers.json').write_text(json.dumps({'studio':str(sp.root),'input_manifest':str(root/'input-manifest.json'),'inputs':ids,'projects':projects,'jobs':jobs},indent=2))
    finally:workbench.close()
    print(root/'real-workers.json')


if __name__=='__main__':main()
