# QWQ AI Trader - CLAUDE.md

> **이 머리글은 현재 상태만 적는다(V09, 2026-11-16).** 작업을 마치면 문단을 새로 쌓지 말고 아래 해당 줄을 고쳐 쓴다. 날짜별 이력은 [머리글 이력 보관](docs/operations/claude-md-header-history.md)(2026-09-16~11-16 원문 그대로), `CHANGELOG.md`, [실행 원장](docs/operations/execution-roadmap.md)에 있다.

## 현재 상태 (2026-11-16 기준)

**목적.** 종목 선정과 진입 시점의 강점을 실제 계좌의 비용 차감 수익으로 연결하고, KODEX200 대비 초과수익으로 판정한다. 계측·감사는 이 판단에 필요한 최소 범위만 한다.

**거래 상태.**
- KR 신규 매수 중지(`KILL_SWITCH_KR`, 2026-09-30~). 청산·손절은 허용되고, 해제는 사용자만 한다.
- 현금 0.5%·보유 1종목(`manual`·자동매도 금지, 자산의 99.5%). 매수 중지를 풀어도 운용할 자본이 없고, 10/7 이후 신규 거래 표본은 0이다.
- 미국 거래는 영구 중단(2026-09-28)이며 운영은 `--market kr`이다.
- 지금 봇의 역할은 관측·기록이다: 보유 청산 관리, 장중 후보 원장, 팀 심의(shadow), 20:30 초과수익 원장, 정기 보고.

**평가 결론([11/13 정본](docs/reviews/engine-evaluation-2026-11-13.md)).** 실거래 253건이 KODEX200 대비 포지션당 −1.62%(t −3.89)이고 양수인 전략이 없다. 손실은 보유 0~1일·손절 단독에 몰려 있다. 그래서 근거 생산을 실거래에서 후보 단위 사후 초과수익으로 옮기는 것이 1순위다.

