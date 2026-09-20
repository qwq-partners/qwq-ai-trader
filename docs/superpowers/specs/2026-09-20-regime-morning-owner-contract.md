# C4 장전 text diagnosis owner 계약

기준 `da74849`. 기존 승인 writer 이행 계획 C4의 구현 분해다. 새 매매 전략·수치·운영 권한을 추가하지 않는다. 이 문서는 구현 계약이며 시험·리뷰 완료 선언이 아니다.

## Plan / 경계

실제 `run_market_trend_monitor`의 installed 경로에서 정상 owned 지수 갱신 뒤 장전 진단을 연결한다. 현재 `continue`로 누락된 경로를 복원한다. JSON 정오 classifier와 다른 `llm_morning_diagnosis` source lane이며, 보호 적용·경제 reducer·outbox를 실행하지 않는다.

- KST 08:48부터 08:55:59까지 신규 시도한다. 정상 늦은 완료에 새 deadline을 붙이지 않는다. 기존 성공 1회/일 의미를 유지하며 실패·빈 응답은 window 내 다음 tick에 재시도할 수 있다.
- 기존 지수 2건 이후 테마 상위5 → 보유 순서 최대5개 시간외 시세 순차 조회 → 뉴스 상위5 → 선택적 Perplexity 1회 → text `MARKET_ANALYSIS`, `max_tokens=150` 1회. 추가 지수 GET·새 TTL·새 모델 timeout은 없다.
- `[방어]`와 bull이면 sideways. 그렇지 않고 `[공격]`과 bear이며 당일 folded horizon이 crash/severe가 아니면 sideways. 원 if/elif 순서와 substring 판정을 유지한다. 다른 문장은 중기 레짐을 바꾸지 않는다.
- 역사 진단문/성공일과 현재 중기 정책 권한을 구분한다. open expectation은 표시용이며 당일 09:30부터 만료한다. 자체가 유효 레짐을 직접 결정하지 않는다.

## Do / 명시 상태와 API

`regime_morning.py`에 canonical frozen `RegimeMorningBaseline`, `RegimeMorningStageInput`, `RegimeMorningResult`를 둔다. API:

```python
await RegimeOwner.register_morning_baseline(runtime, baseline, expected_version=...)
await owner.diagnose_morning(inputs_provider, llm)
owner.morning_assessment(now=...)
```

결과 status는 `completed`, `already_done`, `outside_window`, `pending`; completed만 실제 source receipt를 갖는다. baseline 부재·unknown은 `ApplicationBlocked`, 잘못된 명시 DTO는 `ValueError`다. 동시/복원된 미완료 attempt는 pending이며 자동 외부 재호출하지 않는다. 성공 본문이 공백뿐인 경우에는 유효 진단으로 저장하지 않는다(legacy truthy-before-strip 모호성의 명시적 실패 처리).

`morning_assessment()`는 baseline DTO가 아닌 detached 표시 dict다. 만료된 expectation만 None으로 반환하되 원 as_of·assessment/day는 역사로 보존한다. 그 표시 dict를 그대로 기준선으로 재등록하지 않는다. baseline metadata는 초기 인계의 증거이며 매일의 허가 토큰이 아니다. 매 시도는 별도 current index·day/generation/fence·seal 검사를 거친다. 게시 미완료/unhealthy이면 getter는 기존 owner readiness 장벽으로 차단한다.

Provider는 생성 시 I/O·snapshot을 하지 않는다. `snapshot_themes()`, `snapshot_symbols()`(최대5 tuple), `fetch_overtime_price(symbol)`, `snapshot_news()`, `fetch_macro_context()` 순서로 호출한다. symbols 이외는 schema1/stage/outcome/source/event_id/received_at/market_as_of/payload의 detached stage DTO다. phase는 theme/overtime/news/macro, outcome은 success/missing/failed. 원 시장시각이 없으면 None이고 receipt 시각으로 채우지 않는다. 외부 문자열/메모리 정보는 캡처 사실이지 publisher/current authority 증명이 아니다.

실패 경로의 호출 횟수도 보존한다. 테마/news getter·형식 오류와 symbols snapshot 오류는 기존 바깥 try가 중단하므로 해당 attempt를 failed로 닫고 후속 I/O/모델을 호출하지 않는다. 단순 공급자 부재/빈 자료(missing)는 원래의 빈 optional 입력이다. 개별 시간외 조회와 Perplexity 실패만 기존처럼 누락을 기록하고 다음 단계로 진행한다. 모든 stage 실패를 일괄 무시해 기존에 없던 외부 호출을 만들지 않는다.

