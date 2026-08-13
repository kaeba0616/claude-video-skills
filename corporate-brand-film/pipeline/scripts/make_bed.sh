#!/usr/bin/env bash
# 배경음 베드 생성. bizrouter 카탈로그에 음악 생성 모델이 없어서 ffmpeg 로 합성한다.
#
# 레퍼런스 28편의 음악 성격에 맞춘 구성: 느린 패드, 저역 중심, 서스테인.
# Cm 계열 화음(C-Eb-G-C)에 서브베이스(C1)를 깔고, 느린 트레몰로로 숨을 주고,
# 로우패스와 롱 리버브로 뒤로 물린다.
#
# 레벨은 여기서 맞추지 않는다 — assemble.py 가 나레이션(-16 LUFS) 대비
# -28 LUFS 로 정규화하고 사이드체인으로 눌러준다. 여기서는 모양만 만든다.
#
# 사용법: scripts/make_bed.sh [출력경로] [길이초]
set -euo pipefail

OUT="${1:-assets/bed.wav}"
DUR="${2:-88}"
FADE_OUT_START=$(python3 -c "print(max(0, $DUR - 2.5))")

mkdir -p "$(dirname "$OUT")"

ffmpeg -hide_banner -loglevel error -y \
  -f lavfi -i "sine=frequency=130.81:duration=$DUR" \
  -f lavfi -i "sine=frequency=155.56:duration=$DUR" \
  -f lavfi -i "sine=frequency=196.00:duration=$DUR" \
  -f lavfi -i "sine=frequency=261.63:duration=$DUR" \
  -f lavfi -i "sine=frequency=65.41:duration=$DUR" \
  -filter_complex "\
   [0:a]volume=0.30,tremolo=f=0.5:d=0.35[a0]; \
   [1:a]volume=0.22,tremolo=f=0.35:d=0.30[a1]; \
   [2:a]volume=0.18,tremolo=f=0.25:d=0.40[a2]; \
   [3:a]volume=0.10,tremolo=f=0.15:d=0.45[a3]; \
   [4:a]volume=0.35[a4]; \
   [a0][a1][a2][a3][a4]amix=inputs=5:normalize=0, \
   lowpass=f=1400, aecho=0.8:0.9:1000|1800:0.35|0.2, \
   afade=t=in:st=0:d=4, afade=t=out:st=$FADE_OUT_START:d=2.5, \
   atrim=0:$DUR, loudnorm=I=-26:TP=-6:LRA=7[m]" \
  -map "[m]" -c:a pcm_s16le "$OUT"

echo "생성: $OUT ($(ffprobe -v error -show_entries format=duration -of csv=p=0 "$OUT")초)"
