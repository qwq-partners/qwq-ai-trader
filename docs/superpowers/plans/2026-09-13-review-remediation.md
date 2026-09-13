# Claude 분석 교차 리뷰 후속 수정·재검증 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development 또는 superpowers:executing-plans로 작업별 구현·검토를 수행한다. 이 문서는 계획이며, 구현·운영 변경 완료 보고가 아니다. 사용자의 실행 지시와 프로젝트 안전 경계를 먼저 적용한다.

**Goal:** 리뷰에서 재현한 결함을 수정하고, 실엔진과 시점이 맞는 백테스트·원장 계측을 확보한 뒤 위험 사이징의 적용 여부를 다시 판단한다.

**Architecture:** 기존 청산 판단의 의미를 보존하면서 손절 해석을 공통 함수로 추출한다. 신호·주문·체결에는 진입 위험 스냅샷을 연결하고, 백테스터·운영 게이트는 같은 유효 설정과 정보 시점을 사용한다. 동기화·하트비트 수정은 별도 PR로 분리한다.

**Tech Stack:** Python 3.12, asyncio, Decimal, pytest, pandas, YAML, Git feature 브랜치·격리 worktree.

**Spec:** 이 문서 §1~§3이 후속 구현의 설계 명세다. 기존 근거는 `docs/research/exit-policy-ab-2026-09.md`, `docs/risk/risk-and-exit.md`, `docs/operations/runbook.md`다. 기존 연구의 채택 결론은 아래 재검증이 끝나기 전 확정 근거로 사용하지 않는다.

## Global Constraints

- 작업 전에 `CLAUDE.md`, `CHANGELOG.md`, `docs/README.md`와 담당 분야 문서를 읽는다.
- `feature/*` 브랜치에서 작업한다. 운영 체크아웃의 브랜치를 전환하지 않는다.
- 사용자가 현재 요청에서 명시하지 않은 실거래 주문을 실행하지 않는다.
- 사용자가 현재 요청에서 명시하지 않은 운영 서버 SSH, `systemctl`, 재시작, 배포를 실행하지 않는다.
- 로컬 기본 검증에서는 외부 API와 운영 자격증명을 사용하지 않는다.
- `.env`, 로그, 캐시, PID, 거래 상태 파일을 커밋하지 않는다.
- 구현 후 관련 테스트, 전체 테스트, 비밀정보 검사를 수행한다. 코드 변경에는 CHANGELOG와 관련 문서를 함께 갱신한다.
- 구현자와 최종 리뷰어를 분리한다. 같은 파일은 동시에 수정하지 않는다.
- Claude 구현 브랜치의 교차 리뷰는 `bash scripts/dev/codex_review.sh`로 시도한다. 실행 환경 문제로 실패하면 그 사실과 대체 리뷰 범위를 명시한다.
- 이번 요청은 상세계획 작성이다. 이 문서를 생성한 것만으로 구현·push·PR·merge·배포·주문 변경이 승인되었다고 해석하지 않는다.

## 1. 기준점과 확인된 사실

기준 SHA: `de111b7`. 코드 리뷰 시 전체 테스트 145건, 관련 테스트 66건이 통과했다. 아래 결함은 그 테스트에 없는 경로를 합성 객체로 실행해 확인했다. 테스트 개수 자체를 시스템 안전성의 증거로 삼지 않는다.

| ID | 우선순위 | 재현한 문제 | 기준 코드 | 담당 작업 |
|---|---|---|---|---|
| F1 | P1 | 사이징은 ATR 손절을 가정하지만 신규 체결은 고정 손절 적용. SEPA ATR 1%에서 분모 4%와 실제 설정 5% 불일치 | `src/core/engine.py:2437`, `src/strategies/exit_manager.py:516`, `src/schedulers/kr_scheduler.py:2182` | T2 |
| F2 | P1 | T+1 시가 주문에 진입일 전체 봉의 ATR 사용. 미래 고저만 바꾸면 수량 116→87주 | `scripts/backtest_strategies.py:1308` | T5 |
| F3 | P1 | 평가액 양수·포지션 빈 응답·전부 매도 pending이면 재조회 방어 우회. pending 31분 사례에서 실제 포지션 삭제 | `src/schedulers/kr_scheduler.py:948` | T1 |
| F4 | P2 | 새 risk 태그가 원본 Signal에만 존재하고 이벤트 복사본·주문 캐시·체결 원장에는 없음 | `src/core/engine.py:2446`, `:2102`, `:1572` | T3 |
| F5 | P2 | 공시 조회 전 heartbeat 갱신. 40분간 5회 전부 실패해도 정체 아님. REST에도 유사 경로 | `src/schedulers/kr_scheduler.py:6595`, `:3967` | T4 |
| F6 | P2 | sync 등록 실패 재시도가 `_sync` 설정 대신 빈 설정을 사용 | `src/schedulers/kr_scheduler.py:1071`, `:2373` | T1 |
| F7 | P2 | risk 위험률·상한 변경의 운영 게이트가 기본 nominal 설정으로 실행되어 효과가 없음 | `src/core/evolution/backtest_gate.py:175`, `:250` | T6 |
| F8 | P1·추가 확인 | nominal 기준군도 시가 주문 자산을 당일 종가로 평가. 미래 종가만 바꾸면 신규 주문 250→275주 | `scripts/backtest_strategies.py:1316`, `:1533` | T5 |

추가로 계획에 반영할 사실:

- 신규 체결의 `atr_pct_hint`는 현재 트레일링용이다. 가격 이력이 없는 신규 등록에서 ATR 동적 손절은 만들어지지 않는다.
- KR 손절 판정은 `FeeCalculator.calculate_net_pnl()`의 **수수료 포함 순손익률**이다. 가격 하락률과 동일하지 않다.
- `evolved_overrides.yml`의 `risk_config.default_stop_loss_pct=2.8`이 기본 YAML의 4.0을 덮는다. 이것을 실제 ExitManager 손절로 오인하면 안 된다.
- 캘린더 부스트와 최소 3주 보정 이후의 최종 위험 상한도 검사해야 한다. 현재 테스트는 이 조합을 검증하지 않는다.
- 현재 연구의 risk 축은 수량 공식뿐 아니라 동시 슬롯도 5→7로 변경한다. 실엔진은 기존 가중 슬롯 정책을 사용한다.
- 연구 판정은 기대값·PF 개선, WF 2/3, MDD 악화 3pp 이내다. 운영 게이트는 총수익 개선, WF 2/3, MDD 악화 1pp 이내다. 같은 판정이 아니다.
- 기존 12개월 SEPA 결과는 총수익 23.6430%→22.7676%로, 운영 게이트의 총수익 개선 조건에 미달한다.
- 6개월·12개월 구간은 겹친다. 독립적인 외부 검증 두 번으로 표현하지 않는다.
- 주말의 `stale_loops={}`는 장중 작업 정상 여부를 증명하지 않는다.

