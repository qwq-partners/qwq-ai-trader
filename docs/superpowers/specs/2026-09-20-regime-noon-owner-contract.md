# 정오 레짐·보호 application/replay 계약 (C3)

2026-09-20. 실제2분 C2 기준 `6109d11` 위에서 적용한 고정 인터페이스다. 원 승인 계약 SHA256 `07fa8406111df6386cd882aa5865536508e6f7b015eb5c9a1f28fc2fe7b58023`의 §1 이후를 보존했다. 아래 ‘제안’ 표기는 당시 승인 원문을 보존한 것이며 구현 상태나 검증 결과는 [C3 Plan–Do–See](../../reviews/noon-regime-protection-replay-2026-09-20.md)가 정본이다. 이 문서나 합성 시험은 운영 설치·거래 허가·공식 외부 증거를 부여하지 않는다.

## 1. public 이름과 반환

### 09-20 입력 시각·결측 해석 보완

원 템플릿/산식 보존은 원 관측에 없는 현재 시각을 만들어 표시하거나 생산자의 명시적 결측을 숫자로 되살리는 동작까지 승인하지 않는다. C3 후속에서는 KR 각 지수의 원 수신 시각을 표시하고 시장 시각 미제공과 구분한다. 미국 지수는 각 항목의 원 시장/조회 시각을 유지하며 없는 시각을 분류 clock으로 채우지 않는다. 정규화 항목이 **존재하면서** 결측 또는 소비 필드 None을 선언하면 raw alias 값으로 덮지 않는다. 정규화 key 자체가 없는 기존 형식의 유효 alias fallback은 보존한다. 추가 TTL·추가 조회·수치 임계값 변경은 없으며, 유효0·optional OHLC 결측도 원 계약대로 유지한다. 실제 재현·검증 결과는 위 C3 보고서를 따른다.

~~~python
baseline = RegimeHorizonBaseline.from_dict(supplied_json)
version: int = await RegimeOwner.register_horizon_baseline(
    runtime, baseline, expected_version=runtime.owner.version)
await runtime.owner.register_policy_generations("c3-reads:<stable-id>", (
    "regime_policy.horizon", "intraday_policy.current", "protection.config",
    "protection.current_regime", "protection.intraday_crash_level"))
# 기존 C2 owner/binding을 사용. C3 전용 두 번째 owner를 설치하지 않는다.
source_receipt: RefreshReceipt = await owner.classify(
    "12:00 (장중 업데이트)", inputs_provider=provider, llm=llm)
application: RegimeApplicationReceipt | None = owner.classifier_application_receipt(
    source_receipt.operation_id)
context = RegimeSyncContext.from_dict(context_json)
application = await owner.sync_protection("regime-sync:<stable-id>", supplied_context=context)
~~~

classify는 기존 risk_sources.RefreshReceipt를 반환한다. accepted면 source+즉시 application commit이 완료된 상태다. classifier_application_receipt(classifier_operation_id)는 동기·읽기 전용이며 classifier:<classifier_operation_id>의 원 receipt 또는 None을 반환한다. source가 미accepted면 None; 최신 context 수집/모델/재적용/현재 source 재승인0. SQL/게시 실패는 예외, caller 취소는 accepted 작업 drain 뒤 CancelledError 유지.

RegimeApplicationReceipt는 frozen dataclass로 정확히 operation_id:str, status:str, reason:str, committed_version:int, classifier_operation_id:str, classifier_version:int 필드다. status 제안은 applied(전체 protection 변화), unchanged(원 public 호출 결과 동일), stale(capture 이후 context/source/read 무효)다. 앞 두 성공 reason은 빈 문자열. immediate source accepted와 application unchanged는 양립한다. 동일 재시도는 별도 ALREADY 상태를 만들지 않고 원 receipt를 그대로 반환한다.

최초 sync에 current/accepted classifier가 없으면 기존 ApplicationBlocked로 regime_classifier_not_current를 내며 application row를 만들지 않는다. capture 후 변화는 durable stale receipt이며 protection/replay 변경0. malformed caller 입력과 같은 ID의 다른 본문은 사전 ValueError(regime_application_request_conflict), SQL/게시 오류는 정상 stale로 변환하지 않는다. 동시 중복의 post-await 본문 검사도 필수다.

## 2. horizon supplied JSON

