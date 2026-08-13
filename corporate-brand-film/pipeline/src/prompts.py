"""Shot → Veo 3.1 영문 프롬프트.

구조: [스타일 프리픽스] + [샷사이즈 구문] + [피사체] + [카메라 무브]
한글은 절대 넣지 않는다 (Veo는 영어만 완전 지원). 화면 텍스트도 금지 — ffmpeg가 넣는다.

부정어(negative)는 이 긍정 프롬프트에 섞지 않는다. "no subtitles" 같은 문구를
긍정 프롬프트 끝에 붙이면 오히려 그 요소를 화면에 불러오는 역효과가 보고돼
있다 — 부정어를 언급하는 텍스트 자체가 그 개념의 임베딩을 활성화시키기
때문이다. Gemini(Veo SDK)와 bizrouter 둘 다 negative_prompt 전용 필드를
받으므로, C.VEO_NEGATIVE_PROMPT는 각 provider가 생성 요청 시 그 필드로
직접 전달한다(src/providers/gemini.py, src/providers/bizrouter.py 참고).
"""
from src.brief import Brief
from src.shotlist import Shot, Shotlist

SIZE_PHRASE = {
    "aerial_wide": "extreme wide aerial establishing shot",
    "extreme_close": "extreme macro close-up shot",
    "person_medium": "medium shot of a person at work",
    "cg_diagram": "clean 3D motion graphics visualization on a dark background",
    "studio_product": "white cyclorama studio product shot",
}
SIZE_MOVE = {
    "aerial_wide": "slow drone push forward, steady",
    "extreme_close": "slow rack focus, minimal movement",
    "person_medium": "slow dolly in, handheld micro-movement",
    "cg_diagram": "smooth orbit around the subject",
    "studio_product": "slow turntable rotation, locked-off camera",
}


def _require_ascii(value: str, field: str, shot: Shot) -> str:
    """Veo는 영어만 완전 지원한다. 한글이 섞인 brief 로 클립 33개를 생성하면
    돈만 쓰고 못 쓰는 결과가 나오므로, 생성 전에 여기서 터뜨린다."""
    if not value.isascii():
        bad = "".join(sorted({c for c in value if not c.isascii()}))
        raise ValueError(
            f"Veo 프롬프트에 비ASCII 문자가 섞였습니다 (샷 #{shot.index}, {field}): {bad!r}. "
            f"brief 의 style_prefix 와 subjects 는 영어로만 작성해야 합니다."
        )
    return value


def build_prompt(shot: Shot, brief: Brief) -> str:
    parts = [
        _require_ascii(" ".join(brief.style_prefix.split()), "style_prefix", shot),
        SIZE_PHRASE[shot.size],
        _require_ascii(shot.subject, "subject", shot),
        SIZE_MOVE[shot.size],
    ]
    return ". ".join(p.rstrip(". ") for p in parts) + "."


def build_all(shotlist: Shotlist, brief: Brief) -> dict[int, str]:
    return {s.index: build_prompt(s, brief) for s in shotlist.shots}