## 2. 권고 결정

### 2.1 수정 방향

세 선택지 중 **A를 권고**한다.

| 선택 | 내용 | 판단 |
|---|---|---|
| A | 기존 청산을 기준으로 사이징을 정합화하고 모든 실험을 새 기준군으로 재계산 | 권고. 결함 수정과 청산 전략 변경을 분리할 수 있음 |
| B | ATR hint로 동적 손절을 새로 켜서 기존 사이징 공식에 청산을 맞춤 | 이번 범위 제외. SEPA·gap·VCP 손절 행동 자체가 달라져 별도 전략 검증이 필요 |
| C | 즉시 nominal로 복귀하고 나머지 수정은 나중에 수행 | 기본 대응으로 선택하지 않음. 명목 비중이 더 커질 수 있고 동기화·검증 결함도 남음 |

권고 순서: **동기화 방어 복구 → 실제 손절 기준 위험 상한·계측 연결 → 성공을 구분하는 하트비트 → 시점 정합 백테스터 → 실제 설정 게이트 → A/B 재판정 → 제한된 canary 검토**.

T1/T2/T5는 병행 착수할 수 있다. 이 순서는 완료 의존성·운영 우선순위이며 모든 작업을 직렬로 기다리라는 뜻은 아니다.

### 2.2 현재 운영에 대한 제안과 권한

- 구현·배포 실행 지시가 주어지면, 결함이 있는 risk 경로의 신규 진입을 검증 전까지 보류하는 것이 권고안이다. 기존 매수 전용 차단 수단을 검토하고 청산 경로는 유지한다. 설정·파일 조작은 별도의 명시적 운영 실행 범위 안에서만 수행한다.
- 현금 부족은 안전장치가 아니다. 입금·매도대금·계좌 평가 변화로 신규 주문 가능 여부가 바뀔 수 있다.
- 위험 사이징 미달 시 무조건 nominal로 자동 복귀시키지 않는다. 대체 모드의 주문 크기가 커질 수 있다. 신규 진입 보류와 원인 확인을 우선하고, 복귀 모드·범위는 배포안에 명시한다.
- 기존 보유 포지션의 청산 면제, 청산 단계, 주문 상태를 변경하거나 테스트 목적으로 주문하지 않는다.
- 전체 봇·청산을 막는 전면 킬스위치를 이 작업의 기본 대응으로 사용하지 않는다.

### 2.3 범위

포함: F1~F8, 위험 예산 최종 상한, 위험 원장, 백테스트 시점·실효 설정·판정 정합, 실패 하트비트, 재현 가능한 결과.

이번에 실행하지 않을 것: 레짐 단일화, 신규 전략 활성화, 규칙 #11 승격, 채널 청산 채택, 보유기간 연장, gap/VCP 백테스터 신규 구현, 펩트론 매매·입금 결정, 전체 엔진 재설계.

채널·보유 연장은 **채택 보류**로 유지한다. 기존 미래정보 포함 결과를 근거로 효과 부재가 확정되었다고 표현하지 않는다.

## 3. 작업 분담과 병합 순서

| 담당 | 최초 병렬 작업 | 후속 작업 | 파일 소유 경계 |
|---|---|---|---|
| A — 스케줄러 | T1 동기화·등록 재시도 | T4 하트비트, T2/T3 체결 연결 | `kr_scheduler.py`는 A가 순차 수정 |
| B — 위험·원장 | T2 순수 손절 함수·엔진 사이징 | T3 이벤트·원장 스냅샷 | `engine.py`, `exit_manager.py`, 위험 함수·원장 파일 |
| C — 검증 엔진 | T5 미래정보 제거 | T6 게이트 설정, T7 실험 | `backtest_strategies.py`, `backtest_gate.py`, A/B 러너 |
| D — 독립 리뷰 | F1~F8 재현과 인수 조건 검토 | A/B 근거 검산, 최종 통합 리뷰 | 기본 읽기 전용. 구현자 테스트 파일 동시 수정 금지 |

각 담당은 `feature/review-sync-safety`, `feature/review-risk-contract`, `feature/review-backtest-time` 등 별도 worktree에서 시작한다. D가 구현에 참여했다면 그 변경의 최종 리뷰는 다른 담당이 한다.

권고 PR 단위:

1. T1 동기화 안전성.
2. T2 손절 정합·최종 위험 상한. A가 최신 T1을 반영한 뒤 필요한 체결 연결만 적용.
3. T3 위험 스냅샷 영속화. T2에 의존.
4. T4 하트비트. A가 T1/T2/T3의 스케줄러 변경을 받은 뒤 진행.
5. T5 백테스트 시점 수정.
6. T6 설정·게이트 parity. T2/T5 인터페이스 확정 후 진행.
7. T7 연구 결과·판정 문서. T2/T3/T5/T6 통합 SHA로만 실행.

CHANGELOG·CLAUDE·docs/README는 통합 담당이 각 PR 마지막에 최신 main 기준으로 반영한다. 동일 파일을 여러 에이전트가 동시에 편집하지 않는다. 성급한 연속 merge 대신 매번 최신 main과 통합한 SHA에서 필수 `verify` 성공을 확인한다.

## T0. 재현 기준 고정

**산출물:** 결함별 재현·수정 전후 증거, 테스트 기준 SHA, 실험 입력 명세.

- [ ] 기준 SHA와 작업 트리 상태를 기록한다. 다른 변경이 있으면 새 기준을 명시하고 재현을 갱신한다.
- [ ] 테스트는 기존 `venv/bin/python` 또는 격리된 개발 venv로 실행한다. 운영 `.env`를 로드하지 않는다.
- [ ] F1~F8의 입력·실제 출력·기대 출력을 각 담당의 회귀 테스트로 먼저 고정한다. 실패 원인이 의존성 누락이 아니라 재현 대상 결함임을 확인한다.
- [ ] 추가 검증의 회계 단위를 고정한다: KRW Decimal, SL은 net-pnl %, 포지션 R은 최초 확정 위험금액 기준.
- [ ] 원본 `results/ab_exit_policy_2026-09.json`은 보존한다. 수정 결과로 덮어쓰지 않는다.