~~~python
{
  "schema": 1, "baseline_id": "synthetic-c3-horizon-1",
  "account_scope": "scope", "business_day": "2026-09-20",
  "generation": 0, "fence_id": None,
  "evidence": {"source": "synthetic-test", "event_id": "known-horizon-1",
               "observed_at": "2026-09-20T12:00:00+09:00"},
  "regime_baseline_version": 12,
  "intraday": {"baseline_version": 8, "current": {
    "schema_version": 1, "level": "normal", "kospi_pct": 0.0,
    "updated_at": None, "recovery_until": None}},
  "horizon": {"level": "normal", "change_pct": 0.0, "classified_at": None}
}
~~~

위 key 집합을 정확히 사용한다. 숫자 version은 설명용 기존 참조이며 fixture는 실제 등록 결과를 넣는다. schema=1은 supplied DTO 형식, 설치된 regime root는 schema=2다. regime_baseline_version은 기존 C2 root baseline의 owner version, intraday 두 필드는 C2 baseline과 같은 기존 root 교차검증 형식이다. horizon level은 기존 4 enum, change_pct는 finite number 또는 명시 None, classified_at은 aware ISO 또는 명시 None이다. None 시각의 known 기준선은 accepted source/현재 관측이 아니다.

모든 DTO는 unknown key·bool-as-number·nonfinite·naive/future 관측 시각을 거부하고 canonical JSON 문자열을 frozen 객체 안에 보유한다. to_dict()는 매번 detached 사본이다. from_dict는 구조 검증, runtime API는 실제 clock/account/day/generation/fence/expected_version과 source/root crosslink를 첫 await 이전 및 reducer에서 검증한다. 과거 observed/classified 시각을 오늘로 재각인하지 않는다. recovery_until의 미래 cooldown은 기존 IntradayPolicyState 규칙을 유지하며 미래 관측 거부와 혼동하지 않는다.

새 등록은 정확한 expected_version을 요구하고 int version을 반환한다. 같은 baseline ID+동일 원본문은 원 version, 다른 본문은 conflict. 원 baseline 전체/digest/owner baseline_version을 regime_policy.horizon_baseline에 보존한다. schema2 추가 key는 horizon_baseline, horizon, noon_caps, applications; C2 필드는 보존한다. materialized horizon의 정확한 필드는 level, change_pct, classified_at, writer_kind, operation_id, version; writer_kind는 baseline|intraday_5m|noon_index, baseline operation_id는 baseline_id다. source market_as_of를 이 필드로 만들지 않는다.

## 3. staged inputs_provider 프로토콜

~~~python
class RegimeClassifierInputs(Protocol):
    async def read_daily_bias(self) -> RegimeStageInput: ...
    async def fetch_us_overnight(self) -> RegimeStageInput: ...
    def snapshot_screener(self) -> RegimeStageInput: ...
    async def fetch_index_price(self, index_code: str) -> RegimeStageInput: ...
    def snapshot_application_context(self, *, captured_at: datetime) -> RegimeSyncContext: ...
~~~

RegimeStageInput.from_dict/to_dict는 위와 같은 frozen canonical wrapper다. 정확한 outer 필드는 schema:1, stage:str, outcome:str, source:str, event_id:str|None, received_at:str|None, market_as_of:str|None, payload:dict|None. outcome은 success|missing|failed. 실패 예외 전문/계좌/credential은 넣지 않는다. wrapper metadata는 provenance이며 owner source operation/version을 가장하지 않는다.

| method / stage | payload key와 원 코드 | 의미 |
| --- | --- | --- |
| read_daily_bias / daily_bias | {"data": 원 finite JSON object}. _run_llm_regime_classifier:1384 daily_bias 읽기 | 원 object의 assessment/top_lesson을 기존 기본값/표시 규칙으로 소비. 기타 원 key는 opaque 보존하며 새 정책 입력으로 사용하지 않음. |
| fetch_us_overnight / us_overnight | {"overnight": 원 finite JSON object}. 기존 get_us_market_data().get_overnight_signal() | indices/indices_normalized를 기존 alias·None·VIX price 추출대로 소비. 제공되지 않은 시장시각은 None. raw US 자체 VIX는 detached US 자료다. |
| snapshot_screener / screener | {"closes": [finite numbers], "last_bar_date": ISO date 또는 None, "loaded_at": aware ISO 또는 None} | _kospi_closes, _kospi_last_bar_date, _kospi_loaded_at의 원 사실. owner가 기존 _pct_change/_today_bar_action을 사용. live list 반환 금지. |
| fetch_index_price("0001") / index0001; ("1001") / index1001 | {"quote": 실제 adapter 반환 finite JSON object} | quote의 원 _observation 전체·원 ID·receipt·None market_as_of 보존. wrapper metadata로 원 _observation 결측을 채우지 않음. owner가 필요한 원 필드 검증. |
| snapshot_application_context | 아래 RegimeSyncContext | 기존 _apply_regime_to_exit_manager의 config guard/screener 기술 레짐만 detached 캡처. broker/US/모델 호출0. |

