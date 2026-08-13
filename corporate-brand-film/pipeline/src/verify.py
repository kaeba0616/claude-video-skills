"""완성본을 레퍼런스 분석과 동일한 방법으로 측정하고 합격 대역을 판정한다.

컷 검출: ffmpeg select='gt(scene,0.30)' — reference/ 데이터를 만든 것과 같은 임계값,
    같은 방식(렌더링된 실제 파일에 씬 검출을 돌린다. 샷리스트는 "설계값"이지
    "측정값"이 아니므로 여기서는 참조하지 않는다).
무나레이션 비율: ffmpeg silencedetect — ML 없이 렌더링된 오디오에서 직접 잰다.
발화속도: chars(호출자가 대본에서 센 글자수) ÷ 실측 발화시간(=전체 길이 -
    silencedetect가 잰 무음). 분모가 실제 파일에서 나오므로 상수를 되읽는
    가짜 측정이 아니다.
전사(whisper): faster-whisper large-v3 — 선택적(transcribe=True), 실제 운영
    환경에서 두 지표를 whisper 실측으로 덮어쓴다.

세 군데 함정을 코드로 못박는다:
  1) showinfo 는 -loglevel info 에서만 로그를 찍는다. -loglevel error 로 부르면
     필터 자체는 정상 동작해도 pts_time 로그가 안 보여서 컷이 0개로 측정된다.
     레퍼런스 28편 분석 스크립트가 실제로 이 버그로 한 번 죽었다.
  2) silencedetect 도 마찬가지로 -loglevel info 가 필수다 — 같은 함정.
  3) 발화속도는 whisper가 실제로 인식한 글자수와 실제 발화 구간 길이에서
     계산한다(또는 chars ÷ 실측 발화시간). 대본을 만들 때 쓴
     constants.SPEECH_RATE 를 그대로 돌려주면 세그먼트가 뭐든 항상 같은
     값이 나오는, 아무것도 검증하지 못하는 가짜 측정이 된다.
"""
import re
import subprocess
from dataclasses import dataclass
from pathlib import Path

from src import constants as C


@dataclass
class Metrics:
    duration: float
    cuts: int
    cuts_per_min: float
    avg_shot: float
    speech_rate: float | None = None
    silent_ratio: float | None = None
    # 막마다 (막 이름, 대본 글자수, 실제 발화된 글자수). TTS 가 문장을 통째로
    # 흘리는 사고를 잡는 유일한 장치다 — narration_coverage() 가 채운다.
    coverage: list[tuple[str, int, int]] | None = None

    @property
    def worst_beat(self) -> tuple[str, int, int] | None:
        """발화 누락이 가장 심한 막. 전체 평균이 아니라 최악값으로 판정한다 —
        짧은 막 하나가 통째로 빠져도 전체 비율로는 묻히기 때문이다."""
        if not self.coverage:
            return None
        return min(self.coverage, key=lambda c: c[2] / c[1] if c[1] else 1.0)

    @property
    def heard_ratio(self) -> float | None:
        w = self.worst_beat
        if w is None or not w[1]:
            return None
        return w[2] / w[1]


def _duration(path: Path) -> float:
    out = subprocess.run(
        ["ffprobe", "-v", "error", "-show_entries", "format=duration",
         "-of", "csv=p=0", str(path)],
        capture_output=True, text=True, check=True)
    return float(out.stdout.strip())


def measure_cuts(path: Path, threshold: float = 0.30) -> list[float]:
    """씬 전환 타임코드 목록.

    -loglevel info 를 반드시 써야 한다: showinfo 는 info 레벨에서만 로그를
    찍으므로, 다른 ffmpeg 호출들처럼 -loglevel error 로 통일해버리면 필터는
    동작해도 컷이 조용히 0개로 측정된다.

    ffmpeg 자체가 실패해도(잘린 파일 등, commit 93dafcc에서 실제로 겪은
    문제) pts_time 로그가 하나도 안 잡히면 빈 리스트를 돌려주게 되는데,
    이러면 호출부에서 cuts=1("거대한 정지 샷 하나")과 구분이 안 된다.
    returncode 를 직접 확인해서 툴 실패를 별도 예외로 구분해 던진다.
    """
    out = subprocess.run(
        ["ffmpeg", "-hide_banner", "-loglevel", "info", "-i", str(path),
         "-filter:v", f"select='gt(scene,{threshold})',showinfo", "-f", "null", "-"],
        capture_output=True, text=True)
    if out.returncode != 0:
        raise RuntimeError(
            f"컷 검출 실패 (ffmpeg 종료코드 {out.returncode}, {path}): "
            f"{out.stderr[-2000:]}\n"
            f"이 실패를 '컷 0개(정지 화면)'와 혼동하면 안 됩니다."
        )
    return [float(m) for m in re.findall(r"pts_time:([0-9.]+)", out.stderr)]