```bash
git status --short --branch
git rev-parse HEAD
venv/bin/python -m pytest tests/test_risk_sizing.py tests/test_sync_portfolio_characterization.py tests/test_exit_manager_characterization.py tests/test_backtest_exit_policy.py tests/test_loop_heartbeat.py -q -p no:cacheprovider
```

**완료 조건:** 독립 리뷰어가 합성 입력만으로 같은 결함을 재현한다. 실행 서버의 잔고·토큰·상태파일을 테스트 입력으로 사용하지 않는다.

## T1. 빈 응답 방어와 등록 재시도의 정책 복구

**Modify:** `src/schedulers/kr_scheduler.py`, `tests/test_sync_portfolio_characterization.py`, `docs/operations/runbook.md`, `CHANGELOG.md`.

**입출력:** KIS 잔고·포지션 응답 → 재시도/동기화 보류/확정 동기화. `_strategy_exit_params` → 최초 등록과 재시도에서 동일한 kwargs.

- [ ] 기존 테스트 파일의 `_make`, `_pos`, `_run`을 사용해 아래 회귀 테스트를 추가하고 실패를 확인한다.

```python
def test_empty_reply_with_stale_sell_pending_is_retried(monkeypatch):
    p = _pos("005930")
    sched, bot, sleeps = _make(
        monkeypatch, bot_positions=[p],
        balance={"stock_value": 105000, "available_cash": 100000},
        kis_seq=[{}, {"005930": p}],
    )
    bot._exit_pending_symbols.add("005930")
    bot._exit_pending_timestamps["005930"] = datetime.now() - timedelta(minutes=31)
    _run(sched)
    assert bot.broker.get_positions_calls == 2
    assert sleeps == [5]
    assert "005930" in bot.engine.portfolio.positions
    assert bot.exit_manager.removed == []
```

- [ ] 전체 빈 응답과 부분 누락의 재시도 조건을 다음처럼 분리한다.

```python
empty_inconsistent = bool(bot_symbols) and not kis_symbols and stock_value > 0
partial_missing = (bot_symbols - kis_symbols) - bot._exit_pending_symbols
needs_retry = empty_inconsistent or (bool(partial_missing) and stock_value > 0)
```

- [ ] 재시도 후에도 평가액 양수·전체 빈 응답이면 동기화 실패를 기록하고 상태를 보존한다. 매도 pending 시간·좀비 후보 여부가 이 방어를 우회하지 않게 한다.
- [ ] 평가액 0의 실제 빈 계좌, 부분 누락 회복, 기존 exit_exempt 3주기·재등장 카운터 초기화 동작을 회귀 테스트로 유지한다.
- [ ] `KRScheduler._resolve_registration_params(strategy)`를 추가해 최초 sync와 재시도에서 같이 호출한다. 반환 dict는 복사본이며 조회 우선순위는 `strategy → _sync → {}`다.

```python
def _resolve_registration_params(self, strategy):
    params = self.bot._strategy_exit_params
    fallback = params.get("_sync", {})
    return dict(params.get(strategy, fallback) if strategy else fallback)
```

- [ ] 전략 불명 포지션의 최초 등록을 1회 실패시키고 실제 `run_fill_check()` 재시도까지 실행한다. 최초와 재시도 모두 SL=3, TS=2, TP1=3, stale_high_days=2를 전달해야 한다.
- [ ] 신규 BUY 등록 예외·포지션 생성 지연·재시도 반복 실패도 점검한다. 실패 중에는 대기열을 유지하며, 이미 삭제된 포지션은 대기열에서 정리한다. 성공한 등록만 완료 처리한다.

**검증:** `venv/bin/python -m pytest tests/test_sync_portfolio_characterization.py tests/test_exit_manager_characterization.py -q`.

**완료 조건:** 불확실한 응답으로 포지션·익절 단계를 삭제하지 않고, 재시도 전후 청산 정책이 동일하다. API 호출 횟수 증가가 필요한 불일치 사례에 한정된다.

## T2. 실제 손절을 기준으로 위험 사이징 정합화

**Create:** `src/utils/stop_policy.py`, `tests/test_stop_policy.py`.

**Modify:** `src/core/engine.py`, `src/utils/sizing.py`, `src/strategies/exit_manager.py`, `scripts/run_trader.py`, `tests/test_risk_sizing.py`, `tests/test_exit_manager_characterization.py`, `docs/risk/risk-and-exit.md`, `CLAUDE.md`, `CHANGELOG.md`.

**제안 인터페이스:**

```python
from dataclasses import dataclass
from decimal import Decimal

@dataclass(frozen=True)
class StopDecision:
    stop_pct: Decimal
    source: str
    crash_capped: bool

def resolve_effective_stop(*, dynamic_stop_pct, fixed_stop_pct,
                           global_stop_pct, min_dynamic_stop_pct,
                           crash_stop_pct, is_core) -> StopDecision:
    supplied = (dynamic_stop_pct, fixed_stop_pct, global_stop_pct,
                min_dynamic_stop_pct, crash_stop_pct)
    if any(v is not None and (not v.is_finite() or v <= 0) for v in supplied):
        raise ValueError("유효하지 않은 손절 설정")
    if dynamic_stop_pct is not None:
        value = max(dynamic_stop_pct, min_dynamic_stop_pct)
        source = "dynamic"
    elif fixed_stop_pct is not None:
        value, source = fixed_stop_pct, "strategy"
    else:
        value, source = global_stop_pct, "global"
    capped = not is_core and crash_stop_pct is not None and value > crash_stop_pct
    if capped:
        value = crash_stop_pct
    if not value.is_finite() or value <= 0:
        raise ValueError("유효하지 않은 초기 손절폭")
    return StopDecision(value, source, capped)
```

이 함수의 비율 입력은 모두 `Decimal` 또는 `None`이다. 글로벌 손절과 동적 하한은 필수 `Decimal`이다. 형 변환은 호출 경계에서 수행하고 제공된 수치는 비교 전에 검사한다. 유효하지 않은 설정을 거부하는 부분은 명시적인 안전 보강이며, 기존 정상 입력에서의 손절 우선순위는 유지한다.

- [ ] 아래 테스트를 `tests/test_stop_policy.py`에 추가한다. core 예외와 global fallback도 같은 입력 표로 확장한다.

