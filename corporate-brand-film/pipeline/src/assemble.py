"""ffmpeg 조립: 클립 트림 → 이어붙이기 → ASS 자막 → 오디오 믹스.

Veo 클립은 4초로 생성되지만 샷 길이는 2~5초라 각 클립을 지정 길이로 맞춘다.
샷이 4초보다 길면 tpad로 마지막 프레임을 정지시켜 늘리고, 짧으면 그냥 자른다
(그냥 -t 로만 자르면 4초를 넘는 샷은 조용히 짧게 끝나서 전체 88초가 깨진다).
클립 오디오는 전부 버린다(-an) — 영어 네이티브 오디오라 쓸 수 없다.
"""
import subprocess
from pathlib import Path

from src import constants as C
from src.shotlist import Shot, Shotlist


def _run(args: list[str]) -> None:
    """ffmpeg 호출 실패 시 stderr를 그대로 실어서 다시 던진다.

    subprocess.CalledProcessError의 기본 메시지는 "returned non-zero exit
    status N"뿐이라 어느 필터/입력이 문제인지 알 수 없다. 이 체인에서만
    ffmpeg를 36번 부르는데, 실제 유료 실행(Veo 클립 생성 후 조립 단계)에서
    여기가 실패하면 stderr 없이는 원인을 못 찾는다.
    """
    try:
        subprocess.run(args, check=True, capture_output=True)
    except subprocess.CalledProcessError as e:
        stderr = e.stderr.decode(errors="replace") if e.stderr else ""
        raise RuntimeError(
            f"ffmpeg 실패 ({' '.join(args[:6])}...): {stderr[-2000:]}"
        ) from e


def _frame_span(shot: Shot) -> tuple[int, int]:
    """샷의 (시작프레임, 프레임수).

    shot.seconds 를 샷마다 개별적으로 반올림해 프레임 수를 정하면, 33개 샷을
    합쳤을 때 반올림 오차가 누적돼 2640프레임(88초*30fps)에서 몇 프레임이
    빈다. 대신 누적 시각(shot.start, shot.start+shot.seconds)을 각각
    반올림한 뒤 차를 취하면(누적 반올림) 샷들이 연속이라는 전제 하에 전체
    합이 정확히 보존된다.
    """
    start_f = round(shot.start * C.FPS)
    end_f = round((shot.start + shot.seconds) * C.FPS)
    frames = end_f - start_f
    if frames < 1:
        raise ValueError(
            f"샷 #{shot.index}({shot.beat})의 길이가 1프레임 미만입니다 "
            f"({shot.seconds:.3f}초). 1프레임으로 자동 보정하면 88초/"
            f"{C.TOTAL_CUTS}컷 총합이 조용히 어긋나므로, shotlist.py의 배분을 "
            f"확인하세요."
        )
    return start_f, frames


def _trim_clip(src: Path | None, shot: Shot, dst: Path) -> None:
    """소스 클립을 샷 길이(프레임 단위)에 정확히 맞춰 dst에 쓴다.

    소스가 없으면(차단된 샷) 검정 화면으로 채운다.
    """
    _, frames = _frame_span(shot)
    trim = f"trim=start_frame=0:end_frame={frames},setpts=PTS-STARTPTS"

    if src is None or not src.exists():
        _run(["ffmpeg", "-hide_banner", "-loglevel", "error", "-y",
              "-f", "lavfi", "-i",
              f"color=c=black:s={C.WIDTH}x{C.HEIGHT}:r={C.FPS}:d={shot.seconds + 1.0:.3f}",
              "-vf", f"setsar=1,{trim}",
              "-c:v", "libx264", "-pix_fmt", "yuv420p", "-an", str(dst)])
        return

    # VEO_DURATION(4초)보다 긴 샷을 위한 안전 여유. tpad는 소스가 이미 충분히
    # 길면 그냥 아무 효과가 없고(뒤에서 trim이 잘라낸다), 짧으면 마지막 프레임을
    # 이만큼 더 복제해서 늘린다.
    tail_pad = max(shot.seconds - C.VEO_DURATION, 0.0) + 1.0
    vf = (f"scale={C.WIDTH}:{C.HEIGHT}:force_original_aspect_ratio=increase,"
          f"crop={C.WIDTH}:{C.HEIGHT},fps={C.FPS},setsar=1,"
          f"tpad=stop_mode=clone:stop_duration={tail_pad:.3f},{trim}")
    _run(["ffmpeg", "-hide_banner", "-loglevel", "error", "-y", "-i", str(src),
          "-vf", vf,
          "-c:v", "libx264", "-preset", "medium", "-crf", "18",
          "-pix_fmt", "yuv420p", "-an", str(dst)])


