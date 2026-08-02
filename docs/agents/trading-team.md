# 종목 단위 에이전트 팀 (`src/agents/`)

> 2026-08-02 신설. TradingAgents(TauricResearch) 구조 참고 + harness 아키텍처 패턴으로 정리.

## 왜 만들었나

기존 `src/experts/`의 도메인 전문가 8명은 **시장·섹터 레벨**만 판단한다
(ExpertOpinion: regime_bias, affected_sectors). 개별 종목에 대해
"이걸 사도 되는가", "보유분을 계속 들고 갈 것인가"를 팀으로 논의하는 계층이 없었다.

## 두 층의 구분 (혼동 주의)

| 층 | 위치 | 실행 시점 | 역할 |
|---|---|---|---|
| 개발·운영 보조 | `.claude/agents/` (13개) | Claude Code 세션 | 코드리뷰·분석 지원 |
| **런타임 매매** | **`src/experts/` + `src/agents/`** | **장중 자동** | **실제 매매 판단** |

harness 플러그인은 전자를 생성하는 도구다. 이 문서는 **후자**를 다룬다.

## 파이프라인

```
[전문가 풀]   도메인 전문가 8명 → 시장·섹터 컨텍스트        (src/experts, 재사용)
      ↓
[팬아웃/팬인] Analyst 3인 병렬                              LLM 미사용
      ↓
[생성-검증]   Bull/Bear 2라운드 토론                        LLM 2~4회
      ↓
[감독자]      Trader 종합 → 제안(방향+사이징)               LLM 미사용, 결정론적
      ↓
[생성-검증]   Risk 게이트(cross_validator 11규칙) → PM 승인
      ↓
TeamVerdict → ~/.cache/ai_trader/team_verdicts/ → 대시보드
```

### 왜 Analyst와 Trader는 LLM을 안 쓰나

지표 계산과 점수 합산은 답이 정해진 일이다. 확률적 모델을 넣으면
같은 입력에 다른 출력이 나와 백테스트·감사·회귀 테스트가 불가능해진다.
창의적 판단이 필요한 지점(반대 논거 발굴)에만 LLM을 쓴다.

## 구성 요소

| 파일 | 역할 |
|---|---|
| `types.py` | AnalystReport / DebateResult / TradeProposal / PMDecision / TeamVerdict |
| `analysts.py` | Fundamental(dart+validator) / Technical(indicators) / News(news_curator) |
| `researchers.py` | Bull(OpenAI) vs Bear(Gemini) 2라운드 토론 |
| `trader.py` | 종합 점수 → BUY/HOLD/SELL + 사이징 배수 |
| `portfolio_manager.py` | 최종 승인/거부 + 제한적 게이트 오버라이드 |
| `team.py` | 오케스트레이션, 동시 심의 제한, 결과 저장 |

## 토론 판정 규칙 (중요)

| 상황 | consensus | confidence | 의미 |
|---|---|---|---|
| 양측 일치 | True/False | 1.0 | 만장일치 |
| 의견 분열 | None | 0.5 | 불확실 — Trader가 -10 감점 |
| **단독 반대** | **False** | 0.5 | 존중 (안전 쪽) |
| **단독 긍정** | **None** | 0.3 | **합의로 승격하지 않음** |
| 양측 무응답 | — | 0.0 | failed → fail-open |

> ⚠️ **단독 응답을 합의로 취급하면 안 된다.**
> Bear는 "실패 시나리오를 찾아라"는 역할이라, Bull이 죽고 Bear만 남으면
> 구조적으로 반대 편향이 된다. 반대로 Bear의 ACCEPT는 "감수할 만한 리스크"라는
> 뜻이지 매수 추천이 아니다. 초기 구현이 이 둘을 혼동해 실측에서 오판이 나왔다.

## PM 오버라이드 정책

게이트를 뚫는 것은 통계적으로 불리한 베팅이다 — 2026-08-02 실측에서
차단 신호의 20영업일 수익률은 게이트별 -3.7% ~ -13.2%였다.
그래서 다음을 **모두** 만족할 때만 허용한다.

- 토론 만장일치 지지 (confidence 1.0)
- Trader 확신도 ≥ 0.75
- 차단 게이트가 `SOFT_GATES`에 속함
- 일일 한도(2회) 미소진

**절대 오버라이드 불가 (`HARD_GATES`)**
```
킬스위치 / 일일 손실 한도 / 현금·예산 부족 / 중복 보유 / exit_exempt
```
계좌 생존과 직결된다. "오늘은 확신이 있으니 손실 한도를 넘겨보자"가
계좌를 끝내는 전형적인 경로다.

오버라이드 시 사이징을 ×0.7로 낮추고, 감사 원장 기록 + 텔레그램 알림을 남긴다.

## 안전장치

- **exit_exempt 종목의 SELL 제안은 PM이 무효화** — 자동매도 금지(예: 087010 펩트론)는
  팀 판단보다 우선한다. 수동 판단 전용.
- **토론 실패 시** fail-open으로 매수는 허용하되 사이징을 ×0.7 이하로 제한.
- **게이트 조회 실패 시** fail-closed (미분류 게이트는 보수적 거부).
- **동시 심의 3건 제한** — LLM rate limit 보호.
- **결과 저장은 Lock + 원자적 교체** — 동시 심의가 서로 덮어쓰지 않도록.

## 대시보드

`/engine` 페이지의 "에이전트 팀 심의" 카드.

| API | 내용 |
|---|---|
| `GET /api/team/verdicts?limit=&approved=` | 오늘 심의 목록 |
| `GET /api/team/stats` | 합의율·입장변경률·오버라이드 잔여 |

## 실행 (2026-08-02~)

`kr_scheduler.run_team_deliberation` — 장중 **10:30 / 14:00** 2회.

| 대상 | 범위 |
|---|---|
| 매수 후보 | 최근 스크리닝 상위 5 (`bot._last_screened`) |
| 보유 종목 | 전체 재평가 (`portfolio.positions`) |

> ⚠️ **shadow 단계 — 주문을 내지 않는다.**
> 심의 결과를 저장·알림만 한다. 팀은 2026-08-02 신설이라 실전 데이터가 없고,
> 첫 통합 테스트에서 P0 결함이 3건 나왔다. 규칙 #11을 shadow_mode로 시작했던 것과
> 같은 방식으로, 며칠 관측해 판정 품질을 확인한 뒤 주문 경로 연결을 결정한다.

설정: `config/default.yml` → `kr.trading_team`
```yaml
kr:
  trading_team:
    enabled: true
    debate_rounds: 2
    allow_pm_override: true
    max_concurrent: 3
```

## 남은 작업

- [x] 파이프라인 구현 + 단위·통합 검증
- [x] 대시보드 카드 + API
- [x] 스케줄러 연결 (shadow)
- [ ] **shadow 관측 후 주문 경로 연결 판단** — 승인된 BUY를 실제 시그널로 태울지 결정
- [ ] 심의 결과 → Trade Wiki 학습 루프

## 튜닝 포인트

```python
# team.py
MAX_CONCURRENT = 3          # 동시 심의 (LLM rate limit)
DELIBERATION_TIMEOUT = 90.0

# researchers.py
ROUND_TIMEOUT = 20.0
MAX_TOKENS = 400            # ⚠️ gpt-5 계열은 이 값이 작으면 본문이 빈 문자열로 온다

# trader.py
BUY_THRESHOLD = 20
SELL_THRESHOLD = -30

# portfolio_manager.py
MIN_CONVICTION = 0.75
DAILY_OVERRIDE_LIMIT = 2
```
