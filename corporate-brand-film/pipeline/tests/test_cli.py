import json

import pytest
from src.cli import main, _providers
from src.generate import GenerateResult
from src.providers.base import FakeTTSProvider, FakeVideoProvider
from src.verify import Metrics
from src import constants as C


def test_script_subcommand_writes_scenario(tmp_path):
    out = tmp_path / "out"
    assert main(["script", "--brief", "brief/hanbit.yaml", "--out", str(out)]) == 0
    assert (out / "scenario.md").exists()
    assert "하지만 우리는 멈추지 않았습니다." in (out / "scenario.md").read_text(encoding="utf-8")


def test_shots_subcommand_writes_json_with_33_entries(tmp_path):
    out = tmp_path / "out"
    main(["shots", "--brief", "brief/hanbit.yaml", "--out", str(out)])
    data = json.loads((out / "shotlist.json").read_text(encoding="utf-8"))
    assert len(data) == C.TOTAL_CUTS


def test_prompts_subcommand_writes_one_file_per_shot(tmp_path):
    out = tmp_path / "out"
    main(["prompts", "--brief", "brief/hanbit.yaml", "--out", str(out)])
    assert len(list((out / "prompts").glob("*.txt"))) == C.TOTAL_CUTS


def test_all_with_fake_produces_final_mp4(tmp_path):
    out = tmp_path / "out"
    assert main(["all", "--brief", "brief/hanbit.yaml", "--out", str(out), "--fake"]) == 0
    assert (out / "final.mp4").exists()


def test_live_flag_required_for_real_api(tmp_path, monkeypatch):
    monkeypatch.delenv("GEMINI_API_KEY", raising=False)
    with pytest.raises(SystemExit):
        main(["generate", "--brief", "brief/hanbit.yaml", "--out", str(tmp_path), "--live"])


# --- 과금 게이트 안전성 ---

def test_fake_and_live_together_rejected(tmp_path):
    """--fake 와 --live 를 동시에 주면 argparse 상호배타 그룹이 즉시 거부해야 한다."""
    with pytest.raises(SystemExit):
        main(["generate", "--brief", "brief/hanbit.yaml", "--out", str(tmp_path),
              "--fake", "--live"])


def test_providers_default_live_false_returns_fake_instances():
    """_providers(live=False) 는 항상 Fake 인스턴스만 돌려준다 — 타입 자체를 확인한다."""
    vprov, tprov = _providers(live=False)
    assert isinstance(vprov, FakeVideoProvider)
    assert isinstance(tprov, FakeTTSProvider)


def test_no_flag_invocation_constructs_no_real_provider(tmp_path, monkeypatch):
    """플래그를 아예 안 주는 기본 호출도 실제 파이프라인 호출부(generate_clips)에
    Fake 프로바이더 '인스턴스'가 들어가는지 확인한다 — stdout 문자열이 아니라
    실제로 전달된 객체의 타입을 검사한다.
    """
    captured = {}
    import src.cli as cli
    real_generate_clips = cli.generate_clips

    def spy(sl, b, provider, out_dir, *a, **kw):
        captured["provider"] = provider
        return real_generate_clips(sl, b, provider, out_dir, *a, **kw)

    monkeypatch.setattr(cli, "generate_clips", spy)
    out = tmp_path / "out"
    assert main(["generate", "--brief", "brief/hanbit.yaml", "--out", str(out)]) == 0
    assert isinstance(captured["provider"], FakeVideoProvider)


def test_live_requires_key_even_when_stage_is_all(tmp_path, monkeypatch):
    """all 스테이지에서도 --live 는 GEMINI_API_KEY 없이는 절대 통과하면 안 된다."""
    monkeypatch.delenv("GEMINI_API_KEY", raising=False)
    with pytest.raises(SystemExit):
        main(["all", "--brief", "brief/hanbit.yaml", "--out", str(tmp_path), "--live"])


# --- 검증(verify) ---

