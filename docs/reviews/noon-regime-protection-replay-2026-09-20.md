# 정오 레짐·LLM·보호 재생 통합 — 진행 기록

기준: feature `6109d11` 이후 C3. 구현·네이티브/Opus 한정 재리뷰·최종 전체 양TZ 검증을 마쳤다. 전체 다섯 단계나 운영 전환의 승인이 아니다. [고정 인터페이스](../superpowers/specs/2026-09-20-regime-noon-owner-contract.md)는 원 승인 계약과 좁은 provenance 보완을 구분한다.

## Plan

실제 2분 레짐 단계(C2)의 source 권한·정책 generation·취소 drain 계약을 보존하면서 정오 분류와 보호 적용을 같은 runtime owner에 연결한다. 모델 호출 이전의 정오 위험 cap, 모델 결과의 보호 적용, 실패한 보호 등록의 재생을 서로 구분한다.

- 정오 조회는 기존 KIS 지수 두 건을 순차 호출하는 경로만 사용한다. 테스트에서는 외부 조회를 대체한다.
- 당일 유효한 위험 cap은 모델 호출 전에 저장한다. 모델 오류가 그 관측을 지우지 않는다.
- 분류 완료·보호 적용 receipt·원 전체 보호 DTO·typed replay 증거는 원자적으로 저장한다.
- 분류 직후 후속은 receipt만 읽는다. 30분 동기화는 별도 요청으로 현재 권한과 읽은 정책만 재검증한다.
- 복구는 원 `force=False` 분기와 전체 manager 상태를 검증한다. 체결·현금·수량·초기 R·예약·outbox 효과를 반복하지 않는다.
- JSON 파일은 조회용 결과물일 뿐 판단의 원장이 아니다. 늦은 파일 쓰기가 새 결과를 덮지 않게 한다.

기존 미설치 경로와 합법적인 5분 급락 회복의 `force=True` 동작은 보존한다. 명시적 horizon 기준선 없이 자동 중립 상태를 만들지 않는다.

## Do — 역할과 경계

동일 기준 커밋에서 격리한 feature 워크트리를 사용한다.

| 역할 | 요청 모델/effort | 범위 |
| --- | --- | --- |
| 제품 구현 | Astra/high | owner·typed 저장·보호 재생·스케줄러 배선 |
| 독립 인수시험 | Terra/high | 실제 BUY40 체결 큐·SQLite·복구·경합 13개 범주 |
| 스케줄러 인수시험 | Sol/high | 실제 호출점·입력 어댑터·즉시/30분 분기 |
| 비판적 독립 리뷰 | Astra/xhigh | 최초 결함 재현·수정 delta·추가 시험 검토 |
| 교차 제공자 리뷰 | Opus5/xhigh | 도구 없이 고정 소스의 입력/원자 저장 및 보호 재생 검토 |
| 통합 | 주 에이전트 | 계약 대조·통합 검증·문서·별도 독립 리뷰 |

모델 표기는 요청값이며 실제 모델/effort가 실행 메타데이터에 노출되지 않으면 검증됐다고 주장하지 않는다. 동일 제품 파일을 동시에 수정하지 않는다. 누락된 API로 인한 import 오류나 skip은 행동 RED 또는 인수 통과로 세지 않는다.

## See — 완료 조건

첫 동결 후보의 이력(아래 수정본 결과와 구별):

- 작성자 관련 시험 55건: UTC 7.74초/KST 7.62초 통과, 격리 위반 0.
- 독립 스케줄러 7건: UTC 3.42초/KST 3.51초 통과. 실제 noon 루프 한 주기에서 모델 1회·즉시 receipt 조회 1회·별도 주기 sync 1회를 확인했다.
- 독립 owner 18건: UTC 17통과/1실패 11.06초, KST 17통과/1실패 8.64초. 부모가 세 신규 모듈을 합쳐 실행해도 27통과/1실패 10.99초였다.
- 실패: 오래 대기한 정오 조회보다 이후의 5분 급락→정상 회복이 먼저 적용되면, 정오 입력 캡처에서 `input_capture_context` 예외가 분류 호출 밖으로 새어 나왔다. 원 실패를 보존하고 수정본에서 stale로 종결하도록 했다.

