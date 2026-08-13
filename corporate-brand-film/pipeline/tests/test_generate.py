from pathlib import Path
import pytest

from src.brief import load_brief
from src.script import build_script
from src.beats import build_beatsheet
from src.shotlist import build_shotlist
from src.generate import generate_clips
from src.providers.base import (AmbiguousSubmissionError, BlockedError,
                                BudgetExceededError, FakeVideoProvider,
                                TerminalGenerationError)
from src import constants as C


def _sl():
    b = load_brief("brief/hanbit.yaml")
    return build_shotlist(build_beatsheet(build_script(b)), b), b


def test_generates_one_clip_per_shot(tmp_path):
    sl, b = _sl()
    r = generate_clips(sl, b, FakeVideoProvider(), tmp_path)
    assert len(r.made) == C.TOTAL_CUTS
    assert sorted(p.name for p in tmp_path.glob("*.mp4"))[0] == "01.mp4"


def test_skips_clips_that_already_exist(tmp_path):
    sl, b = _sl()
    generate_clips(sl, b, FakeVideoProvider(), tmp_path)
    p = FakeVideoProvider()
    r = generate_clips(sl, b, p, tmp_path)
    assert r.made == [] and len(r.skipped) == C.TOTAL_CUTS
    assert p.calls == []


def test_blocked_prompt_is_recorded_and_pipeline_continues(tmp_path):
    sl, b = _sl()
    blocker = FakeVideoProvider(block_prompts=["hydrogen powered truck"])
    r = generate_clips(sl, b, blocker, tmp_path)
    assert len(r.blocked) == 1
    assert len(r.made) == C.TOTAL_CUTS - 1


class _FlakyProvider(FakeVideoProvider):
    def __init__(self):
        super().__init__()
        self.failures = 0

    def generate(self, prompt, out_path, duration, resolution, aspect, ref):
        if self.failures < 1:
            self.failures += 1
            raise RuntimeError("transient")
        super().generate(prompt, out_path, duration, resolution, aspect, ref)


def test_transient_error_is_retried(tmp_path):
    sl, b = _sl()
    r = generate_clips(sl, b, _FlakyProvider(), tmp_path)
    assert len(r.made) == C.TOTAL_CUTS


class _DiesMidWriteProvider(FakeVideoProvider):
    """첫 샷을 쓰다가 죽는 provider — 잘린 파일을 남긴다."""

    def generate(self, prompt, out_path, duration, resolution, aspect, ref):
        out_path.parent.mkdir(parents=True, exist_ok=True)
        out_path.write_bytes(b"\x00\x00")   # 잘린 mp4
        raise RuntimeError("killed mid-write")


def test_truncated_write_does_not_survive_as_a_skippable_clip(tmp_path):
    """중간에 죽어 잘린 파일이 최종 이름을 차지하면, 다음 실행이 그걸 건너뛰어
    깨진 클립이 최종 조립까지 흘러간다. .tmp 디렉토리 사용으로 막아야 한다."""
    sl, b = _sl()
    with pytest.raises(RuntimeError):
        generate_clips(sl, b, _DiesMidWriteProvider(), tmp_path, max_retries=0)
    assert not (tmp_path / "01.mp4").exists()
    tmp_dir = tmp_path / ".tmp"
    if tmp_dir.exists():
        assert list(tmp_dir.glob("*.mp4")) == []


def test_report_is_written_even_when_the_run_aborts(tmp_path):
    import json
    sl, b = _sl()
    with pytest.raises(RuntimeError):
        generate_clips(sl, b, _DiesMidWriteProvider(), tmp_path, max_retries=0)
    report = json.loads((tmp_path / "_report.json").read_text(encoding="utf-8"))
    assert report == {"made": [], "skipped": [], "blocked": [], "ambiguous": [],
                      "terminal": [], "rejected": []}


def test_report_records_made_skipped_and_blocked(tmp_path):
    import json
    sl, b = _sl()
    generate_clips(sl, b, FakeVideoProvider(block_prompts=["hydrogen powered truck"]), tmp_path)
    report = json.loads((tmp_path / "_report.json").read_text(encoding="utf-8"))
    assert len(report["made"]) == C.TOTAL_CUTS - 1
    assert len(report["blocked"]) == 1
    assert report["skipped"] == []
    assert report["ambiguous"] == []
    assert report["terminal"] == []