```python
from decimal import Decimal as D
from src.utils.stop_policy import resolve_effective_stop

def test_fixed_stop_is_not_dynamic_clamped():
    result = resolve_effective_stop(
        dynamic_stop_pct=None, fixed_stop_pct=D("3"), global_stop_pct=D("5"),
        min_dynamic_stop_pct=D("4"), crash_stop_pct=None, is_core=False)
    assert result.stop_pct == D("3")
    assert result.source == "strategy"

def test_dynamic_stop_then_crash_cap():
    result = resolve_effective_stop(
        dynamic_stop_pct=D("2"), fixed_stop_pct=D("5"), global_stop_pct=D("5"),
        min_dynamic_stop_pct=D("4"), crash_stop_pct=D("2.5"), is_core=False)
    assert result.stop_pct == D("2.5")
    assert result.crash_capped is True
```

- [ ] ExitManager의 기존 손절 우선순위 부분만 공통 함수 호출로 교체한다. ATR 계산·hint의 trailing 용도·익절/레짐/보유 규칙은 변경하지 않는다. 기존 특성화 테스트 전체가 통과해야 한다.
- [ ] 엔진에 `resolve_entry_stop(strategy) -> StopDecision` 콜백을 주입한다. 신규 fill과 동일한 전략별 설정·현재 config·급락 상태를 조회하고, 실제 신규 등록에 없는 `price_history`를 있다고 가정하지 않는다.
- [ ] 기존 `_exit_stop_params`만으로 분모를 만드는 경로를 위 콜백 기반으로 대체한다. ATR이 없더라도 SEPA의 실제 고정 SL=5%를 해석하며, `risk_config.default_stop_loss_pct=2.8`로 대체하지 않는다.
- [ ] 위험 모드에서 모든 오버레이·최소금액·최소 3주 보정이 끝난 뒤 아래 최종 불변조건을 검사하고, 초과 시 수량을 줄인다.

```text
budget = equity_at_decision × risk_per_trade_pct / 100
entry_cost(q) = price × q + FeeCalculator.calculate_buy_fee(price × q)
planned_risk(q) = entry_cost(q) × resolved_net_stop_pct / 100
최종 q: planned_risk(q) <= budget
동시에 현금·시장가 증거금·개별 비중·전략 잔여 예산·최소금액 조건 충족
```

- [ ] 이 상한은 `risk` 모드의 최종 상한이다. 증액 오버레이가 0.7%를 초과시키지 못하게 한다. 축소 오버레이를 상쇄하기 위해 수량을 다시 키우지 않는다. 상한 내 1~2주는 허용하되 최소금액 미달은 명시적 사유로 거부한다.
- [ ] 계좌 위험은 매수 비용에 대한 net SL로 계산한다. 왕복 수수료를 SL%에 다시 더하지 않는다. 시장가 증거금 1.3배는 별도 현금 제약이며 손실 예산과 혼동하지 않는다.
- [ ] 실제 `SignalEvent.from_signal`과 신규 등록 경로로 다음 인수 조건을 검증한다.

| 입력·상황 | 기대 결과 |
|---|---|
| SEPA ATR 1%, 6% | 현재 신규 fill에는 dynamic stop 없음. 두 경우 모두 실제 fixed SL 5%를 분모로 사용 |
| equity 1천만원, 가격 1만원, net SL 5%, 위험률 0.7% | 매수수수료 포함 최종 상한이면 최대 139주. 140주는 상한을 미세 초과 |
| 캘린더 1.1, 팀 부스트, LLM 배율, 3주 보정 | 최종 수량의 계획 위험이 상한 이내 |
| ATR 없음/0/문자열 오류 | 실제 fixed/global SL로 해석. 실제 SL까지 무효이면 신규 주문 거부·pending 미생성 |
| core 또는 nominal | 기존 결과 보존 |
| 레짐 변경·급락 cap | 기존 ExitManager와 동일 해석. 주문 시점과 체결 시점이 다르면 차이를 T3에 기록 |

**완료 조건:** 주문 진입 시 계획 위험의 최종 상한이 증명된다. 이를 갭·슬리피지·장중 정책 변경까지 포함한 실현손실 보장으로 표현하지 않는다.

## T3. 진입 위험 스냅샷과 canary 원장 연결

**Modify:** `src/core/engine.py`, `src/core/event.py`(복사 계약 테스트 중심), `src/schedulers/kr_scheduler.py`, `src/core/evolution/trade_journal.py`, `src/data/storage/trade_storage.py`, `src/strategies/exit_manager.py`의 기존 영속화 지점, `tests/test_risk_sizing.py`, `docs/risk/risk-and-exit.md`, `CHANGELOG.md`.

**Create:** `tests/test_entry_risk_lifecycle.py`.

**공유 계약:** 기존 JSON 메타데이터 안에 `entry_risk`를 저장한다. DB 테이블의 신규 컬럼·마이그레이션은 우선 도입하지 않는다. 아래 값은 예시이며 스키마 키는 고정한다.

```json
{
  "version": 1,
  "cohort_id": "risk-sepa-v1",
  "sizing_mode": "risk",
  "strategy": "sepa_trend",
  "stop_basis": "net_pnl",
  "stop_pct": "5.0",
  "stop_source": "strategy",
  "equity_at_decision": "10000000",
  "risk_budget_amount": "70000",
  "planned_price": "10000",
  "planned_quantity": 139,
  "planned_risk_amount": "69509.75"
}
```