독립 첫 리뷰는 P1 두 건과 P2 한 건을 발견했다. 거부된 지수 관측의 LLM 재소비, 원 관측에 없는 noon 시장시각의 공개 완료 승인, 계좌를 바꾸고 재해시한 horizon 기준선의 cold restore 승인이다. 동일 여섯 반례·대조가 UTC/KST 각각5실패/1통과였으며 원본을 수정하지 않았다.

두 번째 후보는 소비 필드별 원 provenance 재검증, noon 저장 전/복원 공통 metadata 검증, 보존된 C2 기준선과 account 교차검증을 추가했다. optional OHLC 결측과 실제1초 조회 지연은 정상 대조로 남겼다. malformed US 자료는 narrow 데이터 오류로만 종결하며 SQL/게시 장애를 숨기지 않는다.

수정본 중간 결과:

- 부모 신규4모듈53통과(UTC20.99초), 독립 재리뷰6통과(UTC2.57초/KST3.48초). 네이티브 spec·quality 판정은 R1–R3와 수정 delta에 한정한 `APPROVE_THIS_SLICE`다.
- 부모 전체 UTC4324통과/기존xfail2/경고4(235.93초), exit0·격리0. 이후 추가한2개와 장애 경계 시험 전 실행이므로 최종 suite 수치로 사용하지 않는다.
- 독립 인수 최종32개에 FIRST/SECOND·ATR/core/급락 full DTO 대조와 실제 accepted trend/expert 미소비 lane 대조를 포함했다. 부모 재실행 UTC32통과15.01초·격리0.
- 추가 fault5(게시 실패·commit 응답 유실·generic VIX 및 실제 classifier 완료 취소·nested DTO 불일치)는 부모 UTC5통과3.44초, 독립 리뷰어가 acceptance32와 함께 UTC37통과19.28초·격리0 및 한정 승인했다. VIX 대조를 C3 application 취소 증거로 대신하지 않는다.

Opus A 후속 수정 **이전** 소스/시험의 전체 결과는 UTC4331passed/기존xfail2/경고4(232.16초), KST4331/2/4(231.70초), 각각 exit0·격리0이다. 당시 신규60은 작성자10·독립 인수32·스케줄러7·최초 리뷰6·추가 장애5다. 이전 중간4324/부분시험/ignored probe는 이 수치에 합산하지 않는다. 경고는 기존 pykrx1·의도적 fork3이다. 이때 명시 stage 후 문법·비밀정보 의심 패턴·diff 검사도 통과했으나 아래 provenance 수정 이후 최종 검증을 대체하지 않는다. 전체 시험 명령은 아래와 같다.

```bash
env -i PATH=/usr/bin:/bin LANG=C.UTF-8 TZ=UTC \
  PYTHONDONTWRITEBYTECODE=1 PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 \
  QWQ_DEPLOY_SSH_KEY=/tmp/qwq-offline-no-ssh-key \
  /home/ubuntu/projects/qwq-ai-trader/venv/bin/python \
  -m pytest -q -p no:cacheprovider --tb=short tests
# 같은 명령의 TZ=Asia/Seoul로 별도 직렬 실행
```

모든 시험은 임시 SQLite·실제 owner/core queue·외부 경계 fake·환경 격리로 수행했다. 합성 입력과 모델 동의는 공식 외부 계약이나 거래 수익성의 증명이 아니다.

13개 범주별 핵심 인수는 아래와 같다. ‘근거 있음’을 모든 변형 조합의 완전 증명으로 읽지 않는다. 독립 리뷰 승인은 이 구현 범위에 한정하며 전체 통합/운영 gate를 열지 않는다.

