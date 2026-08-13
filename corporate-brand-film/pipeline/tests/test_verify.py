import subprocess
from pathlib import Path

import pytest
from src.verify import measure_cuts, measure, measure_silence, judge, Metrics, _spoken_stats
from src import constants as C
from src.brief import load_brief
from src.script import build_script
from src.beats import build_beatsheet
from src.shotlist import build_shotlist
from src.overlay import build_ass
from src.generate import generate_clips
from src.voice import build_narration_track
from src.assemble import assemble
from src.providers.base import FakeVideoProvider, FakeTTSProvider


@pytest.fixture(scope="module")
def clip(tmp_path_factory):
    """3초마다 색이 바뀌는 12초 영상(+무음 오디오 트랙) → 컷 3개 검출 기대.

    실제 완성본(assemble.py 산출물)은 항상 오디오 트랙을 갖고 있으므로,
    measure()가 이제 항상 silencedetect를 돌리는 이상 이 픽스처도 오디오
    트랙 없이는 실제 사용 조건을 대표하지 못한다(오디오 스트림이 아예
    없으면 -af 필터가 조용히 스킵되어 무음비가 실제로는 알 수 없는데도
    0.0으로 나온다).
    """
    d = tmp_path_factory.mktemp("v")
    parts = []
    for i, c in enumerate(["red", "blue", "green", "white"]):
        p = d / f"{i}.mp4"
        subprocess.run(["ffmpeg", "-hide_banner", "-loglevel", "error", "-y",
                        "-f", "lavfi", "-i", f"color=c={c}:s=640x360:r=30:d=3",
                        "-c:v", "libx264", "-pix_fmt", "yuv420p", str(p)], check=True)
        parts.append(p)
    lst = d / "l.txt"
    lst.write_text("".join(f"file '{p}'\n" for p in parts), encoding="utf-8")
    out = d / "seq.mp4"
    subprocess.run(["ffmpeg", "-hide_banner", "-loglevel", "error", "-y",
                    "-f", "concat", "-safe", "0", "-i", str(lst),
                    "-f", "lavfi", "-i", "anullsrc=r=48000:cl=mono", "-shortest",
                    "-c:v", "libx264", "-pix_fmt", "yuv420p",
                    "-c:a", "pcm_s16le", str(out)], check=True)
    return out


def _tone_file(path: Path, tone_seconds: float) -> Path:
    """앞 1초 무음 + tone_seconds초 가청음.

    발화속도가 whisper 없이도 실제 발화 구간 길이에 따라 움직이는지 검증하기
    위한 합성 파일 — 상수를 되읽는 가짜 측정이면 tone_seconds를 바꿔도 결과가
    안 바뀐다.
    """
    # 주의: -t 는 반드시 그 입력의 -i "앞"에 와야 한다. -i 뒤(다음 -i 앞)에
    # 두면 ffmpeg CLI가 그 -t 를 다음 입력의 옵션으로 붙여버리고, 맨 끝의
    # -t 는 입력이 아니라 출력 길이 제한으로 떨어진다 — 실측 중 실제로
    # 이 순서 오류로 무음 전용 파일이 만들어지는 걸 확인했다.
    subprocess.run(["ffmpeg", "-hide_banner", "-loglevel", "error", "-y",
                    "-f", "lavfi", "-t", "1", "-i", "anullsrc=r=48000:cl=mono",
                    "-f", "lavfi", "-t", str(tone_seconds),
                    "-i", "sine=frequency=220:r=48000",
                    "-filter_complex", "[0:a][1:a]concat=n=2:v=0:a=1[a]",
                    "-map", "[a]", "-c:a", "pcm_s16le", str(path)], check=True)
    return path


def test_measure_cuts_finds_transitions(clip):
    assert len(measure_cuts(clip)) == 3


