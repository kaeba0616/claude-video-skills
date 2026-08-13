import pytest
from src.brief import load_brief
from src.script import build_script
from src.beats import build_beatsheet
from src import constants as C


def _sheet():
    return build_beatsheet(build_script(load_brief("brief/hanbit.yaml")))


def test_total_is_exactly_88_seconds():
    assert _sheet().total_seconds == pytest.approx(C.TOTAL_SECONDS)


def test_total_is_exactly_33_cuts():
    assert _sheet().total_cuts == C.TOTAL_CUTS


def test_starts_are_contiguous():
    bs = _sheet()
    t = 0.0
    for b in bs.beats:
        assert b.start == pytest.approx(t)
        t += b.seconds


def test_silent_ratio_within_reference_band():
    lo, hi = C.PASS_SILENT_RATIO
    assert lo <= _sheet().silent_ratio <= hi


def test_cold_open_is_slowest_and_chapters_are_fastest():
    bs = _sheet()
    by = {b.name: b.seconds / b.cuts for b in bs.beats}
    assert by["cold_open"] > by["chapter_1"], "느리게 열고 → 조여야 한다"


def test_every_narrated_beat_has_positive_chars():
    for b in _sheet().beats:
        if b.narrated:
            assert b.chars > 0


def test_raises_when_script_too_long_for_format():
    s = build_script(load_brief("brief/hanbit.yaml"))
    s.acts[2].lines = ["아주 긴 문장입니다. " * 60]
    with pytest.raises(ValueError, match="대본이 포맷보다 깁니다"):
        build_beatsheet(s)


def test_raises_on_unsupported_chapter_count():
    from src.script import Act
    s = build_script(load_brief("brief/hanbit.yaml"))
    s.acts.insert(-2, Act("chapter_2", ["추가 챕터입니다."], True))
    with pytest.raises(ValueError, match="컷 배분이 정의되지 않은 막"):
        build_beatsheet(s)