- [ ] 기존 별도 복사본인 `event.metadata`와 `event.signal.metadata` 모두에 snapshot을 넣는다. 이벤트 전체를 공유 참조로 바꾸지 않는다.
- [ ] `_pending_signal_cache`와 `_log_sig`의 metadata allowlist를 연결한다. 체결 원장의 기존 `market_context`에 `entry_risk`를 병합해 저장한다.
- [ ] 실제 우선 구현인 `TradeStorage`의 JSONB 저장과 `TradeJournal` JSON 폴백을 모두 검증한다. DB 연결은 mock으로 대체하고 SQL 인자·직렬화된 JSON 안의 snapshot을 확인한다.
- [ ] 주문식별자·포지션식별자·신호시각·적용 SHA·유효 설정 hash를 기록한다. hash는 자격증명을 제외한 허용 설정만 대상으로 한다.
- [ ] 체결 시 실제 가격·누적 수량·실제 초기 SL로 `initial_risk_amount`를 따로 확정한다. 계획값을 덮어쓰지 말고 `planned_vs_filled_risk_delta`를 남긴다.
- [ ] 첫 매수 주문의 부분체결은 주문 완료까지 위험금액을 누적한다. 완료한 진입의 초기 위험금액은 부분매도·레짐 변경·재시작으로 바꾸지 않는다. 별도 추가 매수는 주문별 위험을 분리 기록한다.
- [ ] 재시작 전후 실제 SL이 바뀌는 기존 경로를 그대로 계측한다. 신규 위험 snapshot을 현재 SL에 강제 대입해 기존 청산 정책을 변경하지 않는다.
- [ ] 과거 거래·수동 포지션에 snapshot이 없으면 `legacy/unmeasured`로 분류한다. 현재 설정으로 초기 위험을 추정해 canary에 편입하지 않는다.
- [ ] 실제 `from_signal → 주문 캐시 → 모의 signal_events 저장 → 모의 체결 원장 저장 → 저장/복원`을 연결한 테스트를 작성한다. 저장 mock에 넘긴 인자까지 검사한다.
- [ ] 등록 재시도·복수 부분체결·부분청산·재시작·레짐 변경 사례에서 중복 기록과 R 분모 변경이 없음을 검사한다.

**R 정의:** 완결된 포지션의 순손익 합계 ÷ 최초 진입 주문 완료 시 확정한 위험금액. 미완결 포지션은 30건 판정에서 제외하고 별도 표시한다. 추가 매수로 lot 구분이 불가능한 사례는 자동 제외하고 제외 건수를 보고한다.

**완료 조건:** 운영 외부 데이터 없이 생명주기 왕복 테스트가 통과한다. 로그 문자열에 태그가 보이는 것만으로 완료 처리하지 않는다.

## T4. 하트비트가 작업 성공·실패·유휴를 구분하도록 수정

**Modify:** `src/utils/loop_heartbeat.py`, `src/schedulers/kr_scheduler.py`, `src/dashboard/data_collector.py`, `scripts/dev/ops_check.sh`, `tests/test_loop_heartbeat.py`, `docs/operations/runbook.md`, `CHANGELOG.md`.

**Create:** `tests/test_loop_heartbeat_integration.py`.

**계약:** 기존 `loops`·`stale_loops` 필드는 호환 유지. 추가 `loop_status`에 enabled, idle_reason, last_attempt, last_success, consecutive_failures, next_due를 노출한다. 구현 규모는 현재 감시 대상 9개에 한정한다.

- [ ] `record_attempt(name)`, `record_success(name)`, `record_failure(name)`, `record_idle(name, reason)`를 메모리 레지스트리에 정의한다. 성공 시각은 `record_success`만 갱신한다.
- [ ] DartChecker를 mock하고 10분 간격 5회 모두 예외를 주는 테스트를 작성한다. 40분 후 성공 시각이 갱신되지 않고 실패 누적 및 정체가 나타나야 한다.
- [ ] REST는 조회 대상이 있는데 성공 0건이면 실패다. 조회 대상 0건 또는 WS로 전부 정상 커버되는 경우는 사유 있는 유휴다. 실패를 유휴로 분류하지 않는다.
- [ ] DART는 보유 종목 전부 실패하면 실패다. 일부 성공·일부 실패는 degraded로 표시하고 실패 종목 수를 기록한다. 정체 기준을 최근 성공으로만 가리면 실패 카운트를 별도 노출한다.
- [ ] 진화 스케줄러에서 예외를 삼킨 뒤 `beat()`하는 경로도 옮긴다. `waiting/keep/변경 없음`은 정상 평가 완료, 예외는 실패다.
- [ ] 비활성 기능은 enabled=false로 표시하며 정체 경보에서 제외한다. 활성인데 초기화 실패한 기능은 disabled로 위장하지 않는다.
- [ ] 일일 잡은 해당 거래일 예정시각 + 60분의 grace로 미완료를 판정한다. 수확 08:40, 변동성 08:30, 진화 20:30을 각 스케줄의 동일 출처에서 가져온다. 주말·휴장일·재시작·이미 완료된 날짜의 상태 복원을 테스트한다.
- [ ] 기존 60초 감시·루프별 시간당 1회 경보 제한을 유지한다. 테스트 알림은 mock으로만 검증한다. 자동 재시작 기능은 추가하지 않는다.

```bash
venv/bin/python -m pytest tests/test_loop_heartbeat.py tests/test_loop_heartbeat_integration.py -q
```

**완료 조건:** 실제 호출 실패를 주입한 스케줄러 테스트에서 장애가 드러나며, 휴장·정상 유휴·비활성 기능에는 거짓 경보가 없다.

## T5. 백테스트의 정보 시점과 초기 손절 수정

**Modify:** `scripts/backtest_strategies.py`, `tests/test_backtest_exit_policy.py`, `docs/research/exit-policy-ab-2026-09.md`, `CHANGELOG.md`.

**Create:** `tests/test_backtest_point_in_time.py`.

**인터페이스:** `BacktestEngine._calc_equity(day_str, *, phase="close")`로 평가 시점을 분리한다. `_execute_pending_buys`의 시가 체결은 phase="open", EOD 성과 기록은 phase="close"를 사용한다. 신규 `_entry_atr(symbol, day_str)`는 **day_str보다 이전의 마지막 확정 거래봉**만 사용한다.