| 범주 | 실제 확인한 경로 | 정확한 경계 |
| --- | --- | --- |
| 1 cap 선행 | 모델 Event 대기 전 정오 cap commit, 모델 None/TimeoutError 후 보존 | 실제15초 벽시계 timeout 대기는 별도 미실행 |
| 2 복구 | 실제 BUY40 등록 실패→classifier→hot/cold repair | baseline·source를 재구성하지 않고 새 store/runtime 복원 |
| 3 전역만 변경 | 원 states 빈 상태·같은 label·config drift도 full digest/replay 기록 | updated 반환값을 권한으로 쓰지 않음 |
| 4 whole manager | 정상 B 또는 core B 존재 시 원 skip 분기 보존 | 복구 A만의 상태로 원 분기를 다시 선택하지 않음 |
| 5 정책 parity | NONE 관련 인수, FIRST/SECOND·core·ATR4·active crash의 공용 ExitManager full DTO 대조 | 모든 label×stage 조합은 아님; 기존5분 force=True 회귀도 보존 |
| 6 적용 수명 | 즉시 receipt 읽기, 독립 주기 sync, 새 classifier 뒤 원 retry, 같은 ID 다른 본문 동시 경합 | 실제 source/정책만 재검증; 모델 재호출 없음 |
| 7 혼합 재생 | 5분→regime→quote→5분 합집합, historical SELL 차단 | 모든 전이 순열은 아님; 무기록 과거 청산은 BLOCKED |
| 8 무결성 | force/rule/changed scope와 관련 row/event/chain 재해시 cold 차단, 계좌 변조 차단, sync ABA 공동삭제 repair 차단 | nested DTO 시험은 내부 digest 불일치; full before/after/읽기 원장까지 일관 재작성한 위조 방어 증명 아님. ABA는 detached 실제 이력의 replay 검사 |
| 9 horizon | 늦은 정오 stale, 동률의 나중 commit, 이후 적용된 normal 회복, 중복 무효 | duplicate/ignored5분은 회복 사실이 아님 |
| 10 의존 경합 | 읽은 보호 정책 변경은 stale, 실제 fill·미소비 VIX/trend/expert는 허용 | C2 pending/ABA 회귀 포함; 모든 C3 await 위치 조합은 아님 |
| 11 시각 | 원 REST market_as_of=None, 전일/위조 시각 거부, model-held day fence | 기준선 day/generation/fence의 모든 손상 변형은 미실행 |
| 12 장애 | SQL 전·commit 응답 유실·게시 실패·새 runtime 복구, 파일 실패/역순, model/완료 task 취소 drain | 게시 내부 모든 줄의 장애 및 begin/seal/complete 취소 전체 행렬은 아님 |
| 13 경제 불변 | portfolio/risk/lots/intents/attempts/reservations/inbox/outbox/source/regime/intraday·큐 불변, 반복 repair | 수익성·실주문 허가와 무관 |

시험 위치: `tests/test_execution_regime_noon_replay.py`, `test_execution_regime_noon_replay_acceptance.py`, `test_execution_regime_noon_review.py`, `test_execution_regime_noon_scheduler.py`, `test_execution_regime_noon_fault_boundaries.py`, `test_execution_regime_noon_provenance_followup.py`.

## Opus 실행 진단과 검토 범위

첫 전체 입력185739bytes(SHA256 `ea873638e0573f24f686c5eae4d71f89d5dde92a0a485c4ff5b1066b23127893`)은 실제 `claude-opus-5` 진행727회 이후900.137초에 절대 시간 상한으로 종료(exit124/child-9). idle 여유178.794초·관측 오류0·stderr0이며 최종 result/리뷰/사용량·비용은 없거나 알 수 없다. **승인으로 세지 않는다.**

systematic-debugging 절차로 입력만 바꾼 짧은 대조는 같은 runner/모델/xhigh/권한에서2.976초·exit0·CONTROL_OK로 완료했다(0.02677USD). 따라서 로그인 실패나 실행기 기본 호출 불능은 관측되지 않았다. 넓은 검토 범위가 주어진 시간 예산과 맞지 않았다는 가설을 검증하기 위해 A(입력·noon/horizon·원자 저장)와 B(full 보호 적용·재생)를 분리했다. 같은900초/5USD 상한·도구 OFF·모델/권한/effort/runner를 유지한 두 검토는 각각233.484초·268.73초에 완료했다. 범위 분할 후 완료된 사실을 기록하되 입력 크기와 시간의 일반적인 비례 법칙으로 확대하지 않는다.

