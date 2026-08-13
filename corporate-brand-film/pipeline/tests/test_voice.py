import re
import subprocess
from pathlib import Path

import pytest
from src.brief import load_brief
from src.script import build_script
from src.beats import build_beatsheet
from src.voice import (narration_text, synthesize_narration, narration_segments,
                       build_narration_track, narration_audio_seconds)
from src.providers.base import FakeTTSProvider
from src import constants as C


def _script():
    return build_script(load_brief("brief/hanbit.yaml"))


def _bs():
    return build_beatsheet(_script())


def _dur(p) -> float:
    return float(subprocess.run(
        ["ffprobe", "-v", "error", "-show_entries", "format=duration",
         "-of", "csv=p=0", str(p)],
        capture_output=True, text=True, check=True).stdout.strip())


def test_narration_text_excludes_silent_acts():
    t = narration_text(_script())
    assert "머리카락 한 올의 십분의 일." in t
    assert t.startswith("머리카락")


def test_narration_text_joins_lines_with_newline():
    t = narration_text(_script())
    assert "\n" in t
    assert t.strip().endswith("한빛소재.")


def test_synthesize_writes_audio_file(tmp_path):
    out = tmp_path / "vo.wav"
    p = synthesize_narration(_script(), FakeTTSProvider(seconds=69.0), out)
    assert p == out and out.exists() and out.stat().st_size > 0


def test_segments_start_at_each_narrated_beat():
    bs = _bs()
    segs = narration_segments(bs)
    narrated = [b for b in bs.beats if b.narrated and b.lines]
    assert [s for s, _ in segs] == [b.start for b in narrated]
    assert all(t.strip() for _, t in segs)


def test_segments_skip_the_silent_cold_open():
    # 콜드오픈은 무나레이션이라 0.0 에서 시작하는 세그먼트가 있으면 안 된다.
    assert all(start > 0.0 for start, _ in narration_segments(_bs()))


def test_track_is_exactly_total_seconds(tmp_path):
    out = build_narration_track(_bs(), FakeTTSProvider(), tmp_path / "vo.wav",
                                work_dir=tmp_path / "seg")
    assert _dur(out) == pytest.approx(C.TOTAL_SECONDS, abs=0.15)


def test_track_is_silent_during_the_cold_open(tmp_path):
    """콜드오픈 구간에 음성이 새지 않는지 — 실제 오디오를 측정한다."""
    out = build_narration_track(_bs(), FakeTTSProvider(), tmp_path / "vo.wav",
                                work_dir=tmp_path / "seg")
    seg = tmp_path / "cold.wav"
    subprocess.run(["ffmpeg", "-hide_banner", "-loglevel", "error", "-y",
                    "-i", str(out), "-t", str(C.COLD_OPEN_SECONDS),
                    str(seg)], check=True)
    r = subprocess.run(["ffmpeg", "-hide_banner", "-i", str(seg),
                        "-af", "volumedetect", "-f", "null", "-"],
                       capture_output=True, text=True)
    assert "max_volume: -91" in r.stderr or "max_volume: -inf" in r.stderr


def test_overrunning_segment_is_rejected(tmp_path):
    """한 막의 음성이 다음 나레이션 막을 침범하면 조립 전에 막는다."""
    with pytest.raises(ValueError, match="나레이션이 막 길이를 넘습니다"):
        build_narration_track(_bs(), FakeTTSProvider(seconds=40.0),
                              tmp_path / "vo.wav", work_dir=tmp_path / "seg")


def test_fake_tts_length_tracks_text_length(tmp_path):
    """Fake 충실도: seconds 를 주지 않으면 글자수/발화속도로 길이를 낸다."""
    p = FakeTTSProvider()
    short, long = tmp_path / "s.wav", tmp_path / "l.wav"
    p.synthesize("가나다라마", short, "Charon")
    p.synthesize("가나다라마" * 10, long, "Charon")
    assert _dur(long) > _dur(short) * 5


# --- 이어받기(resume): mid-narration 실패 후 재실행이 이미 만든 세그먼트를
# 재과금하지 않아야 한다. src/generate.py가 이미 완성된 클립을 건너뛰는
# 것과 같은 원칙 — 세그먼트는 work_dir/seg{i:02d}.wav 에 결정적인 경로로
# 떨어지므로 이어받기가 거의 공짜다.