schema3는 기존 schema2에 명시 `morning_baseline`과 `morning`을 추가한다. 기준선의 계좌/일자/generation/fence/evidence 및 regime/horizon 기준선 version을 첫 await 전과 reducer 내부에서 대조한다. knowledge=known의 명시 None은 미실행 사실이지만 knowledge=unknown은 아니다. unknown은 다음 날에도 자동 known-empty로 합성하지 않으며 명시 day/startup 이행은 후속이다. live adapter 기본값에서 기준선을 만들지 않는다.

기존 index 전이와 morning 전이는 같은 version 순서로 fold한다. morning은 before의 mid_regime만 변경하고 technical last_update, regime_data, pending, sidecar 및 원 index/VIX/expert ancestry를 보존한다. 전이·원 응답·seal·성공일·진단문·open expectation·engine projection을 같은 commit에 교차검증한다. accepted morning source와 전이의 일대일 대응, 당일 성공 최대1, 과거 행 불변을 검증한다. schema1/2 복원 의미를 유지하며 schema3가 C3/5분 경로를 끄지 않도록 모든 schema guard를 검사한다.

## 입력과 소비 권한

첫 optional I/O 전에 command scope/source begin을 접수한다. 실제 prompt/보정이 읽는 정책은 `regime_policy.trend_state`, `regime_policy.horizon`을 versioned seal로 고정한다. 현재 `index_trend` 권한을 source read로 선언한다. morning selector, sidecar/protection/JSON/전체 execution version 의존을 임의로 추가하지 않는다. 원 prompt, stage 원본, 모델 task/토큰수 및 rule digest를 보존한다.

관련 지수/source pending·변경, 정책 ABA, day/generation/fence 변경은 늦은 결과를 stale로 만든다. 미소비 lane 변경이나 fill/ACK만으로는 stale로 만들지 않는다. 취소는 기존 tracked terminal/drain 경계를 따른다. SQL 접수/commit 응답 유실/publish 실패를 단순 외부 결측으로 삼키지 않는다.

최신 중기 전이가 morning일 때 owned policy snapshot은 그 morning source와 실제 index dependency를 검증하고 morning commit version을 내보낸다. 다음 정상 index 전이가 최신 중기 권한을 되찾아도 진단문·성공일은 역사로 유지한다. generic coordinator가 typed morning 전이 없이 accepted 결과를 만들 수 없어야 한다.

schema3의 engine/owned effective-regime cap은 adapter가 쓰는 당일 folded horizon과 일치시킨다. 기존 schema1/2 정책은 바꾸지 않는다. 이는 새 임계값이 아닌 소유 상태 복사본 정합화다. adapter와 engine의 중기 상태/표시 복사본은 한 publish에서 복원한다. owned 읽기 시계는 runtime KST를 사용한다. dashboard/Telegram/audit 복사본은 표시만 하며 매수 권한으로 승격하지 않는다. Telegram은 이번 completed+accepted에만 보내고 재시도/복원 시 재발송하지 않는다.

## See / 인수 게이트

실제 SQLite owner·scheduler를 쓰고 외부 I/O/시계만 합성한다. 기존 C3 RED3을 C4 새 RED로 세지 않는다. 새로운 실제 caller RED, window/호출 순서·횟수/성공일 dedupe/실패 retry, 동시성·역순·관련 정책 ABA·무관 fill, 취소·SQL 실패·cold restore, source/seal/전이 위조, index→morning→index, schema3의 C3/5분 유지, 표시 만료와 정책 version을 구분해 시험한다. 미실행 변형은 별도로 기록한다.

구현자와 독립 시험/리뷰어를 분리한다. critical 최종 native 리뷰와 tools-off Opus 분할 리뷰, 전체 UTC/KST 직렬 시험, 비밀정보·문법 검증 후 feature만 커밋·푸시한다. 실제 모델 metadata가 없으면 요청 모델/effort만 기록한다.

main/운영 SSH·배포·재시작·주문·설정 변경 없음. KIS 거래/잔고, Toss 관측 전용, trading_ready=False, MODIFY 미지원, 미입증 최초 인계·취소 최종성 차단은 유지한다. 전체 writer/factory/qualification·final sizing/gateway/full C/F/G/R/broad/장기 성능·초과수익 검증은 이 C4 승인 범위 밖이다.