def test_measure_cuts_needs_info_loglevel_not_error(clip):
    """showinfo 는 info 레벨에서만 로그를 찍는다. -loglevel error 로 부르면
    필터는 정상 동작해도 pts_time 로그가 안 나와서 컷이 0개로 측정된다 —
    레퍼런스 28편 분석 스크립트가 실제로 이 버그로 한 번 죽었다(태스크 브리핑).
    이 테스트는 우리 코드가 아니라 그 함정 자체가 진짜라는 걸 직접 확인한다:
    error 레벨로 같은 필터를 돌리면 실제로 0개가 나와야 한다."""
    out = subprocess.run(
        ["ffmpeg", "-hide_banner", "-loglevel", "error", "-i", str(clip),
         "-filter:v", "select='gt(scene,0.30)',showinfo", "-f", "null", "-"],
        capture_output=True, text=True)
    assert "pts_time" not in out.stderr
    # 우리 measure_cuts 는 info 레벨을 쓰므로 실제로 컷을 찾는다.
    assert len(measure_cuts(clip)) > 0


def test_measure_reports_duration_and_rate(clip):
    m = measure(clip)
    assert m.duration == pytest.approx(12.0, abs=0.3)
    assert m.cuts == 4
    assert m.cuts_per_min == pytest.approx(20.0, abs=1.0)
    assert m.avg_shot == pytest.approx(3.0, abs=0.2)


def test_measure_without_chars_or_transcribe_leaves_speech_rate_none_but_measures_silence(clip):
    """무나레이션 비율은 ML 없이도 항상 측정된다(silencedetect). chars 를
    안 주면 발화속도만 None으로 남는다 — 분자(글자수)가 없어서다."""
    m = measure(clip)
    assert m.speech_rate is None
    assert m.silent_ratio is not None
    # clip 픽스처의 오디오는 처음부터 끝까지 anullsrc(완전 무음)이므로
    # 무음비는 거의 100%여야 한다.
    assert m.silent_ratio == pytest.approx(1.0, abs=0.05)


def test_silent_ratio_measured_without_ml(tmp_path):
    """무음 14초 + 소리 26초 = 무음비 0.35 를 silencedetect 로 잡아낸다.

    -t 를 대응하는 -i 앞에 둬야 한다(브리프 원안은 -i 뒤에 둬서 -t 14가
    두 번째 입력에, 맨 끝 -t 26이 출력 길이 제한에 붙어버렸다 — 실측으로
    확인한 뒤 고쳤다). 이대로면 anullsrc가 무한 길이로 남아 concat이
    출력 26초 전체를 무음으로 채운다.
    """
    wav = tmp_path / "a.wav"
    subprocess.run(["ffmpeg", "-hide_banner", "-loglevel", "error", "-y",
                    "-f", "lavfi", "-t", "14", "-i", "anullsrc=r=48000:cl=mono",
                    "-f", "lavfi", "-t", "26", "-i", "sine=frequency=220:r=48000",
                    "-filter_complex", "[0:a][1:a]concat=n=2:v=0:a=1[a]",
                    "-map", "[a]", "-c:a", "pcm_s16le", str(wav)], check=True)
    assert measure_silence(wav) == pytest.approx(14.0, abs=0.6)


def test_speech_rate_moves_with_measured_speech_duration(tmp_path):
    """같은 글자수라도 발화 구간이 길어지면 발화속도는 떨어져야 한다.

    상수를 되읽는 가짜 측정이면 두 값이 같게 나온다.
    """
    fast, slow = _tone_file(tmp_path / "f.wav", 20.0), _tone_file(tmp_path / "s.wav", 40.0)
    assert measure(fast, chars=100).speech_rate > measure(slow, chars=100).speech_rate * 1.8


def test_measure_cuts_raises_distinctly_on_ffmpeg_failure(tmp_path):
    """ffmpeg 자체가 실패하면(예: 잘린 파일) '컷 0개(정지 화면)'와 구분되는
    에러를 내야 한다. 구분하지 않으면 pts_time 로그가 하나도 안 잡혀서
    measure_cuts가 조용히 []를 돌려주고 cuts=1이 되어, 툴 실패가 "거대한
    정지 샷 하나"로 둔갑한다 — truncated-clip 버그(commit 93dafcc)와 같은
    계열. check=True 가 아니라 존재하지 않는 파일로 재현한다."""
    missing = tmp_path / "does_not_exist.mp4"
    with pytest.raises(RuntimeError, match="컷 검출 실패"):
        measure_cuts(missing)


