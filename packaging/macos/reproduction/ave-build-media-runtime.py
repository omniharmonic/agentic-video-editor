from pathlib import Path
import hashlib,json,os,shutil,subprocess,time

root=Path(os.environ['AVE_ENGINE_BUILD_ROOT'])
src=root/'sources'; prefix=root/'prefix'; logs=root/'logs'
prefix.mkdir(exist_ok=True);logs.mkdir(exist_ok=True)
env=dict(os.environ)
sdk=subprocess.check_output(['/usr/bin/xcrun','--show-sdk-path'],text=True).strip()
env.update(PATH=str(Path(os.environ['AVE_ENGINE_TOOLS'])/'bin')+':/usr/bin:/bin:/usr/sbin:/sbin',
 PKG_CONFIG_LIBDIR=str(prefix/'lib/pkgconfig'),PKG_CONFIG_PATH='',
 CC='/usr/bin/clang',CXX='/usr/bin/clang++',MACOSX_DEPLOYMENT_TARGET='13.5',SDKROOT=sdk,
 CFLAGS='-O2 -mmacosx-version-min=13.5',CXXFLAGS='-O2 -mmacosx-version-min=13.5',
 CPPFLAGS='-I'+str(prefix/'include'),LDFLAGS='-L'+str(prefix/'lib'),
 CLANG_MODULE_CACHE_PATH=str(root/'module-cache'))
manifest={'started':time.time(),'sdk':sdk,'deployment_target':'13.5','architecture':'arm64',
 'compiler':subprocess.check_output(['/usr/bin/clang','--version'],text=True),
 'environment':{k:env[k] for k in ['PATH','PKG_CONFIG_LIBDIR','PKG_CONFIG_PATH','CC','CXX','MACOSX_DEPLOYMENT_TARGET','SDKROOT','CFLAGS','CXXFLAGS','CPPFLAGS','LDFLAGS','CLANG_MODULE_CACHE_PATH']},'steps':[]}
def run(name,args,cwd):
    log=logs/(name+'.log')
    print('Running '+name,flush=True)
    row={'name':name,'argv':args,'cwd':str(cwd),'log':str(log),'started':time.time()}
    manifest['steps'].append(row)
    (root/'build-manifest.json').write_text(json.dumps(manifest,indent=2)+'\n')
    with log.open('w') as out:
        p=subprocess.run(args,cwd=cwd,env=env,stdout=out,stderr=subprocess.STDOUT)
    row.update(returncode=p.returncode,finished=time.time())
    (root/'build-manifest.json').write_text(json.dumps(manifest,indent=2)+'\n')
    if p.returncode:
        print(log.read_text(errors='replace')[-4500:],flush=True)
        raise SystemExit(p.returncode)
def autotools(name,folder,options):
    cwd=src/folder
    run(name+'-configure',['./configure','--prefix='+str(prefix)]+options,cwd)
    run(name+'-make',['make','-j4'],cwd)
    run(name+'-install',['make','install'],cwd)

autotools('freetype','freetype-2.14.3',['--disable-shared','--enable-static','--with-zlib=no','--with-png=no','--with-bzip2=no','--with-harfbuzz=no','--with-brotli=no'])
hb=src/'harfbuzz-14.2.0'; hbbuild=root/'harfbuzz-build'
run('harfbuzz-configure',['meson','setup',str(hbbuild),str(hb),'--prefix='+str(prefix),'--libdir=lib','--default-library=static','--buildtype=release','--wrap-mode=nodownload','-Dfreetype=enabled']+['-D'+x+'=disabled' for x in ['glib','gobject','cairo','chafa','png','zlib','icu','graphite2','introspection','tests','docs','utilities','raster','vector','gpu','gpu_demo','subset']],root)
run('harfbuzz-make',['ninja','-C',str(hbbuild),'-j4'],root)
run('harfbuzz-install',['ninja','-C',str(hbbuild),'install'],root)
fbbuild=root/'fribidi-build'
run('fribidi-configure',['meson','setup',str(fbbuild),str(src/'fribidi-1.0.16'),'--prefix='+str(prefix),'--libdir=lib','--default-library=static','--buildtype=release','--wrap-mode=nodownload','-Ddocs=false','-Dbin=false','-Dtests=false'],root)
run('fribidi-make',['ninja','-C',str(fbbuild),'-j4'],root)
run('fribidi-install',['ninja','-C',str(fbbuild),'install'],root)
autotools('libass','libass-0.17.5',['--disable-shared','--enable-static','--disable-fontconfig','--disable-libunibreak','--disable-test','--disable-compare'])
run('x264-configure',['./configure','--prefix='+str(prefix),'--enable-static','--enable-pic','--disable-cli','--disable-opencl','--disable-avs','--disable-swscale','--disable-lavf','--disable-ffms','--disable-gpac','--disable-lsmash'],root/'x264')
run('x264-make',['make','-j4'],root/'x264')
run('x264-install',['make','install'],root/'x264')
autotools('lame','lame-3.100',['--disable-shared','--enable-static','--disable-frontend','--disable-decoder'])
ff=src/'ffmpeg-8.0.3'
run('ffmpeg-configure',['./configure','--prefix='+str(prefix),'--arch=arm64','--cc=clang','--cxx=clang++','--disable-autodetect','--disable-shared','--enable-static','--disable-ffplay','--disable-doc','--enable-gpl','--enable-zlib','--enable-libx264','--enable-libmp3lame','--enable-libass','--enable-libfreetype','--enable-libharfbuzz','--enable-libfribidi','--enable-videotoolbox','--enable-audiotoolbox','--pkg-config-flags=--static','--extra-cflags=-I'+str(prefix/'include')+' -mmacosx-version-min=13.5','--extra-ldflags=-L'+str(prefix/'lib')+' -mmacosx-version-min=13.5'],ff)
run('ffmpeg-make',['make','-j4'],ff)
run('ffmpeg-install',['make','install'],ff)
manifest['outputs']=[]
for name in ['ffmpeg','ffprobe']:
    f=prefix/'bin'/name
    manifest['outputs'].append({'name':name,'path':str(f),'sha256':hashlib.sha256(f.read_bytes()).hexdigest(),'size':f.stat().st_size,'version':subprocess.check_output([str(f),'-version'],text=True),'links':subprocess.check_output(['/usr/bin/otool','-L',str(f)],text=True)})
manifest['finished']=time.time()
(root/'build-manifest.json').write_text(json.dumps(manifest,indent=2)+'\n')
print('Matching media runtime built; verification pending',flush=True)
