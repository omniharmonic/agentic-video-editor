from pathlib import Path
import hashlib,json,os,subprocess,time
root=Path(os.environ['AVE_ENGINE_BUILD_ROOT'])
old=json.loads((root/'build-manifest.json').read_text())
manifest={'started':time.time(),'parent_manifest':'build-manifest.json','reason':'Explicit system zlib required for PNG with disable-autodetect','steps':[]}
env=dict(os.environ);env.update(old['environment'])
ff=root/'sources/ffmpeg-8.0.3';logs=root/'logs-zlib';logs.mkdir(exist_ok=False)
args=next(x['argv'] for x in old['steps'] if x['name']=='ffmpeg-configure')
args.append('--enable-zlib')
for name,cmd in [('clean',['make','clean']),('configure',args),('make',['make','-j4']),('install',['make','install'])]:
 log=logs/(name+'.log');print('FFmpeg '+name,flush=True)
 row={'name':name,'argv':cmd,'log':str(log),'started':time.time()};manifest['steps'].append(row)
 with log.open('w') as f:p=subprocess.run(cmd,cwd=ff,env=env,stdout=f,stderr=subprocess.STDOUT)
 row.update(returncode=p.returncode,finished=time.time())
 (root/'build-manifest-zlib.json').write_text(json.dumps(manifest,indent=2)+'\n')
 if p.returncode:
  print(log.read_text(errors='replace')[-4000:]);raise SystemExit(p.returncode)
manifest['outputs']=[]
for name in ['ffmpeg','ffprobe']:
 f=root/'prefix/bin'/name
 manifest['outputs'].append({'name':name,'path':str(f),'sha256':hashlib.sha256(f.read_bytes()).hexdigest(),'size':f.stat().st_size,'version':subprocess.check_output([str(f),'-version'],text=True),'links':subprocess.check_output(['/usr/bin/otool','-L',str(f)],text=True)})
manifest['finished']=time.time()
(root/'build-manifest-zlib.json').write_text(json.dumps(manifest,indent=2)+'\n')
print('Zlib-enabled matching runtime built; validation pending',flush=True)