- A: 입력59891bytes, 0.73316USD, exit0, 실제 `claude-opus-5`, 요청xhigh(유효 effort 미노출), 판정 `CHANGES_REQUIRED`. 실행 성공을 승인으로 세지 않았다.
- B: 입력65964bytes, 0.81104USD, exit0, 실제 `claude-opus-5`, 요청xhigh, 판정 `APPROVE_THIS_SLICE`(P0/P1 없음). 파일 I/O 없는 DTO 복원, full application 검증 이후 재생, 주문 누적 watermark 보존, version 엄격 순서 검사를 실제 코드로 대조했고 관련 기존8시험 UTC3.56초·격리0이다.
- B의 예약 namespace 의견은 비차단 advisory로 보류한다. 정상 classify/scheduler는 내부 UUID를 사용하고 pending/current 검사가 있으므로 정상 caller 충돌 반례는 확인되지 않았다. 향후 저수준 source ID를 선택하는 수동 경로를 제품 지원 계약으로 열면 예약 prefix 검사를 재검토한다. 미사용 import 정리는 기능 변경 근거로 삼지 않았다.

### A 지적 재현·좁은 수정

새 시험15개는 실제 owner·SQLite·application·같은 runtime의 restore를 사용해 UTC8실패/7통과10.49초, KST8실패/7통과10.70초였다. 이 helper의 restore를 새 store/runtime의 cold restore로 세지 않는다. KR 서로 다른 수신 시각과 normalized 유효0 대조를 추가한17개도 수정 전 UTC9실패/8통과8.19초였다. 사례 수를 독립 결함 수로 세지 않는다.

- 확인: KR 원 수신 시각과 잠정봉 입력 시각이 분류 기준시각으로 바뀌어 표시됨. KOSPI/KOSDAQ 각각 원 수신 시각을 보존하고 시장시각 미제공을 명시했다.
- 확인: normalized key가 있으면서 명시 결측인 값을 raw alias로 되살림. 현재 정상 producer가 이런 충돌을 만든다고 입증한 것은 아니며 DTO/provider 경계 반례다. key 자체가 없을 때만 기존 legacy fallback을 유지하고 normalized/legacy 유효0을 보존했다.
- 확인: 미국 지수 원 시각이 없을 때 현재 시각을 만들거나 첫 지수의 시각으로 다른 지수까지 대표함. 소비한 각 지수의 원 `fetched_at/as_of`를 분리 표시하고 미제공 시각은 미상으로 남겼다. 실제 producer의 전부 결측 형태도 시험했다.
- 불성립: “정오 normalizer와 prompt의 TTL이 달라 같은 날 오래된 자료를 한쪽만 거부한다.” 양쪽은 같은 provenance/미래/한국 날짜/등락률 일치 검사를 쓰며 TTL 자체가 없다. 당일1시간 전 성공·전일/미래/잘못된 TR 거부·optional OHLC 결측 허용을 대조했고 새 TTL이나 필수 OHLC 조건을 추가하지 않았다.

제품 delta는 `regime_classifier.py` 한 파일이며 다른11제품은 B 리뷰 후보와 동일하다. 분류 시계 위치·산식·위험 cap·조회 수/순서·모델1회/15초·저장 권한은 그대로다. truthy 비-dict normalized 입력은 모델 호출 전 failed로 끝나는 기존 경계를 보존했다. 정상 JSON을 반환하는 model spy로 calls0/no application을 확인하는 마지막 시험은 수정 후 추가한 positive guard이므로 RED였다고 주장하지 않는다.

작성자 최종 관련79(기존60+provenance19)는 UTC45.29초/KST45.14초 각각 통과·exit0·격리0이다. 제품 SHA256 `1c381124fc8205e953974c83e8ffaa6511be478766f0dad680260afa51096fc8`, 추가 시험 SHA256 `94bc5e96faa27d5e48747f7549ac9363dea82d0a41270db319d584ac5d2ade7a`. 네이티브 독립 후속 spec/quality는 `APPROVE_THIS_SLICE`이며 새19 UTC 직접 실행13.08초·격리0이다.