def test_measure_silence_raises_distinctly_on_ffmpeg_failure(tmp_path):
    """ffmpeg 자체가 실패하면 '무음 0초'와 구분되는 에러를 내야 한다.

    measure_cuts의 실패보다 더 위험하다: measure()에서 silent=0.0이 조용히
    나오면 spoken = dur - 0 = dur 이 되어 speech_rate = chars/dur 가 우연히
    PASS_SPEECH_RATE 안에 들어올 수 있다 — 측정 실패가 거짓 PASS로 둔갑하는
    경로다. 존재하지 않는 파일로 재현한다."""
    missing = tmp_path / "does_not_exist.wav"
    with pytest.raises(RuntimeError, match="무나레이션 비율 측정 실패"):
        measure_silence(missing)


def test_judge_flags_out_of_band_values():
    m = Metrics(duration=88, cuts=100, cuts_per_min=68.2, avg_shot=0.88,
                speech_rate=4.2, silent_ratio=0.22)
    v = judge(m)
    assert v["cuts_per_min"] is False
    assert v["avg_shot"] is False
    assert v["speech_rate"] is True
    assert v["silent_ratio"] is True


def test_judge_passes_designed_values():
    m = Metrics(duration=88, cuts=33, cuts_per_min=22.5, avg_shot=2.67,
                speech_rate=4.2, silent_ratio=0.22)
    assert all(judge(m).values())


def test_judge_flags_out_of_band_speech_metrics():
    """브리프 테스트는 speech_rate/silent_ratio 가 항상 True인 케이스만
    다뤘다. 컷/샷 대역은 정상이어도 발화 지표만 벗어나면 그 필드가
    개별적으로 False 여야 한다."""
    m = Metrics(duration=88, cuts=33, cuts_per_min=22.5, avg_shot=2.67,
                speech_rate=6.5, silent_ratio=0.05)
    v = judge(m)
    assert v["cuts_per_min"] is True
    assert v["avg_shot"] is True
    assert v["speech_rate"] is False
    assert v["silent_ratio"] is False


def test_judge_omits_speech_fields_when_not_measured():
    """transcribe=False 로 측정하면 speech_rate/silent_ratio 가 None이고,
    judge() 결과에 그 키 자체가 없어야 한다(거짓 True/False로 채우지 않는다)."""
    m = Metrics(duration=88, cuts=33, cuts_per_min=22.5, avg_shot=2.67)
    v = judge(m)
    assert "speech_rate" not in v
    assert "silent_ratio" not in v


class _Seg:
    def __init__(self, start, end, text):
        self.start, self.end, self.text = start, end, text


def test_spoken_stats_computes_from_actual_segments_not_constant():
    """발화속도는 whisper 세그먼트의 실제 길이/글자수에서 나와야 한다 —
    SPEECH_RATE 상수를 그대로 돌려주는 구현이면 세그먼트를 바꿔도 결과가
    안 바뀌므로, 상수와 다른 값이 나오는 세그먼트로 이 트랩을 잡는다."""
    segs = [_Seg(0.0, 2.0, "가나다라마바사아자차")]  # 10자 / 2초 = 5.0자/초
    spoken, chars = _spoken_stats(segs)
    assert spoken == pytest.approx(2.0)
    assert chars == 10
    rate = chars / spoken
    assert rate == pytest.approx(5.0)
    assert rate != pytest.approx(C.SPEECH_RATE, abs=0.1)


def test_spoken_stats_strips_punctuation_and_whitespace():
    segs = [_Seg(0.0, 1.0, "안녕, 하세요!"), _Seg(1.0, 2.0, " 반갑습니다.")]
    spoken, chars = _spoken_stats(segs)
    assert spoken == pytest.approx(2.0)
    assert chars == len("안녕하세요반갑습니다")


def test_spoken_stats_empty_segments_is_zero():
    spoken, chars = _spoken_stats([])
    assert spoken == 0.0
    assert chars == 0