class _AmbiguousProvider(FakeVideoProvider):
    """샷 #1 제출에서 서버 도달 여부를 알 수 없는 오류를 낸다.

    calls_made는 모든 샷에 걸친 전체 호출 횟수라 "재시도 안 함"을 검증하는
    데 못 쓴다(33개 샷 각각 최소 한 번씩은 불리므로 33이 되는 게 정상) —
    샷 #1만 따로 세야 "재시도했다면 이 값이 1보다 컸을 것"이라는 주장이
    성립한다.
    """

    def __init__(self):
        super().__init__()
        self.shot1_calls = 0

    def generate(self, prompt, out_path, duration, resolution, aspect, ref):
        if out_path.name == "01.mp4":
            self.shot1_calls += 1
            raise AmbiguousSubmissionError("제출이 서버에 도달했는지 알 수 없습니다")
        super().generate(prompt, out_path, duration, resolution, aspect, ref)


def test_ambiguous_submission_is_recorded_and_not_retried(tmp_path):
    """AmbiguousSubmissionError는 이미 과금됐을 수 있으므로 절대 재시도하면
    안 된다 — 재시도하면 진짜 이중 과금이 날 수 있다. blocked와도 절대
    합치지 않는다: 차단은 과금되지 않지만 애매한 제출은 과금됐을 수 있어서
    의미가 정반대다."""
    sl, b = _sl()
    p = _AmbiguousProvider()
    r = generate_clips(sl, b, p, tmp_path, max_retries=2)
    assert r.ambiguous == [1]
    assert r.blocked == []
    # 샷 #1은 딱 한 번만 불렸다 — 재시도했다면 max_retries+1=3이 됐을 것이다.
    assert p.shot1_calls == 1
    assert len(r.made) == C.TOTAL_CUTS - 1


def test_report_records_ambiguous_separately_from_blocked(tmp_path):
    import json
    sl, b = _sl()
    generate_clips(sl, b, _AmbiguousProvider(), tmp_path)
    report = json.loads((tmp_path / "_report.json").read_text(encoding="utf-8"))
    assert report["ambiguous"] == [1]
    assert report["blocked"] == []


class _TerminalFailureProvider(FakeVideoProvider):
    """샷 #1에서 provider가 이미 과금된 종결 실패(예: bizrouter status=failed,
    Gemini operation.error 둘 다 이제 이걸 던진다)를 냈다고 가정한다.

    shot1_calls는 "몇 번 제출(=몇 번 과금 위험)했는지"를 직접 센다 — Critical
    1 리뷰가 지적한 버그는 이 provider의 실패가 pending을 이미 정리한
    "종결" 상태인데도 generate_clips가 평범한 Exception으로 오인해 최대
    max_retries번 재시도하며 매번 provider.generate()를(=실제 provider라면
    매번 새 POST를) 다시 부르는 것이었다.
    """

    def __init__(self):
        super().__init__()
        self.shot1_calls = 0

    def generate(self, prompt, out_path, duration, resolution, aspect, ref):
        if out_path.name == "01.mp4":
            self.shot1_calls += 1
            raise TerminalGenerationError(
                "provider가 종결된 실패를 보고했습니다(이미 과금됨)")
        super().generate(prompt, out_path, duration, resolution, aspect, ref)


def test_terminal_failure_is_recorded_and_not_retried_no_double_charge(tmp_path):
    """Critical 1(Task 15 리뷰) 회귀 테스트: 이미 과금된 종결 실패
    (TerminalGenerationError)를 일반 Exception으로 분류하면 generate_clips가
    최대 max_retries+1번까지 재시도해서 매번 새 POST(=새 과금)를 낸다.
    BlockedError/AmbiguousSubmissionError처럼 재시도 없이 한 번만 시도돼야
    한다 — 이 테스트는 provider를 단독으로 부르지 않고 generate_clips() 전체
    파이프라인을 구동해서 "제출(=provider.generate 호출)이 정확히 한 번만
    일어났는지"를 확인한다. 기존 provider 단위 테스트들은 provider를 격리해서
    호출했기 때문에 이 재시도 버그를 잡지 못했다."""
    sl, b = _sl()
    p = _TerminalFailureProvider()
    r = generate_clips(sl, b, p, tmp_path, max_retries=2)
    assert r.terminal == [1]
    assert r.blocked == []
    assert r.ambiguous == []
    # 샷 #1은 딱 한 번만 불렸다(=POST 1회) — 재시도했다면 max_retries+1=3이 됐을 것이다.
    assert p.shot1_calls == 1
    assert len(r.made) == C.TOTAL_CUTS - 1


