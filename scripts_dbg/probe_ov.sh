set -u
cd "$(mktemp -d)"
px() { ffmpeg -v error -y -i "$1" -vf "select=eq(n\,${4:-150}),format=rgb24,crop=1:1:${2:-350}:${3:-240}" -frames:v 1 -f rawvideo - | xxd -p; }
BG="-f lavfi -i color=c=0x2040ff:s=640x360:r=30:d=8,format=yuv420p"
ENC="-c:v libx264 -pix_fmt yuv420p -colorspace bt709 -color_primaries bt709 -color_trc bt709 -color_range tv"
ffmpeg -v error -y -f lavfi -i "color=c=0xffff00@0.0:s=100x80:r=30:d=1,format=yuva444p10le" -f lavfi -i "color=c=0xffff00@1.0:s=100x80:r=30:d=1,format=yuva444p10le" -filter_complex "[0][1]concat=n=2:v=1:a=0" -c:v prores_ks -profile:v 4444 -pix_fmt yuva444p10le ov.mov
ffprobe -v error -show_entries stream=pix_fmt,r_frame_rate,nb_frames,time_base,start_time -of csv=p=0 ov.mov
ffprobe -v error -show_entries frame=pts_time,pix_fmt -of csv=p=0 ov.mov | sed -n '1,3p;29,33p;58,60p'
O="eof_action=pass:format=auto:enable='gte(n,120)*lt(n,180)'"
t() { name=$1; chain=$2; shift 2; ffmpeg -v error -y $BG "$@" -i ov.mov -filter_complex "[1:v]$chain[c];[0:v][c]overlay=50:40:$O[z];[z]format=yuv420p[v]" -map "[v]" -frames:v 240 $ENC $name.mp4 2>&1 | head -3; echo "$name: f126=$(px $name.mp4 100 80 126) f150=$(px $name.mp4 100 80 150) f165=$(px $name.mp4 100 80 165)"; }
t base "format=rgba,scale=100:80,setpts=PTS-STARTPTS+120/30/TB"
t t2 "format=rgba,scale=100:80,setpts=PTS-STARTPTS+120/30/TB" -t 2
t fps "fps=30,format=rgba,scale=100:80,setpts=PTS-STARTPTS+120/30/TB"
t nosetpts "format=rgba,scale=100:80"
t yuva420 "format=yuva420p,setpts=PTS-STARTPTS+120/30/TB"
t yuva444 "format=yuva444p,setpts=PTS-STARTPTS+120/30/TB"
t shiftb "format=rgba,scale=100:80,setpts=PTS-STARTPTS+4/TB"
# overlay without enable
ffmpeg -v error -y $BG -i ov.mov -filter_complex "[1:v]format=rgba,setpts=PTS-STARTPTS+120/30/TB[c];[0:v][c]overlay=50:40:eof_action=pass:format=auto[z];[z]format=yuv420p[v]" -map "[v]" -frames:v 240 $ENC noen.mp4; echo "noenable: f126=$(px noen.mp4 100 80 126) f150=$(px noen.mp4 100 80 150) f165=$(px noen.mp4 100 80 165)"
ffmpeg -v error -y $BG -i ov.mov -filter_complex "[1:v]format=rgba,setpts=PTS-STARTPTS+120/30/TB[c];[0:v][c]overlay=50:40[z];[z]format=yuv420p[v]" -map "[v]" -frames:v 240 $ENC plain.mp4; echo "plain: f126=$(px plain.mp4 100 80 126) f150=$(px plain.mp4 100 80 150) f165=$(px plain.mp4 100 80 165)"
ffmpeg -v error -y -i ov.mov -vf "format=rgba,scale=100:80" -frames:v 60 -f rawvideo - | python3 -c "
import sys
d=sys.stdin.buffer.read(); fs=100*80*4
print('frames',len(d)//fs,[d[i*fs+3] for i in (0,10,29,30,31,45,59) if (i+1)*fs<=len(d)])" 