def _seg_index(out_path: Path) -> int:
    m = re.match(r"seg(\d+)\.wav", out_path.name)
    assert m, f"예상치 못한 세그먼트 파일명: {out_path.name}"
    return int(m.group(1))


class _TrackingTTSProvider:
    """실제 TTSProvider를 감싸 세그먼트별 호출 인덱스를 기록한다.

    fail_at: 이 인덱스로 호출되면 (일시적 오류를 흉내내) 오디오를 실제로
    쓴 뒤 예외를 던진다 — "부분적으로 쓰다가 실패"를 재현해서, 임시파일이
    실제로 정리되고 최종 경로에 잘린 결과물이 남지 않는지를 의미 있게
    검증한다.
    forbidden: 이 인덱스로 호출되면 즉시 AssertionError — "이미 완료된
    세그먼트가 재과금됐다"는 증거다.
    """

    def __init__(self, fail_at: frozenset[int] = frozenset(),
                forbidden: frozenset[int] = frozenset(), seconds: float = 0.3):
        self._inner = FakeTTSProvider(seconds=seconds)
        self.fail_at = fail_at
        self.forbidden = forbidden
        self.called_indices: list[int] = []

    def synthesize(self, text, out_path, voice):
        idx = _seg_index(out_path)
        self.called_indices.append(idx)
        if idx in self.forbidden:
            raise AssertionError(
                f"세그먼트 {idx}는 이미 완료됐어야 하는데 다시 호출됐습니다"
                "(= 재과금 위험)")
        self._inner.synthesize(text, out_path, voice)
        if idx in self.fail_at:
            raise RuntimeError(f"세그먼트 {idx} 합성 실패(시뮬레이션, 일시적 오류)")


def test_mid_run_failure_leaves_completed_segments_and_cleans_up_the_failed_one(tmp_path):
    """세그먼트 2에서 실패하면: 0·1은 디스크에 완결된 채로 남고, 2는 최종
    경로에도 임시파일로도 남지 않는다(잘린 결과물이 다음 실행의 exists()
    건너뛰기를 속이면 안 된다)."""
    work = tmp_path / "seg"
    provider = _TrackingTTSProvider(fail_at=frozenset({2}))
    with pytest.raises(RuntimeError, match="세그먼트 2"):
        build_narration_track(_bs(), provider, tmp_path / "vo.wav", work_dir=work)

    assert provider.called_indices == [0, 1, 2]
    assert (work / "seg00.wav").exists() and (work / "seg00.wav").stat().st_size > 0
    assert (work / "seg01.wav").exists() and (work / "seg01.wav").stat().st_size > 0
    assert not (work / "seg02.wav").exists()
    assert not (work / ".tmp" / "seg02.wav").exists()


def test_rerun_after_mid_run_failure_skips_completed_segments_without_repaying(tmp_path):
    """실패한 실행 뒤 재실행하면 이미 만들어진 세그먼트(0·1)는 provider를
    다시 부르지 않는다 — 다시 부르면 재과금이므로, forbidden 세트로 그 사실
    자체를 테스트가 강제한다."""
    work = tmp_path / "seg"
    first = _TrackingTTSProvider(fail_at=frozenset({2}))
    with pytest.raises(RuntimeError):
        build_narration_track(_bs(), first, tmp_path / "vo.wav", work_dir=work)

    n_segments = len(narration_segments(_bs()))
    second = _TrackingTTSProvider(forbidden=frozenset({0, 1}))
    out = build_narration_track(_bs(), second, tmp_path / "vo.wav", work_dir=work)

    assert out.exists() and out.stat().st_size > 0
    assert 0 not in second.called_indices
    assert 1 not in second.called_indices
    assert set(second.called_indices) == set(range(2, n_segments))


