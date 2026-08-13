# 영상 제작 스킬 공통 진입점 — PowerShell 판.
#
#   video.ps1 brand-film <command>    88초 기업 홍보영상 (16:9)
#   video.ps1 shorts     <command>    세로형 쇼츠·광고 (9:16)
#
# bash 판(bin/video)과 동작을 맞춘다. 콜론 별칭(video:brand-film)은 Windows
# 파일명에 콜론을 쓸 수 없어 지원하지 않는다 — 서브커맨드 형태만 쓴다.
#
# brand-film 은 브리프 YAML 하나로 전 구간을 코드가 돌리므로 python -m src.cli
# 로 위임하고, 돈이 나가는 명령(testshot·produce·redo)의 확인 절차만 여기서
# 강제한다. shorts 는 클립 생성과 조립 두 단계만 코드가 맡는다.
[CmdletBinding()]
param(
    [Parameter(Position = 0)][string]$Kind,
    [Parameter(Position = 1, ValueFromRemainingArguments = $true)][string[]]$Rest
)
$ErrorActionPreference = 'Stop'

$Skills = if ($env:CLAUDE_SKILLS) { $env:CLAUDE_SKILLS } else { Join-Path $env:USERPROFILE '.claude\skills' }

function Write-Err([string]$m)  { Write-Host $m -ForegroundColor Red }
function Write-C([string]$m)    { Write-Host $m -ForegroundColor Cyan }
function Write-Warn2([string]$m){ Write-Host $m -ForegroundColor Yellow }

function Find-Python {
    # WindowsApps 의 스토어 스텁(실행하면 스토어가 열리는 가짜 python)을 걸러낸다.
    foreach ($name in 'python', 'python3', 'py') {
        $cmd = Get-Command $name -ErrorAction SilentlyContinue
        if ($cmd -and $cmd.Source -notmatch 'WindowsApps') { return $cmd.Source }
    }
    return $null
}

function Confirm-Cost([string]$question, [string]$word) {
    Write-Warn2 $question
    $ans = Read-Host "계속하려면 '$word' 를 입력하세요"
    if ($ans -ne $word) { Write-Err '취소했습니다.'; exit 1 }
}

function Show-Usage {
@'
사용법: video.ps1 <타입> <command> [인자...]

  brand-film    88초 기업 홍보영상 (16:9) — 브리프 YAML 하나로 전 구간 자동
  shorts        세로형 쇼츠·광고 (9:16)  — 대본은 대화로, 클립·조립은 명령으로

타입별 명령을 보려면:
  video.ps1 brand-film
  video.ps1 shorts
'@ | Write-Host
}