# --- 완성본 전체 파이프라인: Fake로 조립한 88초 실물을 실제로 측정한다 ---

@pytest.fixture(scope="module")
def fake_film(tmp_path_factory):
    d = tmp_path_factory.mktemp("verify_film")
    b = load_brief("brief/hanbit.yaml")
    sc = build_script(b)
    bs = build_beatsheet(sc)
    sl = build_shotlist(bs, b)
    generate_clips(sl, b, FakeVideoProvider(), d / "clips")
    vo = build_narration_track(bs, FakeTTSProvider(), d / "vo.wav", work_dir=d / "vo_seg")
    out = assemble(sl, d / "clips", build_ass(bs, sl, b), vo, d / "final.mp4",
                   work_dir=d / "work")
    return out, sl


def test_measure_runs_on_real_assembled_fake_film(fake_film):
    """Fake 컬러바 클립 + Fake 가청 TTS 로 조립한 실제 88초 mp4에 측정기를
    그대로 돌려서 4개 지표(컷/분, 평균샷, 발화속도, 무나레이션 비율)를
    전부 실측한다. FakeVideoProvider가 이제 인접 클립마다 밝기를 뒤집고
    FakeTTSProvider가 가청음을 내므로, 씬 검출과 silencedetect 둘 다 잴
    거리가 생긴다(Task 11b 이전에는 컷=1, 발화지표 측정 불가였다).

    실측값이 아니라 measure()가 스스로 계산한 값끼리 비교하는 assert(예:
    cuts_per_min == cuts/(dur/60))는 회귀를 하나도 못 잡는다 — cuts=1로
    돌아가도 통과한다. 그래서 여기서는 이 태스크가 존재하는 이유 자체를
    assert한다: 씬 검출이 설계 컷 수 근처를 실제로 잡아내는지, 컷/분·평균샷·
    발화속도가 실제로 합격 대역 안에 들어오는지.
    """
    out, sl = fake_film
    bs = build_beatsheet(build_script(load_brief("brief/hanbit.yaml")))
    chars = sum(b.chars for b in bs.beats)
    m = measure(out, chars=chars)
    assert m.duration == pytest.approx(C.TOTAL_SECONDS, abs=0.5)
    # 씬 검출이 설계 컷 수(33) 근처를 실제로 잡아내는지 — 밝기 반전이
    # 안 먹으면(예: FakeVideoProvider가 다시 인접 클립을 동일하게 뱉으면)
    # 컷 수가 1 근처로 붕괴하므로 이 tolerance를 크게 벗어난다. ±2는
    # 실측(33/33 정확히 일치, huetest 프로브에서도 경계마다 정확히 검출)
    # 대비 인코딩/프레임 경계 지터를 흡수할 여유를 조금만 준 값이다.
    assert m.cuts == pytest.approx(len(sl.shots), abs=2)
    j = judge(m)
    assert j["cuts_per_min"] is True
    assert j["avg_shot"] is True
    assert j["speech_rate"] is True
    # silent_ratio 는 beats.py 에서 독립적으로 유도한 기대값에 근접해야 한다 —
    # 이 수치가 알 수 없는 이유로 바뀌면(회귀) 여기서 잡힌다.
    #
    # 한때 이 값(~31.3%)은 대역(15~30%) 밖이었고 그 사실을 여기 못박아뒀는데,
    # Task 18 이후 레퍼런스 22편을 재측정해 대역을 실측 10~90 백분위(9~40%)로
    # 바로잡자 대역 안으로 들어왔다. 옛 대역은 레퍼런스 영상 자신의 41%를
    # 탈락시키고 있었다 — 통과시키려고 넓힌 게 아니라, 기준이 틀렸던 것을
    # 데이터로 고친 결과다.
    expected_silent_seconds = C.TOTAL_SECONDS - sum(
        b.chars / C.SPEECH_RATE for b in bs.beats if b.narrated)
    expected_silent_ratio = expected_silent_seconds / C.TOTAL_SECONDS
    assert m.silent_ratio == pytest.approx(expected_silent_ratio, abs=0.01)
    assert j["silent_ratio"] is True
    print(f"\n[verify] 설계 컷 수={len(sl.shots)} / 실측 컷 수={m.cuts} "
          f"컷/분={m.cuts_per_min:.2f} 평균샷={m.avg_shot:.2f}s "
          f"발화속도={m.speech_rate:.2f}자/초 무나레이션비율={m.silent_ratio:.2%} "
          f"판정={j}")