- [ ] 미래 당일 고저·종가만 바꾼 두 합성 시계열을 만든다. 같은 과거와 같은 시가에서 nominal/risk 각각의 시가 수량·초기 손절이 동일해야 하는 테스트를 작성해 기존 코드의 실패를 확인한다.
- [ ] 보유 종목 500주·현금 500만원·모든 시가 1만원의 합성 계좌에서 미래 종가를 1만→1.2만원으로 바꾼다. 신규 시가 수량이 달라지면 실패다.
- [ ] 시가 평가에는 그날 시가만 사용한다. 그날 시가가 없는 종목은 마지막 확정 종가로 평가하며, 신규 주문 종목의 시가가 없으면 체결을 만들지 않는다.
- [ ] ATR·채널·신호 지표가 언제 알려졌는지 분리한다. 전일 신호는 전일 종가 이후 정보까지만, T+1 시가 주문은 시가까지 알려진 정보만 사용한다. 당일 종가 데이터는 EOD 판단에서만 사용한다.
- [ ] `pending_buys`에 `signal_date`, `indicator_asof`를 기록한다. 휴일·거래정지·누락봉에서도 날짜가 체결일보다 앞서는지 검사한다.
- [ ] T2의 실제 신규 진입 손절 정책을 백테스트에 명시적 `entry_stop_mode`로 연결한다. 기본 재검증은 `live_policy`다. 신규 ATR 동적 손절은 `atr_dynamic`이라는 별도 연구 축으로 분리하고 운영 후보에 섞지 않는다.
- [ ] 진입 시 R 분모와 초기 손절을 저장한다. 매일 ATR을 다시 계산하여 이미 정한 초기 위험을 소급 변경하지 않는다. 실제 엔진에 있는 후속 SL 변경은 별도 상태 전이로 반영한다.
- [ ] 같은 봉에서 익절·손절을 모두 접촉하는 모호성, 갭 손절, 부분매도 순서, 복합 청산·stale 순서를 합성 시나리오로 검증한다. 일봉으로 모르는 체결 순서는 보수적 규칙을 문서화하고 전 셀에 동일 적용한다.
- [ ] 위 순서 검증과 T6 parity가 끝난 뒤 **nominal 기준군까지 전부 다시 계산**한다. 수정 risk를 과거 JSON baseline과 비교하지 않는다.

```bash
venv/bin/python -m pytest tests/test_backtest_point_in_time.py tests/test_backtest_exit_policy.py -q
```

**완료 조건:** 미래 정보만 바꿔도 이미 발생한 주문이 바뀌지 않는다. 미래정보 제거는 성과가 좋아지는지와 무관한 필수 통과 조건이다.

## T6. 유효 설정·운영 게이트·실거래 parity

**Modify:** `src/core/evolution/backtest_gate.py`, `scripts/backtest_strategies.py`, `scripts/ab_exit_policy.py`, `src/utils/config.py`(필요 시 순수 병합부만), `docs/evolution/evolution-system.md`, `docs/research/exit-policy-ab-2026-09.md`, `CHANGELOG.md`.

**Create:** `tests/test_backtest_gate_config.py`, `tests/test_live_backtest_parity.py`.

**공유 계약:** 새 `build_backtest_config_from_effective(effective_config: dict, *, months: int, strategies: list[str]) -> BacktestConfig`를 백테스트 스크립트에 정의한다. 운영 게이트와 A/B 러너가 같은 builder를 사용한다. `effective_config`는 기본 YAML과 overrides를 실제 우선순위로 병합한 비밀정보 없는 설정이다.

- [ ] risk 모드의 위험률 0.7→0.8, 상한 18→10 변경이 포지션 금액을 바꾸는 테스트를 작성한다. 상한 변화 테스트는 실제 cap이 걸리는 손절폭·위험률을 사용한다.
- [ ] 현재 base 설정을 생성한 뒤 deepcopy하여 **요청 변경 필드만** candidate에 적용한다. baseline에서 현행 risk 모드가 nominal로 되돌아가지 않아야 한다.
- [ ] sizing·전략 활성/배분·초기 손절·복합 청산·stale·수수료·현금 제약·슬롯 의미를 명시적으로 매핑한다. 현재 설정을 지원하지 못하면 `passed=False`와 구체적 이유를 반환한다. 관련 risk 변경을 `skipped=True` 성공으로 처리하지 않는다.
- [ ] 작은 합성 주문/가격 시퀀스를 실엔진과 백테스트에 넣고 초기 손절, 수량, 손절 시점, 익절 수량·단계, 비용·R을 비교한다. 차이가 남으면 무엇이 미지원인지 결과에 기록하고 해당 범위의 승격은 보류한다.
- [ ] 슬롯은 양쪽 모두 같은 정책을 사용한다. 실효 8개 가중 슬롯을 재현하지 못하면 단순 5/7슬롯 실험을 운영 검증이라고 부르지 않는다. 이번에 필요한 슬롯 계산만 순수 계산으로 맞추고 포트폴리오 전체 재설계는 하지 않는다.
- [ ] 운영 게이트 판정 함수는 유지한다. 수익률 개선 조건을 연구의 기대값/PF 조건으로 몰래 교체하지 않는다.
- [ ] 데이터 부족·예외·필수 시계열 누락·WF 평가 불가이면 이번 승격은 보류한다. 기존 코드의 WF 생략 경로가 승인 근거가 되지 않게 결과에 coverage를 남긴다.
- [ ] SEPA-only와 gap/VCP 미지원 범위를 표시한다. 미지원 전략을 제외한 부분 검증 결과로 KR 전체 정책이 통과했다고 보고하지 않는다.

**완료 조건:** 유효 설정 diff·설정 hash·지원 범위·같은 판정 함수의 결과를 재현할 수 있다. 합성 parity가 실패하면 대형 A/B를 시작하지 않는다.

## T7. 작은 실험부터 재검증하고 결과를 보존

**Modify:** `scripts/ab_exit_policy.py`, `docs/research/exit-policy-ab-2026-09.md`, `docs/README.md`, `CLAUDE.md`, `CHANGELOG.md`.

**Create:** `results/ab_exit_policy_review_v2/` 아래 manifest·summary·positions·fills·equity 파일, `docs/research/risk-sizing-revalidation-2026-09.md`.

### T7-A. 실행 전에 고정할 입력

- [ ] 통합 SHA, 설정 snapshot/hash, 계산기 버전, 유니버스·OHLCV 식별 hash, 난수 사용 여부를 manifest에 저장한다.
- [ ] 기존 연구와 같은 마지막 완결 거래일 `2026-09-11`을 사용한다. 6m `2026-03-17~2026-09-11`, 12m `2025-09-18~2026-09-11`을 고정한다.
- [ ] 기본 검증은 고정된 비운영 연구 데이터로 offline 실행한다. 입력이 없으면 데이터 부족으로 종료하고 다운로드·운영 캐시 복사로 자동 전환하지 않는다. 외부 데이터 수집은 별도 명시 범위에서 수행한다.
- [ ] 러너에 `--offline`, `--end-date`, `--output-dir`, `--entry-stop-mode`, `--slot-policy` 옵션을 구현하고 manifest와 CLI 인자가 일치하는지 작은 테스트로 검증한다.
- [ ] 데이터 누락·비정상 봉·유니버스 생존편향·시장충격/슬리피지 미반영 여부를 기록한다. 결과를 유리하게 만드는 종목 제거를 사후 수행하지 않는다.