def test_verify_reports_every_metric_and_gates_exit_code(tmp_path, capsys, monkeypatch):
    """verify 는 계산된 지표를 모두 출력하고, 하나라도 FAIL 이면 0 이 아닌
    코드로 종료해야 한다.

    실패를 만들기 위해 대역 하나를 좁혀서 강제한다 — 예전에는 Fake 파이프라인의
    silent_ratio 가 대역 밖이라는 "알려진 사실"에 기대어 실패를 얻었는데,
    대역을 실측 근거로 바로잡자(Task 18 이후 재측정) 그 사실이 사라져 테스트가
    깨졌다. 사실이 아니라 동작을 검증해야 한다.
    """
    out = tmp_path / "out"
    assert main(["all", "--brief", "brief/hanbit.yaml", "--out", str(out), "--fake"]) == 0

    code = main(["verify", "--brief", "brief/hanbit.yaml", "--out", str(out)])
    text = capsys.readouterr().out
    for metric in ("cuts_per_min", "avg_shot", "speech_rate", "silent_ratio"):
        assert metric in text
    assert code == 0, "정상 완성본은 모든 지표가 대역 안이어야 한다"

    # 이제 대역 하나를 통과 불가능하게 좁혀 게이트가 실제로 물리는지 본다.
    monkeypatch.setattr(C, "PASS_CUTS_PER_MIN", (999.0, 1000.0))
    code = main(["verify", "--brief", "brief/hanbit.yaml", "--out", str(out)])
    text = capsys.readouterr().out
    assert "FAIL  cuts_per_min" in text
    assert code == 1


def _stub_slow_pipeline_steps(monkeypatch):
    """generate/voice/assemble(실제 ffmpeg 호출, 특히 assemble은 느림)를
    무해한 더미로 바꿔서, 배너 로직만 빠르게 겨냥해 검증할 수 있게 한다.
    """
    import src.cli as cli
    monkeypatch.setattr(cli, "generate_clips", lambda *a, **kw: GenerateResult())
    monkeypatch.setattr(cli, "build_narration_track", lambda *a, **kw: None)
    monkeypatch.setattr(cli, "assemble", lambda *a, **kw: None)


def test_all_prints_fail_banner_when_a_metric_fails(tmp_path, monkeypatch, capsys):
    """all 스테이지도 종료 코드로는 실패를 알리지 않으므로(설계상 의도), 표 안의
    FAIL 행 하나만으로는 놓치기 쉽다. 별도 배너가 반드시 찍혀야 한다.
    대역을 확실히 벗어나는 값을 손으로 구성한 Metrics로 재현한다 — 조립 단계
    전체를 다시 돌리지 않고 measure()만 스텁으로 대체한다.

    예전에는 Fake 파이프라인의 실측값(silent_ratio 31.3%)을 그대로 썼는데,
    대역을 실측 근거로 바로잡자(Task 18 이후 재측정) 그 값이 대역 안에 들어와
    테스트가 깨졌다. 특정 시점의 관측값이 아니라 "대역 밖"이라는 성질에
    기대야 한다.
    """
    import src.cli as cli
    _stub_slow_pipeline_steps(monkeypatch)
    lo, hi = C.PASS_SILENT_RATIO
    failing = Metrics(duration=88.0, cuts=33, cuts_per_min=22.5, avg_shot=2.67,
                      speech_rate=4.19, silent_ratio=hi + 0.20)
    monkeypatch.setattr(cli, "measure", lambda *a, **kw: failing)

    out = tmp_path / "out"
    code = main(["all", "--brief", "brief/hanbit.yaml", "--out", str(out), "--fake"])
    text = capsys.readouterr().out

    assert code == 0  # all은 판정으로 종료 코드를 내리지 않는다 — 배너가 신호를 대신한다
    assert "⚠ 검증 실패" in text
    assert "silent_ratio" in text.split("⚠ 검증 실패", 1)[1]
    assert "✓ 검증 통과" not in text


def test_all_prints_pass_banner_when_every_metric_passes(tmp_path, monkeypatch, capsys):
    """네 지표 모두 대역 안인 손으로 구성한 Metrics(all-pass 케이스)로,
    배너의 부재가 아니라 명시적인 통과 배너가 찍히는지 확인한다 — 배너 유무
    자체가 항상 모호하지 않아야 한다는 요구사항을 직접 겨냥한다.
    """
    import src.cli as cli
    _stub_slow_pipeline_steps(monkeypatch)
    passing = Metrics(duration=88.0, cuts=33, cuts_per_min=22.5, avg_shot=2.67,
                      speech_rate=4.19, silent_ratio=0.20)
    monkeypatch.setattr(cli, "measure", lambda *a, **kw: passing)

    out = tmp_path / "out"
    code = main(["all", "--brief", "brief/hanbit.yaml", "--out", str(out), "--fake"])
    text = capsys.readouterr().out

    assert code == 0
    assert "✓ 검증 통과" in text
    assert "⚠ 검증 실패" not in text
