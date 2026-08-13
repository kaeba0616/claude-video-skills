from src.brief import load_brief
from src.script import build_script, Act


def test_act_order_and_names():
    s = build_script(load_brief("brief/hanbit.yaml"))
    assert [a.name for a in s.acts] == [
        "cold_open", "title", "definition", "evidence", "pivot",
        "chapter_0", "chapter_1", "climax", "closing",
    ]


def test_cold_open_and_title_are_silent():
    s = build_script(load_brief("brief/hanbit.yaml"))
    silent = [a for a in s.acts if not a.narrated]
    assert [a.name for a in silent] == ["cold_open", "title"]
    assert all(a.lines == [] for a in silent)


def test_evidence_uses_number_anchor_template():
    s = build_script(load_brief("brief/hanbit.yaml"))
    ev = next(a for a in s.acts if a.name == "evidence")
    assert ev.lines[0] == "1981년 창원의 압연기 한 대에서 시작해, 45년간 단 하나의 기준을 지켰습니다."
    assert ev.lines[1] == "8마이크로미터. 1,200회의 검사."


def test_pivot_is_self_negation_plus_question():
    s = build_script(load_brief("brief/hanbit.yaml"))
    pv = next(a for a in s.acts if a.name == "pivot")
    assert pv.lines == ["하지만 우리는 멈추지 않았습니다.", "더 얇아지는 것이 전부일까?"]


def test_closing_is_slogan_then_name():
    s = build_script(load_brief("brief/hanbit.yaml"))
    cl = next(a for a in s.acts if a.name == "closing")
    assert cl.lines == ["보이지 않는 것이 만드는 차이.", "한빛소재."]


def test_total_chars_excludes_whitespace_and_punctuation():
    s = build_script(load_brief("brief/hanbit.yaml"))
    assert 230 <= s.total_chars <= 275
