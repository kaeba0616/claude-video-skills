---
name: shorts-generate
description: 주제나 제품을 받아 세로형 쇼츠·광고 영상을 자동 생산한다 — bizrouter LLM으로 대본 작성, Veo 3.1로 8초 클립 생성, ffmpeg로 자막 합성까지. 제품 사진을 참조로 넘겨 실제 제품이 등장하는 광고도 만든다. "쇼츠 만들어줘", "광고 영상 만들어줘", "마케팅 영상", "제품 홍보 영상", "/shorts-generate <주제>" 요청 시 사용.
---

# shorts-generate: Veo 3.1 쇼츠·광고 자동 생산

주제(또는 제품 사진) → 대본 → 장면별 Veo 클립 → 자막 합성 → 업로드용 mp4 파이프라인.
과금이 발생하므로 **4단계 승인 게이트 전에는 절대 영상 생성 API를 호출하지 않는다.**

## 비용표 (제출 시점 과금)

| 모델 | 720p 8초 실측 | 참조 이미지 |
|---|---|---|
| google/veo-3.1-lite (일반 쇼츠 기본) | 611~615원/클립 | ✘ |
| google/veo-3.1-fast (광고 기본) | 1,223원/클립 | ✔ |
| google/veo-3.1 | 미측정 — 실행 전 cost_krw 확인 | ✔ |

일반 쇼츠 1편 = 3클립 ≈ 1,845원. 광고 1편 = 3클립 ≈ 3,669원. 1080p/4k는 요금이 다르므로 첫 제출 응답의 `cost_krw`를 확인해 보고할 것.

## 모드 선택

| | 일반 쇼츠 | 광고·제품 영상 |
|---|---|---|
| 모델 | `veo-3.1-lite` | `veo-3.1-fast` |
| 이미지 입력 | 없음 (text-to-video) | `--reference-image <제품사진>` |
| 대본 | 훅 → 전개 → 여운 | 훅 → 제품 가치 → CTA |

광고는 **실제 제품 사진이 있어야** 의미가 있다. 없으면 사용자에게 요청할 것 (없이도 만들 수는 있으나 매 클립마다 제품 외형이 달라진다).

### 모델·필드 제약 (실측, 위반 시 생성 전 400)

- `reference_images`와 `negative_prompt`는 **lite 미지원** → 광고는 반드시 `veo-3.1-fast` 이상
- `reference_images` + `image`/`last_frame` 동시 사용 불가 (배타적 모드)
- `reference_images` + `negative_prompt` 동시 사용 불가
- `--image`(첫 프레임 지정)를 쓸 땐 **입력 이미지를 목표 화면비로 미리 크롭**할 것. 9:16에 정사각 이미지를 넣으면 검은 레터박스 띠가 영상 내내 남는다 (`ffmpeg -i in.png -vf "scale=720:1280:force_original_aspect_ratio=increase,crop=720:1280" out.png`)

제품 재현 품질은 `--reference-image`가 `--image`보다 낫다(`--image`는 입력 화면비가 그대로 박히고 장면 자유도가 없다). 광고에는 `--reference-image`를 기본으로 쓴다.

**단, 참조 이미지도 완벽하지는 않다** (실측): 본체의 색·재질·무늬·비율은 서로 다른 장면의 클립 간에도 정확히 유지되지만, **뚜껑 같은 작은 부속은 클립마다 다르게 재해석된다**. 드리프트는 클립 단위로 결정되고 클립 내부에서는 안정적이다. 대책은 두 가지이며 둘 다 검증됐다: ① 프롬프트에 해당 부속을 한 구절로 못박기(3단계), ② 5-1 검수 후 어긋난 클립만 재생성.

## 파이프라인

### 1. 주제 확정
- 인자로 주제가 오면 그대로 사용.
- 주제가 없으면 WebSearch로 최근 쇼츠 트렌드(동물, ASMR, 미니어처, 자연 등 Veo가 잘 만드는 소재 위주)를 조사해 3~5개 제안하고 사용자가 고르게 한다.
- **광고인 경우**: 제품 사진 경로, 제품명, 핵심 셀링포인트 1~2개, 타깃을 먼저 확인한다. 제품 사진이 여러 장이면 제품이 크고 선명하게 나온 것을 참조로 쓴다.

