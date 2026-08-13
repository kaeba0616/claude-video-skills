"""나레이션 대본 → 음성 파일."""
import hashlib
import subprocess
from pathlib import Path

from src import constants as C
from src.beats import Beatsheet
from src.providers.base import TTSProvider
from src.script import Script


def narration_text(script: Script) -> str:
    """대본에서 나레이션 대사만 추출해서 개행으로 연결한다."""
    return "\n".join(line for act in script.acts if act.narrated for line in act.lines)


def synthesize_narration(script: Script, provider: TTSProvider,
                         out_path: Path, voice: str = C.TTS_VOICE) -> Path:
    """나레이션 대본을 음성 파일로 합성한다 (전체 대본 미리듣기용, 타임라인 미정렬)."""
    out_path = Path(out_path)
    provider.synthesize(narration_text(script), out_path, voice)
    return out_path


def narration_segments(bs: Beatsheet) -> list[tuple[float, str]]:
    """(막 시작 초, 그 막의 나레이션) — 무나레이션 막은 빠진다.

    줄은 공백으로 잇는다. 개행으로 이으면 TTS 가 짧은 마지막 문장을 통째로
    흘리는 일이 있다 — 실측에서 클로징 막의 "한빛소재."(4자)가 음성에서
    사라졌다. 자막은 beat.lines 를 따로 쓰므로(overlay.py) 화면의 줄바꿈은
    그대로 유지되고, 여기서 바뀌는 건 TTS 에 넘기는 문자열뿐이다.
    """
    return [(b.start, " ".join(b.lines))
            for b in bs.beats if b.narrated and b.lines]


def _stamp_path(seg: Path) -> Path:
    return seg.with_suffix(".txt")


def _fingerprint(text: str, voice: str) -> str:
    return hashlib.sha256(f"{voice}\x00{text}".encode()).hexdigest()[:32]


def _write_stamp(seg: Path, text: str, voice: str) -> None:
    _stamp_path(seg).write_text(_fingerprint(text, voice), encoding="utf-8")


def _stamp_matches(seg: Path, text: str, voice: str) -> bool:
    """이 세그먼트 파일이 지금의 대사·음성으로 합성된 것인가.

    이어받기는 파일이 있으면 건너뛴다. 그런데 brief 의 나레이션 한 줄만
    고치고 같은 --out 에서 다시 돌리면, 막 개수와 위치는 그대로라 파일 이름도
    그대로다 — 옛 음성이 조용히 재사용되고 그 위에 새 자막이 얹힌다. 말과 자막이
    서로 다른 문장을 말하는데 길이는 맞아서 아무 에러도 안 난다.
    (브랜치 리뷰 Important 1)

    그래서 합성할 때 (음성 이름 + 대사)의 해시를 옆에 적어두고, 건너뛰기 전에
    지금 값과 대조한다. 지문이 없거나 다르면 다시 합성한다 — 과금되지만,
    말과 자막이 어긋난 완성본을 내보내는 것보다 낫다.
    """
    stamp = _stamp_path(seg)
    if not stamp.exists():
        return False
    return stamp.read_text(encoding="utf-8").strip() == _fingerprint(text, voice)


def narration_audio_seconds(bs: Beatsheet, work_dir: Path) -> dict[str, float]:
    """{막 이름: 그 막 나레이션 음성의 실제 길이(초)}.

    build_narration_track 이 만든 세그먼트 파일을 그대로 잰다. 막의 예약 길이는
    안전 여유를 포함하므로 실제 음성보다 길다 — overlay.py 가 자막을 예약 길이에
    깔면 음성이 끝난 뒤에도 자막이 남아 최대 2.6초까지 밀린다. 그래서 자막은
    이 실측값을 기준으로 분배해야 한다.
    """
    work = Path(work_dir)
    out: dict[str, float] = {}
    for i, (start, _) in enumerate(narration_segments(bs)):
        p = work / f"seg{i:02d}.wav"
        if not p.exists():
            continue
        beat = next((b for b in bs.beats if abs(b.start - start) < 0.01), None)
        if beat is not None:
            out[beat.name] = _duration(p)
    return out


