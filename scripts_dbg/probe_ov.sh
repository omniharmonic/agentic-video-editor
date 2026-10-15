set -u
cd "$(mktemp -d)"
ffmpeg -v error -y -f lavfi -i "color=c=0x00ff00:s=100x80,format=rgba" -frames:v 1 top.png
ffmpeg -v error -y -f lavfi -i "color=c=white@0.5:s=100x80,format=rgba" -frames:v 1 half.png
px() { ffmpeg -v error -y -i "$1" -vf "select=eq(n\,${4:-150}),format=rgb24,crop=1:1:${2:-350}:${3:-240}" -frames:v 1 -f rawvideo - | xxd -p; }
BG="-f lavfi -i color=c=0x2040ff:s=640x360:r=30:d=8,format=yuv420p"
ENC="-c:v libx264 -pix_fmt yuv420p -colorspace bt709 -color_primaries bt709 -color_trc bt709 -color_range tv"
LP="-loop 1 -framerate 30 -t 3"
P="format=rgba,scale=100:80:out_color_matrix=bt709:out_range=tv,format=yuva420p,setpts=PTS-STARTPTS+120/30/TB"
O="eof_action=pass:format=auto:enable='gte(n,120)*lt(n,180)'"
ffmpeg -v error -y $BG $LP -i half.png $LP -i half.png $LP -i top.png -filter_complex "[1:v]$P[a];[2:v]$P[b];[3:v]$P[c];[0:v][a]overlay=300:200:$O[x];[x][b]overlay=50:40:$O[y];[y][c]overlay=300:200:$O[z];[z]format=yuv420p[v]" -map "[v]" -frames:v 240 $ENC three.mp4; echo "three(as compile): $(px three.mp4)"
ffmpeg -v error -y $BG $LP -i half.png $LP -i top.png -filter_complex "[1:v]$P[a];[2:v]$P[c];[0:v][a]overlay=300:200:$O[x];[x][c]overlay=300:200:$O[z];[z]format=yuv420p[v]" -map "[v]" -frames:v 240 $ENC two.mp4; echo "two: $(px two.mp4)"
ffmpeg -v error -y $BG $LP -i top.png -filter_complex "[1:v]$P[c];[0:v][c]overlay=300:200:$O[z];[z]format=yuv420p[v]" -map "[v]" -frames:v 240 $ENC one.mp4; echo "one: $(px one.mp4)"
# variants: no enable
O2="eof_action=pass:format=auto"
ffmpeg -v error -y $BG $LP -i half.png $LP -i top.png -filter_complex "[1:v]$P[a];[2:v]$P[c];[0:v][a]overlay=300:200:$O2[x];[x][c]overlay=300:200:$O2[z];[z]format=yuv420p[v]" -map "[v]" -frames:v 240 $ENC two_noenable.mp4; echo "two_noenable: $(px two_noenable.mp4)"
# variant: overlay placed so both exist longer (-t 6 and no setpts shift by unrelated)
ffmpeg -v error -y $BG -loop 1 -framerate 30 -t 6 -i half.png -loop 1 -framerate 30 -t 6 -i top.png -filter_complex "[1:v]$P[a];[2:v]$P[c];[0:v][a]overlay=300:200:$O[x];[x][c]overlay=300:200:$O[z];[z]format=yuv420p[v]" -map "[v]" -frames:v 240 $ENC two_t6.mp4; echo "two_t6: $(px two_t6.mp4)"
# variant: fps filter after setpts
P2="$P,fps=30"
ffmpeg -v error -y $BG $LP -i half.png $LP -i top.png -filter_complex "[1:v]$P2[a];[2:v]$P2[c];[0:v][a]overlay=300:200:$O[x];[x][c]overlay=300:200:$O[z];[z]format=yuv420p[v]" -map "[v]" -frames:v 240 $ENC two_fps.mp4; echo "two_fps: $(px two_fps.mp4)"
# variant: per-frame sweep for "two"
for n in 100 119 120 121 125 150 179 180; do echo "two frame $n: $(px two.mp4 350 240 $n)"; done
# mov alpha
ffmpeg -v error -y -f lavfi -i "color=c=0xffff00@0.0:s=100x80:r=30:d=1,format=yuva444p10le" -f lavfi -i "color=c=0xffff00@1.0:s=100x80:r=30:d=1,format=yuva444p10le" -filter_complex "[0][1]concat=n=2:v=1:a=0" -c:v prores_ks -profile:v 4444 -pix_fmt yuva444p10le ov.mov
ffprobe -v error -show_entries stream=pix_fmt,r_frame_rate,nb_frames,duration -of csv=p=0 ov.mov
PM="format=rgba,scale=100:80:out_color_matrix=bt709:out_range=tv,format=yuva420p,setpts=PTS-STARTPTS+120/30/TB"
ffmpeg -v error -y $BG -t 2 -i ov.mov -filter_complex "[1:v]$PM[c];[0:v][c]overlay=50:40:$O[z];[z]format=yuv420p[v]" -map "[v]" -frames:v 240 $ENC mov.mp4
for n in 126 135 149 150 151 152 160 170; do echo "mov frame $n: $(px mov.mp4 100 80 $n)"; done
PM0="format=rgba,scale=100:80,setpts=PTS-STARTPTS+120/30/TB"
ffmpeg -v error -y $BG -t 2 -i ov.mov -filter_complex "[1:v]$PM0[c];[0:v][c]overlay=50:40:$O[z];[z]format=yuv420p[v]" -map "[v]" -frames:v 240 $ENC mov0.mp4
for n in 126 150 160; do echo "mov(no explicit) frame $n: $(px mov0.mp4 100 80 $n)"; done
ffmpeg -v error -y -i ov.mov -vf "select=eq(n\,45),format=rgba,crop=1:1:50:40" -frames:v 1 -f rawvideo - | xxd -p
ffmpeg -v error -y -i ov.mov -vf "select=eq(n\,10),format=rgba,crop=1:1:50:40" -frames:v 1 -f rawvideo - | xxd -p