### 2. 작업 폴더 생성
`output/<YYYYMMDD>-<주제-영문-슬러그>/` 생성. 이미 있고 script.md가 존재하면 **이어하기 모드**: 완료된 산출물(클립 등)은 건너뛰고 다음 단계부터 재개한다.
광고인 경우 참조할 제품 사진을 폴더 안에 `product.<확장자>`로 복사해 둔다 (png/jpg/jpeg/webp만 지원).

### 더 읽을 것

`references/veo-pitfalls.md` — 실측으로 확인한 프롬프트 함정.
**프롬프트를 쓰기 전에 읽어라.** 특히 첫 항목(인과 동작을 시키지 마라)은
칼로 자르기·부수기 같은 소재를 다룰 때 실패의 주된 원인이다.

### 3. 대본 + 장면 분해 → `script.md`
bizrouter LLM으로 작성한다. 모델: `anthropic/claude-sonnet-5`.

```bash
set -a && source .env && set +a
curl -s -X POST -H "Authorization: Bearer $BIZROUTER_API_KEY" -H "Content-Type: application/json" \
  -d @request.json https://api.bizrouter.ai/v1/chat/completions
```

script.md 필수 구성:
- **훅**: 첫 1.5초에 시선을 고정할 장면/문구
- **장면 3~4개** (각 8초): 장면별 한 줄 스토리 + 한글 자막 문구(최종 타임라인 초 단위) + 영어 Veo 프롬프트
- Veo 프롬프트 작성 규칙:
  - 영어로. 구성: 피사체 + 행동 + 카메라 워크 + 조명/분위기 + 오디오 큐(예: "gentle ambient garden sounds")
  - 클립 간 일관성: 동일한 스타일 문구 블록(피사체 외형·색감·조명 묘사)을 모든 장면 프롬프트에 반복 포함
  - 영상 안에 글자를 넣지 말 것 (텍스트 렌더링 품질 낮음 — 자막은 assemble이 번인)
  - 마지막 장면은 여운/반전으로 반복 시청 유도

**광고인 경우 추가 규칙:**
- Veo 프롬프트에 **제품의 전체 색·무늬는 묘사하지 말 것**. 외형은 참조 이미지가 담당한다. "the tumbler from the reference image" 처럼 지시하고 프롬프트는 **장면·조명·카메라·분위기**에 집중한다.
- 단 **뚜껑·손잡이·버튼 같은 작은 부속은 한 구절로 못박을 것**. 참조 이미지가 본체(색·재질·무늬·비율)는 클립 간에 정확히 유지하지만 **작은 부속은 클립마다 다르게 재해석한다**(실측: 불투명 오렌지 뚜껑 → 반투명 구리 림 뚜껑). 프롬프트에 `...its opaque bright orange flat lid clearly visible` 한 구절을 넣자 **동일 장면·동일 참조 이미지에서 원본 뚜껑이 그대로 복원됐다**(A/B 검증 완료, 클립 내내 안정). 참조 이미지의 부속 중 광고에서 눈에 띄는 것은 미리 이렇게 못박아 둘 것.
- 브랜드명·가격·CTA 문구는 영상 안에 넣지 말고 자막으로 번인한다.
- 실존 인물이나 타사 로고는 생성이 거부되거나 왜곡되므로 프롬프트에서 배제한다.
- 장면 구성 예: ① 제품 등장(훅) → ② 사용 맥락/가치 → ③ 제품 클로즈업 + CTA 자막

### 4. ✋ 승인 게이트 (필수)
사용자에게 제시: script.md 내용 + `장면 수 × 클립 단가 = 예상 비용`(광고는 fast 단가 1,223원 기준이라 일반 쇼츠의 2배임을 명시).
**명시적 승인 전에는 veo_generate.py를 실행하지 않는다.**

### 실행 위치

이 스킬은 개인 스킬(`~/.claude/skills/`)이라 어느 디렉터리에서든 발동한다.
스크립트는 절대경로(`~/.claude/skills/shorts-generate/scripts/...`)로 부르고,
`output/<폴더>/` 는 **현재 작업 디렉터리 기준**으로 만든다 — 사용자가 있는
곳에 산출물이 남아야 한다.

