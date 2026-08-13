import pytest
from src.brief import load_brief, Brief, Chapter


def test_load_hanbit_brief():
    b = load_brief("brief/hanbit.yaml")
    assert isinstance(b, Brief)
    assert b.name_ko == "한빛소재"
    assert b.name_en == "HANBIT MATERIALS"
    assert b.founded_year == 1981
    assert b.founded_place == "창원"
    assert b.slogan_ko == "보이지 않는 것이 만드는 차이"
    assert len(b.anchors) == 2
    assert len(b.chapters) == 2
    assert all(isinstance(c, Chapter) for c in b.chapters)


def test_chapter_has_subjects_for_every_shot():
    b = load_brief("brief/hanbit.yaml")
    for c in b.chapters:
        assert len(c.subjects) >= 5, f"{c.title_ko}: 챕터당 최소 5개 피사체 필요"


def test_cold_open_has_four_subjects():
    b = load_brief("brief/hanbit.yaml")
    assert len(b.cold_open_subjects) == 4


def test_missing_required_field_raises(tmp_path):
    p = tmp_path / "bad.yaml"
    p.write_text("name_ko: 테스트\n", encoding="utf-8")
    with pytest.raises(ValueError, match="필수 필드 누락"):
        load_brief(str(p))