def test_overrun_check_measures_resumed_segments_from_disk_not_assumed_fresh(tmp_path):
    """세그먼트가 이번 실행에서 건너뛰어졌어도(=디스크에 이미 있어서),
    그 파일이 실제로 막 길이를 넘긴다면 여전히 걸러야 한다 — "방금 만들어서
    괜찮을 것"이라는 가정이 아니라 디스크의 실제 길이를 잰다."""
    from src.voice import _write_stamp  # 이전 실행이 남긴 세그먼트를 그대로 흉내낸다
    from src import constants as C

    bs = _bs()
    work = tmp_path / "seg"
    work.mkdir(parents=True)
    segments = narration_segments(bs)
    starts = [s for s, _ in segments]
    overrun_seconds = (starts[1] - starts[0]) + 10.0
    subprocess.run([
        "ffmpeg", "-hide_banner", "-loglevel", "error", "-y",
        "-f", "lavfi", "-i", "sine=frequency=220:r=48000",
        "-t", f"{overrun_seconds:.3f}", "-c:a", "pcm_s16le",
        str(work / "seg00.wav"),
    ], check=True)
    # 지문까지 남겨야 "같은 대사로 이미 합성된 세그먼트"가 된다 — 지문이 없으면
    # 출처를 알 수 없으므로 이어받기가 신뢰하지 않고 다시 합성한다.
    _write_stamp(work / "seg00.wav", segments[0][1], C.TTS_VOICE)

    provider = _TrackingTTSProvider(forbidden=frozenset({0}))
    with pytest.raises(ValueError, match="나레이션이 막 길이를 넘습니다"):
        build_narration_track(bs, provider, tmp_path / "vo.wav", work_dir=work)
    assert 0 not in provider.called_indices


def test_edited_narration_is_resynthesized_not_silently_reused(tmp_path):
    """brief 의 대사를 고치고 같은 --out 에서 다시 돌리면 그 막은 다시 합성돼야 한다.

    이어받기는 파일이 있으면 건너뛴다. 그런데 막 개수와 위치가 그대로면 파일
    이름도 그대로라, 옛 음성이 조용히 재사용되고 그 위에 새 자막이 얹힌다 —
    말과 자막이 서로 다른 문장을 말하는데 길이는 맞아서 아무 에러도 안 난다.
    (브랜치 리뷰 Important 1)

    이 테스트를 되돌리려면 _stamp_matches 를 지워야 하는데, 그러면 두 번째
    실행에서 provider 가 0번을 부르지 않아 곧바로 실패한다.
    """
    bs = _bs()
    work = tmp_path / "seg"

    first = _TrackingTTSProvider()
    build_narration_track(bs, first, tmp_path / "vo1.wav", work_dir=work)
    n = len(narration_segments(bs))
    assert set(first.called_indices) == set(range(n))

    # 대사가 그대로면 두 번째 실행은 아무것도 다시 부르지 않는다.
    again = _TrackingTTSProvider(forbidden=frozenset(range(n)))
    build_narration_track(bs, again, tmp_path / "vo2.wav", work_dir=work)
    assert again.called_indices == []

    # 첫 막의 대사를 바꾸면 그 막만 다시 합성된다.
    bs.beats[[b.name for b in bs.beats].index("definition")].lines[0] = "완전히 다른 문장입니다."
    edited = _TrackingTTSProvider(forbidden=frozenset(range(1, n)))
    build_narration_track(bs, edited, tmp_path / "vo3.wav", work_dir=work)
    assert edited.called_indices == [0], \
        "대사가 바뀐 막은 다시 합성돼야 한다 — 옛 음성에 새 자막이 얹히면 안 된다"


def test_narration_audio_seconds_reports_measured_segment_durations(tmp_path):
    """자막을 실제 음성 길이에 맞추려면 그 길이를 실측으로 알아야 한다.

    막의 예약 길이를 돌려주면(= 이 함수를 지우고 beat.seconds 를 쓰면) 자막이
    음성보다 길게 남아 막마다 최대 2.6초까지 밀린다. 실제 실행에서 사용자가
    "자막과 말의 싱크가 안 맞는다"고 지적한 원인이다.
    """
    bs = _bs()
    work = tmp_path / "seg"
    build_narration_track(bs, FakeTTSProvider(), tmp_path / "vo.wav", work_dir=work)

    got = narration_audio_seconds(bs, work)
    narrated = [b for b in bs.beats if b.narrated and b.lines]
    assert set(got) == {b.name for b in narrated}

    for i, b in enumerate(narrated):
        measured = _dur(work / f"seg{i:02d}.wav")
        assert got[b.name] == pytest.approx(measured, abs=0.02)
        assert got[b.name] < b.seconds, \
            f"{b.name}: 실측 음성이 예약 길이보다 짧아야 여유가 생긴다"


def test_narration_audio_seconds_is_empty_without_segments(tmp_path):
    """세그먼트가 없으면 빈 매핑 — build_ass 가 예약 길이로 되돌아간다."""
    assert narration_audio_seconds(_bs(), tmp_path / "없는디렉터리") == {}