missing/failed stage는 payload=None이다. 실제 legacy에서 optional인 daily/US/screener/index 실패는 그 stage 결측으로 유지하고 missing_fields에 기존 의미대로 반영한다. valid0은 성공 값이다. adapter의 부분 지수 quote는 필드별 metadata를 보존하며, owner는 noon cap에 필요한 KOSPI fact와 prompt의 optional price/KOSDAQ fact를 구분한다. 2분용 required OHLC 전체 규칙을 정오 모든 optional 입력에 복사하지 않는다. Python datetime/date는 provider 경계에서 원 의미의 ISO로 직렬화하며, 시각을 증명할 수 없으면 None/결측으로 남긴다.

provider 생성/바인딩은 I/O·live 입력 캡처를 하지 않는다. actual scheduler adapter는 기존 함수 본문의 입출력만 위 단계로 옮기고 전역 모델/US singleton 조회도 호출 stage 안에서 수행한다. fixture는 stage entry Event를 통해 begin-before-I/O를 검증할 수 있다.

호출 순서는 정확히 다음이다.

1. outer command scope → llm_regime begin(require_seal=True) → read_daily_bias. 기존 now 분류 기준시각은 daily_bias 뒤/US 이전에 owner clock으로 고정한다.
2. fetch_us_overnight → snapshot_screener. 기존 intraday window 판단은 위 분류시각과 scheduler의 기존 상수를 사용한다.
3. window 안이면 noon_index begin(require_seal=True) 후 await fetch_index_price("0001"), 이어 await fetch_index_price("1001"). 각 실패는 별도 fact로 처리하여 원 순차 호출을 유지한다. window 밖 두 호출/noon begin은 0.
4. 필요한 KOSPI 관측이 valid면 당시 batch/horizon reads로 noon cap seal/commit. invalid/missing이면 noon terminal만 남기고 cap을 accepted normal로 만들지 않는다. 두 경우 모두 기존 optional prompt 결측 의미 유지.
5. snapshot_application_context(captured_at=실제 owner clock) → 아래 실제 reads capture → 그 원값으로 prompt/application 입력 계산 → 원 reads를 지정한 outer seal → complete_json 한 번/기존15초 → terminal completion.

llm은 기존 async complete_json(*, prompt, system, task) 객체이며 LLMTask.QUICK_ANALYSIS와 원 system/prompt 템플릿·max_tokens 미지정 계약을 사용한다. owner가 asyncio.wait_for(15.0)를 적용한다. raw result는 원 finite JSON object로 보존; error/empty/None 등 실패와 unsupported label의 명시 neutral fallback은 기존 승인 branch를 구분한다. 새 threshold·자동 재조회/모델 재호출·0 대체는 없다.

## 4. exact sync context와 즉시 scheduler 연결

~~~python
{
  "schema": 1,
  "captured_at": "2026-09-20T12:00:01+09:00",
  "regime_conflict_guard_enabled": True,
  "screener": {
    "regime": "neutral",  # str 또는 None; absent 사실은 None
    "source": "batch_analyzer._screener.get_market_regime",
    "event_id": None, "observed_at": None
  }
}
~~~

정확한 key 집합이며 supplied source/version/portfolio/protection/horizon 필드는 없다. screener regime의 known string은 원문 그대로 보존하고 기존 cap lookup을 사용한다. None/예외는 기존 neutral 계산 fallback의 별도 기록 대상이며 source 성공으로 바꾸지 않는다. guard=False면 기존처럼 get_market_regime을 호출하지 않고 regime/event_id/observed_at은 None으로 둔다. observed_at은 실제 제공된 원 시각만, captured_at은 지금 detached read한 시각이다. 새 sync의 captured_at은 해당 runtime KST day·미래 아님을 검사하고 재시도는 동일 원 context를 그대로 사용한다. historical retry의 본문은 현재 source/시각으로 재작성하지 않는다.

_run_llm_regime_classifier(label) installed branch는 위 owner.classify의 RefreshReceipt를 반환한다. 08:10/12:00 loop의 즉시 후속은 그 receipt.operation_id로 classifier_application_receipt만 읽는다. _apply_regime_to_exit_manager installed 30분 branch는 매 tick 새 regime-sync:<uuid>와 위 context를 owner.sync_protection에 넘긴다. uninstalled 기존 동작은 유지한다. 파일 existence/import/provider module 오류 때문에 과거 JSON을 authority로 읽거나 새로운 사용자 승인 gate를 만들지 않는다.