def _spoken_stats(segments) -> tuple[float, int]:
    """(실제 발화 구간 합계 초, 실제 인식된 글자수).

    whisper 세그먼트 목록에서 순수 산술만 뽑아낸 함수. ASR 호출 자체(모델
    다운로드·GPU 필요)와 분리해뒀기 때문에, 합성 세그먼트로 "발화속도가
    세그먼트 내용에 따라 실제로 바뀌는지"를 whisper 없이도 검증할 수 있다.
    """
    spoken = sum(s.end - s.start for s in segments)
    chars = len(re.sub(r"[\s,.?!]", "", "".join(s.text for s in segments)))
    return spoken, chars


def measure_silence(path: Path, noise_db: float = -40.0,
                    min_silence: float = 0.30) -> float:
    """무음 구간 합계 초 — silencedetect 로 렌더링된 오디오에서 직접 잰다.

    whisper 없이 무나레이션 비율을 재기 위한 것. showinfo 와 마찬가지로
    silencedetect 도 info 레벨에 로그를 찍으므로 -loglevel info 가 필수다.

    ffmpeg 자체가 실패하면(잘린 파일, 깨진 오디오 코덱 등 — measure_cuts를
    깨뜨렸던 것과 같은 commit-93dafcc 계열 시나리오) silence_duration 로그가
    하나도 안 잡혀 0.0을 돌려주게 되는데, 이건 measure_cuts의 "컷 0개"보다
    더 나쁘다: measure()에서 spoken = dur - 0 = dur 이 되어 speech_rate가
    우연히 PASS_SPEECH_RATE 대역 안에 들어올 수 있다 — 측정이 실패했는데
    judge()가 조용히 True를 돌려주는, 이 검증기에서 최악의 결과다.
    measure_cuts와 마찬가지로 returncode를 확인해서 구분되는 예외로 던진다.

    noise_db 기본값 -40dB는 FakeTTSProvider의 평탄한 220Hz 사인파를 기준으로
    실측 보정한 값이다(loudnorm 통과 후 디지털 무음은 -91dB, 사인파 나레이션은
    -34~-29dB — -40dB는 그 사이에서 10dB 이상 여유를 두고 앉는다). 실제
    TTS(Task 13에서 연결)는 사인파보다 조용한 자음/숨소리 구간을 포함할 수
    있으므로, 실제 TTS 공급자가 붙으면 이 임계값을 다시 검증해야 한다.
    """
    out = subprocess.run(
        ["ffmpeg", "-hide_banner", "-loglevel", "info", "-i", str(path),
         "-af", f"silencedetect=noise={noise_db}dB:d={min_silence}",
         "-f", "null", "-"], capture_output=True, text=True)
    if out.returncode != 0:
        raise RuntimeError(
            f"무나레이션 비율 측정 실패 (ffmpeg 종료코드 {out.returncode}, {path}): "
            f"{out.stderr[-2000:]}\n"
            f"이 실패를 '무음 0초'로 읽으면 안 됩니다 — speech_rate 계산의 "
            f"분모(dur - silent)가 조용히 dur 전체가 되어 판정이 거짓으로 "
            f"통과할 수 있습니다."
        )
    return sum(float(v) for v in re.findall(r"silence_duration: ([0-9.]+)", out.stderr))


def _clean_segments(segments, duration: float) -> list:
    """whisper 의 환각(hallucination) 세그먼트를 걷어낸다.

    whisper 는 파일 끝의 무음 구간에서 직전 문장을 반복 생성하는 실패 모드가
    잘 알려져 있고, 심지어 파일 길이를 넘는 타임스탬프까지 만들어낸다.
    Task 18 실측에서 88.000초 파일에 98.4초까지 세그먼트가 생겼고, 같은 문장이
    12번 반복돼 인식 글자수가 253→465 로 부풀었다(발화속도 4.09→7.25).

    두 가지로 거른다:
      1) 파일 길이를 벗어난 세그먼트는 버리고, 걸친 것은 끝을 잘라낸다.
      2) 연속으로 같은 문장이 반복되면 첫 번째만 남긴다.
    둘 다 "실제로 말한 것"만 세기 위한 것이지, 값을 원하는 쪽으로 옮기려는
    보정이 아니다.
    """
    cleaned, prev_text = [], None
    for s in segments:
        if s.start >= duration:
            break
        text = s.text.strip()
        if text and text == prev_text:
            continue
        prev_text = text
        cleaned.append(_Seg(s.start, min(s.end, duration), s.text))
    return [s for s in cleaned if s.end > s.start]


@dataclass
class _Seg:
    start: float
    end: float
    text: str


def _transcribe(path: Path) -> tuple[float, float, int]:
    """(자/초, 무나레이션 비율, 인식된 글자수). 실제 전사 결과 기반 — 상수를 되읽지 않는다."""
    from faster_whisper import WhisperModel
    model = WhisperModel("large-v3", device="cuda", compute_type="float16")
    segs, _ = model.transcribe(str(path), language="ko", vad_filter=True)
    dur_for_clean = _duration(path)
    segs = _clean_segments(segs, dur_for_clean)
    spoken, chars = _spoken_stats(segs)
    dur = _duration(path)
    rate = chars / spoken if spoken else 0.0
    silent_ratio = (dur - spoken) / dur if dur else 0.0
    return rate, silent_ratio, chars


