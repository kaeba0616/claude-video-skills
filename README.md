# claude-video-skills

한국어 영상을 만드는 [Claude Code](https://claude.com/claude-code) 스킬 모음.
대본부터 완성 mp4까지 한 번에 간다 — 시나리오, AI 영상 클립(Veo 3.1), TTS 나레이션,
한글 자막·타이포, 배경음, 조립.

> Claude Code skills for producing Korean-language video: a 88-second corporate
> brand film (16:9) and vertical shorts / product ads (9:16). Korean-only docs.

| 스킬 | 만드는 것 | 비용 |
|---|---|---|
| [`corporate-brand-film`](corporate-brand-film/) | 88초 기업 홍보영상 · 33컷 · 1920×1080 | 1편 약 2만원 |
| [`shorts-generate`](shorts-generate/) | 세로형 쇼츠·제품 광고 · 8초 클립 3개 | 1편 약 1,800~3,700원 |

**돈이 나가는 파이프라인이다.** 두 스킬 다 기본이 무료 모드이고, 실제 API 호출은
명시적으로 켜야 한다. 승인 게이트와 예산 상한을 반드시 거치도록 설계돼 있으니
SKILL.md 의 순서를 건너뛰지 마라 — 각 단계는 실제로 돈을 날린 사고에서 나왔다.

## 설치

```bash
# 개인 스킬로 (어느 프로젝트에서든 쓴다)
cp -r corporate-brand-film shorts-generate ~/.claude/skills/

# 또는 저장소 스킬로 (팀과 공유)
cp -r corporate-brand-film <프로젝트>/.claude/skills/
```

필요한 것: `ffmpeg`, 나눔고딕(Regular + ExtraBold), Python 3.12+, `pyyaml`, `requests`.
영상 생성에는 [bizrouter](https://bizrouter.ai) 또는 Google Gemini API 키.

```bash
~/.claude/skills/corporate-brand-film/scripts/setup.sh   # 환경 점검
```

## 쓰는 법

Claude Code 에서 그냥 말하면 스킬이 걸린다.

> 우리 회사 홍보영상 만들어줘. 이차전지 소재 회사고 1998년 창업했어.

> 이 제품으로 쇼츠 광고 만들어줘 (제품 사진 첨부)

직접 돌리려면 공통 진입점을 PATH 에 둔다.

```bash
cp bin/video ~/.local/bin/ && chmod +x ~/.local/bin/video
ln -sf video ~/.local/bin/video:brand-film
ln -sf video ~/.local/bin/video:shorts
```

```bash
video brand-film prepare      # 환경 점검
video brand-film new 우리회사  # 브리프 템플릿 → 채운다
video brand-film preview      # 무료로 88초 완성본 (컬러바 배경, 편집은 실물)
video brand-film produce      # 실제 생성 (확인 문구를 받는다)

video shorts assemble ...     # 세로형 쇼츠 조립
video:brand-film verify       # 콜론 형태도 된다
```

디스패처는 `bin/video` 와 `corporate-brand-film/bin/video` 두 군데 있는데 같은 파일이다 —
저장소를 통째로 받으면 앞의 것을, 스킬 하나만 떼어가면 뒤의 것을 쓴다.

## corporate-brand-film

실제 한국 기업 홍보영상 **28편**(총 117분)을 전사·컷분석해서 뽑은 문법을 따른다 —
5막 구조, 전환 장치의 위치, 컷 리듬 대역, 타이포 3계층. 브리프 YAML 한 파일에
회사명·업종·창업연도·숫자 앵커·사업부 2개를 적으면 나머지가 전부 생성된다.

```
0-14초   콜드오픈 (나레이션 없음, 음악만)
14-18초  타이틀 카드
18-30초  정의 — 회사가 무엇인지
30-43초  근거 — 창업연도 + 숫자 앵커
43-50초  전환 — "하지만 우리는 멈추지 않았습니다"
50-73초  사업부 2개 챕터
73-82초  클라이맥스
82-88초  클로징 + 로고 엔드카드
```

완성본은 레퍼런스 28편과 같은 계측기로 재서 같은 대역에 넣고 판정한다 —
컷/분, 평균 샷 길이, 발화속도, 무나레이션 비율, 대본 대비 실제 발화.
대역의 근거와 산출 과정은 [`references/reference-analysis.md`](corporate-brand-film/references/reference-analysis.md) 에 있다.

파이프라인 코드와 215개 테스트가 `pipeline/` 아래 함께 들어 있다.

## shorts-generate

주제나 제품 사진을 받아 세로 쇼츠를 만든다. 대본은 LLM 이 쓰고 클립 생성과 자막
합성은 스크립트가 한다. 제품 사진을 참조 이미지로 넘기면 실제 제품이 등장하는
광고가 된다(`veo-3.1-fast` 이상 필요 — lite 는 참조 이미지를 안 받는다).

[`references/veo-pitfalls.md`](shorts-generate/references/veo-pitfalls.md) 에 실측으로 확인한 Veo 함정이 정리돼 있다.
특히 **자르기·부수기 같은 인과 동작은 Veo 가 순서를 어긋나게 그린다** — 프롬프트를
결과에서 시작하도록 다시 쓰면 해결된다. 프롬프트를 손대기 전에 읽어라.

## 라이선스

파이프라인·스크립트 코드는 [MIT](LICENSE). 자유롭게 쓰고 고쳐도 된다.

`corporate-brand-film/references/reference-analysis.md` 는 공개된 기업 홍보영상들을
분석·비평한 결과물이다. 분석 대상 영상의 저작권은 각 제작사와 클라이언트에 있으며,
이 저장소에는 영상·전사 원문·프레임이 포함돼 있지 않다.
