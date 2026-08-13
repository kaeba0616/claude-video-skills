import pytest
import re
from src.brief import load_brief
from src.script import build_script
from src.beats import build_beatsheet
from src.shotlist import build_shotlist
from src.overlay import build_ass
from src import constants as C



def _narration_cues(ass: str) -> list[tuple[float, float, str]]:
    """ASS 문자열에서 (시작초, 종료초, 텍스트) 나레이션 큐 목록."""
    def sec(v: str) -> float:
        h, m, s = v.split(":")
        return int(h) * 3600 + int(m) * 60 + float(s)
    return [(sec(a), sec(bb), c) for a, bb, c in re.findall(
        r"Dialogue: 0,([\d:.]+),([\d:.]+),Narration,,0,0,0,,(.+)", ass)]

def _ass():
    b = load_brief("brief/hanbit.yaml")
    bs = build_beatsheet(build_script(b))
    sl = build_shotlist(bs, b)
    return build_ass(bs, sl, b)


def test_declares_play_resolution():
    a = _ass()
    assert f"PlayResX: {C.WIDTH}" in a and f"PlayResY: {C.HEIGHT}" in a


def test_defines_all_four_styles():
    a = _ass()
    for name in ("Narration", "LabelKo", "LabelEn", "Chapter"):
        assert f"Style: {name}," in a


def test_narration_lines_appear_as_dialogue():
    a = _ass()
    assert "한빛소재는 보이지 않는 곳에서 세상을 움직입니다." in a
    assert "하지만 우리는 멈추지 않았습니다." in a


def test_two_tier_label_emits_both_ko_and_en_lines():
    a = _ass()
    # 라벨은 페이드 태그가 앞에 붙는다: {\fad(300,300)}초정밀 압연
    assert r",LabelKo,,0,0,0,,{\fad(300,300)}초정밀 압연" in a
    assert r",LabelEn,,0,0,0,,{\fad(300,300)}Ultra-Precision Rolling" in a
    assert r",LabelKo,,0,0,0,,{\fad(300,300)}금속 분리판" in a


def test_title_card_uses_chapter_style_with_company_english_name():
    a = _ass()
    assert ",Chapter,,0,0,0,," in a
    assert "HANBIT MATERIALS" in a


def test_timestamps_are_ass_formatted():
    import re
    for line in _ass().splitlines():
        if line.startswith("Dialogue:"):
            assert re.search(r"\d:\d\d:\d\d\.\d\d,\d:\d\d:\d\d\.\d\d", line)


def test_timestamp_rolls_over_instead_of_emitting_second_60():
    """실수 포맷은 59.999 를 "0:00:60.00" 으로 만든다 — libass 가 거부하는 값이다."""
    from src.overlay import _ts
    assert _ts(0) == "0:00:00.00"
    assert _ts(15.0) == "0:00:15.00"
    assert _ts(88.0) == "0:01:28.00"
    assert _ts(59.999) == "0:01:00.00"
    assert _ts(3599.999) == "1:00:00.00"


def test_every_dialogue_field_count_matches_its_format_header():
    """Format 헤더와 필드 수가 어긋나면 libass 가 조용히 값을 밀어 넣는다."""
    a = _ass()
    styles_fmt = next(l for l in a.splitlines() if l.startswith("Format:") and "Fontname" in l)
    events_fmt = next(l for l in a.splitlines() if l.startswith("Format:") and "Layer" in l)
    n_style = len(styles_fmt.split(":", 1)[1].split(","))
    n_event = len(events_fmt.split(":", 1)[1].split(","))
    for line in a.splitlines():
        if line.startswith("Style:"):
            assert len(line.split(":", 1)[1].split(",")) == n_style
        if line.startswith("Dialogue:"):
            # Text 는 마지막 필드이고 콤마를 포함할 수 있으므로 앞의 n-1개만 센다
            assert len(line.split(":", 1)[1].split(",", n_event - 1)) == n_event


def test_title_card_is_uppercased_regardless_of_brief_casing():
    b = load_brief("brief/hanbit.yaml")
    b.name_en = "Hanbit Materials"
    bs = build_beatsheet(build_script(b))
    a = build_ass(bs, build_shotlist(bs, b), b)
    assert "HANBIT MATERIALS" in a
    assert "Hanbit Materials" not in a


def test_narration_cues_follow_measured_audio_not_reserved_beat_length():
    """자막은 막의 예약 길이가 아니라 실제 음성 길이 안에 깔려야 한다.

    막의 예약 길이에는 안전 여유가 들어 있다(실측 0.34~2.60초). 예약 길이로
    깔면 음성이 끝난 뒤에도 자막이 남아 막마다 그만큼 밀린다 — 실제 실행에서
    사용자가 "자막과 말의 싱크가 안 맞는다"고 지적한 원인이다.

    audio_seconds 를 무시하도록 되돌리면 이 테스트가 실패한다.
    """
    b = load_brief("brief/hanbit.yaml")
    bs = build_beatsheet(build_script(b))
    sl = build_shotlist(bs, b)

    narrated = [x for x in bs.beats if x.narrated and x.lines]
    # 실제 음성이 예약 길이의 70% 라고 가정
    audio = {x.name: x.seconds * 0.7 for x in narrated}

    cues = _narration_cues(build_ass(bs, sl, b, audio_seconds=audio))
    for beat in narrated:
        own = [c for c in cues
               if beat.start - 0.01 <= c[0] < beat.start + beat.seconds - 0.01]
        assert own, f"{beat.name}: 나레이션 자막이 없다"
        assert own[0][0] == pytest.approx(beat.start, abs=0.02)
        if beat.name == "closing":
            # 클로징 마지막 줄(사명)은 하단 자막이 아니라 로고 카드가 받는다 —
            # 같은 글자를 한 화면에 두 번 띄우지 않기 위해서다. 그래서 나레이션
            # 자막은 그 줄이 시작하기 전에 끝난다.
            assert own[-1][1] < beat.start + audio[beat.name] - 0.01
            continue
        assert own[-1][1] == pytest.approx(beat.start + audio[beat.name], abs=0.02), \
            f"{beat.name}: 자막이 실제 음성 종료 시각에 끝나야 한다"