# ---------------------------------------------------------------- brand-film
function Invoke-BrandFilm([string[]]$Args2) {
    $dir = Join-Path $Skills 'corporate-brand-film'
    if (-not (Test-Path $dir)) {
        Write-Err "스킬이 없습니다: $dir"
        Write-Err "설치: Copy-Item -Recurse corporate-brand-film $Skills\"
        exit 1
    }
    $pipe = Join-Path $dir 'pipeline'
    if (-not (Test-Path $pipe)) { $pipe = $dir }

    $brief    = if ($env:VIDEO_BRIEF)    { $env:VIDEO_BRIEF }    else { 'brief/hanbit.yaml' }
    $out      = if ($env:VIDEO_OUT)      { $env:VIDEO_OUT }      else { 'build' }
    $provider = if ($env:VIDEO_PROVIDER) { $env:VIDEO_PROVIDER } else { 'bizrouter' }
    $ratePerSec = 152; $shots = 33; $secPerShot = 4   # bash 판과 같은 단가표

    $py = Find-Python
    if (-not $py) { Write-Err 'python 이 없습니다. https://www.python.org/downloads/ 또는: winget install Python.Python.3.12'; exit 1 }

    function Invoke-Cli { param([string[]]$CliArgs)
        Push-Location $pipe
        try { & $py -m src.cli @CliArgs; return $LASTEXITCODE } finally { Pop-Location }
    }
    function Test-Key {
        $var = if ($provider -eq 'gemini') { 'GEMINI_API_KEY' } else { 'BIZROUTER_API_KEY' }
        if (-not (Get-Item "env:$var" -ErrorAction SilentlyContinue).Value) {
            Write-Err "$var 환경변수가 필요합니다.";  Write-Err "  `$env:$var = '...'"
            exit 1
        }
    }

    $cmd  = if ($Args2.Count -ge 1) { $Args2[0] } else { '' }
    $rest = if ($Args2.Count -ge 2) { $Args2[1..($Args2.Count - 1)] } else { @() }

    switch ($cmd) {
        'prepare' {
            $ok = $true
            foreach ($c in 'ffmpeg', 'ffprobe') {
                if (Get-Command $c -ErrorAction SilentlyContinue) { Write-Host "  ✓ $c" -ForegroundColor Green }
                else { Write-Err "  ✗ $c 없음 — winget install Gyan.FFmpeg (설치 후 새 터미널)"; $ok = $false }
            }
            Write-Host "  ✓ python $((& $py --version) -replace 'Python ','')" -ForegroundColor Green
            foreach ($mod in 'yaml', 'requests') {
                & $py -c "import $mod" 2>$null
                if ($LASTEXITCODE -eq 0) { Write-Host "  ✓ $mod" -ForegroundColor Green }
                else { Write-Err "  ✗ $mod 없음 — pip install -e '$pipe'"; $ok = $false }
            }
            if (Test-Path "$env:WINDIR\Fonts\NanumGothic*.ttf") { Write-Host '  ✓ NanumGothic' -ForegroundColor Green }
            else { Write-Warn2 '  - NanumGothic 없음 — 자막이 Malgun Gothic 으로 렌더링됩니다 (설치 권장: https://hangeul.naver.com/fonts)' }
            if ($env:BIZROUTER_API_KEY -or $env:GEMINI_API_KEY) { Write-Host '  ✓ API 키 설정됨' -ForegroundColor Green }
            else { Write-Warn2 '  - API 키 없음 — preview(무료)만 돌릴 수 있습니다' }
            if (-not $ok) { exit 1 }
            Write-C '준비 완료.'
        }
        'new' {
            $name = if ($rest.Count -ge 1) { [IO.Path]::GetFileNameWithoutExtension($rest[0]) } else { '' }
            if (-not $name) { Write-Err '이름을 주세요:  video.ps1 brand-film new 우리회사'; exit 1 }
            $dst = Join-Path $pipe "brief\$name.yaml"
            if (Test-Path $dst) { Write-Err "이미 있습니다: $dst"; exit 1 }
            Copy-Item (Join-Path $pipe 'brief\template.yaml') $dst
            Write-C "생성: brief/$name.yaml"
            Write-Host '회사 정보로 채운 뒤:'
            Write-Host "  `$env:VIDEO_BRIEF = 'brief/$name.yaml'; video.ps1 brand-film preview"
        }
        'script' {
            foreach ($s in 'script', 'beats', 'shots', 'prompts') {
                if ((Invoke-Cli @($s, '--brief', $brief, '--out', $out)) -ne 0) { exit 1 }
            }
            Write-C "생성 완료: $out/scenario.md · beatsheet.json · shotlist.json · prompts/"
        }
        'preview' {
            Write-C 'Fake 로 전 구간 실행합니다 (무료, 약 5분)'
            if ((Invoke-Cli @('all', '--brief', $brief, '--out', $out, '--fake')) -ne 0) { exit 1 }
            Write-C "완성: $out/final.mp4  — 배경은 컬러바지만 편집·자막·타이밍은 실물입니다."
        }
        'testshot' {
            Test-Key
            $cost = $ratePerSec * $secPerShot
            Write-C '먼저 드라이런 — 아무것도 제출하지 않고 배선만 확인합니다.'
            if ((Invoke-Cli @('generate', '--brief', $brief, '--out', $out, '--live', '--provider', $provider, '--limit', '0')) -ne 0) { exit 1 }
            Confirm-Cost "이제 1컷을 실제로 생성합니다. 예상 비용 약 ${cost}원." 'testshot'
            if ((Invoke-Cli @('generate', '--brief', $brief, '--out', $out, '--live', '--provider', $provider, '--limit', '1', '--budget-krw', "$($cost + 100)")) -ne 0) { exit 1 }
            Write-C "완성: $out/clips/01.mp4 — 화질과 톤을 확인하세요."
        }
        'produce' {
            Test-Key
            $total = $ratePerSec * $secPerShot * $shots
            $budget = $total + [int]($total / 10)
            if (-not (Test-Path (Join-Path $pipe "$out\clips\01.mp4"))) {
                Write-Warn2 '1컷 시험을 먼저 하는 것을 권합니다:  video.ps1 brand-film testshot'
                Write-Warn2 '화질을 확인하지 않고 전체를 생성하면, 마음에 안 들 때 전액을 다시 씁니다.'
            }
            Confirm-Cost "전체 ${shots}컷을 생성합니다. 예상 비용 약 ${total}원 (상한 ${budget}원)." 'produce'
            $cliArgs = @('all', '--brief', $brief, '--out', $out, '--live', '--provider', $provider, '--budget-krw', "$budget")
            $music = if ($env:VIDEO_MUSIC) { $env:VIDEO_MUSIC } else { 'assets/bed.wav' }
            if (Test-Path (Join-Path $pipe $music)) { $cliArgs += @('--music', $music) }
            if ((Invoke-Cli $cliArgs) -ne 0) { exit 1 }
            Write-C "완성: $out/final.mp4"
        }
        'redo' {
            if ($rest.Count -eq 0) { Write-Err '컷 번호를 주세요:  video.ps1 brand-film redo 5 13 27'; exit 1 }
            Test-Key
            $clipDir = Join-Path $pipe "$out\clips"
            $keep = Join-Path $pipe "$out\clips_prev"
            New-Item -ItemType Directory -Force $keep | Out-Null
            foreach ($n in $rest) {
                if (-not (Test-Path (Join-Path $clipDir ('{0:d2}.mp4' -f [int]$n)))) { Write-Err "없는 컷: $n"; exit 1 }
            }
            $cost = $ratePerSec * $secPerShot * $rest.Count
            Confirm-Cost "컷 $($rest -join ' ') 를 다시 생성합니다 ($($rest.Count)컷, 예상 약 ${cost}원). 나머지는 건드리지 않습니다." 'redo'
            foreach ($n in $rest) {
                $f = '{0:d2}.mp4' -f [int]$n
                Move-Item (Join-Path $clipDir $f) (Join-Path $keep $f) -Force   # 지우지 않고 옮긴다 — 새 컷이 더 나쁠 수 있다
            }
            if ((Invoke-Cli @('generate', '--brief', $brief, '--out', $out, '--live', '--provider', $provider, '--budget-krw', "$($cost + [int]($cost / 10) + 100)")) -ne 0) { exit 1 }
            Write-C "이전 컷은 $out/clips_prev/ 에 남겨뒀습니다."
            Write-Host '재조립하려면:  video.ps1 brand-film rebuild'
        }
        'rebuild' {
            $music = if ($env:VIDEO_MUSIC) { $env:VIDEO_MUSIC } else { 'assets/bed.wav' }
            $cliArgs = @('assemble', '--brief', $brief, '--out', $out)
            if (Test-Path (Join-Path $pipe $music)) { $cliArgs += @('--music', $music) }
            if ((Invoke-Cli $cliArgs) -ne 0) { exit 1 }
            Write-C "재조립 완료: $out/final.mp4"
        }
        'verify' {
            & $py -c 'import faster_whisper' 2>$null
            $cliArgs = @('verify', '--brief', $brief, '--out', $out)
            if ($LASTEXITCODE -eq 0) { $cliArgs += '--transcribe' }
            if ((Invoke-Cli $cliArgs) -ne 0) { exit 1 }
        }
        'music' {
            Write-Err 'music 은 bash 스크립트(make_bed.sh)입니다 — Git Bash 나 WSL 에서 실행하세요:'
            Write-Err "  bash `"$pipe/scripts/make_bed.sh`" `"$pipe/assets/bed.wav`" 88"
            exit 1
        }
        default {
@'
사용법: video.ps1 brand-film <command> [인자...]

  prepare           실행 환경 점검 (ffmpeg · 폰트 · 의존성)
  new <이름>        브리프 템플릿 복사 → brief/<이름>.yaml
  script            대본·비트·샷리스트·프롬프트만 생성 (영상 없음, 무료)
  preview           Fake 로 88초 완성본 (무료) ← 항상 여기서 시작

  testshot          실제 API 로 1컷만 (약 600원, 확인 받음)
  produce           전체 33컷 생성 + 조립 (약 2만원, 확인 받음)
  redo <번호...>    그 컷만 다시 생성 (컷당 약 600원, 확인 받음)
  rebuild           재조립만 (무료)
  verify            완성본 측정 → 레퍼런스 대역 판정

브리프는 환경변수로 지정한다:
  $env:VIDEO_BRIEF = 'brief/우리회사.yaml'
'@ | Write-Host
            if ($cmd -and $cmd -notin '', '-h', '--help', 'help') { Write-Err "모르는 명령: $cmd"; exit 1 }
        }
    }
}