## 5. 실제 read 의존성과 capture 시점

| 실제 소비 | owner 읽기/고정 | 넣지 않을 가짜 의존 |
| --- | --- | --- |
| daily_bias/US/US 자체 VIX/screener closes·기술레짐/config guard | 위 detached stage/context 전체와 digest, 원 provenance | 파일 version 또는 vix_regime/expert_regime/index_trend source version을 임의 부여하지 않음. |
| noon cap | intraday_policy.current, regime_policy.horizon generation + 원 KOSPI quote | 자기 intraday lane을 다시 seal dependency로 선언하지 않음. noon은 batch 정책 writer가 아님. |
| classifier prompt의 batch·현재 horizon 및 실제 사용한 noon | 위 두 selector와 실제 사용한 intraday source fact/reference. 완료 noon이 있으면 그 operation을 보존. absent/failed도 실제 읽은 상태 그대로 | 단지 시장 관련이라는 이유로 trend/expert/VIX 세 lane을 자동 선언하지 않음. |
| 즉시 적용 | 세 protection selectors(config,current_regime,intraday_crash_level) + prompt가 읽은 batch/horizon selectors, 원 classifier source/outcome/seal | 전체 execution version을 stale 판정으로 쓰지 않음. per-position 전체 DTO를 외부 계산 시작 때 고정하지 않음. |
| 30분 적용 | 세 protection selectors는 항상, batch/horizon은 conflict guard=True일 때만 추가. 원 classifier source/outcome/seal | guard=False 경로가 실제 읽지 않은 새 application policy dependency를 만들지 않음. 원 classifier의 이미 선언된 source 의존 권한은 계속 검증. |

captured reads를 **먼저** 만들고 그 값에서 계산한다. classifier/noon은 C2의 capture_reads(ticket,...)->canonical str, seal(...expected_reads_json=원문)이 최종 제공되면 그대로 쓴다. 계산 뒤 새로운 reads를 읽어 옛 계산에 붙이지 않는다. namespace/schema 및 source row 직렬화는 기존 capture 출력 그대로 사용하며 fixture가 helper 내부를 복제하지 않는다.

선택 이름을 고정하면 noon은 source_lanes=(), versioned_policy_reads=(intraday_policy.current, regime_policy.horizon), classifier는 source_lanes=("intraday",), versioned_policy_reads=위 5개다. classifier는 prompt provenance에 실제 읽은 최신 intraday source의 absent/failed/accepted 상태를 보존한다. 원 완료 noon을 사용한다면 operation/outcome/seal 참조를 입력에 함께 묶는다. noon accepted 이후 outer capture 전에 intraday가 이미 바뀌어 그 원 사실이 더는 현재 소비 권한이 없으면 오래된 cap 계산에 새 reads를 붙이지 않고 stale로 종료한다. 성공 noon의 원 cap 계산 reads는 noon 원장/원 seal에 남기며 outer는 그 완료 결과와 자신이 실제 읽은 정책을 구분한다.

sync는 accepted classifier ticket을 capture/reseal하지 않는다. 원 classifier를 current/accepted로 확인하고 새 application read_bundle을 만든다. 이 bundle의 top-level은 captured_version:int, captured_at:str, classifier_ref:dict, versioned_policies:dict다. versioned_policies는 guard=True면 위 5개, False면 protection의 3개 selector만 key로 하고 기존 versioned_fact 반환(selector,registered_version,generation,present,value,digest) 그대로 보존한다. classifier source authority는 캡처 및 reducer에서 기존 C2b evaluator로 확인한다. captured_version은 역사 read cutoff/감사 근거이며 단독 equality stale gate가 아니다.

reducer는 capture generation/source 참조를 다시 검증한 뒤 **그 시점의 최신 전체 canonical protection DTO**를 읽어 actual public method를 호출한다. 중간 fill/ACK가 selector/필수 source를 바꾸지 않았다면 허용된다. 기록한 full before는 외부 snapshot이 아니라 이 최신 reducer before다. 완료 application 자신의 generation 변경을 선행 read 변화로 오인하지 않는다.

부모 지적 반영: preflight의 “늦은 trend/expert/VIX이면 stale”은 무조건적인 새 dependency 계약이 아니다. 실제 읽은 upstream이 그 lane에 이미 의존해 C2b 전파가 발생한 경우에만 stale이다. legacy classifier가 읽지 않은 독립 lane 변경은 non-stale 대조군이다. US raw VIX는 owner VIX lane과 별개인 detached 입력이다.

