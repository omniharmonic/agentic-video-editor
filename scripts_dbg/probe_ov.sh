set -u
cd "$(mktemp -d)"
python3 - <<'PY'
from PIL import Image
Image.new("RGBA",(100,80),(0,255,0,255)).save("top.png")
Image.new("RGBA",(100,80),(255,255,255,128)).save("half.png")
PY
px() { ffmpeg -v error -y -i "$1" -vf "select=eq(n\,150),format=rgb24,crop=1:1:${2:-350}:${3:-240}" -frames:v 1 -f rawvideo - | xxd -p; }
BG="-f lavfi -i color=c=0x2040ff:s=640x360:r=30:d=8,format=yuv420p"
run() { name=$1; shift; ffmpeg -v error -y $BG "$@" -c:v libx264 -pix_fmt yuv420p -colorspace bt709 -color_primaries bt709 -color_trc bt709 -color_range tv "$name.mp4" 2>&1 | head -3; echo "$name -> $(px $name.mp4)"; }
run v0 -i top.png -filter_complex "[1:v]format=rgba[o];[0][o]overlay=300:200"
run v1 -i top.png -filter_complex "[1:v]format=rgba[o];[0][o]overlay=300:200:format=auto"
run v2 -i top.png -filter_complex "[1:v]format=rgba,format=yuva420p[o];[0][o]overlay=300:200:format=auto"
run v3 -loop 1 -framerate 30 -t 5 -i top.png -filter_complex "[1:v]format=rgba,setpts=PTS-STARTPTS+120/30/TB[o];[0][o]overlay=300:200:eof_action=pass:format=auto:enable='gte(n,120)*lt(n,180)'"
run v4 -loop 1 -framerate 30 -t 5 -i top.png -filter_complex "[1:v]format=rgba,scale=100:80:out_color_matrix=bt709:out_range=tv,format=yuva420p,setpts=PTS-STARTPTS+120/30/TB[o];[0][o]overlay=300:200:eof_action=pass:format=auto:enable='gte(n,120)*lt(n,180)'"
run v5 -loop 1 -framerate 30 -t 5 -i top.png -filter_complex "[1:v]format=rgba,scale=100:80:out_color_matrix=bt709:out_range=tv,format=yuva420p,setpts=PTS-STARTPTS+120/30/TB[o];[0][o]overlay=300:200:eof_action=pass:format=yuv420:enable='gte(n,120)*lt(n,180)'"
echo "dump v3 pixel region"; for x in 300 310 350 399; do echo "x=$x $(px v3.mp4 $x 240)"; done
echo "bg only: $(px v0.mp4 10 10)"
ffmpeg -v error -y $BG -c:v libx264 -pix_fmt yuv420p -colorspace bt709 -color_primaries bt709 -color_trc bt709 -color_range tv bg.mp4; echo "bg: $(px bg.mp4 350 240)"