def assemble(shotlist: Shotlist, clips_dir: Path, ass_text: str, voice_path: Path,
             out_path: Path, music_path: Path | None = None,
             work_dir: Path | None = None) -> Path:
    clips_dir, out_path = Path(clips_dir), Path(out_path)
    work = Path(work_dir or out_path.parent / "_work")
    work.mkdir(parents=True, exist_ok=True)
    out_path.parent.mkdir(parents=True, exist_ok=True)

    # 1) 각 클립을 샷 길이로 트림 + 규격 통일
    trimmed: list[Path] = []
    for shot in shotlist.shots:
        src = clips_dir / f"{shot.index:02d}.mp4"
        dst = work / f"t{shot.index:02d}.mp4"
        _trim_clip(src if src.exists() else None, shot, dst)
        trimmed.append(dst)

    # 2) 이어붙이기
    listfile = work / "concat.txt"
    listfile.write_text("".join(f"file '{p.resolve()}'\n" for p in trimmed), encoding="utf-8")
    video = work / "video.mp4"
    _run(["ffmpeg", "-hide_banner", "-loglevel", "error", "-y",
          "-f", "concat", "-safe", "0", "-i", str(listfile),
          "-c:v", "libx264", "-preset", "medium", "-crf", "18",
          "-pix_fmt", "yuv420p", "-an", str(video)])

    # 3) ASS 자막 굽기
    ass_path = work / "overlay.ass"
    ass_path.write_text(ass_text, encoding="utf-8")
    subbed = work / "subbed.mp4"
    _run(["ffmpeg", "-hide_banner", "-loglevel", "error", "-y", "-i", str(video),
          "-vf", f"ass={ass_path.resolve()}",
          "-c:v", "libx264", "-preset", "medium", "-crf", "18",
          "-pix_fmt", "yuv420p", "-an", str(subbed)])

    # 4) 오디오 믹스 — 나레이션 위주, 음악은 나레이션 구간에서 덕킹
    _mux_audio(subbed, voice_path, out_path, music_path)
    return out_path


def _mux_audio(video_path: Path, voice_path: Path, out_path: Path,
                music_path: Path | None = None) -> None:
    """영상(무음)에 나레이션(+선택적 배경음악)을 입혀 out_path에 최종 mp4를 쓴다."""
    args = ["ffmpeg", "-hide_banner", "-loglevel", "error", "-y",
            "-i", str(video_path), "-i", str(voice_path)]
    if music_path:
        args += ["-i", str(music_path)]
        # [vo] 라벨은 sidechaincompress(사이드체인 입력)와 amix(실제 믹스 입력)
        # 두 곳에서 다 필요하다. ffmpeg 필터그래프에서 라벨 하나는 한 번만
        # 소비할 수 있으므로(브리프 원안은 [vo]를 두 번 참조해서
        # "matches no streams"로 즉시 죽는다), asplit으로 복제해서 각각 먹인다.
        # 나레이션과 음악을 각각 정규화한 뒤 고정 간격으로 섞는다.
        #
        # 예전에는 섞은 뒤 전체에 loudnorm 을 걸었는데, loudnorm 은 동적
        # 정규화라 음악만 나오는 구간(콜드오픈 14초)에서 음악을 말소리 크기까지
        # 끌어올린다 — 실측에서 콜드오픈 음악이 -17.4dB 로 나레이션(-21.7dB)보다
        # 커졌고, 나레이션 구간에서도 게인이 출렁이며 클로징이 배경음에 묻혀
        # whisper 가 전사에 실패했다.
        #
        # 대신 나레이션을 -18 LUFS 로, 음악을 -30 LUFS 로 각각 맞춘다. 12dB
        # 간격은 나레이션 위에 음악을 까는 표준적인 배치다. 섞은 뒤에는 동적
        # 정규화 없이 리미터만 걸어 피크만 잡는다 — 그래야 구간마다 균형이
        # 흔들리지 않는다.
        filt = ("[1:a]aformat=sample_fmts=fltp:sample_rates=48000:channel_layouts=stereo,"
                "loudnorm=I=-16:TP=-3:LRA=7,asplit=2[vo1][vo2];"
                "[2:a]aformat=sample_fmts=fltp:sample_rates=48000:channel_layouts=stereo,"
                # TP 의 허용 범위는 -9~0 이다. 음악은 I(적분 라우드니스)로
                # 눌러야 하므로 TP 는 하한에 둔다.
                "loudnorm=I=-28:TP=-9:LRA=5[bg];"
                "[bg][vo1]sidechaincompress=threshold=0.03:ratio=8:attack=20:release=400[duck];"
                # normalize=0 이 필수다. amix 는 기본값(normalize=1)에서 입력을
                # 1/n 로 줄이므로, 2입력이면 양쪽이 -6dB 깎여 완성본이 통째로
                # 조용해진다(실측 통합 라우드니스 -28 LUFS).
                "[duck][vo2]amix=inputs=2:duration=first:dropout_transition=0:normalize=0,"
                "alimiter=limit=0.95[aout]")
        args += ["-filter_complex", filt, "-map", "0:v", "-map", "[aout]"]
    else:
        args += ["-filter_complex",
                 "[1:a]aformat=sample_fmts=fltp:sample_rates=48000:channel_layouts=stereo,"
                 "loudnorm=I=-16:TP=-1.5:LRA=11[aout]",
                 "-map", "0:v", "-map", "[aout]"]
    # -shortest 를 쓰면 안 된다: 나레이션 트랙이 88초여도 마지막 무음이 잘려
    # 영상보다 짧아지면 출력이 통째로 잘린다. 길이는 -t 로만 고정한다.
    args += ["-c:v", "copy", "-c:a", "aac", "-b:a", "192k",
             "-t", str(C.TOTAL_SECONDS), str(out_path)]
    _run(args)
