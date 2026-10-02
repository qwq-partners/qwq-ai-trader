# Live-screening gate trace — 39차 설계

## 목적과 범위

38차 실제 pilot의 반환 후보9/신호0을 사후 추측하지 않도록, 다음 명시 관측에서 **어떤 후보가 실제로 어느 조건까지 평가되고 왜 진입 신호를 만들지 못했는지** 설명한다. 비용 후 선정·진입 개선을 검증하기 위한 근거이며 수익/매수 승인 기능이 아니다. 사용자의 순차 Plan→Do→See 진행 요청과 이번 계속 지시에 따라 로컬 구현·검증·독립 리뷰를 진행한다. 운영 주문·API·계좌 원본·SSH·배포·재시작·설정·새 수집 예약은 범위 밖이다.

기준은 main `dafd4766a55868b6456c0ea9a9796791a95985fc`. 대상은 `run_screening`의 `live_screening` 경로다. 별도 `intraday_buy`/`sector_surge`/engine 주문 게이트는 이 기록으로 통과했다고 표시하지 않는다. 최초 pilot5468208과 기존 v1/v2는 새 필드를 갖지 않는다.

## 선택한 방식

사후 조건 재계산은 실제 short-circuit/시각을 복원하지 못한다. 로그 파싱은 전체 분모·생략 단계·스키마를 보장하지 못한다. **기존 조건의 실제 결과를 동기식 유한 trace에 복사하고 기존 원장에 후보별 한 행으로 저장**한다. 거래 조건·순서·외부 호출·예약/카운터/emit 동작을 바꾸지 않는다. 관측 예외는 기존 흐름 밖으로 전파하지 않는다.

새 `runner-first-scan-v3`만 `selection_basis`와 `entry_gate_trace` 설정을 명시한다. 기존 v1/v2 계약과 기본 비활성 동작은 유지한다. 새 study/epoch/소스 참조가 필요하며 설치/수집은 자동 실행하지 않는다.

## 계약

- `entry_gate_trace` 설정: `version=entry-gate-trace-v1`, `policy_ref=kr-live-screening-gates-v1`, `max_candidates` 정수1..100, `source_version_ref`, `configuration_ref` 비어 있지 않은 최대200자 문자열. source/config 참조는 capture의 같은 선언과 일치해야 한다. 참조는 실제 설정/출처 인증이 아니다.
- scan에 `entry_gate_trace_expected`, trace version/policy/source/config 참조를 저장한다. 원래 후보 순서/ID/분모를 보존한다. 후보 한도 초과 시 기록 일부를 정상 전체로 표시하지 않는다.
- `entry_gate_trace` 행: `candidate_id`, `symbol`, `observed_at`, `trace_version`, `policy_ref`, `outcome`, `terminal_reason`, `signal_id`, `steps`.
- step은 `stage`, `status`, `observed_at`, `reason`, `value`, `threshold`의 scalar 허용 필드만 가진다. 원문 뉴스·예외 문자열·객체·자격증명은 기록하지 않는다. 값은 유한 scalar 또는 null이고 문자열은 제한한다.
- 실제 평가 `pass/fail`, 해당 전략에 적용되지 않음 `not_applicable`, 오류로 판단 근거 불명 `unknown`을 구분한다. 실제 평가하지 않은 조건을 재계산해 pass/fail로 채우지 않는다.
- terminal `blocked`는 실제 fail, `signal_created`는 실제 SignalEvent 생성과 ID, `not_reached`는 batch/신호 개수 제한 또는 앞선 emit 실패, `unknown`은 미완료/예외다. 신호 생성은 주문 승인·접수·체결이 아니다.
- 미기록 후속 단계는 명시 terminal 뒤에는 `not_reached`, trace 미완결/옛 자료에는 `unknown/unavailable`로 보고한다. 후보별 실제 최초 fail과 이전 근거 결측을 함께 표시한다.
- 단계 순서: enabled, session, engine, broker, entry_time, regime, cash, score, excluded, cooldown, daily_count, scan_change, strategy, batch_limit, signal_limit, sector, quote_fetch, quote_price, rt_min_change, rt_max_change, open_price, volume, momentum_volume, momentum_strength, rsi, early_supply, news, atr, risk_reward, chase, signal_created.
- 전역 조건은 실제로 도달한 순서대로 전체 반환 후보에 반영한다. score/exclusion/cooldown/count/change는 기존 and-chain 순서를 보존한다. 조회/검증 실패의 기존 계속/중단 정책은 보존하고 fallback/미적용 사유를 구분한다.
- 기록은 후보≤100·단계≤31로 제한한다. publish 실패·종료/기간 경계·버퍼/큐 초과는 incomplete로 남기며 trace가 매매를 재시도/취소하지 않는다. v3 저장 최소 burst는 scan1+선정 근거N+traceN이며 max_record_bytes≥65536, 전체 호가 유량의 예산 증거는 별도다.