def build_narration_track(bs: Beatsheet, provider: TTSProvider, out_path: Path,
                          voice: str = C.TTS_VOICE,
                          work_dir: Path | None = None) -> Path:
    """막별 TTS를 각 막 시작 시각에 배치한 TOTAL_SECONDS 길이의 단일 wav.

    자막(overlay.py)이 beat.start 기준으로 깔리므로 음성도 같은 격자에 맞춘다.
    이어붙이기가 아니라 배치라서, 무나레이션 막은 자동으로 무음이 된다.
    """
    out_path = Path(out_path)
    work = Path(work_dir or out_path.parent / "_vo")
    work.mkdir(parents=True, exist_ok=True)
    out_path.parent.mkdir(parents=True, exist_ok=True)

    segments = narration_segments(bs)
    if not segments:
        raise ValueError("나레이션 막이 하나도 없습니다. script.py 의 narrated 플래그를 확인하세요.")

    # generate.py와 동일한 이어받기 규율: .tmp 서브디렉토리에 쓰고 성공했을
    # 때만 원자적으로 최종 이름으로 옮긴다. 확장자(.wav)는 유지한다 —
    # FakeTTSProvider(ffmpeg)가 출력 포맷을 파일 확장자로 추론하므로,
    # ".wav.tmp" 같은 접미사를 쓰면 ffmpeg가 포맷을 못 정해 실패한다.
    tmp_dir = work / ".tmp"
    paths: list[Path] = []
    for i, (start, text) in enumerate(segments):
        p = work / f"seg{i:02d}.wav"
        paths.append(p)
        if p.exists() and p.stat().st_size > 0 and _stamp_matches(p, text, voice):
            # 이전 실행에서 같은 대사·같은 음성으로 이미 합성됐다 —
            # generate.py가 이미 만들어진 클립을 건너뛰는 것과 같은 이어받기
            # 원칙. 다시 부르면(TTS는 매 호출이 과금이다) 이미 낸 돈을 또 낸다.
            continue
        tmp_dir.mkdir(parents=True, exist_ok=True)
        partial = tmp_dir / f"seg{i:02d}.wav"
        try:
            provider.synthesize(text, partial, voice)
            partial.replace(p)
            _write_stamp(p, text, voice)
        finally:
            # 중간에 실패해도(네트워크 오류 등) 최종 경로엔 잘린 파일이
            # 남지 않으므로, 다음 실행의 exists() 건너뛰기가 깨진 세그먼트를
            # 완료로 오인해 통과시키지 않는다.
            partial.unlink(missing_ok=True)

    # 세그먼트가 다음 나레이션 막을 침범하면 조립 전에 막는다.
    starts = [s for s, _ in segments]
    limits = starts[1:] + [float(C.TOTAL_SECONDS)]
    for (start, _), p, limit in zip(segments, paths, limits):
        dur = _duration(p)
        if start + dur > limit + 0.5:
            raise ValueError(
                f"나레이션이 막 길이를 넘습니다: {start:.1f}s 시작 세그먼트가 "
                f"{dur:.1f}초라 {limit:.1f}s 를 침범합니다. "
                f"해당 막의 대본을 줄이거나 beats.py 의 배분을 조정하세요.")

    args = ["ffmpeg", "-hide_banner", "-loglevel", "error", "-y"]
    for p in paths:
        args += ["-i", str(p)]
    chains = [f"[{i}:a]aformat=sample_fmts=s16:sample_rates=48000:channel_layouts=mono,"
              f"adelay={int(round(start * 1000))}:all=1[s{i}]"
              for i, (start, _) in enumerate(segments)]
    mix = "".join(f"[s{i}]" for i in range(len(segments)))
    filt = (";".join(chains) + ";" + mix
            + f"amix=inputs={len(segments)}:duration=longest:normalize=0,"
              f"apad,atrim=0:{C.TOTAL_SECONDS}[aout]")
    args += ["-filter_complex", filt, "-map", "[aout]",
             "-c:a", "pcm_s16le", str(out_path)]
    subprocess.run(args, check=True, capture_output=True)
    return out_path


def _duration(path: Path) -> float:
    return float(subprocess.run(
        ["ffprobe", "-v", "error", "-show_entries", "format=duration",
         "-of", "csv=p=0", str(path)],
        capture_output=True, text=True, check=True).stdout.strip())