### T7-B. 먼저 수행할 4셀

| 축 | 고정 또는 비교 |
|---|---|
| 전략 | SEPA 단독, 실제 예산 40%, 잔여 현금 |
| 청산·보유 | 현행 ladder/current, T2/T6로 맞춘 초기 손절 |
| 수량 방식 | nominal / risk 0.7%, 상한 18% |
| 슬롯 | 두 방식 모두 동일한 실효 슬롯 정책 |
| 기간 | 6m / 12m |
| 기타 | 동일 신호·순서·수수료·현금·오버레이·체결 규칙 |

- [ ] 4셀로 수량 방식의 효과를 먼저 비교한다. 선택적으로 고정 14% 명목 배정 대조군을 기간별 1셀씩 추가해 총 6셀로 노출 축소 효과를 분해한다. 14%는 `0.7/5`의 사전 기준이며 결과를 보고 바꾸지 않는다.
- [ ] 의사결정 시점·신호·초기 위험·체결·왕복 포지션·일별 자산을 저장한다. R, PF, 비용, 회전, 상위 3건 제외 결과를 저장 데이터만으로 재계산할 수 있어야 한다.
- [ ] risk가 실제 fixed SL 기준으로 거의 고정 비중이 되면 그대로 보고한다. ATR에 따른 적응 효과가 생긴 것처럼 해석하지 않는다.

### T7-C. 판정 기준

**기술적 필수 조건:** 미래정보 테스트·실효 설정 parity·원장 재계산·경계값 테스트 전부 통과. 하나라도 실패하면 성과와 무관하게 승격 불가.

**운영 게이트 승격 후보 조건:** 양 윈도우 모두 총수익 개선 >0pp, MDD 악화 ≤1pp, 동일 구간 WF 승수 ≥2/3, 기존 운영 최소 거래 수 ≥10. WF가 없으면 보류한다. 최소 거래 10건은 통계적 신뢰성 기준이 아니라 기존 기술적 하한이다.

**함께 보고할 지표:** 포지션 수, 평균/중앙 R, PF, 총수익, MDD, 평균 노출, 거래 비용, 회전, 보유기간, 최대 연패, 상위 3건 제외 순손익, KODEX200 대비 초과수익. 수익률이 낮고 위험만 줄어든 경우는 별도 위험감축 정책 후보이며 운영 게이트 통과로 이름을 바꾸지 않는다.

**성과 미달 시:** window·슬롯·위험률·상한·판정 조건을 바꿔 통과 셀을 찾는 작업으로 전환하지 않는다. 미달 사유와 어떤 가설이 남았는지 보고한다. 새 가설은 별도 연구 계획이다.

### T7-D. 후순위 청산·보유 재평가

- [ ] 이번 필수 산출물은 T7-B/C다. 채널·보유기간 실험 때문에 결함 수정 PR을 지연하지 않는다.
- [ ] 별도 연구 실행 범위가 주어지면 `청산 2 × 보유 2 × 사이징 2 × 기간 2 = 16셀`로 교호작용을 다시 검사한다. 입력·코드가 같다면 앞선 4셀을 재사용하고 12셀만 추가한다.
- [ ] `none` 보유정책·폐지 RSI2 혼합은 진단용 후속이다. 본 실험의 승격 기준군으로 섞지 않는다.

**완료 조건:** 실패·미달 결과도 동일한 형식으로 저장하고, 독립 리뷰어가 raw 파일에서 표와 판정을 재계산한다. 기존 결과·수정 결과·채택 여부를 별도 열로 보여준다.

## T8. canary·배포 제안서와 관측 종료 조건

**Modify:** `docs/operations/monitoring-checkpoints.md`, `docs/operations/runbook.md`, `docs/risk/risk-and-exit.md`.

**Create:** `scripts/review_risk_canary.py`, `tests/test_risk_canary_report.py`. 보고 도구는 입력 원장을 읽는 offline CLI이며 주문·설정 변경 기능이 없다.

**CLI 계약:** `--input <검증용 포지션 원장 JSON> --benchmark <고정 벤치마크 CSV> --cohort risk-sepa-v1 --output <리포트 JSON>`을 받는다. 파일 누락·필수 필드 오류에는 비정상 종료하고 부족 항목을 출력한다. 데이터가 정상이나 완결 30건 미만이면 정상 종료하면서 `status: insufficient_sample`을 반환한다. 성과 판정과 파일/계측 오류를 종료 코드만으로 혼동하지 않는다.

### 적용 전

- [ ] 채택 제안에는 대상 SHA, 대상 전략·계좌 범위, 기존 포지션 처리, 설정 diff, 검증된/미검증된 범위, 복구 방법을 넣는다. source diff 승인과 전략 채택 승인은 구별한다.
- [ ] SEPA 결과만으로 gap/VCP 전체 risk 적용을 승인하지 않는다. 전략별 sizing 모드가 필요하면 `risk.sizing_modes_by_strategy` map을 추가하고, 값은 명시적인 `nominal|risk`만 허용한다. map에 없는 전략은 기존 전역 모드를 유지해 조용한 모드 변경을 막는다.
- [ ] canary 적용 제안은 검증된 SEPA 범위로 제한한다. 미검증 전략의 신규 진입을 계속 허용할지·보류할지는 배포안에 명시한다. 명시가 없으면 KR 전체 자동 적용을 진행하지 않는다.
- [ ] 코드 수정 완료만으로 배포하지 않는다. 프로젝트의 현재 요청 안전 경계에 따라, 운영 변경 실행 범위가 명시된 뒤 기존 배포 절차를 사용한다.
- [ ] 배포 시점 pending, 버전, 메모리, 복구 SHA, 읽기 전용 API 상태, 유효 설정을 확인한다. `.env` 내용·토큰·계좌번호는 출력하지 않는다.

### 첫 5·10·30개 완결 포지션

| 시점 | 확인 항목 | 통과·중단 기준 |
|---|---|---|
| 첫 5개 | 계획/체결 수량, 실제 SL, 초기 위험금액, 이벤트→원장 연결을 건별 대조 | 필수 필드 누락·중복·계획 위험 초과·SL 해석 불일치가 한 건이라도 있으면 기술 검증 실패 |
| 10개 | 부분체결·부분청산·재시작 사례 포함 상태 정합, 수수료·R 재계산 | 재계산 불일치·원장 유실 없음. 실제 시장가 체결 차이는 별도 분류하고 반복되면 사이징 가정 재검토 |
| 30개 | 완결 포지션 기준 평균 R·PF·연패·비용·회전·초과수익·초기 위험 편차 | 기술 조건 전부 충족해야 다음 검토로 이동. 30건만으로 엣지 입증·자동 확대하지 않음 |