class _FakeSeg:
    def __init__(self, start, end, text):
        self.start, self.end, self.text = start, end, text


def test_clean_segments_drops_hallucinated_tail_beyond_file_length():
    """whisper 는 파일 끝 무음에서 직전 문장을 반복 생성하고, 파일 길이를 넘는
    타임스탬프까지 만들어낸다. Task 18 실측: 88.000초 파일에 98.4초까지 세그먼트가
    생겼고 같은 문장이 12번 반복돼 인식 글자수가 253→465 로 부풀었다(발화속도
    4.09→7.25). 이 방어를 지우면 이 테스트가 실패한다."""
    from src.verify import _clean_segments
    segs = [_FakeSeg(10.0, 20.0, "실제 문장."),
            _FakeSeg(85.0, 88.5, "끝 문장."),
            _FakeSeg(88.5, 89.5, "환각."),
            _FakeSeg(89.5, 98.4, "환각.")]
    out = _clean_segments(segs, 88.0)
    assert [s.text for s in out] == ["실제 문장.", "끝 문장."]
    assert out[-1].end == 88.0, "파일 길이에 걸친 세그먼트는 끝을 잘라낸다"


def test_clean_segments_collapses_repetition_loop():
    """같은 문장이 연속 반복되면 첫 번째만 남긴다 — 반복은 발화가 아니다."""
    from src.verify import _clean_segments
    segs = [_FakeSeg(i, i + 1, "보이지 않는 것이 만드는 차이.") for i in range(10, 20)]
    out = _clean_segments(segs, 88.0)
    assert len(out) == 1


def test_clean_segments_keeps_a_repeated_line_that_is_not_consecutive():
    """일부러 반복한 대사(후렴)는 사이에 다른 말이 끼어 있으므로 살린다."""
    from src.verify import _clean_segments
    segs = [_FakeSeg(1, 2, "같은 말."), _FakeSeg(3, 4, "다른 말."),
            _FakeSeg(5, 6, "같은 말.")]
    assert len(_clean_segments(segs, 88.0)) == 3


def test_audio_metrics_come_from_the_voice_track_not_the_music_mix(tmp_path):
    """배경음이 섞인 믹스에서 재면 음악이 계측을 교란한다 — 실측에서 같은
    나레이션에 배경음만 추가했더니 발화속도가 4.48→4.87 로 움직였다.
    voice_path 를 주면 오디오 지표는 그 트랙에서, 영상 지표는 완성본에서 재야 한다."""
    from src.verify import measure

    # 완성본: 계속 소리가 나는 트랙(= 음악이 깔린 믹스를 흉내)
    mixed = tmp_path / "final.mp4"
    subprocess.run(["ffmpeg", "-hide_banner", "-loglevel", "error", "-y",
                    "-f", "lavfi", "-i", "testsrc=size=320x180:rate=30:duration=10",
                    "-f", "lavfi", "-i", "sine=frequency=220:r=48000:duration=10",
                    "-c:v", "libx264", "-pix_fmt", "yuv420p", "-c:a", "aac",
                    "-shortest", str(mixed)], check=True)
    # 나레이션 트랙: 앞 5초만 소리, 뒤 5초는 무음
    voice = tmp_path / "vo.wav"
    # -t 는 해당 -i 앞에 와야 그 입력의 길이 제한으로 적용된다. 뒤에 두면
    # 출력 옵션으로 해석돼 첫 입력이 무한 길이가 되고 무음이 붙지 않는다.
    subprocess.run(["ffmpeg", "-hide_banner", "-loglevel", "error", "-y",
                    "-t", "5", "-f", "lavfi", "-i", "sine=frequency=220:r=48000",
                    "-t", "5", "-f", "lavfi", "-i", "anullsrc=r=48000:cl=mono",
                    "-filter_complex", "[0:a][1:a]concat=n=2:v=0:a=1[a]",
                    "-map", "[a]", "-c:a", "pcm_s16le", str(voice)], check=True)

    mixed_only = measure(mixed)
    with_voice = measure(mixed, voice_path=voice)

    assert mixed_only.silent_ratio == pytest.approx(0.0, abs=0.05), \
        "믹스만 보면 계속 소리가 나니 무음이 0으로 보인다"
    assert with_voice.silent_ratio == pytest.approx(0.5, abs=0.1), \
        "나레이션 트랙에서 재면 실제 무나레이션 비율이 나온다"
    assert with_voice.duration == mixed_only.duration, \
        "영상 지표(길이·컷)는 언제나 완성본에서 잰다"


