# 66차 B — 판단·호가 동시 관측 준비 (2026-10-13)

목적은 다음 작은 실측에서 **실제 LLM 판단 1건과 같은 종목·시간의 호가·장 상태를 평가 가능한 한 쌍**으로 확보하는 것이다. 기간을 늘리는 것보다 연결 한 쌍을 먼저 확인한다. 이번 단계는 준비·검증까지이며 새 날짜·배포 SHA·설정/연구 지문·단발 활성화는 10월14일 파일럿 결과와 배포 이후 상태를 확인해 확정한다. 기존 예약은 연장·재사용하지 않는다.

## PLAN — 무엇이 이미 연결돼 있고 무엇이 남았나

| 구성 | 상태 | 근거 |
|---|---|---|
| 신호 단계부터 관측 | 있음 — `runner-signal-window-v1` 은 `quote_admission=signal_created` 로 LLM 거절·수량0·리스크 탈락도 `signal`/`emit_result`/gate trace 에 남긴다 | 10월8일 원장 signal 3·emit_result 3·gate 279 |
| 장 상태 채널 | 있음 — `channels.market_state_evidence=true` 면 `_lease_channels` 가 후보마다 BOOK+체결(H0STCNT0)+장운영(H0STMKO0) 3채널을 배정하고 `accepts_market_state` 가 같은 lease·세대만 받는다. 윈도 버퍼는 `market_state` 레코드를 수용하고 CapturePlan 이 큐≥버퍼·저장 상한을 검사한다 | `quote_subscription.py`, `entry_observation_runtime.py` 293~338행 |
| 판단 기록 | main 에만 있음(64차, 운영 미배포) — `decision_id`·`signal_id`·시도별 모델/비용/예약 토큰·`decision_available_at` | PR #183 |
| 조인 | main 에만 있음(65차+PR #185) — 수신 시각 기준 첫 호가, 같은 연결·세대의 채널별 장 상태, 평가 불가 사유 | `scripts/report_decision_quotes.py` |
| **남은 것** | (1) 장 상태 증거를 켠 signal-window 프로필과 그 용량 산정 (2) 판단 기록이 켜진 SHA 의 운영 배포 (3) 구독 요청·ACK·수신을 구분한 판독 규칙 | 아래 |

## DO — 수집 설정 초안과 오프라인 통합 검사

10월8일 study(`20261008-signal-window-v1`)를 복사해 **채널 플래그·용량·일정만** 바꾼 초안을 실제 `CapturePlan.load`(manifest 경유)로 검증했다(스크래치 경로, 미설치).

| 항목 | 10월8일 | 초안(검증 통과) | 이유 |
|---|---|---|---|
| `channels.market_state_evidence` | 없음 | `true`, `market_state_max_records` 30,000 | 후보 5개 × 3채널 = 15 + 운영 여유 10 ≤ 등록 상한 41 |
| `buffer_capacity` / `queue_capacity` | 300,000 / 8,192 | **40,000 / 40,000** | 상태 증거 시 `queue ≥ buffer` 와 `buffer ≥ metadata(200×12=2,400)+max_records` 가 필수 |
| `max_bytes` | 402,653,184 | 2,703,362,048 (= 40,000×(65,536+2,048)+2,048) | 같은 검사의 저장 상한(실사용 아님, 10월8일 실사용 14.7MB) |
| 창 | 01:00→01:50(admission)→02:10Z | 01:00→01:30→01:50Z | `end − admission_end > markout 900초`, `as_of == end_at`, `fixed_at ≤ start` |
| 스캔/후보 | 12스캔·선정 100·gate 100 | 동일 | 선정 품질 변경 없음 |

통과 수치는 **상한 검사**이며 실제 유량(10월8일 ws_quote 22,177 + 상태 레코드)이 버퍼 40,000 안에 드는지는 파일럿 결과로 확인한다. 10월14일 파일럿의 상태 레코드 수가 그 근거다. 운영 SHA·소스/설정 지문(`source_version_ref`·`configuration_ref`)은 배포 뒤 다시 묶는다.

## SEE — 판독 규칙(사전 고정)

| 상황 | 판독 |
|---|---|
| 판단 0건 | gate trace 로 미도달 사유(점수·국면·중복/보유·예산·쿨다운) 분포를 보고한다. 임계값·매수 중지·국면·위험 제한을 바꿔 표본을 만들지 않는다 |
| 상태 미수신 | 그 종목의 판단은 `market_state_unknown` 으로 평가 불가. 사건이 없다고 정상·VI 없음으로 채우지 않는다 |
| 구독 지연 | `quote_subscription` 레코드의 요청·ACK·첫 수신 시각을 각각 보고. ACK 는 수신이 아니다 |
| 재연결 | 호가와 다른 `connection_id`/`generation` 의 상태는 쓰지 않는다(65차 조인 규칙) |
| 용량 초과 | 봉인의 `incomplete_reasons`(`market_state_record_limit` 등) → 그 원장의 모든 행은 `ledger_incomplete` |
| 조기 종료 | 미봉인 → `ledger_unverified`. 봉인 사실만으로 유효 표본·정상 시장을 인증하지 않는다 |
| 판단 ID↔신호 ID↔종목·시각·세대 대조 | `signal_events.metadata.llm_decision.decision_id` ↔ 월 파일 `decision_id` ↔ 원장 `signal.signal_id` ↔ 같은 종목 첫 `ws_quote.provenance.received_at > decision_available_at` ↔ 그 호가의 연결·세대 |

H1 의 `order_ready` 대상 실험과 모델 비교의 `signal` 대상 관측은 같은 원장에서 서로 다른 분모로 보고하되 혼합하지 않는다.

## 시작 조건

1. 10월14일 09:05 파일럿 종료·봉인·누락·상태 레코드 수·연결 세대·봇 건강 확인.
2. 장외·미체결 0·복구 ready 에서 리뷰·CI 통과 고정 SHA(판단 기록 포함) 배포, 기록기 활성 로그 확인.
3. 위 초안에 새 날짜·운영 SHA 지문을 묶어 설치기(`--check`)로 검증한 뒤 단발 활성화 예약 — 사용자 승인 범위 확인 뒤.