# -------------------------------------------------------------------- shorts
function Invoke-Shorts([string[]]$Args2) {
    $dir = Join-Path $Skills 'shorts-generate'
    if (-not (Test-Path $dir)) { Write-Err "스킬이 없습니다: $dir"; exit 1 }
    $py = Find-Python
    if (-not $py) { Write-Err 'python 이 없습니다. winget install Python.Python.3.12 (설치 후 새 터미널)'; exit 1 }

    $sub  = if ($Args2.Count -ge 1) { $Args2[0] } else { '' }
    $rest = if ($Args2.Count -ge 2) { $Args2[1..($Args2.Count - 1)] } else { @() }

    switch ($sub) {
        'clip'     { & $py (Join-Path $dir 'scripts\veo_generate.py') @rest; exit $LASTEXITCODE }
        'chain'    { & $py (Join-Path $dir 'scripts\chain.py') @rest;        exit $LASTEXITCODE }
        'assemble' { & $py (Join-Path $dir 'scripts\assemble.py') @rest;     exit $LASTEXITCODE }
        'prepare' {
            $ok = $true
            foreach ($c in 'ffmpeg', 'ffprobe') {
                if (Get-Command $c -ErrorAction SilentlyContinue) { Write-Host "  ✓ $c" -ForegroundColor Green }
                else { Write-Err "  ✗ $c 없음 — winget install Gyan.FFmpeg (설치 후 새 터미널)"; $ok = $false }
            }
            Write-Host "  ✓ python $((& $py --version) -replace 'Python ','')" -ForegroundColor Green
            # Windows 는 Malgun Gothic 이 기본 탑재라 한글 자막이 항상 나온다.
            if (Test-Path "$env:WINDIR\Fonts\NanumGothic*.ttf") { Write-Host '  ✓ NanumGothic' -ForegroundColor Green }
            else { Write-Warn2 '  - NanumGothic 없음 — 자막은 Malgun Gothic 으로 렌더링됩니다' }
            if ($env:BIZROUTER_API_KEY) { Write-Host '  ✓ BIZROUTER_API_KEY' -ForegroundColor Green }
            else { Write-Warn2 '  - BIZROUTER_API_KEY 없음 — 클립 생성 불가' }
            if ($ok) { Write-C '준비 완료.' } else { exit 1 }
        }
        default {
@'
사용법: video.ps1 shorts <command> [인자...]

  prepare                    실행 환경 점검
  clip   <veo_generate 인자>  Veo 클립 1개 생성 (약 600원/8초)
  chain  <chain 인자>         프롬프트 목록 → 체이닝 생성 → 조립 원샷 (확인 받음)
  assemble <assemble 인자>    클립 이어붙이기 + 자막 번인 (무료)

대본과 장면 분해는 명령이 아니라 대화로 한다 — Claude Code 에서
"쇼츠 만들어줘 <주제>" 라고 하면 shorts-generate 스킬이 진행한다.

직접 쓸 때:
  video.ps1 shorts clip --prompt-file prompts\scene_01.txt --out clips\scene_01.mp4 `
      --model google/veo-3.1-lite --resolution 720p --aspect-ratio 9:16 --duration 8
  video.ps1 shorts assemble --clips clips\*.mp4 --out final.mp4 --subtitles subs.json
'@ | Write-Host
            if ($sub -and $sub -notin '', '-h', '--help', 'help') { Write-Err "모르는 명령: $sub"; exit 1 }
        }
    }
}

switch -Regex ($Kind) {
    '^(brand-?film|film)$' { Invoke-BrandFilm $Rest }
    '^(shorts?)$'          { Invoke-Shorts $Rest }
    '^(|-h|--help|help)$'  { Show-Usage }
    default                { Write-Err "모르는 타입: $Kind"; Write-Host ''; Show-Usage; exit 1 }
}
