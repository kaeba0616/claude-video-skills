import re
import subprocess
from pathlib import Path

import pytest
from src.brief import load_brief
from src.script import build_script
from src.beats import build_beatsheet
from src.shotlist import build_shotlist, Shot
from src.overlay import build_ass
from src.generate import generate_clips
from src.voice import build_narration_track
from src.assemble import assemble, _frame_span, _trim_clip, _mux_audio
from src.providers.base import FakeVideoProvider, FakeTTSProvider
from src import constants as C


def _probe(p: Path, entries: str) -> str:
    return subprocess.run(
        ["ffprobe", "-v", "error", "-show_entries", entries, "-of", "csv=p=0", str(p)],
        capture_output=True, text=True, check=True).stdout.strip()


@pytest.fixture(scope="module")
def built(tmp_path_factory):
    d = tmp_path_factory.mktemp("build")
    b = load_brief("brief/hanbit.yaml")
    sc = build_script(b)
    bs = build_beatsheet(sc)
    sl = build_shotlist(bs, b)
    generate_clips(sl, b, FakeVideoProvider(), d / "clips")
    vo = build_narration_track(bs, FakeTTSProvider(), d / "vo.wav",
                               work_dir=d / "vo_seg")
    out = assemble(sl, d / "clips", build_ass(bs, sl, b), vo, d / "final.mp4",
                   work_dir=d / "work")
    return out


def test_output_exists(built):
    assert built.exists() and built.stat().st_size > 0


def test_output_duration_is_88_seconds(built):
    assert float(_probe(built, "format=duration")) == pytest.approx(C.TOTAL_SECONDS, abs=0.5)


def test_output_resolution_is_1080p(built):
    assert _probe(built, "stream=width,height").splitlines()[0] == f"{C.WIDTH},{C.HEIGHT}"


def test_output_has_an_audio_stream(built):
    assert "audio" in _probe(built, "stream=codec_type")


def test_output_video_codec_is_h264_yuv420p(built):
    assert _probe(built, "stream=codec_name").splitlines()[0] == "h264"
    assert _probe(built, "stream=pix_fmt").splitlines()[0] == "yuv420p"


def test_output_fps_is_30(built):
    rate = _probe(built, "stream=r_frame_rate").splitlines()[0]
    num, den = rate.split("/")
    assert float(num) / float(den) == pytest.approx(C.FPS, abs=0.01)


def test_ass_burn_resolves_configured_fonts_not_fallback(tmp_path):
    """실제 ASS 굽기 ffmpeg 호출(assemble()의 3단계와 같은 vf=ass=... 필터)이
    constants.py에 선언된 FONT_BOLD_NAME/FONT_REGULAR_NAME을 DejaVu 같은
    시스템 기본 폰트로 폴백하지 않고 실제 NanumGothic 파일로 붙이는지,
    -loglevel verbose의 "fontselect:" 로그로 직접 확인한다.

    이전 버전의 FONT_BOLD_NAME("NanumGothic ExtraBold", 공백 포함)은 그
    폰트 파일의 이름 테이블에 없는 문자열이라 DejaVu Sans로 조용히
    폴백했다 — 렌더링 자체는 성공하므로 이 로그를 직접 안 보면 못 잡는다.
    이 테스트가 실패하는 조건: 폰트가 시스템에서 삭제됐거나,
    constants.py의 이름이 다시 파일과 안 맞는 문자열로 바뀌는 경우.
    """
    ass_path = tmp_path / "burn_test.ass"
    ass_path.write_text(
        "[Script Info]\nScriptType: v4.00+\nPlayResX: 640\nPlayResY: 360\n\n"
        "[V4+ Styles]\n"
        "Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, "
        "OutlineColour, BackColour, Bold, Italic, Underline, StrikeOut, "
        "ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, "
        "Alignment, MarginL, MarginR, MarginV, Encoding\n"
        f"Style: Bold,{C.FONT_BOLD_NAME},48,&H00FFFFFF,&H000000FF,&H00000000,"
        "&H00000000,0,0,0,0,100,100,0,0,1,0,0,5,0,0,0,1\n"
        f"Style: Reg,{C.FONT_REGULAR_NAME},48,&H00FFFFFF,&H000000FF,&H00000000,"
        "&H00000000,0,0,0,0,100,100,0,0,1,0,0,5,0,0,0,1\n\n"
        "[Events]\n"
        "Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text\n"
        "Dialogue: 0,0:00:00.00,0:00:01.00,Bold,,0,0,0,,BOLD\n"
        "Dialogue: 0,0:00:00.00,0:00:01.00,Reg,,0,0,0,,REG\n",
        encoding="utf-8")

    result = subprocess.run(
        ["ffmpeg", "-hide_banner", "-y", "-f", "lavfi",
         "-i", "color=c=black:s=640x360:r=30:d=1",
         "-vf", f"ass={ass_path}", "-frames:v", "1",
         "-loglevel", "verbose", "-f", "null", "-"],
        capture_output=True, text=True)
    assert result.returncode == 0, result.stderr

    fontselect_lines = [l for l in result.stderr.splitlines() if "fontselect:" in l]
    bold_line = next(l for l in fontselect_lines if f"({C.FONT_BOLD_NAME}," in l)
    reg_line = next(l for l in fontselect_lines if f"({C.FONT_REGULAR_NAME}," in l)

    assert "DejaVu" not in bold_line and re.search(r"NanumGothic[^,]*\.ttf", bold_line)
    assert "DejaVu" not in reg_line and re.search(r"NanumGothic[^,]*\.ttf", reg_line)