- [ ] 표본은 `cohort_id`·적용 SHA로 고정한다. 수동·legacy·미완결·초기 위험 누락 포지션은 제외 사유와 수를 표시한다.
- [ ] 20거래일이 지나도 30건 미만이면 `표본 부족/판정 보류`를 출력한다. 표본을 만들려고 거래를 늘리지 않고, nominal 자동 복귀도 하지 않는다.
- [ ] 초기 위험 상한은 주문 계획 시점 기준이다. 체결 슬리피지·갭 손절·체결 사이 정책 변경은 별도 경보와 분석 사유다. 이를 모두 소프트웨어 위반으로 단정하거나 반대로 모두 정상으로 묶지 않는다.
- [ ] KODEX200 비교는 진입 시점과 각 부분청산 시점의 동일 구간 수익률을 청산 수량 비중으로 가중한다. 예: 40%를 t1, 60%를 t2에 청산했다면 benchmark=0.4×B(entry,t1)+0.6×B(entry,t2). 포지션 순수익률에서 이를 빼고 가격 시점·비용 가정을 함께 기록한다.
- [ ] 벤치마크 입력이 없으면 초과수익은 null/미측정이다. 0으로 채워 통과시키지 않는다.
- [ ] 30건에서 평균 R≤0, PF≤1 또는 평균 동일기간 초과수익≤0이면 확대 보류로 보고한다. 반대 경우도 자동 승격이 아니라 추가 기간·표본 검토 대상으로 보고한다. 이는 보수적 확대 검토 규칙이며 통계적 유의성 판정이 아니다.

### 실제 장중 상태 확인

- [ ] 배포 직후 정상 기동과 장중 정상 동작을 별도 확인한다. 다음 거래일 09:00~09:30과 하루 전체의 KIS 코드별 오류·재시도·동기화 보류·pending age·하트비트 실패를 본다.
- [ ] EGW00201·EGW00215·토큰 오류를 분리하고, 오류 0건이라는 보고에는 관측 시작·종료 시각을 붙인다.
- [ ] 하트비트가 등록한 9개 루프만 확인한 사실을 명시한다. 26개 태스크 전체가 검증되었다고 확대하지 않는다.

**완료 조건:** 코드 수정 완료, 연구 검증 완료, 배포 확인, 장중 관측, canary 판정을 각각 별도 상태로 보고한다.

## 4. 공통 검증과 최종 전달 형식

각 구현 PR은 아래 순서를 따른다. 테스트 수를 임의로 목표로 삼지 않고 위 인수 사례가 모두 들어갔는지 확인한다.

```bash
# 담당 회귀 테스트 실패를 먼저 확인하고, 최소 수정 후 해당 테스트를 다시 실행한다.
# 저장소 표준: 문법 → 전체 tests/ → 비밀정보 검사
bash scripts/dev/verify.sh

# Claude 구현 feature 브랜치의 읽기 전용 교차 리뷰
bash scripts/dev/codex_review.sh

git diff --check
git status --short --branch
```

worktree에 venv가 없으면 이미 준비된 개발 interpreter 경로를 `QWQ_VERIFY_PYTHON`으로 지정할 수 있다. 테스트 소스는 반드시 해당 worktree에서 읽게 한다. 운영 venv에 새 패키지를 설치하지 않는다.

이 환경에서 교차 리뷰 CLI는 `bwrap: loopback: Failed RTM_NEWADDR: Operation not permitted`로 실패한 이력이 있다. 실패 시 성공으로 기록하지 않는다. 샌드박스를 무력화하지 말고 현재 허용된 읽기 전용 도구로 독립 리뷰를 수행하여 대체 범위·제약을 명시한다.

최종 보고에는 다음을 포함한다.

1. F1~F8별 수정 커밋·실패하던 테스트·수정 후 결과·미해결 사항.
2. 전체 검증 결과와 실제 검사 SHA. PR 업데이트 전의 성공 check를 재사용하지 않는다.
3. 실효 설정 snapshot 및 baseline/candidate 차이. 자격증명은 포함하지 않는다.
4. 저장된 입력·체결·포지션·곡선에서 재계산한 A/B 결과와 운영 게이트 판정.
5. 검증 범위 밖인 전략·일봉 체결 근사·겹치는 기간·표본 부족 등 결론의 한계.
6. 운영 변경을 했다면 명시적으로 승인된 범위·대상 SHA·기동/장중 관측 시각. 안 했다면 배포 전임을 표시한다.
7. canary는 `미시작/수집 중/기술 실패/표본 부족/확대 보류/추가 검토` 중 실제 상태로 보고한다.

## 5. Claude에게 전달할 실행 요약

이 계획의 기준은 main `de111b7`이며, 목표는 기존 PR #25~#30을 무조건 유지하거나 뒤집는 것이 아니라 재현된 결함을 고치고 올바른 근거로 다시 판단하는 것이다.

먼저 F1~F8을 실패하는 회귀 테스트로 고정하라. 신규 체결의 실제 고정 손절 의미를 유지한 채 공통 손절 해석·최종 위험 상한·원장 스냅샷을 연결하라. ATR hint로 동적 손절을 새로 활성화하지 마라. sync 빈 응답 방어와 `_sync` 재시도 설정을 복구하고, 하트비트는 실제 성공과 실패를 구분하라.

백테스터는 당일 ATR뿐 아니라 시가 주문의 당일 종가 자산 평가도 제거하라. nominal과 risk 기준군 모두 다시 계산하고, 슬롯·예산·청산·보유·오버레이를 동일하게 고정하라. 운영 게이트에는 실제 유효 설정을 넣고 연구 판정과 운영 판정을 구분하라. 먼저 4셀로 사이징만 비교하며, 결과를 본 뒤 기준을 바꾸지 마라.

같은 `kr_scheduler.py`를 동시에 수정하지 말고 위 파일 소유권을 따라라. 관련 테스트·전체 검증·독립 리뷰·문서 갱신 후 변경과 근거를 전달하라. 운영 배포·주문 변경은 현재 사용자 실행 지시가 명시한 범위 안에서만 수행하라. 30건 canary를 엣지 증명이나 자동 nominal 복귀 조건으로 사용하지 마라.