### 5. 클립 생성
장면별로 실행 (실패한 장면만 재실행하면 되도록 개별 파일):

```bash
# 일반 쇼츠
python3 ~/.claude/skills/shorts-generate/scripts/veo_generate.py \
  --prompt-file output/<폴더>/prompts/scene_01.txt \
  --out output/<폴더>/clips/scene_01.mp4 \
  --model google/veo-3.1-lite --resolution 720p --aspect-ratio 9:16 --duration 8

# 광고 — 모든 장면에 같은 제품 사진을 참조로 넘겨 외형을 통일한다
python3 ~/.claude/skills/shorts-generate/scripts/veo_generate.py \
  --prompt-file output/<폴더>/prompts/scene_01.txt \
  --out output/<폴더>/clips/scene_01.mp4 \
  --reference-image output/<폴더>/product.png \
  --model google/veo-3.1-fast --resolution 720p --aspect-ratio 9:16 --duration 8
```

- 각 실행은 제출→폴링→다운로드→faststart 리먹스까지 자동 (약 1분). 여러 장면은 run_in_background로 병렬 실행 가능.
- **주의**: 결과물이 완료 후 1~2분 내 만료되므로 스크립트를 중간에 끊지 말 것.
- 실패/만료 시 해당 장면만 재실행 (성공한 클립은 보존 → 중복 과금 방지).
- `model_blacklisted` 에러 시: bizrouter 콘솔 조직 설정에서 모델 차단 해제 안내.
- Veo 원본은 moov 아톰이 파일 끝에 있어 그대로는 일부 플레이어(특히 Windows에서 WSL 경로로 열 때) 재생이 안 된다. 스크립트가 다운로드 후 자동으로 무손실 리먹스하며, 결과 JSON의 `faststart` 값으로 확인할 수 있다. `false`면 ffmpeg 설치 여부를 확인할 것.

**5-1. ✋ 광고 클립 간 일관성 검수 (광고 필수, 조립 전)**

각 클립에서 제품이 크게 보이는 프레임을 1장씩 뽑아 가로로 붙이고 Read로 대조한다:

```bash
for c in output/<폴더>/clips/*.mp4; do
  ffmpeg -v error -ss 3 -i "$c" -frames:v 1 -vf scale=360:640 -y "/tmp/cmp_$(basename $c .mp4).png"
done
ffmpeg -v error -i output/<폴더>/product.png -vf "scale=360:640:force_original_aspect_ratio=decrease,pad=360:640:(ow-iw)/2:(oh-ih)/2:white" -y /tmp/cmp_00src.png
ffmpeg -v error $(printf ' -i %s' /tmp/cmp_*.png) -filter_complex "hstack=inputs=N" -y /tmp/montage.png
```

원본과 모든 클립의 **색·무늬·비율·부속(뚜껑 등)**을 대조해, 어긋난 클립만 프롬프트에 해당 부속을 명시해 **재생성**한다(장당 1,223원). 드리프트는 클립 단위로 결정되고 클립 내부에서는 안정적이므로, 틀어진 클립만 다시 뽑으면 해결된다.

### 6. 조립
script.md의 자막을 `subs.json`(`[{"start","end","text"}]`, 최종 타임라인 기준 초)으로 저장 후:

```bash
python3 ~/.claude/skills/shorts-generate/scripts/assemble.py \
  --clips output/<폴더>/clips/scene_01.mp4 output/<폴더>/clips/scene_02.mp4 \
  --subtitles output/<폴더>/subs.json \
  --out output/<폴더>/final.mp4
```

완료 후 final.mp4에서 프레임 2~3장을 추출(ffmpeg)해 Read로 자막·화면을 검수한다.

### 7. 메타데이터 → `metadata.md`
업로드용으로 작성: 제목 후보 3개(호기심 유발형), 설명 2~3문장, 해시태그 10개(#shorts 포함).

### 8. 결과 보고
final.mp4 경로, 총 비용(각 응답의 cost_krw 합산), 길이/해상도, metadata.md 요약을 보고한다.