def test_frame_span_cumulative_rounding_preserves_total():
    """개별 샷을 초 단위로 각각 반올림하면 33개 합계가 88초에서 어긋난다
    (2638/2640 프레임, task-10-report.md 참고). 누적 반올림이 그 문제를
    실제로 안 일으키는지 두 개의 연속 샷으로 확인한다.
    """
    a = Shot(index=1, beat="t", start=0.0, seconds=1.0167, size="s", subject="x",
             label_ko=None, label_en=None)
    b = Shot(index=2, beat="t", start=1.0167, seconds=1.0167, size="s", subject="x",
             label_ko=None, label_en=None)
    (_, fa), (_, fb) = _frame_span(a), _frame_span(b)
    assert fa + fb == round((a.seconds + b.seconds) * C.FPS)


def test_frame_span_rejects_sub_frame_shot():
    """1프레임 미만 샷은 조용히 1프레임으로 자동 보정하면 88초 총합이
    소리 없이 어긋난다. 발생하면 즉시 터져야 한다(shotlist.py 배분이
    바뀌어 이 경로를 밟게 될 경우를 위한 방어).
    """
    tiny = Shot(index=1, beat="t", start=0.0, seconds=0.01, size="s", subject="x",
                label_ko=None, label_en=None)
    with pytest.raises(ValueError, match="1프레임 미만"):
        _frame_span(tiny)


def test_trim_clip_pads_shot_longer_than_veo_duration(tmp_path):
    """VEO_DURATION(4초)짜리 소스보다 긴 샷(6초)을 요청하면, tpad로 마지막
    프레임을 정지시켜 늘린 뒤 정확히 요청한 프레임 수로 잘라야 한다.
    hanbit.yaml에서는 모든 샷이 정확히 4.0초라 이 분기가 module fixture로는
    전혀 실행되지 않으므로, 합성 소스로 직접 겨냥해서 검증한다.
    """
    src = tmp_path / "src.mp4"
    subprocess.run(["ffmpeg", "-hide_banner", "-loglevel", "error", "-y",
                    "-f", "lavfi", "-i",
                    f"testsrc=size={C.WIDTH}x{C.HEIGHT}:rate={C.FPS}:duration={C.VEO_DURATION}",
                    "-c:v", "libx264", "-pix_fmt", "yuv420p", "-an", str(src)], check=True)

    shot = Shot(index=1, beat="t", start=0.0, seconds=6.0, size="s", subject="x",
                label_ko=None, label_en=None)
    _, expected_frames = _frame_span(shot)
    assert expected_frames == 180  # 6.0s * 30fps, 소스(4초=120프레임)보다 김

    dst = tmp_path / "trimmed.mp4"
    _trim_clip(src, shot, dst)

    probed = subprocess.run(
        ["ffprobe", "-v", "error", "-count_frames",
         "-show_entries", "stream=nb_read_frames", "-of", "csv=p=0", str(dst)],
        capture_output=True, text=True, check=True).stdout.strip()
    assert int(probed) == expected_frames


def test_mux_audio_with_music_path_does_not_crash(tmp_path):
    """음악 믹스 경로([vo] 라벨을 sidechaincompress와 amix 둘 다에 재사용하는
    필터그래프)가 실제로 동작하는지 확인한다. assemble()의 브리프 원안은 asplit
    없이 [vo]를 두 번 참조해서 "Stream specifier 'vo' ... matches no streams"로
    죽었다 — 브리프의 기본 테스트는 music_path를 넘기지 않아 이 경로를 전혀
    실행하지 않으므로 이 버그를 잡지 못했다.

    5초 길이를 쓴다: 2초 이하의 완전 무음을 loudnorm에 먹이면(게이팅 윈도가
    충분한 블록을 못 모아서) libx264와 무관한 별개의 문제로 AAC 인코더가
    "Input contains (near) NaN/+-Inf"를 내며 죽는다. 실제 파이프라인은 항상
    88초(TOTAL_SECONDS)로만 이 함수를 부르므로 실전에서는 나타나지 않는
    현상이라 프로덕션 코드는 건드리지 않고, 테스트만 그 문턱 위로 잡았다.
    """
    video = tmp_path / "video.mp4"
    vo = tmp_path / "vo.wav"
    music = tmp_path / "music.wav"
    subprocess.run(["ffmpeg", "-hide_banner", "-loglevel", "error", "-y",
                    "-f", "lavfi", "-i", "testsrc=size=320x180:rate=30:duration=5",
                    "-c:v", "libx264", "-pix_fmt", "yuv420p", str(video)], check=True)
    subprocess.run(["ffmpeg", "-hide_banner", "-loglevel", "error", "-y",
                    "-f", "lavfi", "-i", "anullsrc=r=48000:cl=mono", "-t", "5",
                    "-c:a", "pcm_s16le", str(vo)], check=True)
    subprocess.run(["ffmpeg", "-hide_banner", "-loglevel", "error", "-y",
                    "-f", "lavfi", "-i", "anullsrc=r=48000:cl=stereo", "-t", "5",
                    "-c:a", "pcm_s16le", str(music)], check=True)

    out = tmp_path / "mixed.mp4"
    _mux_audio(video, vo, out, music_path=music)

    assert out.exists() and out.stat().st_size > 0
    assert _probe(out, "stream=codec_type") == "video\naudio"
