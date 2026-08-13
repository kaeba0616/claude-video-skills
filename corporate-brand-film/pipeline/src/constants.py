"""파이프라인 전역 상수. 매직넘버는 전부 여기에만 둔다."""

# 나레이션 (reference/00_분석리포트.md 3-1절에서 측정된 값)
SPEECH_RATE = 4.2          # 자/초 — 표준 기업 나레이션 대역(3.5~4.7)의 중앙
PAUSE_FACTOR = 1.15        # 문장 간 호흡 여백 계수 (검증 대역 판정에만 사용)
# 막 길이 역산 모델: 초 = 글자수/SPEECH_RATE_PURE + SEGMENT_OVERHEAD
#
# SPEECH_RATE(4.2)는 "순수 발화속도"와 "세그먼트당 고정비용"을 한 값에 뭉갠
# 근사였다. Task 18 에서 실제 TTS(gpt-4o-mini-tts, ash) 7개 막을 측정해
# 최소자승을 걸어보니 초 = 글자수/5.08 + 1.72 로, 두 항이 분명히 분리된다.
# 고정비용은 도입부 무음·문장 사이 쉼처럼 글자수와 무관한 비용이다.
#
# 비례항만 쓰면 짧은 막을 구조적으로 과소 추정한다: 16자짜리 클로징 막이
# 설계 4.00초 대비 실측 5.15초(실효 3.11 자/초)로 나와 조립 전 방어에 걸렸다.
# 같은 대본의 긴 막 6개는 4.0~4.55 자/초로 잘 맞았으므로 발화속도가 틀린 게
# 아니라 모델에 상수항이 없던 것이다.
#
# 회귀값(5.08 / 1.72)을 그대로 쓰면 최대 과소 추정이 0.42초로 허용치(0.5초)에
# 여유가 없다. 안전 쪽으로 반올림해 5.08→4.8, 1.72→2.0 을 쓴다.
SPEECH_RATE_PURE = 4.8
SEGMENT_OVERHEAD = 2.0

# 포맷
TOTAL_SECONDS = 88
TOTAL_CUTS = 33
WIDTH = 1920
HEIGHT = 1080
FPS = 30

# 무나레이션 구간 (콜드오픈 + 타이틀 카드)
COLD_OPEN_SECONDS = 14
COLD_OPEN_CUTS = 4
TITLE_SECONDS = 4
TITLE_CUTS = 1

# Veo 3.1
VEO_DURATION = 4
VEO_RESOLUTION = "720p"
VEO_ASPECT = "16:9"
# 부정어를 긍정 프롬프트 끝에 문자열로 붙이면(과거 방식) 오히려 그 요소를
# 불러오는 역효과가 알려져 있다 — "no subtitles"라는 텍스트 자체가 subtitles를
# 연상시키는 임베딩을 만든다는 보고. Gemini(Veo SDK)와 bizrouter 둘 다
# negative_prompt 전용 필드를 받으므로, 이 값은 build_prompt()가 만드는
# 긍정 프롬프트에는 더 이상 들어가지 않고 각 provider가 생성 요청의
# negative_prompt 필드로 직접 전달한다.
# Task 18 실측으로 항목이 늘었다. "35mm film grain" 을 스타일 접두사에 넣었더니
# Veo 가 이를 "필름 그레인 질감"이 아니라 "35mm 필름을 보여줘"로 해석해서,
# 33컷 중 약 10컷에 퍼포레이션 구멍과 필름 엣지 마킹(-35, 35.MM, 36 등)이
# 화면에 구워져 나왔다. 접두사에서 "35mm" 를 뺐고, 여기에도 명시적으로 금지한다.
VEO_NEGATIVE_PROMPT = (
    "no on-screen text, no subtitles, no captions, no logos, no watermark, "
    "no film strip border, no sprocket holes, no film perforations, "
    "no filmstrip frame, no timecode, no lettering, no signage"
)
# negative_prompt 를 받는 모델. veo-3.1-lite 는 이 필드를 보내면 400 으로
# 거절한다("`negativePrompt` isn't supported by this model", Task 17 에서 실측).
# 한글 타이포는 전부 ffmpeg 오버레이로 얹으므로 Veo 클립은 글자 없는 깨끗한
# 판이어야 한다 — 이 필드를 못 쓰는 모델은 그 보장 수단이 없다는 뜻이다.
BIZROUTER_NEGATIVE_PROMPT_MODELS = frozenset({"google/veo-3.1",
                                              "google/veo-3.1-fast"})

