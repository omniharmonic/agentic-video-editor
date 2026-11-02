"""Compiled media keeps exact playback bytes without an unused preview encode."""
import json
import subprocess
import pytest
from ave import media
from ave.config import binary
from ave.publish.safety import sha256_file
from ave.studio import video_workshop as V
from .test_workbench import studio
from .test_episode_workshop import recording


@pytest.mark.parametrize('kind',['video','audio','wav'])
def test_compiled_waveform_only_decodes_complete_pcm(studio,tmp_path,monkeypatch,kind):
    source=recording(tmp_path,duration=2)
    output=studio.sp.work/('compiled'+('.mp4' if kind=='video' else '.m4a' if kind=='audio' else '.wav'))
    command=[binary('ffmpeg'),'-v','error','-y']
    if kind=='video':command+=['-f','lavfi','-i','color=c=blue:s=640x360:r=24:d=2']
    command+=['-i',str(source)]
    if kind=='video':command+=['-c:v','libx264','-pix_fmt','yuv420p']
    command+=['-c:a','pcm_s24le' if kind=='wav' else 'aac',str(output)]
    subprocess.run(command,check=True)
    before=sha256_file(output);original=V.subprocess.Popen;commands=[]
    def captured(cmd,*args,**kwargs):commands.append(cmd);return original(cmd,*args,**kwargs)
    monkeypatch.setattr(V.subprocess,'Popen',captured)
    wave=V.compiled_waveform(studio.sp,output,studio.sp.work/'wave',media.probe_file(output,studio.sp.root))
    value=json.loads(wave.read_text())
    assert value['source_sha256']==before==sha256_file(output)
    assert abs(value['decoded_samples']-16000)<200 and value['peaks'] and max(value['peaks'])>.1
    assert not list(wave.parent.glob('preview*'))
    ffmpeg=[cmd for cmd in commands if str(cmd[0])==str(binary('ffmpeg'))]
    assert len(ffmpeg)==1 and ffmpeg[0][-1]=='-' and '-vn' in ffmpeg[0] and '-c:v' not in ffmpeg[0]
    info=media._ffprobe(output)
    if kind=='video':info['streams'][0]['pix_fmt']='yuv444p'
    else:info['format']['format_name']='unsupported'
    monkeypatch.setattr(media,'_ffprobe',lambda p:info)
    with pytest.raises(ValueError,match='not browser-compatible'):V.compiled_waveform(studio.sp,output,studio.sp.work/'refused',value)
    assert not (studio.sp.work/'refused').exists()