**과제 V01~V12** — 정본은 [원장](docs/operations/execution-roadmap.md#engine-evaluation-20261113).

| 과제 | 상태 |
| --- | --- |
| V01 기존 후보 판독 | DONE(11/13). 배치 SEPA 후보 1,408건·장중 신호 2,078 종목-일 모두 사전 등록 기준 미입증. 초과수익이 지수 방향에 좌우돼 V02는 같은 날 후보 간 비교를 주 지표로 둔다. [결과](docs/research/v01-candidate-forward-excess-2026-11-13.md) |
| V02 후보 전수 기록 | 기록 중(운영 11/13 21:11~, 끄려면 `CANDIDATE_LEDGER=0`). 창 1은 V06 배포로 11/17부터 다시 센다(11/16 원장 파일은 보존). 판독은 최소 15거래일·300종목-일 뒤 `scripts/research/candidate_ledger_readout.py`로 한다. [설계·판독 사전 등록](docs/research/v02-candidate-ledger-2026-11-13.md) |
| V03 재개 기준 | DONE(11/15). 사용자 결정: D1 수용, D2 당분간 (c) 자본 없이 관측만, D3 −10%. (c)인 동안 canary는 열지 않고 V02 판독은 경로 종료 여부만 가른다. [사전 등록](docs/research/v03-resume-criteria-2026-11-15.md) |
| V04 진입 직후 손실 해부 | V02 판독 뒤(사용자 결정 11/13) |
| V05 LLM 판단 가치 시험 | DONE(11/13). 두 모델 모두 `buy` 0~1건이라 주 판정 불가. 모델보다 심의 입력이 먼저라는 결론 → 입력 정직화 A안 운영(11/15 00:08~, `TEAM_CONVICTION=0`일 때만 적용). 실제 자료 연결(B안)은 V02 판독 뒤. [결과](docs/research/v05-team-rejudge-2026-11-13.md) · [입력 진단](docs/reviews/team-input-diagnosis-2026-11-14.md) |
| V06 테마 탐지 주기 | DONE(11/16, 사용자 결정). 10분→30분, 운영 `f8b3dcd5` 11/16 22:19~. 장중 스캔의 뉴스 원천에 들어가는 선정 정책 변경이라 V02 창 1을 11/17부터 다시 센다 |
| V07 Fable 5.2 전환 | DONE(운영 11/13 13:30~). 고정 보고 역할 3개만 5.2, heavy 슬롯은 5.1 유지. 시장 보고는 당분간 유지(사용자 결정 11/16) |
| V08 밸류코어 섀도우 판정 | 대기(선행 조건 없음, 주간 이력 14주) |
| V09 문서·원장 정리 | DONE(11/16) — 이 머리글, 원장 §1.1, 체크포인트, 섀도우 표 |
| V10 백테스터 정합 | 대기(선행 조건 없음) |
| V11 레짐 단일화·G1 검증 | V02 4주 축적 뒤 |
| V12 계좌 기준선 | 미정(사용자 결정 11/16). 500만 원 이상 여유현금이 입금돼 거래가 시작되는 시점을 기준선으로 한다 |

V01~V05가 끝날 때까지 보고서·라우팅·인프라의 새 작업은 하지 않는다(사용자가 명시 승인한 것만 예외 — V06·V07).

**운영.**
- 운영 서버는 이 AWS 호스트이고 시각은 KST다. 운영 checkout은 `/home/ubuntu/projects/qwq-ai-trader`(여기서 개발하지 않는다), 서비스는 systemd `qwq-ai-trader`.
- 최근 배포: `f8b3dcd5`(2026-11-16 22:19:06 KST, V06 — 서버 직렬 전체 10,860 passed, 롤백 없음). 문서만 바뀐 커밋은 재배포하지 않는다. main에는 미배포 청산 수정(`EXIT-CRASH-EFFECTIVE-TS`, PR #385)이 있어 다음 배포에 함께 실린다.
- 배포는 장외에만 한다(평일 09:00~15:30 KST는 배포 스크립트가 거부). 배포 검증 중에는 같은 호스트에서 다른 pytest를 돌리지 않는다.
- 운영 checkout의 `config/evolved_overrides.yml`에는 사용자 소유의 미커밋 수정이 있다(갭 전략 배분 5% — 저장소 커밋본은 15%). 되돌리지 않는다.

**LLM 비용([11/16 판독](docs/reviews/llm-cost-7day-readout-2026-11-16.md)).**
- 역할표 `engine-roles-v8-fable52`, 월 상한 200·일 상한 12(둘 다 원장 단위). 상세는 아래 "LLM 모델 선택".
- 11/16 하루 179건·약 7.1달러, 11월 누계 37.74(18.9%). 가장 큰 항목은 테마 탐지였다(하루 약 3.1) — V06으로 30분 주기가 되어 약 1.1로 줄 것으로 추정한다.
- 원장은 Anthropic 실제 청구보다 약 30% 높게 적는다(입력을 1시간 캐시 쓰기 상한으로 계산). OpenAI 쪽은 콘솔과 일치한다.

**다음 확인(예약 없음 — 사용자가 요청할 때만).**
- V06 배포 뒤 첫 거래일(11/17): 테마 탐지 로그 간격 약 30분, 하루 약 20건([체크포인트](docs/operations/monitoring-checkpoints.md)).
- V02 창 1 판독: 11/17부터 15거래일·300종목-일이 차는 12월 중순 무렵.
- Fable 5.2 시장 보고 재판독: 자연 슬롯 5거래일 뒤(읽기만, 유지 결정은 그대로).
- 월 상한 80% 문턱: 12월 첫 주 누계로 재확인.
- 그 밖의 미결: 운영 `.env`에 의존하는 `TEAM_CONVICTION` 코드 기본값, WSL 임시 키 등록.

**바꾸지 않는 것(사용자 지시).**
- 실주문·매수 재개·현금 투입·배분 변경·전략 활성화를 하지 않는다. 가상자금·모의투자로 자료를 만들지 않는다.
- 기존 설정, `evolved_overrides.yml` 미커밋 수정, `KILL_SWITCH_KR`, core 배분 0%, `missing_cost_policy=log_and_continue`, 비교 R1 OFF·worker 비활성, Gemini 차단을 유지한다.
- 명시 승인 없는 모델·시세 실호출, 텔레그램 시험 발송, 슬롯 재실행, 새 서버 예약·삭제·권한 확대, 관측 기간 연장을 하지 않는다.
- 프롬프트·응답 원문, 계좌번호·계좌 금액, `.env`·자격 증명을 로그·문서·커밋·외부 리뷰에 넣지 않는다.
- 같은 대기 상태만 반복 기록하는 PR은 만들지 않는다.

## 세션 시작 시 필수 읽기

작업 시작 전 **반드시** 아래 파일을 읽을 것:

1. **`CHANGELOG.md`** — 최근 변경 이력 확인
   - 이미 구현된 기능 중복 작업 방지
   - 설계 결정 맥락 파악 (왜 이렇게 짜여 있는지)
   - 알려진 미해결 이슈 파악
2. **`docs/README.md`** — 기술 문서 인덱스
   - 아키텍처, 전략, 리스크, 진화 시스템, 운영, API 연동 상세 문서
   - 에이전트별 참조 가이드 포함
3. **`docs/operations/execution-roadmap.md`** — 최신 main 기준 현재 작업 ID·완료 조건·예약·NEXT 확인. 리뷰 지적은 먼저 현재 작업의 하위 수정으로 처리하고, 별도 과제는 근거·우선순위·복귀 지점을 기록한 뒤 착수한다. 조건 충족 후속 작업은 기존 사용자 승인 범위 안에서 이어간다.

> 예: 유저가 "X 기능 추가해줘" 요청 시 → CHANGELOG에서 이미 구현됐는지 먼저 확인
> 예: 전략 수정 시 → `docs/strategies/kr-strategies.md` 참조

## 언어 & 소통
- 모든 대화는 반드시 한국어(한글)로 진행할 것
- '커밋해줘' = commit AND push. '푸시' = push. 애매하면 commit + push 기본.
- 'new' 또는 'fresh'로 요청하면 이전 실패한 패턴 참조 금지.

## Git & GitHub
- Use SSH for git push (not HTTPS). PAT-based auth if SSH unavailable.
- Always commit and push together unless explicitly told otherwise.
- `gh auth login` interactive mode does NOT work in this environment.

## 에이전트 팀 (정의 13개 — 운영·분석 6 + 도메인 전문가 7)
- 명단·역할·주기: `.claude/agents/` 디렉토리 참조. 엔진 안의 전문가 모듈은 8명 + 섹터 카운슬(`src/experts/`)
- 전문가 시스템 상세: `docs/agents/expert-system.md` / 코드: `src/experts/`
- 출력: `ExpertOpinion` (score/bias/confidence/findings) → market_regime + cross_validator

## 하위 에이전트 위임 규칙 (2026-09-14 사용자 지시)
- Agent/Workflow 로 하위 에이전트를 띄울 때는 기본값을 쓰지 말고 **작업 성격에 맞춰 모델·effort 를 매번 명시**한다.
  - 모델 역할·기본값은 전역 정책 `/home/ubuntu/.config/ai-agents/model-routing.md` 를 따른다. Claude 개발·리뷰는 `claude-opus-5-5`를 명시하고 실제 응답 신원을 확인한다. Claude Sonnet/Haiku/Fable은 현재 자동 배정하지 않으며, 이동 별칭 `opus`나 세션 기본값으로 모델을 추정하지 않는다.
  - 기계적·저위험(복사·포맷·grep 요약·단순 테스트 실행) → low~medium, 일반 구현·특성화 테스트·문서 초안 → medium~high
  - 돈이 걸린 경로(사이징·주문·청산·동기화)·백테스트 판정·보안·독립/적대적 리뷰·최종 통합 판단 → 정본의 `claude-opus-5-5` 또는 `gpt-6-astra`, high~xhigh를 명시하고 작성자와 최종 리뷰어를 분리한다.
- 같은 워크플로 안에서도 단계별로 다르게 지정하고, 선택 근거를 label/프롬프트 첫 줄에 남긴다.

## 프로젝트 개요
- KR+US 통합 트레이딩 엔진 (Full Rewrite)
- 단일 KIS appkey로 국내+해외 주식 동시 운영
- 비동기(asyncio) 이벤트 기반 아키텍처
- 단일 포트 8080에서 KR+US 대시보드 통합 서빙
- 크로스 전략 검증 게이트 + 시장 체제 사전 적응

### 토스 후속 작업 상태 (2026-09-16)

> **현재(2026-09-30): 퇴역.** 관측 grant 09-22 18:00 KST 만료, `qwq-toss-observer.service`·`qwq-toss-observer-retention.timer` 모두 disabled/inactive(사용자 결정). 코드·원장 자료(`/var/lib/qwq-toss-observer`)는 보존 — 재가동은 새 grant·새 관측 계획으로만. 09-16~17 인계 기록은 [머리글 이력 보관](docs/operations/claude-md-header-history.md)에 있다.

## 프로젝트 경로
- 소스: `/home/ubuntu/projects/qwq-ai-trader`
- 가상환경: `venv/` (.venv 아님)
- 설정: `config/default.yml` (kr: + us: 섹션) + `config/evolved_overrides.yml`
- 환경변수: `.env`
- 로그: `logs/YYYYMMDD/`
- 캐시/상태: `~/.cache/ai_trader/`
  - `trade_journal[_kr|_us].json` — 거래 기록
  - `daily_stats[_kr|_us].json` — 일일 손익 영속화
  - `unified_trader.pid` — PID 파일
  - `candidate_ledger/candidates_YYYYMMDD.jsonl` — 장중 스캔 후보 전수 원장(V02, 게이트 전 기록·측정 전용, 설계 `docs/research/v02-candidate-ledger-2026-11-13.md`)
  - `excess_return/` — 실거래 KODEX200 초과수익 원장(20:30 갱신, 측정 전용 — 2026-09-29 06:45 KST 배포, 설계 `docs/superpowers/specs/2026-09-28-kodex200-excess-return-ledger-design.md`)

## 설정 주의사항
> **`evolved_overrides.yml`이 `default.yml` 위에 머지됨**
>
> 설정 변경 시 양쪽 모두 확인 필요. evolved_overrides가 default를 덮어쓰므로,
> default.yml만 바꿔도 evolved_overrides에 같은 키가 있으면 적용 안 됨.

## 아키텍처 & 실행 흐름
- UnifiedEngine 구조·스케줄러 태스크 주기·KR 배치 시각: `docs/architecture/system-overview.md` 및 `src/schedulers/` 소스 참조
- 디렉토리 구조는 `src/` 하위 `ls`로 확인 (모듈별 한 줄 설명은 위 아키텍처 문서)

---

## 매매 전략

### 공통 사항
- 모든 전략은 `BaseStrategy` 상속, `generate_signal()` + `calculate_score()` 구현
- Decimal 정밀 계산, 최소 주가 KR 1,000원 / US $5

### KR 전략
| 전략 | 파일 | 설명 |
|------|------|------|
| 모멘텀 | `kr/momentum.py` | 20일 고가 돌파 + 거래량 급증 — 🚫 **비활성** (`enabled=false`, 배분 0%) |
| 테마추종 | `kr/theme_chasing.py` | 🚫 **폐지** (2026-05-04, allocation 0%) |
| 갭상승 | `kr/gap_and_go.py` | 갭상승 후 눌림목 매수 |
| SEPA | `kr/sepa_trend.py` | SEPA 추세 전략 (스윙) |
| RSI2 반전 | `kr/rsi2_reversal.py` | 🚫 **폐지** (2026-08-02, enabled=false + allocation 0% — 백테스트 단독 -15.44%, 근거는 evolved_overrides `_meta`) |
| VCP 돌파 | (배치 스캔 라인) | 변동성 수축 후 20일 고점 돌파 — **선행 발굴** (2026-08-03~) |
| 밸류코어 | `value_growth_screener.py` | 가치·성장 2버킷 장기보유 — 🔍 **shadow 관측 중** (2026-08-04~, 주문 없음, 설계 `docs/strategies/value-growth-core-design.md`) |

**유효 배분·활성 (2026-11-16, `load_effective_config` 기준):** sepa 40% · vcp 10% · gap 15%(운영 서버는 사용자 미커밋 수정으로 5%) · core 0% · 그 외 0%. 활성은 sepa·gap·vcp·core(배분 0)·밸류코어(shadow)이고, 매수 중지 중이라 신규 진입은 없다.

### US 전략 (4개) — ⛔ **미국 거래 영구 중단 (2026-09-28 사용자 결정)**
> 운영 systemd 는 `--market kr` 로 기동하며 `run_trader.py` 의 `--market` 기본값도 `kr` 이다(US 엔진·브로커·WS·대시보드 US API 미초기화). 아래 표는 코드 존재 기록일 뿐 운영 대상이 아니다. **US 거래 관련 시험·적용은 활성 경로로 다루지 않고 예외 처리한다.** 단, `us_market_data`·`us_market_expert`(SPY/QQQ/VIX 야간 신호)는 KR 레짐 판단의 입력이라 유지한다.

| 전략 | 파일 | 설명 |
|------|------|------|
| 모멘텀 | `us/momentum.py` | 20일 고가 돌파 브레이크아웃 |
| SEPA | `us/sepa_trend.py` | SEPA 추세 (RS 등급 기반) |
| 어닝스 드리프트 | `us/earnings_drift.py` | EPS 서프라이즈 후 모멘텀 (🚫 비활성 — 2026-08-03 검증 통과했으나 US 미운용·EPS 커버리지 부족으로 보류) |
| 어닝스 리버설 | `us/earnings_reversal.py` | 발표 전 낙폭과대 반등 (⛔ 검증 기각 2026-08-03 — 활성화 금지) |

### 청산 관리 (ExitManager)
값은 **필드마다** 정해진다(아래 숫자에는 어느 단계의 값인지 적었다):
- 포지션을 등록할 때, ② 전략별 등록값(`scripts/run_trader.py` `_strategy_exit_params`)이 있는 필드는 그 값을 쓰고, 없는 필드는 그 시점의 런타임 `ExitConfig` 값을 쓴다.
- 런타임 `ExitConfig`는 기동 때 ① 병합 설정 `exit_manager`(`default.yml` ← `evolved_overrides.yml`)이다. ③ 레짐 값(`REGIME_EXIT_PARAMS`, `apply_regime_params`)이 한 번 적용되면 손절·트레일링·익절 목표·신고가 실패 일수가 레짐 값으로 바뀐 상태가 된다(매도 비율은 그대로).
- ③ 레짐 적용은 **레짐이 바뀌었거나 강제 재적용할 때만** 일어나고, 그때 등록돼 있는 비코어 포지션도 갱신한다(코어는 제외). 갱신은 단계별이다:
  - 손절·트레일링·신고가 실패 일수: 모든 단계에서 레짐 값으로 바꾼다. ATR 연동 트레일링이 있으면 `min(max(레짐 값, ATR×배수), 상한)`으로 다시 계산한다.
  - 익절 목표: 아직 지나지 않은 단계만 바꾼다 — 1차 목표는 익절 전(`NONE`)일 때만, 2차는 `NONE`·`FIRST`, 3차는 `NONE`·`FIRST`·`SECOND`일 때만. 이미 지난 단계의 목표는 그대로 둔다.
  - 장중 급락 단계가 켜져 있으면 레짐 적용 직후 손절과 **원시** 트레일링 값을 급락 단계 값 이하로 다시 조인다(익절 목표는 레짐 값 유지).
  - ATR 연동 실효 트레일링(`effective_trailing_stop_pct`)도 급락 중에는 급락 값 이하로 유지하고, 급락이 해제되면 레짐 기준으로 다시 계산한다(`EXIT-CRASH-EFFECTIVE-TS` 수정 — PR #385 병합, **운영 미배포: 다음 배포 때 함께 올린다**(사용자 결정 11/17). 종전에는 급락 중 신규 등록·레짐 재적용 두 경로에서 실효값이 넓게 남았다).
  - 정확한 규칙의 정본은 `src/strategies/exit_manager.py` `apply_regime_params`와 `docs/risk/risk-and-exit.md`다.
- 예: sepa·vcp는 익절 목표를 등록하지 않으므로 새 포지션의 목표는 현재 런타임 값이다(기동 직후 +10/+15/+25, ranging 적용 뒤 +8/+8/+14, bear 적용 뒤 +5/+8/+14). 손절·트레일링은 등록값(sepa 5%/3.0%, vcp 4%/2.5%)이라 다음 레짐 변경·강제 재적용 전까지 그 값을 유지한다.
- ④ 장중 급락 단계·ATR 연동이 그 위에 걸린다.
- **1차 익절**: 목표 +10%(①병합 설정 — 레짐 적용 뒤에는 레짐 값) → 10% 매도(①병합 설정 비율, sepa·vcp가 상속). gap은 ②등록값으로 +4%에서 40%, core는 분할 익절 없음. 2026-08-02 백테스트 검증으로 +5%/20%에서 조정)
  - 기존값은 평균 1.9일에 발동해 추세 초입을 절단 (익절 +5.2% < 손절 -6.2%)
  - ⚠️ 변경 시 4곳 동시 수정: `default.yml` / `evolved_overrides.yml` / `ExitConfig` / **`REGIME_EXIT_PARAMS`**
- **2차 익절**: 목표 +15%(①병합 설정 — 레짐 적용 뒤에는 레짐 값) → 잔여의 40% 매도(①병합 설정 비율 — `default.yml`의 50%를 `evolved_overrides`가 덮는다)
- **3차 익절**: 목표 +25%(①병합 설정 — 레짐 적용 뒤에는 레짐 값) → 잔여의 35% 매도(①병합 설정 비율 — `default.yml`은 50%). 레짐이 바꾸는 것은 익절 **목표 수익률**(예: neutral 10/15/25%, ranging 8/8/14%, bear 5/8/14%)이고 매도 비율은 아니다
- **트레일링**: 수익 +5% 이상에서 활성화. 폭은 ②전략별 등록값이 먼저다 — sepa 3.0% · vcp 2.5% · gap 2.0% · core 12%. ③레짐이 바뀌거나 강제 재적용되면 그때 등록돼 있던 비코어 포지션은 2.0~4.0%(bull 4 / neutral·turning_point 3 / ranging 2.5 / bear 2)로 바뀌고, 같은 레짐에서 새로 들어온 포지션은 그때까지 전략별 값을 유지한다. ①병합 설정의 4.5%는 등록값이 없는 포지션의 초기값일 뿐 실제 손실 허용폭이 아니다
- **ATR 동적 손절**: 기본 5%, ATR×2, 범위 4~8% (evolved_overrides, min_stop 4.0)
- **포지션 상태**: `PositionExitState` — NONE/FIRST/SECOND/THIRD/TRAILING 단계 추적

### 코어홀딩 A안 (2026-05-11~ "장기 추세 캐처")
- **진입 필터**: MA200 위 + 60일 ≥+5% + 신고가 80% 이내 → 박스권 자동 배제
- **점수 (100점)**: 추세 20 + 펀더 20 + 수급 20 + 모멘텀 30 + RS등급 10
  + **저변동성 감점** 0~-10 (일수익률 60일 σ 기준, 2026-08-03~, 급등락형 배제)
  + **자산 확장 감점** 0~-5 (DART 총자산 증가율 ≥30/50%, 2026-08-03~, 퀄리티 팩터)
- **청산**: stop_loss 10%, trailing 12% (느슨), 분할익절 OFF, max_holding 무제한 — 런타임 값은 `run_trader.py` `_strategy_exit_params["core_holding"]`의 고정값이다. `default.yml` `core_holding`의 stop 15%·trailing 8%는 ExitManager 등록에 쓰이지 않는 옛 값이다(2026-11-16 V09 확인, 코어 배분 0%)
- **stale**: Tier1 20영업일±3%, Tier2 30영업일±3% OR 20영업일±2%+거래량50%
- **리밸런싱**: 격주 (rebalance_interval_weeks=2)

### 캘린더 시즈널리티 오버레이 (2026-08-03~)
- `src/utils/calendar_seasonality.py` — 전 전략 매수 **사이징 배율** (독립 전략 아님)
- turn-of-month(월말 2+월초 3거래일) KR/US ×1.10, US 옵션만기주 ×1.05 (KR 만기주 미적용)
- 부스트 전용(차단·축소 없음), 상한(max_position_pct)은 재적용됨
- 비활성화: `CALENDAR_SEASONALITY=0`

### 조건부 변동성 타게팅 오버레이 (2026-08-19~)
- `src/utils/volatility_targeting.py` — 모멘텀 계열(sepa/gap/momentum/vcp) 매수 **사이징 축소 배율**
- KOSPI 20일 실현변동성 > **25%**(전체 거래일의 ~11% 극단 국면)일 때만 ×(25/vol), 하한 0.4
- 검증: KODEX200 2015~26 — Sharpe 0.721→0.787, MDD -40.8→-34.8% (CAGR -2.1%p 비용)
- 캐시: `~/.cache/ai_trader/vol_targeting.json` (매 거래일 08:30 갱신, 노후 3일+ 시 무개입)
- 원천(2026-09-28 배포): `src/utils/kospi_benchmark.py` 공용 로더 — Yahoo ^KS11 → KS11 → 069500, 신선하지 않으면 캐시 미기록.
  FDR `KS11` 은 GitHub 캐시 CSV 라 09-17 에서 예외 없이 멈췄다(그동안 31.8%/×0.786 이 매일 재기록됨)
  수확 shadow·스윙스크리너 레짐도 같은 `FALLBACK_SOURCES`(Yahoo → KS11 → 069500 최후 대체, 같은 검증)를 쓴다 (2026-09-29, 15:33 KST 배포 main `974a71f`)
- 축소 전용(레버리지 없음), 일수익률 |12%| 초과는 데이터 오류로 제외
- 비활성화: `VOL_TARGETING=0` / 상세: `docs/research/ai-trading-research-2026-08.md`

### 팀 심의 conviction 부스트 (2026-08-20~)
- `src/utils/team_conviction.py` — 팀 심의(BUY 승인 + conviction ≥0.75/0.90)
  종목의 신규 매수 사이징 ×1.10/×1.20 **부스트 전용**
- HOLD/REJECT는 사이징에 미반영 — CF 실측(47건): HOLD 차단 후보가 5일 +6.31%
  (차단·감액 용도는 손해로 판명)
- **2026-09-13 비활성화 (`TEAM_CONVICTION=0`, 운영 .env)**: 근거였던 "+6.31%"는 강세장 베타(KODEX200 대비 -1.0pp, 최신 코호트 -3.1pp), 승인 BUY 종목이 HOLD보다 열위, conviction 94%가 0.90 고정 — 초과수익 ≥60건 확보 전 부스트 금지 (리뷰 §3)

---

## 리스크 관리

### KR 리스크
| 항목 | 값 | 비고 |
|------|---|------|
| 일일 최대 손실 | -5.0% | effective_daily_pnl 기준 |
| 일일 거래 횟수 | 10회 | daily_max_trades |
| 일일 신규 매수 | 5개 | max_daily_new_buys |
| 최대 포지션 수 | 8개 | max_positions |
| 기본 포지션 비율 | 25% | nominal 모드만. **`sizing_mode: risk`(2026-09-13~, 09-14 정합화)** — equity×0.7% ÷ **신규 체결 실제 고정 SL**(sepa 5 / gap 3.5 / vcp 4, 급락 cap 미적용) → sepa 14%·gap 18%(상한)·vcp 17.5%. 모든 오버레이·3주 보정 뒤 매수수수료 포함 계획 위험 ≤ 0.7% 를 최종 상한으로 재클램프(1천만·1만원·SL5% → 139주) |
| 최대 포지션 비율 | 28% | nominal 상한 / risk 모드는 `risk_max_position_pct` 18% |
| 최소 현금 보유 | 5% | total_equity 대비 |
| 최소 포지션 금액 | 20만원 | 미달 시 매수 거부 |

### US 리스크
| 항목 | 값 | 비고 |
|------|---|------|
| 일일 최대 손실 | -3.0% | |
| 최대 포지션 수 | 10개 | |
| 기본 포지션 비율 | 25% | |
| 최대 포지션 비율 | 35% | |
| 최소 현금 보유 | 10% | |
| 최소 포지션 금액 | $50 | |
| 연속 손실 중단 | 3회 | 사이징 50% 축소 |

### 팩터 버킷 위험예산 (2026-08-08~, shadow 관측 중)
- trend 65% / quality 20% / reversion 10% — 상관 전략 묶음 총 노출 캡 (`default.yml factor_budgets`)
- 현재 `enforce: false` (초과 시 로그만) — 상세 `docs/risk/risk-and-exit.md`
- **승격 보류 (2026-08-19)**: 8월 매수 0건으로 초과 이벤트 표본 부재 →
  일일 노출 스냅샷(`factor_exposure_log.jsonl`, 저녁 품질검증 잡) 2주 축적 후 재판단
  → **2026-09-03 재판단 불가**: 전략 포지션 0건이라 스냅샷 전부 0% — 매수 재개 후 2주로 이월
  → 2026-09-29 매수 가능 상태 확인(현금 19.4%)으로 2주 관측을 다시 시작했으나 09-30 매수 중지
  → **2026-11-16 재판단 불가 유지**: 스냅샷 61일(08-19~) 중 노출이 0이 아닌 날은 7일뿐 — 매수 재개 뒤 2주 관측으로 이월

### 운영 상태 — 현금 (2026-11-13 확인: 현금 0.5%·매수 불가)
- **현금 비중 0.5%·보유 1종목(`manual`, 자산의 99.5%)** — 최소 포지션 1건도 살 수 없다(11/13 06:43 사후 점검, 11/6에도 0.52%). 매수 중지를 풀어도 봇이 운용할 자본이 없다.
- 이력: 09-29 20:47 에는 현금 19.4%·보유 3종목으로 매수 가능했다(09-29 06:51 22.9% 로 처음 확인).
- 이력: 2026-07-01~(09-03 확인) 펩트론 087010 120주(`manual`, exit_exempt)가 자산 99.6%·현금 0.4% 라 신규 매수·진화/CF 표본 축적이 정지됐었다.
- `/api/portfolio`의 `cash_ratio`로 즉시 확인. 코어홀딩 "빈슬롯 매수 시도" 로그의 예산은 equity 기준이라 현금과 무관하다.

### 전략·아키텍처 종합 리뷰 (2026-09-13) — `docs/reviews/strategy-architecture-review-2026-09.md`
- **1단계 반영(2026-09-13)**: 배분 core 0 / gap 15 / sepa 40 / vcp 10 (합 65, 잔여 현금) · `TEAM_CONVICTION=0` · 계측 기준 교체
- **2단계 반영(2026-09-13) → 후속 수정(2026-09-14, `docs/superpowers/plans/2026-09-13-review-remediation.md`)**: 09-13 배포본(de111b7)의 위험 사이징은 분모가 실제 신규 체결 SL(고정 sepa 5/gap 3.5/vcp 4)과 달랐고(F1) 근거 A/B 는 미래정보 포함(F2·F8). PR #33~#38 로 F1~F8 수정(동기화 빈 응답 방어·실제 SL 기준 사이징+0.7% 최종 상한·백테스터 시점·하트비트 성공/실패 구분·entry_risk 원장·유효 설정 게이트·parity). **2026-09-15 03:08 KST main `3f6b1bf`(F1~F22 전부 포함) 배포·재기동 완료**(1차 시도는 conftest 가드 과잉 차단으로 자동 롤백 후 핫픽스 #48).
- **재검증(2026-09-14, `docs/research/risk-sizing-revalidation-2026-09.md`)**: SEPA 단독 6셀(nominal/risk/고정 14% 대조군 × 6m/12m) 오프라인 재실행 — risk 는 운영 게이트 4조건 충족이나 **대조군도 동일 통과**·parity 미해소 2건 → **승격 보류**. live_policy 에서 SEPA SL 5% 단일값이라 risk = 고정 14% 명목(상한 18% 미발동). 검증된 것은 노출 축소(노출 26~30→21~26%, 회전 39~41→33~34배, MDD 3pp). 전 셀 KOSPI 대비 -26~-96pp, 상위 3건 제외 순손익 전 셀 음수 — 엣지 미입증 유지. canary(`scripts/review_risk_canary.py`, 원장 `scripts/export_risk_ledger.py --source db`) **미시작**. 자동 nominal 복귀 금지.
- **T9(2026-09-14 저녁, PR #42~#45)**: 모닝브리프↔실제 장 괴리(-3.26%) 분석 반영 — 12:00 레짐 재분류가 당일 지수·급락 상태(당일 갱신 게이트)를 쓰고 crash/severe 면 bull 미적용, LLM 입력 결측은 '결측'(0 금지)·`input_meta`, 지수 키 정규화·VIX 수집, 유효 레짐 `effective_regime`(장중 위험 당일 게이트, bull→sideways 강등 전용), 모닝브리프는 미국 자료뿐이면 "미국시장 마감 요약"으로 제한·개장 단정 문장 제거·전문가 상충 표시, 전문가 결측 `data_status`(insufficient ≤0.2/partial ≤0.7)·집계 제외·커버리지 게이트(<4명 무보정), 수동 거시 오버라이드 `valid_until`, 야간선물 `fetched_at`, 장전 전망 사후 평가 원장(20:30). verify 456 passed. 2026-09-15 `3f6b1bf` 로 배포됨. 상세 `docs/reviews/remediation-2026-09-14.md` §10
- **T10(2026-09-15, PR #47·#48 머지·배포)**: T9 후속 교차 리뷰 결함 10건(F13~F22) — 단계는 맞았으나 단계 사이 연결(자료→검증→레짐→소비자→발송→평가)이 끊긴 결함. 정오 당일 봉 교체/직전거래일 추가 구분, 급락 캡을 감지기·이번 조회·어댑터 당일 관측 3소스 중 보수적 값으로 병합, `monitor_positions`의 레짐 재적용 제거(30분 sync 단일화), G2가 어댑터 유효 레짐 우선, 6명 전문가 `data_status` 판정 확장 + `from_dict` unknown 집계 제외, 야간선물 세션 as_of 확인 시만 집계, Yahoo 지수 결측 None 보존, 07:00 문구 한국 방향 단정 제거, 모닝브리프 테마-업종 매핑 사전 고정, 07:30 발송 스냅샷 기준 저녁 평가(날짜별 아카이브 신설). 임계값 무변경. verify 552 passed / 2 xfailed(D 독립 재현 14건 + 통합 E2E 8건), Codex 리뷰 미실행(샌드박스). **2026-09-15 03:08 KST main `3f6b1bf` 배포·재기동 검증 완료**(보고서 §11.8). 상세 `docs/reviews/remediation-2026-09-14.md` §11, 계획서 `docs/superpowers/plans/2026-09-13-review-remediation.md` T10 절
- **T11(2026-09-15, PR #50~#53 머지 → 15:41 KST main `8c27fe8` 배포·재기동 완료)**: 에이전트 팀 근거 계약(EvidenceItem, positive_basis≠risk_clear, 관측 시각 모르면 None)·판단 v2 shadow(`TeamAssessment`: 매수 매력/위험 허용/자료 충분성/진입 조건 분리, 합의≠확률, 확률 미보정)·EntryPlan(PendingSignal 정본, `check_entry_plan` shadow 기록만)·append-only 심의 원장·실행 상태 6단계(plan_rejected 포함)·CF `team_buy_unfilled`·A/B/C 오프라인 러너(합성 검증만). 기존 TradeProposal/conviction/주문 경로 기준선 불변(특성화 테스트). 승인된 기존 경로 버그 수정 2건(vol_ratio 키, confidence=0 유효 소스). **투자 성능 미검증·운영 승격 없음(shadow 기록만).** 1차 배포(15:33, `04279c6`)는 시각 의존 테스트 7건이 장 마감 후 verify 에서 실패해 자동 롤백 → #53(테스트 시계 동결) 후 재배포. 분석가 산식 수정 2건은 shadow 플래그 밖이라 사이징 차단은 운영 `.env` `TEAM_CONVICTION=0`(+위험모드 0.7% 클램프)에 의존 — 재활성화 시 재평가 필수. 현금 고갈 상태에선 EntryPlan shadow 행·CF 승인 BUY 표본이 쌓이지 않음(엔진 현금 검사가 훅보다 앞). 상세 `docs/agents/trading-team.md` T11 절, 계획서 `docs/superpowers/plans/2026-09-15-agent-team-evidence-entryplan.md`
- **실거래 초과수익 원장(09-29 20:30 첫 실행): KODEX200 대비 포지션당 −1.66%, t=−3.98, 251건, 보유기간 벤치 평균 +1.28% vs 봇 −0.38% — 시장 탓이 아닌 선택·청산 열위.** 펩트론 등 `strategy=manual` 은 `manual_entry` 로 제외. → 09-30 KR 신규 매수 중지. 11/16 현재 원장은 포함 253건·평균 −1.62%로 같은 결론이다
- 실거래 266건: 수수료 전 총손익 ≈ 0, 차감 후 -139만, t=-0.18, KOSPI +37% vs 자산 -7.5% — **엣지 미입증**
- 구조 원인: 연 91배 회전(수수료 = 손실 전부) · 1차 익절이 타이트 청산 무장(p90 +4.9%) · 명목 사이징 3~4종목 집중 · 배분 55%가 근거 없는 라인 · 레짐 4겹 후행
- 판정 기준은 **KODEX200 초과수익 + 손절 클립** (절대수익 판정으로 04-23·08-20 결정이 뒤집힘). 권고 1~8·금지 목록은 리뷰 §5~6

### 섀도우 관측 현황 (2026-11-16 운영 캐시 판독 기준)
관측 전용(주문 무관) 항목이다. 09-30 매수 중지 뒤에는 실거래가 전제인 항목(팩터 버킷 초과, 승인 BUY 체결)이 표본을 쌓지 못한다.

| 항목 | 시작 | 상태 (11/16) |
|------|------|------|
| 밸류코어 (`value_growth_core.shadow_mode`) | 08-04 | 주간 이력 14주(W34~W47) — 판정 가능한 분량, V08 대기 |
| 비대칭 수확 G3 (`harvest_shadow.py`) | 08-13 | 매 거래일 08:40 실행 중(마지막 11/16) — 청산 기록 4건, 누적 −5.5R |
| 에이전트 팀 심의 (`trading_team`) | 08-02 | 장중 10:30/11:30/13:00/14:00 실행 중 — verdict 72일, append-only 원장 40일·1,011행(09-16~). 11/15부터 입력 정직화 A안 |
| 규칙 #11 전문가 BEAR (`experts.shadow_mode`) | 08-02 | 발화 로그 23건(09-03 이후 9건, 마지막 11/09), CF 추적 19건 |
| 규칙 #12 섹터 카운슬 | 08-07 | 발화 2건(08-25, 10-14) |
| 팩터 버킷 (`factor_budgets.enforce: false`) | 08-08 | 노출 스냅샷 61일(08-19~) 중 노출이 0이 아닌 날 7일 — 재판단은 매수 재개 뒤 |
| Counterfactual 추적 | 08-08 | 836건 추적(팀 HOLD 666 · 팀 승인 미체결 149 · 규칙 #11 19 · 규칙 #12 2) |
| 장중 후보 전수 원장 (V02) | 11-13 | 기록 중 — 위 "현재 상태" 참조 |
| Shadow Lab (calibration/bandit) | 08-10 | 리포트 로그 갱신 중(`shadow_report.log`, 11/16) |
| LLM Shadow A/B (`openai_model_light_shadow`) | 06-17 | 🚫 비활성화(08-19) — 마지막 표본 08-03 |

### 수수료
- **KR** (한투 BanKIS, 2026년~): 매수 0.014%, 매도 0.213% (수수료+거래세 0.20%), 왕복 약 0.227%
- **US** (KIS 해외주식): Zero-commission

---

## 검증 프로토콜 (절대 규칙)
코드 수정 후 반드시 아래 순서 수행:
0. **푸시 전 전체 검증은 CI·배포와 동일한 `bash scripts/dev/verify.sh`** — 로컬(WSL)에서도 같은 스크립트를 쓴다(준비 `scripts/dev/bootstrap.sh`, 한글 글꼴 포함). 로컬 전용 `QWQ_VERIFY_PARALLEL=auto`(xdist)·`QWQ_VERIFY_TESTS=<경로>` 는 중간 확인용이며 최종·CI·서버는 직렬 전체. Codex 도 같은 규칙(`AGENTS.md`). [로컬 개발 6절](docs/operations/local-development.md)
1. `python3 -m py_compile <수정파일>` — 문법 검증
2. **봇 재시작**: `sudo systemctl restart qwq-ai-trader`
   - ⚠️ `nohup python scripts/run_trader.py` 직접 실행 **절대 금지** (systemd와 충돌)
3. 상태 확인: `systemctl is-active qwq-ai-trader`
4. 로그 확인: `journalctl -u qwq-ai-trader -n 20 --no-pager`
5. 에러 없으면 완료 보고, 있으면 즉시 수정

```bash
# 문법 검증 (전체)
cd /home/ubuntu/projects/qwq-ai-trader
source venv/bin/activate
find src/ scripts/ -name "*.py" -size +0c -exec python3 -m py_compile {} \;

# 봇 관리 명령어
sudo systemctl restart qwq-ai-trader  # 재시작
sudo systemctl stop qwq-ai-trader     # 중지
systemctl is-active qwq-ai-trader                              # 상태
journalctl -u qwq-ai-trader -f                                 # 실시간 로그
```

### 운영 스킬 (2026-09-10~)
- `/ops-check` 운영 점검 요약 (`scripts/dev/ops_check.sh`) · `/deploy-local` 서버 측 배포 (`scripts/deploy/local_deploy.sh`, 자동 롤백) · `/pr-merge` gh 기반 PR→verify→머지
- **WSL에서의 배포(2026-10-31~, Claude·Codex 공통)**: `bash scripts/deploy/deploy_lightsail.sh`(`--check` → 사용자 승인 뒤 `--deploy [SHA]` → 150초 뒤 `--postcheck`)가 서버의 `local_deploy.sh`를 SSH로 부른다. 장중 거부·pending 0·evolved_overrides 외 변경 중단·고정 SHA. 정본 [docs/operations/lightsail-deployment.md](docs/operations/lightsail-deployment.md)
- `main`은 보호 브랜치 — 직접 push 불가, 반드시 PR + `verify` 통과

## 코드 리뷰 프로토콜
사용자가 "리뷰해봐" 요청 시:
1. 변경된 모든 파일 재읽기 (캐시 의존 금지)
2. P0(치명적), P1(중요), P2(경미) 우선순위로 이슈 분류
3. 각 이슈: 파일명 + 라인번호 + 구체적 문제 + 수정방안
4. P0부터 수정 → py_compile → 재시작 → 로그 확인

## 문서 업데이트 (절대 규칙)

> **대전제: 모든 코드 변경은 관련 문서에도 반드시 반영해야 한다.**

코드 변경 시 **반드시** 아래 문서를 함께 업데이트:

1. **`CHANGELOG.md`** — 변경 이력 (날짜, 커밋, 수정 파일, 상세 내용)
2. **`docs/` 관련 문서** — 변경된 기능에 해당하는 기술 문서 업데이트
   - 전략 수정 → `docs/strategies/kr-strategies.md` 또는 `us-strategies.md`
   - 리스크/청산 변경 → `docs/risk/risk-and-exit.md`
   - 진화/위키 변경 → `docs/evolution/evolution-system.md`
   - 아키텍처/흐름 변경 → `docs/architecture/system-overview.md`
   - API 연동 변경 → `docs/integrations/external-apis.md`
   - 운영 절차 변경 → `docs/operations/runbook.md`
3. **`CLAUDE.md`** — 현재 상태(current state) 반영 (설정값, 전략 배분 등)
4. **`MEMORY.md`** — 교훈/패턴/규칙만 기록 (변경 이력 금지, 150줄 이하)

**문서 미업데이트 시 코드 변경 불완전으로 간주한다.**

---

## 대시보드 개발
- 새 기능 추가 절차·API 라우트·페이지 목록: `.claude/skills/dashboard-feature/SKILL.md` (대시보드 작업 시 로드)
- 가상 오피스 `/office`: 재빌드 `bash tools/office/build.sh` — **`static/office/`는 빌드 산출물이므로 직접 수정 금지** (상세: `docs/operations/virtual-office.md`)

---

## 코딩 규칙

### 패턴
- **비동기**: 모든 I/O는 `async/await` (aiohttp, asyncio)
- **데이터클래스**: 도메인 모델은 `@dataclass`
- **정밀 계산**: 금액/가격은 `Decimal` 사용 — `Decimal(str(value))` 로 변환 (float → Decimal 오차 방지)
- **한국어**: 주석, 로그 메시지 모두 한국어
- **로그 태그**: `[리스크]`, `[스크리닝]`, `[진화]` 등
- **pykrx**: 반드시 `await asyncio.to_thread(pykrx_func)` 래핑 — 동기 블로킹 금지
- **aiohttp timeout**: `timeout=aiohttp.ClientTimeout(total=30)` (숫자 리터럴 금지)

### 절대 금지 패턴

```python
# ❌ 잘못된 패턴 — 0, 0.0, "" 이 False로 처리됨
if value and value < 0:        # 0.0은 통과 안 됨
if atr and atr > 0:            # atr=0 조건 누락
result = value or default      # value=0 이면 default 반환

# ✅ 올바른 패턴
if value is not None and value < 0:
if atr is not None and atr > 0:
result = value if value is not None else default
```

### 주의사항
- `.env`에 API 키 저장 (커밋 금지)
- KIS API 토큰은 `~/.cache/ai_trader/`에 캐시
- **Position.current_price 반드시 체결가로 초기화** — 미초기화 시 unrealized_pnl -100% → 일일손실 즉시 트리거
- **pending 상태 관리**: 예외 핸들러에서 반드시 `clear_pending()` 호출 (누수 방지)
- **파일 수정 시 연관 체크**: types.py ↔ engine.py, exit_manager.py ↔ schedulers, config.py ↔ YAML
- **수수료 계산**: `FeeCalculator` 단일 사용 — data_collector/storage 내 하드코딩 금지
- **영업일 계산**: `is_kr_market_holiday()` 반드시 사용 (주말/공휴일 처리). engine·session 판정은 KIS 동적 ∪ fallback 으로 같다(2026-09-28~).
  fallback 목록은 `src/utils/session._KR_FALLBACK_HOLIDAYS` 한 곳 — 합집합이라 잘못 넣은 날은 KIS 가 되돌리지 못하니 **확정된 날만** 추가한다
- **KIS 취소 0건은 '소멸'이 아니다**: `cancel_order`/`cancel_all_for_symbol` 은 실패를 예외로 올리지 않고 False/0 을 돌려준다 — 0건에는 '방금 체결'·'거래소 생존'이 섞여 있다. 0건(또는 같은 종목의 다른 주문이 취소된 건수)을 근거로 **분할 매도**를 재발행·pending 해제·`rollback_stage` 하기 전에 `RiskManager.stale_order_still_live`(브로커 장부의 그 방향 활성 주문 → 한 주기 대기 → 거래소 실 미체결)로 가린다. 전량 청산은 KIS 가 수량 초과로 막으므로 종전 경로(손절 지연 금지). 브로커 장부는 완전 체결·취소 성공에서만 빠진다(수동 취소 주문은 남는다). 취소·조회 `await` 를 건넌 뒤에는 같은 pending 인지 재검증한다 — 수량 장부가 비면 폴백이 보유 전량으로 번진다. 상세 `docs/risk/risk-and-exit.md` (2026-09-21)
- **자동매도 금지(exit_exempt) 종목의 SELL**: `SignalEvent` 로 나가는 SELL 은 엔진 `RiskManager.on_signal` 중앙 가드가 발행처와 무관하게 막는다(2026-09-21). 그래도 새 SELL 발행처가 emit **전에** 부작용(텔레그램 '자동 청산' 알림, `core_state.sold` 기록, 잔여액 차감)을 내면 발행처에서도 `is_exit_exempt` 로 뺀다. **브로커에 직접 `submit_order` 하는 SELL 경로는 중앙 가드를 안 거치므로 자체 검사 필수.** 면제는 런타임에도 등록되므로(`run_manual_buy_orders`) "면제면 pending SELL 이 없다"고 가정하지 않는다 — 면제 종목의 미체결 SELL 은 브로커 추적(`get_open_orders`)에서 사라진 것을 확인한 뒤에만 pending 을 푼다. 설정의 종목코드는 반드시 따옴표(`000660` → YAML 8진수 432). 상세 `docs/risk/risk-and-exit.md`
- **엔진 pending 은 종목 단위·방향 무구분**: BUY pending 이 남아 있으면 `_check_exit_signal` 이 반환해 그 종목의 손절 신호 자체가 생성되지 않는다 — pending 을 '안전하게 유지'하는 수정은 부분체결 포지션의 청산을 막는다(보유 중이면 해제, 유지 중 부분체결·동기화 반영은 즉시 해제 — `release_kept_stale_buy`). **브로커 `cancel_order`/`cancel_all_for_symbol`·`submit_order` 는 실패를 예외로 올리지 않는다**(0/False/`(False,msg)`) — 호출부의 `except` 에 기대지 말고 반환값과 브로커 추적(`get_open_orders`)·거래소 실 미체결(`get_exchange_open_orders(symbol=…)`, 첫 페이지 한계)로 가른다 (2026-09-21 PR #81)
- **KIS 주문 POST는 재전송 금지**: 접수/정정은 `_api_post(retry=False)` — 응답 유실 시 재전송하면 중복 주문 (2026-09-03 P0). 새 주문 계열 TR도 동일. **취소는 예외(재시도 유지)** — 전량 취소는 `ORGN_ODNO` 하나를 겨냥해 두 번 닿아도 새 노출을 만들 수 없고, 빼면 보호 취소 성공률만 떨어진다 (멱등 보장이 아니라 효과 한정 논증 — `docs/integrations/external-apis.md`)
- **KIS 주문 POST 접수 불명은 거절이 아니다** (2026-09-29 구현, 15:33 KST 배포 main `974a71f`): POST 진입 뒤 응답을 믿을 수 없으면(비-JSON·네트워크 오류·`rt_cd`·`msg_cd` 없는 JSON·POST 뒤 예외) `submit_order` 가 `(False, "[접수불명] …")` 를 돌려주고 브로커 장부(`~/.cache/ai_trader/order_unknown.json`)에 남긴다 → 그날 BUY 전부 보류, 불명 SELL 종목은 분할 SELL 재발행 금지(전량은 허용). 해제는 날짜 변경뿐. 새 주문 경로도 `submit_order` 를 거치고, 엔진은 `broker.unknown_buy_hold()`/`has_unknown_sell()` 를 **비어 있지 않은 str / 정확히 True** 일 때만 믿는다. 상세 `docs/risk/risk-and-exit.md`
- **새 주문 CLI 는 인자 파싱 직후 `src.utils.trader_lock.hold_or_exit("<도구명>")` 를 부른다** — 봇 싱글톤 flock 을 잡아 보고 봇·다른 CLI 가 실행 중이면 KIS 호출 전에 exit 2(2026-09-29, 20:47 KST 배포 main `785f1fe`). 별도 프로세스 `KISBroker` 는 봇 장부·레이트 리미터를 공유하지 않는다. 락 파일은 지우지 않는다(봇 `release_singleton_lock` 도 unlink 안 함)
- **KIS 직접 호출은 `await kis_rate_limit.acquire(tr_id)` 선행**: 브로커·시세·스크리너가 같은 appkey라 초당 한도는 합산(EGW00201). 원장 TR(잔고/매수가능/체결/미체결)은 계좌당 초당 1건(EGW00215) — 새 원장 TR은 `utils/kis_rate_limit.LEDGER_TR_IDS`에 추가. 구/신 TR 양쪽을 다 넣는다(전환·롤백 어느 쪽에서도 직렬화가 끊기면 안 됨). 게이트웨이 상한 수치는 공식 저장소에 없고 전부 운영 실측값이다
- **KIS TR ID는 리터럴로 쓰지 않는다**: `kis_kr._tr_id("rvsecncl"|"daily"|"cancelable")`, 주문은 `_get_tr_id_for_session(side, use_new)` — `KIS_TR_SET`(기본 legacy, 정확히 `new` 일 때만 전환) 한 곳에서만 구/신이 갈린다. `new` 전용 본문 키(`EXCG_ID_DVSN_CD`·`CNDT_PRIC`)는 그 가드 안에만 두고, hashkey 발급보다 **먼저** 붙인다(hashkey는 본문 무결성 검사). **주문 접수의 신 TR·신 본문은 정규장(`regular`) 세션 한정** — NXT 세션의 `EXCG_ID_DVSN_CD` 값이 공식 저장소에 없어 그 세션은 `new` 에서도 legacy 로 나간다
- **KIS 연속조회 종료는 응답 헤더 `tr_cont`(F/M 다음, D/E 마지막)로 판정** — 본문 `ctx_area_*100` 키는 마지막 페이지에도 채워져 오므로 종료 근거가 못 된다(2026-09-15 EGW00215 반복 원인: 보유 1종목 계좌가 8434R 을 10회 호출). `_api_get` 이 `data["_tr_cont"]` 로 실어 준다. **요청 헤더 `tr_cont` 는 첫 페이지 미송신·2페이지째부터 `"N"`**(2026-09-21, `_api_get(..., tr_cont="N")` — 1페이지로 끝나는 호출의 요청은 무변경). 미체결 조회(`get_exchange_open_orders`)의 잘림 처리는 PR #81 이 같은 함수에서 다룬다 — 이 PR 은 건드리지 않는다

---

## 환경변수 (.env)
```
KIS_APPKEY, KIS_APPSECRET, KIS_CANO, KIS_ENV (prod/dev)
KIS_EXT_ACCOUNTS (외부 계좌, 형식: 이름:CANO:ACNT_PRDT_CD 쉼표 구분)
OPENAI_API_KEY, GEMINI_API_KEY
MANUS_API_KEY (미사용 — 2026-08-19 구독 해지, manus.enabled=false)
TEAM_CONVICTION (운영 .env 는 0 — 팀 conviction 부스트 끔, 2026-09-13~. 심의 입력 정직화 A안은 이 값이 0일 때만 적용된다.
                 코드 기본값은 켜짐이라 .env 에서 빠지면 종전 규칙으로 돌아간다)
TEAM_ASSESSMENT_V2 (기본 1 — 팀 심의 shadow 판단 v2·원장 기록; 0 이면 미계산. 돈 경로 무영향, 2026-09-15 T11)
CANDIDATE_LEDGER (기본 1 — 장중 스캔 후보 전수 원장 기록; 0 이면 미기록·점수 구성 미부착. 돈 경로 무영향, 2026-11-13 V02)
ENTRY_PLAN_SHADOW (기본 1 — 주문 직전 EntryPlan shadow 검증 기록만; 0 이면 미호출. 허용/차단 없음, 2026-09-15 T11)
TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID
INITIAL_CAPITAL (KR, 기본 500000)
KIS_TR_SET (기본 legacy — 구 TR 그대로. new 면 KIS 신 TR 5종 + EXCG_ID_DVSN_CD. import 시점
            상수라 재시작 필요, 롤백은 줄 삭제 + 재시작. 2026-09-21, 아직 전환 안 함)
```

## LLM 모델 선택

**10/28 사용자 지시:** Gemini 중단·GPT/Claude 전환. 아래 계약은10/28 17:40 운영 인수로 적용했다. 실제 적용·다음 조건은 [E04-D2](docs/operations/execution-roadmap.md#e04-gpt-claude-20261028)를 따른다. 사용자 선택은 구독 CLI 우선·API 대체이며 PR #284에 연결/실측/검증을 완료했다. 운영 적용은 완료했으며 실제 서비스의 자연 호출 결과는 별도 인수한다.

고정 역할 정책은 `src/utils/llm_roles.py`의 `engine-roles-v8-fable52`다(V07 소스 11/13 — 고정 보고 역할 3개를 Fable 5.1 → 5.2로, heavy task 슬롯은 5.1 유지, 운영 11/13 13:30~; v7-terra는 L02 운영 11/11 22:38~, v6는 L01 Task 4 Claude 역할표, v5는 OpenAI 역할표). 일반 task와 고정 보고/복기 역할을 구분한다. task 슬롯 라우팅은 `config/default.yml` `llm.task_routes`(heavy/light: anthropic)로 켜며 코드 기본값은 openai다(롤백 = 설정 두 줄 + 재시작).

| 작업 | Primary | Fallback / 한도 |
|------|---------|----------|
| 테마·뉴스·일반 경량 분석·위키 생성(light 슬롯) | Claude Opus 5.5 / low (Anthropic API, `task_routes.light`) | fallback 없음; `response_format` 대신 system JSON 지시 + 기존 `_extract_json`; 기본 출력 상한 `max_output_tokens`(경량 절약 시 4096). `STOCK_MAPPING`(GPT-5.6 Terra/none, L02 운영 11/11 22:38~)·`REPORT_SUMMARY`(OpenAI heavy gpt-5.6-sol → light 폴백, 절약 모드에서 light)는 OpenAI 그대로 |
| 거시·한국경제·글로벌미시 한 줄 설명(`REPORT_ONE_LINE`) | Claude Opus 5.5 / low | 없음; 출력512·20초 |
| 아침 보고 고정 역할 | GPT-5.6 Terra / none (L02, 운영 11/11 22:38~; 종전 GPT-5 mini/minimal) | 없음; 출력1600·45초; 현행 아침 본문은 결정론적 조립 |
| 시장 조사 보고 고정 역할(`MARKET_BRIEF`) | Claude Fable 5.2 / medium (V07, 종전 5.1) | 없음; 출력6144(thinking 포함, 종전 4096 — 5.2는 medium 이상에서 thinking을 줄이지 않는다)·90초·입력64KiB·standard·JSON(system 지시, 완료 사유 `end_turn`); 시한은 첫 자연 슬롯 실측 뒤 별도 결정 |
| 주간 매도 후속 복기(`POST_EXIT_RESEARCH`) | Claude Fable 5.2 / medium (V07) | 없음; 출력6144·90초·research 예산 |
| 웹 데일리 리포트 고정 역할(`WEB_REPORT`) | Claude Fable 5.2 / medium (V07) | 없음; 출력16384(종전 12288)·180초·입력96KiB·standard·JSON(system 지시). 보수 추정 입력 16/출력 40 USD/MTok(1h 캐시 쓰기 상한, 5.1은 20/50) |
| 거래 복기·전략 진화·시장 심층 분석(heavy 슬롯) | Claude Fable 5.1 / medium (Anthropic API, `task_routes.heavy`; V07에서 5.1 유지 — 크로스검증 100·레짐 150토큰처럼 작은 출력 상한 소비자가 있어 5.2 전환은 상한 전수 점검 뒤) | 가용성 실패 시 종전대로 GPT light(gpt-5.6-terra/none, L02 운영 11/11 22:38~) 1회; 거절/401·403/예산 차단은 우회하지 않음 |
| Bull / 독립 Bear | Bull GPT light(gpt-5.6-terra/none, L02 운영 11/11 22:38~; 종전 gpt-5-mini/minimal) / Bear Claude Opus5.5·low | **L01 Task 2(운영 11/9 21:22~, `31edd820`): 예산 내 API 1회 우선·가용성 실패에서만 구독 CLI 1회 대체(남은 시한 ≥4초)** — 자연 호출 인수 DONE(11/10 48/48·11/16 40/40 모두 API 1차). 종전(10/28~11/9) 구독 CLI 우선·API 대체는 이력. 미완료 독립 검토를 단독 승인으로 바꾸지 않음 |

테마 탐지는 거래일 06:30~16:45 창에서 30분 주기다(V06, 운영 11/16 22:19~ — 종전 10분).

월 상한은 USD200(L01 Task 5 — 운영 원장은 11/11 18:28 이관 완료)·일 상한 12(11/13~)·80/95/100% 정책·KST 월 전환과 `missing_cost_policy=log_and_continue`를 유지한다. Gemini의 과거 가격·비용/불명 비용 원장은 보존하지만 신규 key/model 설정은 호출을 재활성화하지 못한다. 과거 R1 비교는 비활성 유지다. Claude 연결/모델 검증은 완료됐지만 실제 비교는 현재 Claude용 manifest/runner 지원·동일 입력/응답 후 가격·비용 상한 설계가 필요하며 옛 Gemini 합성 경로를 재활성화하지 않는다. 모델 변경은 정확도나 순수익 향상의 증거가 아니다. [이전 비교 근거와 현행 전환 링크](docs/research/engine-model-allocation-2026-10-15.md)를 참고한다.

---

## 긴급 정지 (킬스위치, 2026-08-02~)

파일 하나로 주문을 즉시 차단. **봇 재시작 불필요**, 브로커 계층에서 검사하므로 엔진 오작동 시에도 동작.

```bash
touch ~/.cache/ai_trader/KILL_SWITCH       # 신규 매수만 차단 (청산 허용)
touch ~/.cache/ai_trader/KILL_SWITCH_ALL   # 전면 동결 (⚠️ 손절도 막힘)
rm ~/.cache/ai_trader/KILL_SWITCH          # 해제
```

- 시장별: `KILL_SWITCH_KR` / `KILL_SWITCH_US`, 파일 내용은 차단 사유로 기록됨 (반영 최대 2초)
- 감사 원장: `~/.cache/ai_trader/audit/audit_YYYYMM.jsonl` (시도된 모든 주문 append-only)

## 진화 시스템

- 매일 20:30 자동 실행 (KR)
- `TradeReviewer` → `DailyReviewer` → `StrategyEvolver` → **`BacktestGate`**
- 최대 1개 파라미터만 변경 (race condition 방지)
- 평가 기간: 5영업일 + 10건 이상 거래
- 신뢰도 >= 0.6인 파라미터만 자동 적용
- **백테스트 사전 검증 (2026-08-02~)**: 적용 전 A/B 백테스트(6개월/60종목)로 개선 확인.
  수익률 개선 + **walk-forward 구간승 2/3** (2개월×3구간, 2026-08-03~) + MDD 악화 ≤1%p
  + 거래 ≥10건이어야 통과. 실패 시 **보류(fail-closed)**.
  `EVOLUTION_BACKTEST_GATE=0`으로 비활성화 가능
- 롤백: 적용 후 5영업일+10건 실거래에서 승률 -5%p 또는 손익비 -0.3 악화 시 (10영업일 초과·10건 미달도 보수적 롤백) — 코드 기준 (2026-09-13 문서 정정)
- 제안 경로 4종(내장 규칙·약점 마이너·일일 복기·LLM) 전부 게이트 경유, 14일 내 기각·롤백 파라미터는 소스 무관 재제안 억제 (2026-09-13 WikiSkill 정렬)
- `RUNTIME_WIKI=0`: 런타임(크로스검증 규칙#9·G4 LLM 2차·팀 심의)에 위키/메모리 비노출 — 기본 켜짐, 신호 이벤트 `wiki_context_used`·팀 verdict 태그로 영향 측정 후 결정
- 내장 규칙: 승률 < 40% → 진입 기준 +5, 승률 > 65% → 진입 기준 -5
- 결과는 `evolved_overrides.yml`에 영속화

## 주간 매도 후속 복기 (Post-Exit Review)

- 매주 토요일 09:30~09:44 KST 실행 창 (`run_post_exit_review_scheduler`, 실제 루프 기준; 이전 09:00 문서 정정)
- 최근 30일 KR 매도 거래 → KIS 현재가 조회 → 매도 후 변동 추적
- 분류: +3% 이상=놓침, -3% 이하=회피, 그 사이=타당
- LLM: Claude Fable 5.2 / medium (`POST_EXIT_RESEARCH`, 역할표 v8 — 운영 11/13 13:30~; v6~v7은 Fable 5.1, v5는 GPT-6.1 Sol), 출력6144·90초·research 예산, 자동 fallback 없음; 표본 ≥5건
- 출력: JSON + Wiki 페이지(`weekly_post_exit_YYYY-WNN.md`) + 텔레그램
- Wiki 페이지는 다음 weekly rebalance 시 LLM 컨텍스트로 자동 흡수

## Trade Wiki (Karpathy LLM Wiki 패턴)

- 거래 교훈을 전략/섹터/시장체제/**종목**별 마크다운 위키로 축적 (종목 차원 2026-08-07~)
- 위치: `~/.cache/ai_trader/wiki/`
- 3가지 오퍼레이션:
  - **Ingest**: 매도 체결 → 관련 위키 3~5개 페이지 자동 업데이트 + LLM 교훈 추출. 소스는 light 슬롯(`WIKI_INGEST`) — `task_routes.light: anthropic`이면 Claude Opus 5.5/low, 아니면 OpenAI light(gpt-5.6-terra/none, L02 운영 11/11~)
  - **Query**: 크로스검증 시 전략/섹터/체제별 교훈 + **종목 노트**(query_symbol) 컨텍스트 반환
  - **Lint**: 토요일 주간 헬스체크 (stale/저조 페이지 감지 + 180일 종목 페이지 아카이브)
- **종목 페이지** (`symbols/<코드>.md`): 거래 이력·교훈 + 전문가 리서치 노트
  (orchestrator가 9명 전문가 affected_symbols를 fire-and-forget 기록, 일일 출처 dedup,
  30개 롤링, 리서치 전용 신규 페이지 상한 200)
- 동시성: `asyncio.Lock`, fire-and-forget (매매 비차단)
- 크기 제한: 페이지 200줄, 로그 500줄, 전체 ~1MB

## US 엔진 고도화

- ATR 기반 포지션 사이징 (3개 전략 통일)
- SPY/QQQ 기반 시장 체제 판단 (`us_market_regime.py`)
- 크로스 검증 게이트 6규칙 (수급 제외, bear시 어닝스 허용)
- 체제별 파라미터: min_score_adj, max_daily_new_buys, position_mult_boost

---

## 트러블슈팅
- **전체 절차는 `docs/operations/runbook.md` 참조** — 봇 미응답, 싱글톤 락 충돌, 매수 미실행 체크리스트, 유령 포지션, WebSocket 중복 프로세스, DB 좀비 정리, 긴급 전량 매도

## 실행 방법
```bash
source venv/bin/activate
python scripts/run_trader.py --market kr                  # KR 실거래 (운영은 systemd 로만 기동 — 직접 실행 금지)
python scripts/run_trader.py --market kr --dry-run        # KR 테스트 — ⚠️ 운영 봇이 떠 있으면 실행 금지
# (2026-09-29 23:28 `bb03aa3` 부터 exit 1 로 거부 — 이전 코드는 PID 파일의 운영 봇을 SIGTERM/SIGKILL 했다)
# --market us / both: 미국 거래 영구 중단(2026-09-28) — 사용하지 않는다
```
