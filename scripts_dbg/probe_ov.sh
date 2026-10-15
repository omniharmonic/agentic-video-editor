set -u
cd "$(mktemp -d)"
ffmpeg -v error -y -f lavfi -i "color=c=0x00ff00:s=100x80,format=rgba" -frames:v 1 top.png
ffprobe -v error -show_entries stream=pix_fmt -of csv=p=0 top.png
px() { ffmpeg -v error -y -i "$1" -vf "select=eq(n\,150),format=rgb24,crop=1:1:${2:-350}:${3:-240}" -frames:v 1 -f rawvideo - | xxd -p; }
BG="-f lavfi -i color=c=0x2040ff:s=640x360:r=30:d=8,format=yuv420p"
run() { name=$1; shift; ffmpeg -v error -y $BG "$@" -c:v libx264 -pix_fmt yuv420p -colorspace bt709 -color_primaries bt709 -color_trc bt709 -color_range tv "$name.mp4" 2>&1 | head -3; echo "$name -> $(px $name.mp4)"; }
run v0 -loop 1 -framerate 30 -t 8 -i top.png -filter_complex "[1:v]format=rgba[o];[0][o]overlay=300:200"
run v3 -loop 1 -framerate 30 -t 5 -i top.png -filter_complex "[1:v]format=rgba,setpts=PTS-STARTPTS+120/30/TB[o];[0][o]overlay=300:200:eof_action=pass:format=auto:enable='gte(n,120)*lt(n,180)'"
run v4 -loop 1 -framerate 30 -t 5 -i top.png -filter_complex "[1:v]format=rgba,scale=100:80:out_color_matrix=bt709:out_range=tv,format=yuva420p,setpts=PTS-STARTPTS+120/30/TB[o];[0][o]overlay=300:200:eof_action=pass:format=auto:enable='gte(n,120)*lt(n,180)'"
ffmpeg -v error -y $BG -c:v libx264 -pix_fmt yuv420p -colorspace bt709 -color_primaries bt709 -color_trc bt709 -color_range tv bg.mp4; echo "bg: $(px bg.mp4)"
for f in v0 v3 v4 bg; do echo "$f yuv: $(ffmpeg -v error -y -i $f.mp4 -vf "select=eq(n\,150),crop=2:2:350:240" -frames:v 1 -pix_fmt yuv420p -f rawvideo - | xxd -p)"; done
for vf in "format=rgb24" "scale=in_color_matrix=bt709:out_color_matrix=bt709,format=rgb24" "scale=in_color_matrix=bt601,format=rgb24"; do
  echo "decode $vf -> $(ffmpeg -v error -y -i v4.mp4 -vf "select=eq(n\,150),$vf,crop=1:1:350:240" -frames:v 1 -f rawvideo - | xxd -p)"; done
ffprobe -v error -show_entries stream=pix_fmt,color_space,color_range -of csv=p=0 v4.mp4