def test_narration_cues_fall_back_to_beat_length_without_measurements():
    """음성 길이를 모르면(세그먼트가 아직 없으면) 예약 길이에 분배한다."""
    b = load_brief("brief/hanbit.yaml")
    bs = build_beatsheet(build_script(b))
    sl = build_shotlist(bs, b)
    beat = next(x for x in bs.beats if x.narrated and x.lines)

    cues = _narration_cues(build_ass(bs, sl, b))
    own = [c for c in cues
           if beat.start - 0.01 <= c[0] < beat.start + beat.seconds - 0.01]
    assert own[-1][1] == pytest.approx(beat.start + beat.seconds, abs=0.02)


def test_audio_seconds_longer_than_beat_is_clamped():
    """음성이 예약 길이보다 길게 보고돼도 자막이 다음 막을 침범하면 안 된다."""
    b = load_brief("brief/hanbit.yaml")
    bs = build_beatsheet(build_script(b))
    sl = build_shotlist(bs, b)
    beat = next(x for x in bs.beats if x.narrated and x.lines)

    cues = _narration_cues(build_ass(bs, sl, b,
                                     audio_seconds={beat.name: beat.seconds + 5.0}))
    own = [c for c in cues
           if beat.start - 0.01 <= c[0] < beat.start + beat.seconds - 0.01]
    assert own[-1][1] <= beat.start + beat.seconds + 0.02


def _style_cues(ass: str, style: str) -> list[tuple[float, float, str]]:
    def sec(v):
        h, m, s = v.split(":")
        return int(h) * 3600 + int(m) * 60 + float(s)
    return [(sec(a), sec(bb), c) for a, bb, c in re.findall(
        rf"Dialogue: 0,([\d:.]+),([\d:.]+),{style},,0,0,0,,(.+)", ass)]


def test_logo_card_appears_when_the_company_name_is_spoken_and_holds_to_the_end():
    """레퍼런스 28편의 클로징 문법은 슬로건 → 사명 → 로고다.

    로고는 사명이 발화되는 순간 떠서 끝까지 홀드해야 한다. 나레이션이 다 끝난
    뒤에 띄우면 홀드가 1초 남짓밖에 안 남아 스치듯 지나간다(실측).
    """
    b = load_brief("brief/hanbit.yaml")
    bs = build_beatsheet(build_script(b))
    sl = build_shotlist(bs, b)
    closing = next(x for x in bs.beats if x.name == "closing")
    audio = {closing.name: closing.seconds * 0.75}

    ass = build_ass(bs, sl, b, audio_seconds=audio)
    for style, text in (("LogoKo", b.name_ko),
                        ("LogoEn", b.name_en.upper()),
                        ("LogoRule", b.slogan_en.upper())):
        cues = _style_cues(ass, style)
        assert len(cues) == 1, f"{style}: 로고 줄이 정확히 하나여야 한다"
        start, end, body = cues[0]
        assert text in body
        assert end == pytest.approx(C.TOTAL_SECONDS, abs=0.02), \
            f"{style}: 로고는 완성본 끝까지 홀드한다"
        assert start < closing.start + audio[closing.name], \
            f"{style}: 로고는 사명이 발화되는 동안 떠야 한다"
        assert end - start >= 1.2, f"{style}: 스치듯 지나가는 로고는 의미가 없다"

    assert len(_style_cues(ass, "LogoScrim")) == 1, \
        "배경이 무엇이든 사명이 읽히도록 어두운 막을 깐다"


def test_company_name_is_not_shown_twice_at_the_end():
    """로고가 사명을 화면 중앙에 세우는 동안 하단 자막에도 같은 글자가 나오면
    한 화면에 같은 말이 두 번 나온다 — 마지막 줄은 로고에 맡긴다."""
    b = load_brief("brief/hanbit.yaml")
    bs = build_beatsheet(build_script(b))
    sl = build_shotlist(bs, b)
    closing = next(x for x in bs.beats if x.name == "closing")

    ass = build_ass(bs, sl, b, audio_seconds={closing.name: closing.seconds * 0.75})
    narration = [c[2] for c in _narration_cues(ass)]
    assert closing.lines[-1] not in narration, \
        "클로징 마지막 줄은 하단 자막으로 중복 표시되면 안 된다"
    # 그 앞줄(슬로건)은 그대로 자막으로 나온다
    assert closing.lines[0] in narration