# --- bizrouter (Veo 3.1 우회 라우터) ---
# 엔드포인트/스키마는 추정이 아니라 GET /v1/models 조회 + 공식 문서
# (bizrouter.ai/docs/video-output, /docs/audio-output, 2026-08 확인)로 확보됐다.
BIZROUTER_BASE = "https://api.bizrouter.ai/v1"
BIZROUTER_VIDEO_MODEL = "google/veo-3.1-fast"      # 사용자 승인: 1컷 시험은 Fast
BIZROUTER_TTS_MODEL = "openai/gpt-4o-mini-tts"     # 이 카탈로그에 Gemini TTS 없음
# gpt-4o-mini-tts 전용 말투 지시(instructions) 기본값. voice와 달리 자유
# 텍스트라 API가 400으로 거부할 화이트리스트가 없다 — 그래도 최종 문구는
# 컨트롤러가 후보 음성 4종 샘플을 들어보고 다듬어 확정한다(task-16-brief.md
# 세 번째 항목). 지금은 레퍼런스 28편 톤을 그대로 시작값으로 둔다.
BIZROUTER_TTS_INSTRUCTIONS = "차분하고 권위 있는 기업 다큐멘터리 톤으로 낭독해줘."
# 나레이션 기본 음성. 후보 4종(ash/echo/sage/verse)을 실제로 합성해 들어보고
# 사용자가 ash 를 선택했다. 실측 발화속도 4.47 자/초로 합격 대역(3.5~4.7)의
# 중앙, 설계 기준값 SPEECH_RATE(4.2)에 가장 가깝다.
# 이 값은 gpt-4o-mini-tts 의 화이트리스트에 있어야 한다 — Gemini 음성(Charon 등)을
# 넣으면 API 가 조용히 무시하지 않고 400 으로 거절한다.
TTS_VOICE = "ash"
# 720p 초당 원화 — bizrouter 가격표(2026-08 확인). 예산 가드의 사전 추정에만 쓰고,
# 실제 청구액은 매 응답의 cost_krw 를 신뢰한다(추정치를 누적하지 않는다).
BIZROUTER_KRW_PER_SEC = {"google/veo-3.1-lite": 76,
                         "google/veo-3.1-fast": 152,
                         "google/veo-3.1": 607}
# 완료 후 다운로드 가능 창(초). Gemini 직결(2일)보다 훨씬 짧다 — 만료되면
# 영상은 서버에서 삭제되고, 재생성은 곧 재과금이다.
BIZROUTER_DOWNLOAD_WINDOW_SECONDS = 30 * 60