## 6. application row 최소 정확한 shape

regime_policy.applications[operation_id]의 exact top-level key 제안:

~~~python
{
  "schema": 1, "operation_id": "...", "kind": "classifier" | "sync",
  "account_scope": "...", "business_day": "YYYY-MM-DD", "generation": 0, "fence_id": None,
  "request": {...}, "request_digest": "<sha256>",
  "receipt": {...}, "classifier_ref": {...},
  "read_bundle": {...}, "context": {...}, "context_digest": "<sha256>",
  "calculation": {...} | None, "digest": "<sha256>"
}
~~~

request는 classifier에서 {"kind":"classifier","classifier_operation_id":str}, sync에서 {"kind":"sync","supplied_context":RegimeSyncContext.to_dict()}다. operation_id는 상위 key/field와 교차검증하고 request_digest는 digest({"operation_id":id, **request})다. receipt는 §1 frozen receipt의 동일 6필드 JSON이다. classifier_ref 정확한 필드는 operation_id, committed_version, request_digest, outcome_digest, seal_digest; 이는 source ticket/terminal/원 seal과 대조한다. sync read_bundle은 §5, classifier read_bundle은 {"source_seal_digest":str,"reads_json":원 classifier seal reads canonical string}이며 kind로 검증한다. context는 즉시 적용에서도 §4의 detached context다.

calculation은 stale에서만 None이다. applied/unchanged에서는 정확히 raw_result_json, labels, rule_digest, force, original_protection_before, before_digest, original_protection_after, after_digest, global_before_digest, global_after_digest, changed_symbol_scopes를 보존한다. raw_result_json은 원 classifier result canonical 문자열이며 sync도 같은 원 결과를 참조·대조한다. labels는 raw, classifier_capped, fallback, final 4필드: raw/capped는 원 JSON 값 보존, fallback은 없으면 None, final은 실제 ExitManager에 넘긴 지원 label이다. force는 정확히 False다. labels는 기존 cap/fallback 순서의 결과를 기록하며 새 산식을 정의하지 않는다.

global digest 대상은 기존 _require_replay_policy와 동일한 schema,market,config,max_holding_days,current_regime,intraday_crash_level,exit_exempt 부분 객체다. changed_symbol_scopes는 {symbol:{"before_digest":str,"after_digest":str}}이며 전체 원 DTO의 symbol 보호 scope에서 직접 도출한다. 이 digest는 _scope의 protection 하위 객체와 같은 제한 규칙이며 경제 position/lots/cursors를 포함한 event scope digest와 구분한다. global 변화가 있는 degraded 종목도 포함한다. updated 반환값이나 별도 had_states/changed boolean을 권한으로 쓰지 않는다. row.digest는 자기 digest 필드를 제외한 row의 기존 protection_recovery.digest다.

typed replay event의 추가 payload는 {"kind":"regime_application","application_id":str,"application_digest":str}를 제안한다. 나머지는 기존 _append의 symbol,source_version,applied_at,before,after,policy_code,previous,digest를 유지한다. source_version은 **application receipt committed_version**이며 classifier source version과 혼동하지 않는다. full original DTO는 application에만 한 번 저장하고 event scope를 원 DTO에서 제한해 대조한다. force=False 원 full 호출 검증 뒤에만 승인된 private body 재생을 선택한다.

noon_caps 및 horizon fold의 상세 내부 row는 기존 승인 계약의 source/원 read/후보/disposition/version을 만족하는 구현 내부이며 독립 시험은 public receipt와 materialized horizon을 우선 관찰한다. 새로운 별도 source API/거래 권한/운영 bootstrap은 추가하지 않는다. 위 stage/application 명명은 구현자와 시험자에게 같은 버전으로 공급한다.

부모 확인: 외부 stage/model의 잘못된 타입·비유한 값·파싱 실패는 데이터 실패이며 SQL/게시 예외와 별도로 terminal 처리한다. 잘못된 response container를 단순 neutral 성공으로 포장하지 않는다. 기존 함수가 지원하지 않는 scalar label을 neutral로 처리하는 명시 fallback과 model 자체 오류를 구별하고 실제 legacy 대조를 남긴다. fixed classification clock은 daily_bias 뒤/US 이전의 원 위치이며 noon normalization의 received_at 미래 검증은 실제 수신 이후 owner clock을 사용한다. captured_at/received_at을 시장 as_of로 대신하지 않는다. 원 force=False body의 refactor는 제품 parity·기존 합법 force=True 대조까지 포함한다.