def test_heard_ratio_is_per_beat_not_whole_film():
    """막 단위로 재야 한다. 전체 비율로는 문장 누락이 묻힌다.

    Task 18 실측: 사라진 "한빛소재."(4자)는 대본 253자 대비 1.6% 라 완성본
    전체 비율로는 100% 로 보여 통과했다. 같은 누락이 막 단위로는 16자 중 4자,
    즉 75% 여서 곧바로 잡힌다. 사고는 문장 단위로 일어나므로 문장이 속한 막
    단위로 재야 한다 — 이 테스트를 전체 비율 방식으로 되돌리면 실패한다.
    """
    from src.verify import Metrics, judge

    # 클로징만 4자 누락, 나머지는 정상. 전체로 합치면 249/253 = 98%.
    coverage = [("definition", 46, 46), ("evidence", 49, 49), ("pivot", 25, 25),
                ("chapter_0", 46, 46), ("chapter_1", 38, 37), ("climax", 33, 33),
                ("closing", 16, 12)]
    m = Metrics(duration=88.0, cuts=33, cuts_per_min=22.5, avg_shot=2.67,
                speech_rate=4.09, silent_ratio=0.22, coverage=coverage)

    total_ratio = sum(c[2] for c in coverage) / sum(c[1] for c in coverage)
    assert total_ratio > C.PASS_HEARD_RATIO, "전체 비율로는 통과해버린다 — 그래서 막 단위여야 한다"

    assert m.worst_beat[0] == "closing"
    assert m.heard_ratio == pytest.approx(12 / 16)
    v = judge(m)
    assert v["heard_ratio"] is False, "문장이 통째로 빠지면 잡아야 한다"
    # 다른 지표는 전부 통과한다는 점이 이 검사가 필요한 이유다
    assert v["speech_rate"] is True and v["silent_ratio"] is True


def test_heard_ratio_passes_on_transcription_noise():
    """정상 막의 실측 인식률은 95~104%(±2자) 였다 — 전사 오차는 통과시켜야 한다."""
    from src.verify import Metrics, judge
    m = Metrics(duration=88.0, cuts=33, cuts_per_min=22.5, avg_shot=2.67,
                coverage=[("definition", 46, 47), ("chapter_1", 38, 36),
                          ("closing", 16, 16)])
    assert judge(m)["heard_ratio"] is True


def test_heard_ratio_is_absent_without_coverage():
    """전사를 안 했으면 이 지표는 판정에 끼지 않는다 — 없는 값으로 FAIL 을 만들지 않는다."""
    from src.verify import Metrics, judge
    m = Metrics(duration=88.0, cuts=33, cuts_per_min=22.5, avg_shot=2.67)
    assert m.heard_ratio is None and m.worst_beat is None
    assert "heard_ratio" not in judge(m)


def test_heard_ratio_has_no_upper_bound():
    """전사가 대본보다 길게 나오는 건 ASR 이 조사·어미를 다르게 적은 것이지
    사고가 아니다 — 잡아야 할 건 누락뿐이다."""
    from src.verify import Metrics, judge
    m = Metrics(duration=88.0, cuts=33, cuts_per_min=22.5, avg_shot=2.67,
                coverage=[("closing", 16, 22)])
    assert judge(m)["heard_ratio"] is True
