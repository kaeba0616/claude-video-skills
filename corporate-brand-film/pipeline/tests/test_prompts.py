import pytest
from src.brief import load_brief
from src.script import build_script
from src.beats import build_beatsheet
from src.shotlist import build_shotlist
from src.prompts import build_prompt, build_all
from src import constants as C


def _ctx():
    b = load_brief("brief/hanbit.yaml")
    sl = build_shotlist(build_beatsheet(build_script(b)), b)
    return b, sl


def test_no_prompt_contains_negative_language():
    """부정어를 긍정 프롬프트에 섞으면 오히려 그 요소를 불러오는 역효과가
    있다고 보고돼 있다 — negative_prompt는 이제 provider가 전용 필드로
    따로 넘긴다(prompts.py 상단 docstring 참고). 여기서는 build_prompt()의
    출력에 부정어 단서가 전혀 남아있지 않은지만 확인한다."""
    b, sl = _ctx()
    for p in build_all(sl, b).values():
        assert "no " not in p.lower()
        assert "subtitle" not in p.lower()
        assert "caption" not in p.lower()
        assert "logo" not in p.lower()


def test_every_prompt_starts_with_the_full_style_prefix():
    b, sl = _ctx()
    prefix = " ".join(b.style_prefix.split())
    for p in build_all(sl, b).values():
        assert p.startswith(prefix)


def test_prompt_is_ascii_only():
    b, sl = _ctx()
    for i, p in build_all(sl, b).items():
        assert p.isascii(), f"샷 {i}에 비ASCII 문자: Veo는 영어만 지원"


def test_prompt_contains_shot_size_phrase():
    b, sl = _ctx()
    p = build_prompt(sl.shots[0], b)
    assert "aerial" in p.lower()


def test_all_returns_one_prompt_per_shot():
    b, sl = _ctx()
    assert sorted(build_all(sl, b)) == list(range(1, C.TOTAL_CUTS + 1))


def test_korean_subject_raises_before_any_generation():
    b, sl = _ctx()
    sl.shots[0].subject = "거대한 공장 전경"
    with pytest.raises(ValueError, match="비ASCII"):
        build_prompt(sl.shots[0], b)


def test_korean_style_prefix_raises():
    b, sl = _ctx()
    b.style_prefix = "시네마틱 기업 브랜드 필름"
    with pytest.raises(ValueError, match="비ASCII"):
        build_prompt(sl.shots[0], b)


def test_style_prefix_does_not_ask_veo_to_render_film_stock():
    """스타일 접두사에 "35mm" 를 넣으면 Veo 가 "필름 그레인 질감"이 아니라
    "35mm 필름을 보여줘"로 해석한다 — Task 18 실측에서 33컷 중 10컷에
    퍼포레이션 구멍과 필름 엣지 마킹(-35, 35.MM, Foutt)이 화면에 구워져
    7,878원을 들여 재생성했다. 한글 타이포는 전부 오버레이로 얹으므로
    클립은 글자 없는 깨끗한 판이어야 한다."""
    prefix = load_brief("brief/hanbit.yaml").style_prefix.lower()
    for bad in ("35mm", "16mm", "8mm", "film strip", "filmstrip", "film burn"):
        assert bad not in prefix, (
            f"style_prefix 에 {bad!r} 가 있으면 Veo 가 필름 자체를 그린다")


def test_generated_subjects_do_not_request_objects_covered_in_text():
    """negative_prompt 로 "글자 없음"을 요구하면서 피사체로 글자가 적힌 물건을
    지정하면 모순된 지시가 되고, Veo 는 피사체를 따른다 — Task 18 에서
    "rolling machine nameplate"(명판)가 화면 한가운데 ROLLING MACHINE 간판을
    만들어냈다."""
    from src.shotlist import GENERIC
    banned = ("nameplate", "certificate", "scale bar", "signage", "sign board",
              "label", "poster", "banner", "text", "lettering", "logo")
    for beat, subjects in GENERIC.items():
        for s in subjects:
            low = s.lower()
            for bad in banned:
                assert bad not in low, (
                    f"GENERIC[{beat!r}] 의 피사체가 글자를 부른다: {bad!r} in {s!r}")


def test_negative_prompt_covers_the_defects_found_in_production():
    """실제 실행에서 실제로 나온 것들을 부정 프롬프트가 명시적으로 막는가."""
    neg = C.VEO_NEGATIVE_PROMPT.lower()
    for must in ("text", "subtitles", "captions", "logos",
                 "film strip", "sprocket", "perforation", "lettering", "signage"):
        assert must in neg, f"negative_prompt 에 {must!r} 가 빠졌다"