def narration_coverage(segments: list[tuple[str, str]],
                       work_dir: Path) -> list[tuple[str, int, int]]:
    """막마다 (막 이름, 대본 글자수, 실제 발화된 글자수).

    segments 는 [(막 이름, 그 막에 넘긴 대본)] 이고, work_dir 에는
    build_narration_track 이 만든 seg00.wav... 가 있다. 각 세그먼트를 따로
    전사해 제 대본과 대조한다.

    완성본 전체를 한 덩어리로 비교하면 안 된다: Task 18 에서 사라진
    "한빛소재."(4자)는 대본 253자 대비 1.6% 라 전체 비율로는 보이지 않는다.
    같은 누락이 막 단위로는 16자 중 4자, 즉 25% 여서 곧바로 드러난다.
    사고는 문장 단위로 일어나므로 문장이 속한 막 단위로 재야 한다.
    """
    from faster_whisper import WhisperModel
    model = WhisperModel("large-v3", device="cuda", compute_type="float16")

    out: list[tuple[str, int, int]] = []
    for i, (name, text) in enumerate(segments):
        p = Path(work_dir) / f"seg{i:02d}.wav"
        if not p.exists():
            continue
        # vad_filter 는 끄고 있는 그대로 듣는다 — 여기서 궁금한 건 발화 구간이
        # 아니라 "무엇이 발화됐는가"다.
        segs, _ = model.transcribe(str(p), language="ko", vad_filter=False)
        heard = _norm_chars("".join(s.text for s in segs))
        out.append((name, _norm_chars(text), heard))
    return out


def _norm_chars(s: str) -> int:
    return len(re.sub(r"[\s,.?!]", "", s))


def measure(path: Path, chars: int | None = None, transcribe: bool = False,
            voice_path: Path | None = None) -> Metrics:
    """완성본을 실측한다.

    무나레이션 비율(silent_ratio)은 ML 없이 항상 measure_silence()로 잰다.
    발화속도(speech_rate)는 chars(대본에서 호출자가 센 글자수)가 주어지면
    dur - silent(실측 발화시간)로 나눠서 낸다 — 분모가 렌더링된 실제
    파일에서 나오므로, TTS가 빨라지거나 세그먼트가 잘리면 값이 실제로
    움직인다(상수를 되읽는 가짜 측정이 아니다). transcribe=True 면 whisper
    실측으로 두 값을 덮어쓴다(실제 운영 환경 전용, 이 값이 최종 판정 기준).

    voice_path 를 주면 오디오 두 지표(발화속도·무나레이션)는 최종 믹스가 아니라
    그 나레이션 트랙에서 잰다. 두 지표는 "나레이션이 얼마나 촘촘한가"를 묻는
    것인데, 배경음이 깔린 믹스에서 재면 음악이 계측을 교란한다 — 실측에서
    같은 나레이션에 배경음만 추가했더니 발화속도가 4.48→4.87, 무나레이션이
    35.6%→37.7% 로 움직였다(silencedetect 는 음악을 소리로 세고, whisper 는
    음악 때문에 음성 구간 경계를 좁힌다). 영상 지표(컷/분·평균 샷)는 언제나
    최종 완성본에서 잰다.
    """
    path = Path(path)
    dur = _duration(path)
    cuts = len(measure_cuts(path)) + 1
    m = Metrics(duration=dur, cuts=cuts,
                cuts_per_min=cuts / (dur / 60), avg_shot=dur / cuts)

    audio_src = Path(voice_path) if voice_path else path
    audio_dur = _duration(audio_src)
    silent = measure_silence(audio_src)
    m.silent_ratio = silent / audio_dur if audio_dur else None
    if chars is not None:
        spoken = audio_dur - silent
        m.speech_rate = chars / spoken if spoken > 0 else None
    if transcribe:
        m.speech_rate, m.silent_ratio, _ = _transcribe(audio_src)
    return m


def judge(m: Metrics) -> dict[str, bool]:
    def band(v, rng):
        return v is not None and rng[0] <= v <= rng[1]
    result = {
        "cuts_per_min": band(m.cuts_per_min, C.PASS_CUTS_PER_MIN),
        "avg_shot": band(m.avg_shot, C.PASS_AVG_SHOT),
    }
    if m.speech_rate is not None:
        result["speech_rate"] = band(m.speech_rate, C.PASS_SPEECH_RATE)
    if m.silent_ratio is not None:
        result["silent_ratio"] = band(m.silent_ratio, C.PASS_SILENT_RATIO)
    if m.heard_ratio is not None:
        # 상한은 두지 않는다 — 전사가 대본보다 길게 나오는 건 ASR 이 조사나
        # 어미를 다르게 적은 것이지 사고가 아니다. 잡아야 할 건 누락뿐이다.
        result["heard_ratio"] = m.heard_ratio >= C.PASS_HEARD_RATIO
    return result
