"""타이포 3계층 ASS 자막.

reference/00_분석리포트.md 4-3절:
  1. 챕터 타이틀 — 화면 중앙, 대문자 영문 볼드
  2. 기술 라벨   — 좌하단, 한글 굵게 + 영문 소문자 부제 2단
  3. 나레이션 자막 — 하단 중앙, 얇은 흰 글씨
ASS 색상은 &HAABBGGRR. Alignment는 넘패드(1=좌하, 2=중앙하, 5=중앙).
"""
from src import constants as C
from src.beats import Beatsheet
from src.brief import Brief
from src.shotlist import Shotlist

HEADER = f"""[Script Info]
ScriptType: v4.00+
PlayResX: {C.WIDTH}
PlayResY: {C.HEIGHT}
WrapStyle: 2
ScaledBorderAndShadow: yes

[V4+ Styles]
Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding
Style: Narration,{C.FONT_REGULAR_NAME},44,&H00FFFFFF,&H000000FF,&H96000000,&H96000000,0,0,0,0,100,100,0,0,1,0,2,2,120,120,64,1
Style: LabelKo,{C.FONT_BOLD_NAME},48,&H00FFFFFF,&H000000FF,&H00000000,&H00000000,0,0,0,0,100,100,1,0,1,0,2,1,110,0,158,1
Style: LabelEn,{C.FONT_REGULAR_NAME},26,&H00C8C8C8,&H000000FF,&H00000000,&H00000000,0,0,0,0,100,100,3,0,1,0,2,1,110,0,124,1
Style: Chapter,{C.FONT_BOLD_NAME},96,&H00FFFFFF,&H000000FF,&H00000000,&H00000000,0,0,0,0,100,100,8,0,1,0,0,5,0,0,0,1
Style: LogoScrim,{C.FONT_REGULAR_NAME},20,&H1A000000,&H000000FF,&H00000000,&H00000000,0,0,0,0,100,100,0,0,1,0,0,7,0,0,0,1
Style: LogoKo,{C.FONT_BOLD_NAME},110,&H00FFFFFF,&H000000FF,&H00000000,&H00000000,0,0,0,0,100,100,16,0,1,0,0,5,0,0,0,1
Style: LogoEn,{C.FONT_REGULAR_NAME},32,&H00C8D2D5,&H000000FF,&H00000000,&H00000000,0,0,0,0,100,100,12,0,1,0,0,5,0,0,0,1
Style: LogoRule,{C.FONT_REGULAR_NAME},22,&H00B8A54F,&H000000FF,&H00000000,&H00000000,0,0,0,0,100,100,7,0,1,0,0,5,0,0,0,1

[Events]
Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text
"""


# 로고 카드 타이밍. 나레이션이 끝나고 이만큼 쉰 뒤 로고가 뜨고, 홀드가
# 이보다 짧으면 아예 띄우지 않는다 — 스치듯 지나가는 로고는 없느니만 못하다.
LOGO_GAP = 0.35
LOGO_MIN_HOLD = 1.2


def _ts(sec: float) -> str:
    """ASS 타임스탬프 H:MM:SS.cc.

    센티초로 먼저 반올림한 뒤 정수 연산으로 자릿수를 올린다. 실수 그대로
    포맷하면 59.999 가 "0:00:60.00" 이 되어 libass 가 거부하는 값이 나온다.
    """
    total_cs = round(max(sec, 0.0) * 100)
    cs = total_cs % 100
    total_s = total_cs // 100
    h, rem = divmod(total_s, 3600)
    m, s = divmod(rem, 60)
    return f"{h}:{m:02d}:{s:02d}.{cs:02d}"


def _dialogue(start: float, end: float, style: str, text: str) -> str:
    return f"Dialogue: 0,{_ts(start)},{_ts(end)},{style},,0,0,0,,{text}"