**최종 통합 전체: UTC4350passed/기존xfail2/경고4(244.32초), KST4350/2/4(239.88초), 각각 exit0·격리0.** 최종 제품12·시험6 파일은 독립 리뷰본과 SHA256이 전부 일치한다. 신규79를 전체4350에 다시 더하지 않는다. 전체 시험을 별도로 실행한 뒤 문법·비밀정보 의심 패턴·diff 검사를 수행했다. 문서 감사에서도 당시 미완 결과 placeholder를 제외한 수치/범위 과장은 발견하지 않았고, 그 placeholder는 실제 완료 결과로 갱신했다.

Opus A 좁은 재리뷰도 `APPROVE_THIS_SLICE`, P0/P1 없음으로 완료했다. 입력44792bytes(SHA256 `cf9b5ca67e427d2e975f0104ffa07bb82fc844fa38b191c2a2ffbc630e4b63a8`), 243.331초, exit0/child0, 0.740075USD, 실제 `claude-opus-5`·요청xhigh이며 오류/도구 호출0이다. 이는 제공된 소스/시험의 정적 리뷰이고 Opus가 pytest를 재실행한 결과가 아니다. 기존 첫 A의 수정 요청은 이 재리뷰로 닫고, 제품이 동일한 B 승인은 유지한다.

후속 advisory는 비차단으로 기록한다: KOSPI 실패/KOSDAQ 성공 표시의 주어 명확화, 수신 시각 포맷 통일, 분 단위 봉 라벨 offset 표시, US 미상 시각 문구의 양성 assertion 보강. 새 기능이나 전체 입력 스키마 강화로 범위를 넓히지 않았다. classifier는 runtime의 aware KST clock을 사용한다는 기존 불변식을 유지하며, 따라서 prompt의 한국 날짜 검사와 normalizer의 고정 KST 검사는 같은 날짜 축이다. 모델/effort 및 입력 범위 변경 내역은 위 실행 이력과 구분해 기록했다.

부모가 확정한 좁은 판단: application과 replay의 A→B→A 왕복을 함께 삭제한 경우에는 기존 보호 정책 generation 이력을 복구 완전성 검사에 사용한다. 실제 체결 anchor 이후 설명되지 않는 전역 보호 변경은 복구를 차단한다. 일반 정책 변경·게시 자체를 새롭게 금지하지는 않는다. 이로 인해 아직 이행되지 않은 writer의 보호 변경 이력은 복구 불가로 남을 수 있으며, 근거 없이 복구 성공으로 추정하지 않는다.

## 미변경 안전 경계와 이후 작업

이번 작업은 오프라인 feature 구현이다. main 병합·운영 SSH·배포·재시작·주문·설정 변경은 하지 않는다. KIS만 주문·잔고를 담당하며 Toss는 관측용이다. `trading_ready=False`, 미지원 정정 및 공식 최초 인계/취소 최종성 증거 부족에 대한 시작 차단을 유지한다.

C3 인수와 독립 리뷰를 닫았어도 C4 아침 경로, 나머지 writer·실제 설치, 전체 C/F/G/R, 통합 broad 리뷰와 긴 이력의 성능 검증은 별도로 남는다. 테스트나 모델 간 합의를 수익성·운영 준비의 증거로 해석하지 않는다.

다음 순서는 C4의 장전 text diagnosis·once/day/window/optional 입력 및 모든 소비 경로 감사(Plan) → 원본 RED를 보존한 owner 이행과 실제 caller 연결(Do) → 역순·누락·취소·SQL·새 runtime 복원 인수와 독립 리뷰(See)다. 이후 qualification/최종 sizing·gateway·나머지 writer·factory를 연결하고 전체 C/F/G/R·broad 리뷰를 수행한다. 공식 최초 인계·취소 최종성 증거가 없으면 미지원/시작 차단을 유지하며, main 통합·운영 전환은 별도 판단한다.
