import pytest
from src.brief import load_brief
from src.script import build_script
from src.beats import build_beatsheet
from src.shotlist import build_shotlist, SIZES
from src import constants as C


def _shots():
    b = load_brief("brief/hanbit.yaml")
    return build_shotlist(build_beatsheet(build_script(b)), b).shots


def test_shot_count_matches_format():
    assert len(_shots()) == C.TOTAL_CUTS


def test_indexes_are_sequential_from_one():
    assert [s.index for s in _shots()] == list(range(1, C.TOTAL_CUTS + 1))


def test_starts_are_contiguous_and_end_at_88():
    shots = _shots()
    t = 0.0
    for s in shots:
        assert s.start == pytest.approx(t)
        t += s.seconds
    assert t == pytest.approx(C.TOTAL_SECONDS)


def test_all_sizes_are_valid():
    assert all(s.size in SIZES for s in _shots())


def test_no_two_adjacent_shots_share_a_size():
    shots = _shots()
    for a, b in zip(shots, shots[1:]):
        assert a.size != b.size, f"{a.index}-{b.index} 같은 샷사이즈 연속"


def test_each_beat_opens_on_a_wide():
    shots = _shots()
    seen = set()
    for s in shots:
        if s.beat not in seen:
            seen.add(s.beat)
            if s.beat not in ("title", "closing"):
                assert s.size == "aerial_wide", f"{s.beat} 첫 컷은 와이드여야 한다"


def test_chapter_shots_carry_a_two_tier_label():
    labeled = [s for s in _shots() if s.beat.startswith("chapter_") and s.label_ko]
    assert len(labeled) == 2, "챕터당 라벨 1개"
    for s in labeled:
        assert s.label_en and s.label_en != s.label_ko


def test_every_shot_has_a_nonempty_subject():
    assert all(s.subject.strip() for s in _shots())


def test_raises_when_a_single_cut_beat_would_collide():
    """막 첫 컷은 무조건 와이드다. 컷이 1개뿐인 막이 생기면 그 막의 유일한 컷과
    다음 막의 첫 컷이 둘 다 와이드가 되어 인접 중복이 발생한다 — 즉시 터져야 한다."""
    b = load_brief("brief/hanbit.yaml")
    bs = build_beatsheet(build_script(b))
    for beat in bs.beats:          # pivot(2컷) → 1컷으로 줄여 pivot/chapter_0 경계에 충돌 유발
        if beat.name == "pivot":
            beat.cuts = 1
    with pytest.raises(ValueError, match="인접 샷 사이즈 중복"):
        build_shotlist(bs, b)
