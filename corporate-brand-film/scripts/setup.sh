#!/usr/bin/env bash
# 실행 환경 점검. 없는 것만 알려주고, 고치는 명령을 같이 준다.
set -uo pipefail

SKILL_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
PIPELINE="$SKILL_DIR/pipeline"
fail=0

ok()   { printf "  \033[32m✓\033[0m %s\n" "$1"; }
bad()  { printf "  \033[31m✗\033[0m %s\n" "$1"; fail=1; }
hint() { printf "      %s\n" "$1"; }

echo "== 외부 도구 =="
for cmd in ffmpeg ffprobe; do
  if command -v "$cmd" >/dev/null 2>&1; then ok "$cmd"; else
    bad "$cmd 없음"
    hint "sudo apt install ffmpeg   /   brew install ffmpeg"
  fi
done
if command -v fc-match >/dev/null 2>&1; then ok "fontconfig"; else
  bad "fc-match 없음 (폰트 탐색 불가)"
  hint "sudo apt install fontconfig"
fi

echo
echo "== 한글 폰트 =="
# 파이프라인이 찾는 것과 같은 방식으로 확인한다.
for family in NanumGothicExtraBold NanumGothic; do
  path=$(fc-match -f "%{file}" "$family" 2>/dev/null || true)
  norm_family=$(echo "$family" | tr 'A-Z' 'a-z')
  norm_path=$(echo "$path" | tr 'A-Z' 'a-z' | tr -d '-')
  if [ -n "$path" ] && echo "$norm_path" | grep -q "$norm_family"; then
    ok "$family → $(basename "$path")"
  else
    bad "$family 못 찾음 (지금은 '$(basename "${path:-없음}")' 로 폴백됨)"
    hint "나눔고딕 Regular + ExtraBold 를 설치하고 fc-cache -f 실행"
    hint "sudo apt install fonts-nanum fonts-nanum-extra"
    hint "폰트가 없으면 한글 자막이 조용히 다른 폰트로 렌더링됩니다."
  fi
done

echo
echo "== Python =="
PY=$(command -v python3 || true)
if [ -z "$PY" ]; then
  bad "python3 없음"
else
  ver=$("$PY" -c 'import sys; print("%d.%d" % sys.version_info[:2])')
  if "$PY" -c 'import sys; sys.exit(0 if sys.version_info >= (3,12) else 1)'; then
    ok "python3 $ver"
  else
    bad "python3 $ver — 3.12 이상 필요"
  fi
fi

echo
echo "== Python 패키지 =="
for mod in yaml requests; do
  if "$PY" -c "import $mod" 2>/dev/null; then ok "$mod"; else
    bad "$mod 없음"
    hint "pip install -e '$PIPELINE'"
  fi
done
for mod in faster_whisper; do
  if "$PY" -c "import $mod" 2>/dev/null; then ok "$mod (선택)"; else
    printf "  \033[33m-\033[0m %s\n" "$mod 없음 (선택 — verify --transcribe 에만 필요)"
    hint "pip install faster-whisper"
  fi
done

echo
echo "== 콜론 별칭 =="
# './video:prepare' 같은 콜론 형태 명령. 콜론은 Windows 파일명에 쓸 수 없어
# 저장소에 담으면 Windows 에서 clone 자체가 깨진다(checkout 실패) —
# 그래서 저장소에는 넣지 않고 POSIX 환경에서 여기서 만든다.
if ln -sf video "$SKILL_DIR/video:prepare" 2>/dev/null; then
  for c in music new preview produce rebuild redo script shots stills testshot verify; do
    ln -sf video "$SKILL_DIR/video:$c"
  done
  ok "video:<command> 별칭 12개 생성"
else
  printf "  \033[33m-\033[0m 심볼릭 링크를 만들 수 없는 파일시스템 — './video <command>' 형태만 사용"
fi

echo
echo "== API 키 =="
if [ -n "${BIZROUTER_API_KEY:-}" ]; then ok "BIZROUTER_API_KEY 설정됨"
elif [ -n "${GEMINI_API_KEY:-}" ]; then ok "GEMINI_API_KEY 설정됨"
else
  printf "  \033[33m-\033[0m API 키 없음 — --fake 로만 돌릴 수 있습니다\n"
  hint "export BIZROUTER_API_KEY=...   (또는 GEMINI_API_KEY)"
fi

echo
if [ "$fail" -eq 0 ]; then
  echo "준비 완료. 무료로 전 구간 돌려보기:"
  # video 래퍼가 물려준 호출 형태가 있으면 그 형태로 안내한다 —
  # 사용자가 친 것과 다른 명령을 알려주면 그대로 따라 치다가 막힌다.
  if [ -n "${VIDEO_INVOKED_AS:-}" ]; then
    echo "  $VIDEO_INVOKED_AS new 우리회사"
    echo "  VIDEO_BRIEF=brief/우리회사.yaml $VIDEO_INVOKED_AS preview"
  else
    echo "  cd $PIPELINE && python3 -m src.cli all --brief brief/template.yaml --out build --fake"
  fi
else
  echo "위의 ✗ 항목을 먼저 해결하세요."
  exit 1
fi
