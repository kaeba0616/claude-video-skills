import re
import subprocess
from pathlib import Path

import pytest
from src.providers.base import FakeVideoProvider, FakeTTSProvider, BlockedError


def _duration(p: Path) -> float:
    out = subprocess.run(
        ["ffprobe", "-v", "error", "-show_entries", "format=duration",
         "-of", "csv=p=0", str(p)],
        capture_output=True, text=True, check=True)
    return float(out.stdout.strip())


def _resolution(p: Path) -> tuple[int, int]:
    out = subprocess.run(
        ["ffprobe", "-v", "error", "-select_streams", "v:0",
         "-show_entries", "stream=width,height", "-of", "csv=p=0", str(p)],
        capture_output=True, text=True, check=True)
    w, h = out.stdout.strip().split(",")
    return int(w), int(h)


def test_fake_video_writes_file_of_requested_duration(tmp_path):
    out = tmp_path / "c.mp4"
    FakeVideoProvider().generate("a factory", out, 4, "720p", "16:9", None)
    assert out.exists()
    assert _duration(out) == pytest.approx(4.0, abs=0.2)


def test_fake_video_raises_blocked_for_configured_prompt(tmp_path):
    p = FakeVideoProvider(block_prompts=["forbidden"])
    with pytest.raises(BlockedError):
        p.generate("a forbidden scene", tmp_path / "c.mp4", 4, "720p", "16:9", None)


def test_fake_tts_writes_wav_of_requested_duration(tmp_path):
    out = tmp_path / "vo.wav"
    FakeTTSProvider(seconds=3.0).synthesize("안녕하세요", out, "ko-KR")
    assert out.exists()
    assert _duration(out) == pytest.approx(3.0, abs=0.2)


def test_fake_video_emits_the_requested_resolution_not_the_final_canvas(tmp_path):
    """Fake 가 곧장 1080p 를 뱉으면 Task 10 의 리스케일이 no-op 이 되어
    깨진 scale 체인을 테스트가 통과시킨다. 실제 Veo 와 같은 크기를 내보내야 한다."""
    out = tmp_path / "c.mp4"
    FakeVideoProvider().generate("a factory", out, 4, "720p", "16:9", None)
    assert _resolution(out) == (1280, 720)


def test_fake_video_rejects_unknown_resolution(tmp_path):
    with pytest.raises(ValueError, match="알 수 없는 해상도"):
        FakeVideoProvider().generate("x", tmp_path / "c.mp4", 4, "480p", "16:9", None)


def test_fake_video_clips_differ_between_calls(tmp_path):
    """씬 검출이 컷을 볼 수 있으려면 연속 클립의 그림이 실제로 달라야 한다."""
    p = FakeVideoProvider()
    a, b = tmp_path / "a.mp4", tmp_path / "b.mp4"
    p.generate("first shot", a, 4, "720p", "16:9", None)
    p.generate("second shot", b, 4, "720p", "16:9", None)
    assert a.read_bytes() != b.read_bytes()


def test_fake_tts_is_audible_not_silence(tmp_path):
    """무음을 뱉으면 silencedetect 기반 계측을 Fake 로 검증할 수 없다."""
    out = tmp_path / "a.wav"
    FakeTTSProvider().synthesize("가나다라마바사아자차", out, "Charon")
    r = subprocess.run(["ffmpeg", "-hide_banner", "-i", str(out),
                        "-af", "volumedetect", "-f", "null", "-"],
                       capture_output=True, text=True)
    peak = float(re.search(r"max_volume: (-?[0-9.]+) dB", r.stderr).group(1))
    assert peak > -40.0