def test_report_records_terminal_separately_from_blocked_and_ambiguous(tmp_path):
    """blocked=과금 안 됨, ambiguous=과금 여부 불명, terminal=확실히 과금됨 —
    세 의미가 다르므로 리포트에서 절대 섞이면 안 된다."""
    import json
    sl, b = _sl()
    generate_clips(sl, b, _TerminalFailureProvider(), tmp_path)
    report = json.loads((tmp_path / "_report.json").read_text(encoding="utf-8"))
    assert report["terminal"] == [1]
    assert report["blocked"] == []
    assert report["ambiguous"] == []


class _BudgetExceededProvider(FakeVideoProvider):
    """샷 #1에서 예산 가드가 트립됐다고 가정한다 — 로컬 계산(제출 전)이라
    재시도해도 매번 같은 결과만 나온다."""

    def __init__(self):
        super().__init__()
        self.shot1_calls = 0

    def generate(self, prompt, out_path, duration, resolution, aspect, ref):
        if out_path.name == "01.mp4":
            self.shot1_calls += 1
            raise BudgetExceededError("예산 초과(테스트)")
        super().generate(prompt, out_path, duration, resolution, aspect, ref)


def test_budget_exceeded_is_not_retried_and_halts_the_whole_run(tmp_path):
    """Minor 7(Task 15 리뷰): BudgetExceededError는 로컬 계산(제출 전)이라
    재시도해도 같은 결과만 나온다 — 재시도는 시도만 낭비하고 리포트만 흐리게
    만든다. 게다가 예산은 이번 실행 전체에 적용되는 상한이므로 이 샷만
    건너뛰고 다음 샷으로 넘어가면 안 된다(다음 샷들도 결국 같은 이유로 막힐
    것이므로) — 즉시 전체 실행을 중단해야 한다."""
    sl, b = _sl()
    p = _BudgetExceededProvider()
    with pytest.raises(BudgetExceededError):
        generate_clips(sl, b, p, tmp_path, max_retries=2)
    # 샷 #1은 딱 한 번만 불렸다 — 재시도했다면 max_retries+1=3이 됐을 것이다.
    assert p.shot1_calls == 1
    # 전체 실행이 즉시 중단됐으므로 샷 #2 이후는 시도조차 되지 않았다.
    assert p.calls == []


class _ProviderWithSpend(FakeVideoProvider):
    """bizrouter처럼 실제 청구액을 추적하는 provider를 흉내낸다(HTTP 없이)."""

    def __init__(self, spent):
        super().__init__()
        self.spent_krw = spent


def test_report_includes_spent_krw_when_provider_exposes_it(tmp_path):
    """Important 2(Task 15 리뷰): provider가 spent_krw를 노출하면(bizrouter처럼)
    _report.json에 반영돼야 한다 — 그전엔 누적 청구액이 provider 내부
    (pending.json)에만 있어서 예산 안에서 끝난 실행도 사용자가 실제로 얼마를
    냈는지 볼 방법이 없었다."""
    import json
    sl, b = _sl()
    generate_clips(sl, b, _ProviderWithSpend(12160), tmp_path)
    report = json.loads((tmp_path / "_report.json").read_text(encoding="utf-8"))
    assert report["spent_krw"] == 12160


def test_report_omits_spent_krw_when_provider_does_not_track_cost(tmp_path):
    """Fake/Gemini처럼 원화 청구액 개념이 없는 provider는 spent_krw 속성이
    없다 — duck typing이라 조용히 생략돼야 한다(없는 값을 0으로 꾸며 보여주면
    "0원 썼다"는 거짓 정보가 된다)."""
    import json
    sl, b = _sl()
    generate_clips(sl, b, FakeVideoProvider(), tmp_path)
    report = json.loads((tmp_path / "_report.json").read_text(encoding="utf-8"))
    assert "spent_krw" not in report