# 검증 합격 기준
#
# 세 지표(컷/분·평균 샷·무나레이션)는 레퍼런스 28편 중 "나레이션 중심 기업홍보"
# 22편의 실측 10~90 백분위다. 인터뷰 다큐(01·02), 무나레이션 비주얼(03·04),
# 3D 강의형(05), 쇼릴(28)은 만들려는 것과 다른 포맷이라 뺐다.
#
# 처음에는 실측 분포를 계산하지 않고 "적당한 중심 범위"로 잡았는데(컷/분 17~30,
# 평균 샷 2.0~4.5, 무나레이션 15~30%), 나중에 재측정해보니 적중률이 50~59%였다 —
# 레퍼런스 영상의 절반 가까이가 자기 대역을 통과하지 못하는 대역은 기준으로서
# 실패다. 특히 무나레이션은 분석리포트 본문이 이미 "전체의 8~46%"라고 적어둔
# 값을 상수에서 절반으로 좁혀놓은 상태였다(재계산 결과 7.3~45.9%로 본문이 옳다).
#
# 10~90 백분위를 쓰는 이유: 양 끝의 극단(컷/분 9.1 감성 공간물, 33.3 제품
# 카탈로그)까지 품으면 대역이 아무것도 걸러내지 못한다.
PASS_CUTS_PER_MIN = (10.2, 31.5)      # 중앙 19.5 · 실측 범위 9.1~33.3
PASS_AVG_SHOT = (1.9, 5.9)            # 중앙 3.08s · 실측 범위 1.80~6.57
PASS_SILENT_RATIO = (0.09, 0.40)      # 중앙 21.0% · 실측 범위 8~46%
# 발화속도만 성격이 다르다. 분석리포트 4-2절이 유형별로 나눈 값이고
# (감성 2.7~3.0 / 표준 3.5~4.7 / 설명형 4.8~5.4 / 인터뷰 5.4~5.9),
# 이 파이프라인은 "표준 기업 나레이션"을 목표로 삼는다. 전체 분포로 넓히면
# 감성물과 인터뷰물까지 통과시켜 목표 자체가 사라진다.
PASS_SPEECH_RATE = (3.5, 4.7)
# 대본 글자수 대비 실제로 발화된(=whisper 가 인식한) 글자수의 하한.
#
# TTS 가 문장을 통째로 흘리는 사고를 잡는 유일한 장치다. Task 18 실측에서
# 클로징 막의 "한빛소재."(4자)가 음성에서 사라졌는데, 길이·발화속도·자막
# 어느 검증도 이를 잡지 못했다 — 짧아진 음성은 여전히 막 안에 들어가고,
# 발화속도는 오히려 대역 중앙(4.09)이었으며, 자막은 두 줄 다 정상 표시됐다.
# 요청한 텍스트와 실제 발화를 대조해야만 보인다.
#
# 하한은 전사 오차를 감안해 정한다: 정상 막 6개의 실측 인식률이 96~104%
# (±2자)였고, 문장이 통째로 빠진 클로징만 75%였다. 0.90 이면 전사 오차는
# 통과시키고 문장 누락은 잡는다.
PASS_HEARD_RATIO = 0.90

# 폰트
#
# 패밀리명은 libass 가 ASS 스타일에서 찾는 이름이고, 경로는 설치 여부를
# 확인하기 위한 것이다. 절대경로를 박아두면 다른 머신에서 그대로 죽으므로
# fontconfig 로 찾는다.
#
# 패밀리명이 중요하다: "NanumGothic ExtraBold"(공백)는 fontconfig 가 못 찾아
# 조용히 DejaVu Sans 로 폴백한다 — 한글이 전부 틀린 폰트로 렌더링되는데
# 에러는 없다. 실제 패밀리명은 공백 없는 "NanumGothicExtraBold" 다.
FONT_BOLD_NAME = "NanumGothicExtraBold"
FONT_REGULAR_NAME = "NanumGothic"


def _find_font(family: str) -> str:
    """fontconfig 로 패밀리명에 해당하는 파일 경로를 찾는다.

    찾지 못하면 빈 문자열. 호출부(font_check)가 사람이 읽을 수 있는 안내로
    바꿔서 알린다 — 여기서 예외를 던지면 import 자체가 실패해서, 폰트와
    무관한 단계(script/beats/shots)조차 돌릴 수 없게 된다.
    """
    import shutil
    import subprocess
    if not shutil.which("fc-match"):
        return ""
    try:
        out = subprocess.run(["fc-match", "-f", "%{file}", family],
                             capture_output=True, text=True, timeout=10)
    except (OSError, subprocess.SubprocessError):
        return ""
    path = out.stdout.strip()
    # fc-match 는 못 찾아도 기본 폰트를 돌려주므로, 실제로 그 패밀리인지 본다.
    if not path or family.lower().replace(" ", "") not in path.lower().replace("-", ""):
        return ""
    return path


FONT_BOLD = _find_font(FONT_BOLD_NAME)
FONT_REGULAR = _find_font(FONT_REGULAR_NAME)


def font_check() -> list[str]:
    """빠진 폰트에 대한 안내 문구 목록. 비어 있으면 정상."""
    msgs = []
    for family, path in ((FONT_BOLD_NAME, FONT_BOLD),
                         (FONT_REGULAR_NAME, FONT_REGULAR)):
        if not path:
            msgs.append(
                f"한글 폰트 '{family}' 를 찾을 수 없습니다. 설치 후 fc-cache -f 를 "
                f"실행하세요. 없으면 자막이 조용히 다른 폰트로 렌더링됩니다.")
    return msgs
