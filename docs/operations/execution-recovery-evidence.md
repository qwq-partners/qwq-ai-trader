# 과거 실행 복구를 위한 읽기 전용 증거 대사

현재 엔진이 비용 후 순수익을 냈는지 판단하려면 주문·체결·거래 장부·잔고의 차이를 먼저 설명할 수 있어야 한다. 이 도구는 **명시적으로 제공한 독립 파일의 일치 여부와 추가로 필요한 근거**를 보고한다. 주문, 장부 보정, 잔고 변경, 재생, 보류 해제 기능은 없다. 실제 운영 자료 확보·복구 승인·운영 적용은 별도 단계다.

구현/검증 이력: [36차 통합 PDS](../research/current-engine-integrated-pds-2026-10-02.md#36차-plan--복구-증거의-읽기-전용-대사). 상세 입력 계약: [36차 계획](../superpowers/plans/2026-10-02-recovery-evidence.md).

## Plan — 준비할 네 자료

| 입력 | 필요한 근거 | 현재 알려진 한계 |
|---|---|---|
| 실행 원장 SQLite | 계좌/환경 scope, 세션, POST 전 의도, 접수 주문번호, 관측 증분, 적용/반환 영수증 | session/event 원래 시각이 없으며 반환 영수증은 장부 DB commit 증명이 아님 |
| 증권사 원주문 JSON | 날짜·원주문번호·종목·방향·원수량·누적 체결·평균가·취소 확인·잔량·거절량, 모든 페이지 | 기존 체결 helper는 0체결과 일부 필드를 버리므로 이 입력의 대체물이 아님 |
| 거래 이벤트 JSON | DB 행 ID, trade ID, KST 거래일, 명시 KIS 주문번호, 종목/방향, 증분 수량/가격 | 기존 일반 진입·청산 기록에 NULL 주문번호가 있어 종목/수량으로 추정 연결하면 안 됨 |
| 잔고 JSON | broker와 internal 각각의 취득 시각, 종목별 수량, 현금 금액과 종류 | API 응답 시각만으로 어느 과거 체결까지 반영됐는지 알 수 없음 |

실제 자료의 계좌 범위·누락 없는 페이지·기간은 원천에서 확인해야 한다. `declared_complete=true`는 제출자의 선언이며 인증 결과가 아니다. 파일 지문은 어떤 자료를 비교했는지만 고정한다. 선언이 같거나 빈 자료끼리 맞더라도 실제 계좌를 검증한 것으로 해석하지 않는다.

SQLite는 **동시에 쓰는 주체가 없는 완결된 독립 복사본**만 받는다. 실행 중 원장에 직접 도구를 연결하거나 WAL 파일을 삭제해 입력을 맞추지 않는다. 이 개발 작업에서 운영 원장이나 계좌 자료를 읽지 않았다. 원장은 128MiB·100,000 이벤트 이내, symlink/비정규 파일·WAL 헤더·`-wal/-shm/-journal` 동반 파일이 없어야 한다. `mode=ro`, `query_only`, WAL 공유메모리를 만들지 않는 `unix-none` VFS를 사용하며 해당 VFS 미지원 환경은 거부한다. 잠금을 제공하는 온라인 snapshot 도구가 아니다.

## Do — 입력 계약과 실행

세 JSON 모두 아래 envelope에 각 행을 넣는다. 모든 날짜 범위는 KST 거래일이며 scope/기간을 통일한다. 취득 시각은 timezone을 포함하고 기간 종료일보다 이르면 안 된다. `source_ref`에는 원본 추적에 필요한 설명을 넣되 자격증명은 넣지 않는다.

```json
{
  "schema_version": 1,
  "account_scope": "synthetic-scope",
  "market": "KR",
  "source_kind": "broker_all_orders",
  "captured_at": "2026-10-02T20:00:00+09:00",
  "window_start": "2026-10-02",
  "window_end": "2026-10-02",
  "declared_complete": false,
  "source_ref": "synthetic example; replace with independently checked provenance",
  "rows": []
}
```

`source_kind`는 broker=`broker_all_orders`, journal=`journal_events`, baseline=`baseline_snapshots`다. JSON은 파일당 16MiB, 입력당 100,000행 이내다. 중복 key·NaN/Infinity·실수형 수량·비표준 날짜는 허용하지 않는다. 수량은 0~10¹² 범위, 금액은 정수부 16자리/소수부 28자리 이내의 음수 없는 고정소수점 문자열이다. 체결 수량/가격은 양수여야 한다. 아래는 각 `rows`의 **합성 예시 한 행**이다.

증권사: 문자열 원천 필드를 보존하며 0체결 주문도 제외하지 않는다. 체결·취소 확인·거절·잔량 합계가 원수량과 맞아야 한다. 이 수량 분할식은 도구의 보수적 입력 규약이며 증권사가 공식 보장한 식으로 해석하지 않는다. 맞지 않는 원본은 변경하지 말고 무효/추가 확인 대상으로 남긴다. 정정 계보는 자동 복구를 지원하지 않는다.

```json
{"ord_dt":"20261002","odno":"000123","orgn_odno":"","pdno":"005930","sll_buy_dvsn_cd":"02","ord_qty":"10","tot_ccld_qty":"10","avg_prvs":"106","cncl_yn":"N","cnc_cfrm_qty":"0","rmn_qty":"0","rjct_qty":"0"}
```

거래 이벤트: `event_id`는 실제 DB 행의 고유 ID, `trade_id`는 해당 거래의 실제 ID를 문자열로 전달한다. 하나의 주문에 여러 증분 행을 허용하며 같은 event ID의 중복은 오류다. 주문번호를 모르면 NULL로 남긴다. 거래일을 새로 추정하거나 여러 행을 평균가 한 행으로 합치지 않는다.

```json
{"event_id":"entry-row-1","trade_id":"trade-1","market":"KR","event_date":"2026-10-02","date_basis":"Asia/Seoul","symbol":"005930","side":"buy","kis_order_no":"000123","quantity":4,"price":"100"}
```

잔고: `owner`는 `broker`/`internal` 각 최대 한 행. 같은 종목 중복은 오류이며 현금 0은 유효값이다. 현금을 모르면 `amount:null`, 종류를 모르면 `basis:"unknown"`으로 남긴다. 주문가능금액과 예수금/포트폴리오 현금을 같은 값으로 간주하지 않는다.

```json
{"owner":"broker","captured_at":"2026-10-02T20:00:00+09:00","positions":[{"symbol":"005930","quantity":10}],"cash":{"amount":"0","basis":"settled"}}
```

프로젝트 Python 환경에서 명시한 네 **오프라인 파일**만 읽는다. 기본 운영 경로·환경 파일·네트워크 호출은 없다.

```sh
python scripts/reconcile_execution_evidence.py \
  --ledger /path/to/offline/ledger.sqlite \
  --account-scope synthetic-scope \
  --broker /path/to/offline/broker.json \
  --journal /path/to/offline/journal.json \
  --baseline /path/to/offline/baseline.json
```

보고서는 표준 출력 JSON이다. 유효한 보고서를 만들면 종료 코드0이며 **불일치 보고서도0**이다. 형식/원장 검증 실패는 코드2, 민감 원문을 포함하지 않는 오류 안내를 출력한다. 자동 소비자는 종료 코드만으로 성공 처리하지 말고 `comparison_status`, `issues`를 읽어야 한다. 어떤 결과도 runtime 승인 입력으로 사용하면 안 된다.

## See — 보고서를 읽는 순서

1. `inputs`의 파일 지문, 행 개수/무효/중복 개수, 선언 출처·기간을 확인한다. 계좌·기간 혼합은 비교에 사용하지 않고 이슈로 남긴다.
2. `unclean_sessions`와 `unresolved_outside_window`를 확인한다. 세션 시각이 없으므로 빈 세션의 위험기간을 주문 날짜로 만들어내지 않는다.
3. `(주문일, ODNO)`별 종목/방향/원수량, 누적 체결, 명시 최종 수량을 확인한다. 같은 ODNO라도 다른 날짜면 별개다. 원장 밖 수동/다른 프로그램 주문은 귀속 근거가 필요한 별도 이슈다.
4. 장부 수량·금액과 증분 구성을 각각 확인한다. `missing_journal_deltas`/`extra_journal_deltas`는 원장과 다른 `(수량, 가격, 개수)`를 보여준다. 현재 Fill과 같은 소수2자리 반올림을 쓴다. broker 누적 평균가×수량은 별도의 **계산금액**이며 실제 결제·수수료·세금·순손익이 아니다.
5. `baseline.position_differences`, 현금 값/종류, 양쪽 시각을 확인한다. 다른 종류의 현금은 비교하지 않는다. 시각이 다르면 수량이 같아도 시차를 이슈로 남긴다.
6. `next_actions`의 원천별 이슈를 해결한다. 주문번호 NULL에는 독립 원본 연결 근거, 정정에는 전체 계보, 잔고 포함 여부에는 시작 잔고와 전체 계좌 이동 및 증권사 포함 기준이 필요하다. 추정 행이나 가상의 현금 이동을 생성하지 않는다.

| 상태 | 의미 |
|---|---|
| `consistent` | 제공된 자료 안에서 검출된 차이/결측이 없음. 출처 진실/계좌 전체 완전성 인증 아님 |
| `incomplete` | 누락·기간/출처·잔고 시차·미완료 주문·비정상 세션 등 추가 근거 필요 |
| `mismatch` | 수량/가격/증분 구성/종목·방향/중복/잔고 등에 차이. 결측도 함께 있을 수 있으므로 전체 이슈 확인 |

항상 `runtime_release_allowed=false`, `replay_allowed=false`, `state_mutated=false`, `baseline_inclusion="unverified"`다. 일치해도 과거 체결을 현재 시작 잔고에 재생하면 중복 반영할 수 있다. 정상 보류 해제는 이 도구에서 수행하지 않는다.

## 다음 우선순위와 수익 평가 연결

- 미래 식별 체결의 주문번호·실행 ID와 DB commit 연결은 [37차 개발/검증](../research/current-engine-integrated-pds-2026-10-02.md#37차-plan--체결-identity와-장부-commit)로 진행했다. 운영 미적용이며 과거 NULL 값이나 옛 receipt를 새 근거로 승격하지 않는다.
- P0: 승인된 계좌 원본이 확보되면 실제 대사. 독립 체결29건·현금 이동·baseline 포함 여부를 확인한 뒤 복구 범위와 잔고 채택을 결정한다. 현재는 실제 자료 검증 미완료다.
- P1: 기존 예약이 생성한 관측의 품질을 확인하고 버전·시장국면별 **종목 선정/진입 시점/동일 진입 청산**을 비용 후 비교한다. 이 도구의 장부 정합성이 전략 수익성의 증거는 아니다.
- P1: 계좌 총수익·전체 KODEX200 총수익을 같은 기간/현금흐름으로 비교한다. 반도체 제외 보조 비교를 함께 보여 집중도 영향을 분리하며 전체 지수의 기회비용 비교를 지우지 않는다.

이번 도구는 운영 봇, 매수 중지,10월2일 예약5468208 및 설치 입력을 변경하지 않는다. 기존 예약 자료에 이후 개발한 계측 필드가 있다고 가정하지 않는다.