def build_ass(bs: Beatsheet, sl: Shotlist, brief: Brief,
              audio_seconds: dict[str, float] | None = None) -> str:
    """3계층 타이포 ASS.

    audio_seconds 는 {막 이름: 그 막 나레이션 음성의 실제 길이(초)}. 주면 나레이션
    자막을 그 길이 안에 분배하고, 없으면 막의 예약 길이 전체에 분배한다.

    막의 예약 길이는 실제 음성보다 길다(비트 모델이 안전 여유를 둔다 — 실측
    0.34~2.60초). 예약 길이 기준으로 자막을 깔면 음성은 이미 끝났는데 자막이
    남아, 막마다 최대 2.6초까지 뒤로 밀린다. 실제 실행에서 사용자가 "자막과 말의
    싱크가 안 맞는다"고 지적한 원인이다.
    """
    lines: list[str] = []

    # 1) 타이틀 카드 — 영문 사명, 페이드 인/아웃.
    #    챕터 타이틀 계층은 전부 대문자가 규칙이므로 brief 표기와 무관하게 강제한다.
    title = next(b for b in bs.beats if b.name == "title")
    lines.append(_dialogue(title.start + 0.4, title.start + title.seconds - 0.4,
                           "Chapter", r"{\fad(500,500)}" + brief.name_en.upper()))

    # 2) 기술 라벨 — 챕터 두 번째 컷에 3초 노출, 한글/영문 2단
    for shot in sl.shots:
        if shot.label_ko:
            end = shot.start + min(3.0, shot.seconds * 2)
            lines.append(_dialogue(shot.start, end, "LabelKo",
                                   r"{\fad(300,300)}" + shot.label_ko))
            lines.append(_dialogue(shot.start, end, "LabelEn",
                                   r"{\fad(300,300)}" + shot.label_en))

    # 3) 나레이션 자막 — 실제 음성 길이 안에서 글자수 비례로 분배
    closing_last_line_start: float | None = None
    for beat in bs.beats:
        if not beat.narrated or not beat.lines:
            continue
        span = beat.seconds
        if audio_seconds and beat.name in audio_seconds:
            # 음성보다 길게 깔지 않는다. 예약 길이보다 길어질 일은 없지만
            # (build_narration_track 이 초과를 막는다) 방어적으로 함께 제한한다.
            span = min(audio_seconds[beat.name], beat.seconds)
        weights = [max(len(l), 1) for l in beat.lines]
        total = sum(weights)
        t = beat.start
        for i, (line, w) in enumerate(zip(beat.lines, weights)):
            dur = span * w / total
            if beat.name == "closing" and i == len(beat.lines) - 1:
                # 클로징 마지막 줄은 사명이고, 같은 시점에 로고 카드가 그 사명을
                # 화면 중앙에 크게 세운다. 하단 자막까지 같이 띄우면 한 화면에
                # 같은 글자가 두 번 나온다 — 로고 쪽에 맡기고 여기선 건너뛴다.
                closing_last_line_start = t
                t += dur
                continue
            lines.append(_dialogue(t, t + dur, "Narration", line))
            t += dur

    # 4) 로고 카드 — 사명이 발화되는 순간 화면 중앙에 사명을 세워 끝맺는다.
    #
    # 레퍼런스 28편의 클로징은 슬로건 → 사명 → 로고 순서다. 슬로건은 나레이션
    # 자막이 담당하고, 마지막 줄(= 사명)이 발화되는 시점에 로고가 떠서 끝까지
    # 홀드한다 — "한빛소재." 하고 부르는 순간 로고가 뜨는 게 이 장르의 문법이다.
    #
    # 나레이션이 다 끝난 뒤에 띄우면 홀드가 1초 남짓밖에 안 남아 스치듯 지나간다
    # (실측: 클로징 음성 4.25초 + 여백이면 로고에 1.2초). 발화와 겹쳐야 제 시간을
    # 갖는다.
    closing = next((b for b in bs.beats if b.name == "closing"), None)
    if closing is not None:
        if closing_last_line_start is not None:
            start = closing_last_line_start
        else:
            spoken = closing.seconds
            if audio_seconds and closing.name in audio_seconds:
                spoken = min(audio_seconds[closing.name], closing.seconds)
            start = closing.start + spoken + LOGO_GAP
        end = float(C.TOTAL_SECONDS)
        if end - start >= LOGO_MIN_HOLD:
            fade = r"{\fad(600,300)}"
            cx = C.WIDTH // 2
            # 배경 위에 어두운 막을 깔아야 사명이 읽힌다 — 클로징 샷이 무엇이든
            # 로고는 항상 같은 밝기 위에 놓인다. ASS 도형으로 전체 화면을 덮는다.
            scrim = (r"{\fad(500,300)\p1}m 0 0 l "
                     f"{C.WIDTH} 0 {C.WIDTH} {C.HEIGHT} 0 {C.HEIGHT}" + r"{\p0}")
            lines.append(_dialogue(start, end, "LogoScrim", scrim))
            # 위치는 \pos 로 못박는다 — Alignment 5(중앙)에서 MarginV 는
            # libass 가 무시하는 경우가 있어 줄이 겹친다.
            lines.append(_dialogue(start, end, "LogoKo",
                                   fade + rf"{{\pos({cx},{int(C.HEIGHT*0.46)})}}" + brief.name_ko))
            lines.append(_dialogue(start, end, "LogoEn",
                                   fade + rf"{{\pos({cx},{int(C.HEIGHT*0.565)})}}" + brief.name_en.upper()))
            lines.append(_dialogue(start, end, "LogoRule",
                                   fade + rf"{{\pos({cx},{int(C.HEIGHT*0.625)})}}" + brief.slogan_en.upper()))

    return HEADER + "\n".join(lines) + "\n"