## 고정 인터페이스

`src.analytics.entry_gate_trace.begin_gate_trace(observer, scan_id, stocks)`는 안전한 trace 또는 무동작 trace를 반환한다. trace API는 모두 동기식이며 내부 관측 오류를 흡수한다. scheduler에는 `safe_trace_call(trace, method, *args, **kwargs)`를 제공해 대체 trace의 예외도 막는다. 매매 분기는 원래 조건의 로컬 결과를 사용하며 trace 반환값으로 제어하지 않는다.

- `check(stage, result, *, symbol=None, value=None, threshold=None, reason=None)`: 주어진 조건 결과를 그대로 반환하며 실제 bool 결과만 기록한다. `symbol=None`은 아직 terminal이 없는 모든 후보다.
- `note(stage, status, *, symbol=None, value=None, threshold=None, reason=None)`: 이미 계산된 결과/미적용/불명을 기록한다.
- `stop(symbol, reason, *, stage=None, outcome='not_reached')`: 검사하지 못한 후보 또는 근거가 없는 중단을 not_reached/unknown으로 닫는다.
- `signal(symbol, signal_id)`: 실제 이벤트 생성 뒤 `signal_created`를 기록한다. 뒤의 emit 성공 여부는 기존 emit_result가 담당하며 현재 행을 매수/발행 성공으로 해석하지 않는다.
- `finish(reason='scope_finished')`: 미완료 후보는 unknown으로 보존하고 모든 행을 한 번 publish한다. 반복 호출은 무동작이다.

실효 점수/시간대 등락률/현금 최소/횟수/전략 조건값을 도달 지점에서 복사한다. 단순 참조를 운영 실효 설정 전체의 지문 인증으로 부르지 않는다. 예외/취소 시 partial trace를 종료하며 원래 예외 처리 의미를 유지한다.

## 보고와 검증

별도 오프라인 보고기는 전체 후보와 strict trace를 연결하고 단계별 실제 평가/탈락·최초 관측 fail·not_reached/unknown을 출력한다. 시각 역전/보고 이후 데이터/중복 후보/중복 단계/잘못된 terminal/고아 trace를 거부한다. 원장/연구 지문은 기존 reader를 재사용하며 모든 결과에 `profit_comparison_available=false`, `production_eligible=false`를 고정한다. 옛 입력은 불명으로 남기고 signal0을 cash/손익0으로 바꾸지 않는다.

테스트는 실제 scheduler 메서드의 합성 한 회 실행에서 비활성/활성/관측 실패의 호출·signal 내용·카운터·쿨다운 동등성을 확인한다. 전역 차단/score→change short circuit/전략 미적용/quote 실패/sector/news/ATR/추격/8개·5신호 제한/emit 예외/취소·창 종료·overflow를 검증한다. 합성 원장→보고 CLI roundtrip과 변조/옛 자료도 검증한다. 전체 시험·비밀정보 검사·독립 최종 리뷰 후 통합한다.

개발 baseline: 기존 buffer/runtime/journal 관련116 passed·4.37초·exit0·격리0. 동일 main의 전체3,696 통과는38차 기록을 따른다.
