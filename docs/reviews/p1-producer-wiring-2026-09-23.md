# P1 보호 생산자·엔진 배선 — 진행 및 검증 원장

## 범위와 판정

사용자 "니가 판단해서 진행해"에 따라 [실행 계획](../superpowers/plans/2026-09-23-p1-producer-wiring.md)의
S2~S5를 개발 브랜치에서 진행한다. 시작은 `6394d22`, 정책·계획 커밋은 `2ddacf4`다.
**P1 S2~S5의 개발 배선 구현·범위 리뷰·최종 전체 검증·feature 통합/푸시를 완료했다.**
제품 통합은 `2a51d28508d634d9872adc0f094e5e94140b86df`이고 문서·주석 선행 커밋은
`474aebd`다. main 통합·운영 설치나 legacy 대비 보호 성능 완전 동등을 뜻하지 않는다.

거래와 잔고는 KIS 정본이다. 실 API·주문·운영 checkout·배포·재시작·설정·킬스위치·Toss
권한은 변경하지 않는다. 신규 durable schema나 거래 준비 상태 강제도 추가하지 않는다.

## Plan — 위임받은 결정

| 선택 | 결정 | 남는 대가 |
|---|---|---|
| 정규장 보호 매도 | regular MARKET | 실제 체결가·슬리피지 보장 없음 |
| 마감 구간 보호 매도 | closing LIMIT, 취소·MARKET 폴백 없음 | 미체결의 일자 전환 차단22 유지 |
| 보유기간 | ExitManager 영업일 정본 | 달력일 기준 legacy와 차이 유지 |
| 무결정 틱 쓰기 | 20초, 결정 틱은 즉시 기록 | 순간 고점 누락을 미스로틀 대조로 실측; 운영 채택은 별도 |
| 복구 결정 | 원 admission의 intent·명령·가격 보존 | 매도 attempt 증거가 없으면 제출 성공으로 보지 않음 |
| 종료 | 기존 runtime command scope에서 shield 작업 배수 | 종료 후 신규 작업은 거부; 별도 task를 보호 장벽에 등록하지 않음 |
| 시세 출처 전환 | 원 관측으로 REST/WS 분기 | REST의 보호-only 무효화 유지; WS 복귀도 기존 신선도 검증을 통과해야 함 |

## Do·See — 단계별 근거

| 단계 | 구현 범위 | 검증/통합 상태 |
|---|---|---|
| S2 | 신규 보호 생산자와 실제 owner/store 시험 | fix3 `9aa292d`, 소유138/관련253건(29.01초,rc0). 원 독립4 RED→7 GREEN와 정상 대조9건으로 native 재승인. S4와 공동 교차 승인·조건 처분·최종 전체 통과 |
| S3 | engine attach 가격 writer·매도 수량/유형 | fix3 `0477c29`, 관련106건. native 및 실제 Opus 한정 승인, 최종 전체 통과 |
| S4 | factory 설치 및 runtime 읽기 전용 health | `11b9f3e`, 신규13/관련245건(42.80초,rc0). 작성자·독립 변이 각3종 검출·원복. native Spec/Quality 승인·독립68건(17.05초,rc0). 외부 승인·조건 처분·최종 전체 통과 |
| S5 | legacy 주석·문서·범위 통합 리뷰 | 주석2파일 AST 동일·상대 링크124개 누락0, 문서 검토7건 처분. 독립 broad Spec 승인/Quality 경미 이월·신규 C/I 0·55건(9.44초,rc0) |

### S2/S3 격리 통합 후보의 전체 검증

정본 feature의 제품은 아직 바꾸지 않았다. 별도 `feature/p1-integration-20260923`에서
`1be587c` 위에 `9aa292d`·`0477c29`를 충돌 없이 결합했다. 검증 tree는
`c50d1f1082a4c0e5f67263266b71280c665442ce`다. 모든 작업자 종료 뒤 coordinator만
아래 명령을 UTC→Asia/Seoul 순서로 실행한다(운영 설정/환경을 상속하지 않음).

```text
env -i PATH=/usr/bin:/bin LANG=C.UTF-8 TZ=UTC PYTHONDONTWRITEBYTECODE=1 PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 QWQ_DEPLOY_SSH_KEY=/tmp/qwq-offline-no-ssh-key /home/ubuntu/projects/qwq-ai-trader/venv/bin/python -m pytest tests -q -p no:cacheprovider --tb=short
```

UTC는 **5364 passed / 기존2 xfailed / 경고4 / 430.89초 / rc0 / 격리 위반0**.
동일 명령의 TZ만 Asia/Seoul로 바꾼 KST도 **5364 passed / 기존2 xfailed / 경고4 /
426.80초 / rc0 / 격리 위반0**을 통과했다. 제품/시험 트리는 두 실행 전후 동일하다.
`verify.sh`의 문법·비밀정보 의심 패턴 검사도 rc0이며, 그 실행의 테스트 부분은 이미 위
두 명시 실행을 완료해 `QWQ_VERIFY_SKIP_TESTS=1`로 건너뛰었다. 이를 별도 전체 시험으로
세지 않는다. 격리 후보 고정 commit은 `5499b6d`이며 정본 feature 제품 통합은 아직 아니다.
전체 결과와 별개로 S2 fix3의 마지막 교차 공급자 게이트는 S4와 함께 남아 있다.

이전 S1·S1′의 5183 passed는 [해당 부품 보고서](p1-components-2026-09-23.md)의
검증이며 후속 후보의 검증으로 재사용하지 않는다. 단계별 전체 시험은 모든 작업자 종료 뒤
coordinator 단독으로 UTC·KST 순서로 실행한다.

사전 대조에서 독립 gateway 인수 E1의 `update_position_price` 예외 단언이 S3 no-op
설계와 모순됨을 확인했다. 그 단언만 owner/live 상태 무변경 검증으로 교체하고,
체결·주문 writer의 명시 거부는 유지한다. 무수정37건 통과를 주장하지 않는다.
또한 재시작 후 현재 보호 intent의 확정 미체결이 증명되면 첫 관측에서60초를 새로 기다린다.
durable 종결 시각이 없는 상황의 보수적 대가이며 UNKNOWN이나 체결을 시간으로 지우지 않는다.

### S4·범위 독립 리뷰의 별도 근거

설치 독립 리뷰는 입력 검사 위치·내부 기동 오류 전파·health 별칭 노출의 행동 변이3종을
각각 검출하고 원복했다. 후보13개와 기존 거부/배선 시험, 추가 독립4개를 합한68건이
통과했다. 전체 범위 리뷰는 시작 `6394d22` 이후에 한정하며 이전234+커밋 재승인이 아니다.
추가 독립2개 포함55건에서 익절5+5 체결의 stage·양의 초기R50000·고점11200 보존,
완료 후 새90주 예약을 확인했다. regular→closing 경계에서는 원 NOT_SENT 행을 보존하고
59초 미송신/60초에 새 LIMIT100 예약을 확인했다. 합성 readiness/HTTP/최종성 증거를
쓴 오프라인 시험이며 실계좌 증거로 승격하지 않는다.

기존 경미 이월은 Lock 사적 waiter의 시험 구현 결합, 직접 S3 closing90 예약의 명시 단언,
향후 cooldown 정책 변경을 위한 시험 주석, EOD에서 source 전환 계수가 갱신되지 않는
관측 누락이다. 마지막 계수를 모든 관측의 완전한 출처 전환 지표로 사용하지 않는다.

### S4 포함 최종 후보 전체 검증

최종 제품 후보 `11b9f3e99fc8ac3a4ca6ab03dede5b06e7016b57`, tree
`71f10852598d830651783aab573752026f7d6668`에서 외부·native 전 작업자 종료 뒤
coordinator 단독으로 위 명령을 UTC→Asia/Seoul 순서로 다시 실행했다.

| 환경 | 실제 결과 | 시간 | 종료/격리 |
|---|---|---|---|
| UTC | 5377 passed / 기존2 xfailed / 경고4 | 428.86초 | rc0 / 접근 시도0 |
| Asia/Seoul | 5377 passed / 기존2 xfailed / 경고4 | 430.32초 | rc0 / 접근 시도0 |

원 출력은 `s4-full-utc.txt`·`s4-full-kst.txt`다. 실행 전후 HEAD/tree/clean이 동일했다.
이전 S1 기준보다 신규 회귀194개(S2 138+S3 43+S4 13)가 늘었다. 별도 독립 scratch의
시험 수를 전체 수에 중복 합산하지 않는다. 경고4개는 기존 pykrx 자원 API1개와 fork
계좌 lease 시험3개의 deprecation 경고다. 정적 문법·비밀정보 의심 패턴 검사도 수행하며,
전체 시험을 이미 직접 실행한 `verify.sh` 단계는 `QWQ_VERIFY_SKIP_TESTS=1`로 구분한다.
일반 의심 패턴 검사는 완전한 비밀정보 부재의 증명이 아니다.

### 정본 통합 후 확인

문서·주석 선행 커밋 위에 후보를 충돌 없이 합쳤다. `11b9f3e`와 비교해 tests는 바이트
동일하고 제품 차이는 scheduler/batch의 주석2곳뿐이며 두 파일의 전체 AST가 같다.
그 외 차이는 Markdown뿐임을 검사했다. 통합본에서 다음8파일의 관련 회귀를 새로 실행해
**308 passed / 58.84초 / rc0 / 격리0**을 확인한 뒤 제품 merge `2a51d28`을 커밋·푸시했다.

```text
env -i PATH=/usr/bin:/bin LANG=C.UTF-8 TZ=UTC PYTHONDONTWRITEBYTECODE=1 PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 QWQ_DEPLOY_SSH_KEY=/tmp/qwq-offline-no-ssh-key /home/ubuntu/projects/qwq-ai-trader/venv/bin/python -m pytest tests/test_execution_p1_install.py tests/test_execution_install_factory.py tests/test_execution_p03_wiring.py tests/test_execution_p1_engine.py tests/test_execution_p1_producer.py tests/test_execution_p1_producer_recovery.py tests/test_execution_signal_gateway_acceptance.py tests/test_engine_legacy_stale_eviction_characterization.py -q -p no:cacheprovider --tb=short
```

원 출력 `merged-focused.txt`를 보존했다. 통합본에서도 문법/비밀정보 의심 패턴 검사 rc0,
diff check0, 상대 문서 링크135개(최종 마감 문서 재검사137개) 누락0이다. 전체5377쌍은 위 고정 후보의 실행이고,
문서/주석 포함 통합본의 새 실행은308건으로 구분한다. 제품 운영 설치 호출자0도 유지한다.

## 모델·예산·외부 리뷰

- critical 구현: native `gpt-6-astra/high`, 독립 재현·최종 리뷰: `gpt-6-astra/xhigh`.
- 후속 배선 목록 분석: `gpt-5.6-sol/high`, 문서 갱신 위치 대조: `gpt-5.6-luna/medium`. 문서 통합은 coordinator.
- native 실제 모델/effective effort는 실행 메타데이터 미노출로 미검증이다.
- 외부 리뷰는 도구0의 `claude-opus-5`, requested xhigh. 회당 CLI 예산 $5,
  총8호출 이내(대조·재시도 포함), 절대600초·시작120초·정체180초다.
  동일 일시 실패 재시도는 최대1회이며, 예산·인증·모델·안전 경계를 우회하지 않는다.
- 외부 입력은 선택한 코드·합성 시험·계약만이다. CLAUDE 원문·자격증명·계좌 실응답은 제외한다.

| 호출 | 목적 | actual model / 결과 | 비용 / 시간 | 입력 근거 |
|---|---|---|---|---|
| 1/8 | 도구 없는 소입력 연결 대조 | claude-opus-5, rc0, CONTROL_OK | $0.030395 / 3.242초 | 629B, SHA-256 `c65e50c08cda036f5e8baba5d76f9b4058470efef020a4888e780c98b3c292c7` |
| 2/8 | S2 최초 소스 리뷰 | claude-opus-5, rc0, REQUEST_CHANGES | $1.2563 / 438.555초 | 102341B, SHA-256 `d1e146c7cd249a399f9b8e7e38ba73ca48842c4042b39dcb1a904e6f518d4b3e` |
| 3/8 | S3 최초 소스 리뷰 `14a396a` | claude-opus-5, rc0, REQUEST_CHANGES | $0.805425 / 305.89초 | 43030B, SHA-256 `6f4c780b7b11a629f93698505e56175f26bb7cf81c91d700c278905424e42d9e` |
| 4/8 | S3 fix2 재리뷰 `a28fd5a` | claude-opus-5, rc0, REQUEST_CHANGES | $0.74705 / 278.482초 | 44486B, SHA-256 `b6bec1c668b2564572a6618cbedeb7f6a316e9e1418e003abd7e452db89e5bd2` |
| 5/8 | S2 fix1 재리뷰 `f5a3ae0` | claude-opus-5, rc0, 조건부 APPROVE_THIS_SLICE | $1.050285 / 328.248초 | 103275B, SHA-256 `83ef2a320fa0e1a6b20763329704d93e10e99df2f49213d1ea9adae074c3fddf` |
| 6/8 | S3 fix3 재리뷰 `0477c29` | claude-opus-5, rc0, APPROVE_THIS_SLICE | $0.76057 / 245.304초 | 79671B, SHA-256 `7de10ba85663fa51609bee35baaa03783c831c68df522b3e4de9902689d7e303` |
| 7/8 | S2 fix2 재리뷰 `31512bf` | claude-opus-5, rc0, 조건부 APPROVE_THIS_SLICE | $0.99541 / 363.509초 | 72679B, SHA-256 `2a5d92db5401108c643296c86fdb50b157e69143d104670aff2489e35c61dda4` |
| 8/8 | S2 fix3+S4 공동 리뷰 `11b9f3e` | claude-opus-5, rc0, APPROVE_THIS_SLICE·조건 I-1은 아래 실계약/독립 재현으로 처분 | $1.201735 / 314.432초 | 147650B, SHA-256 `5a1512ded54e5ae440a08f7fef057d0a61df2ea3c8bd58a1e11852a216df9be7` |

연결 성공은 코드 승인이 아니다. effective effort는 외부에서도 노출되지 않아 요청값과 구분한다.
원시 실행·리뷰·변이 출력은 커밋 제외 경로 `.superpowers/sdd/2026-09-23-p1-producer-wiring/`에
보관하고, 이 문서에는 원격 인계 가능한 요약만 기록한다.
호출8까지의 누적 실비는 **$6.847170**이며8회 한도를 모두 사용했다. 마지막 공동 리뷰는
승인과 함께 보호 상태 결손+pending 잔존의 조건부 I-1을 남겼다. 아래 계약/재현 처분을
수행했으며, 추가 외부 호출·예산 증액·조건 확인 생략은 하지 않았다.

### 마지막 교차 리뷰의 조건·미제공 계약 확인

I-1은 미제출 감사/pending이 있는데 보호 states만 없으면 `_audit_recovery`가 KeyError로
설치 후 sweep을 중단한다는 지적이다. 리뷰는 승인 차단으로 올리지는 않았지만 통합 전에
수정 또는 실제 degrade 계약의 증거를 요구했다. 별도 Astra/xhigh가 실제 실패 전이와
손상 입력을 구분해 **5 passed / 2.53초 / rc0 / 격리0**으로 재현했고 coordinator가 코드와
시험을 대조했다. 실제 quote 복구 계산 실패는 원 states와 pending을 **둘 다 보존**한다.
실제 체결 reducer도 중간 임시 상태 삭제 후 예외가 나면 원 DTO의 둘을 보존하고 degraded를
더하며, 전량 청산은 성공/실패 모두 둘을 함께 제거한다. “degrade가 pending을 항상 삭제”는
사실이 아니고, “정상 이전 상태에서 그 결손 조합을 새로 만들지 않는다”가 확인된 불변식이다.

반대로 손으로 states만 지운 합성 저장본은 현재 관계 검증을 통과하고 KeyError가 실제로
재현된다. 이때 상태 무변경·HTTP0·last_error 기록을 확인했다. 따라서 schema가 손상을
거부한다거나 임의 저장본을 정상 복구한다는 보증은 하지 않는다. 해당 불일치는 명명된
복구 보류 대신 설치 실패로 끝나는 미지원 손상 복구이며 차단29에 남긴다. 제안된 `.get`
수정을 무조건 적용해 전역 예외를 종목별 보류로 바꾸지 않았다. 외부가 요청한 실계약
증거로 조건을 처분한 것이며, 변경된 코드를 외부가 재리뷰했다고 주장하지 않는다(제품 수정0).

추가 정적 대조: runtime은 필수 clock을 `_now()`에서 aware/KST로 검증한다. 실제 KR REST
생산자는 OHLC를 Decimal로 만들고 WS는 파서 record와 원관측을 함께 싣는다. 단 KIS 응답의
high/low 결측이0으로 내려오는 경우에는 새 생산자가 틱을 거부하고 실패를 기록한다.
정상 타입 호환을 확인했을 뿐 실제 피드의 항상 유효한 자료 공급/보호 지속성을 입증하지
않았으며, 실제 설치 전 데이터 품질 인수 대상으로 남긴다.

## 최초 리뷰의 재현과 처분

S2 독립 변이3종 및 S3 독립 변이3종은 각각 검출 후 원복했다. 변이 성공은
미수정 결함의 승인이 아니다. 별도 실제 owner/gateway/합성 HTTP에서 다음을 확인했다.

- S2: 최신 WS 뒤 오래된 가격이 EOD 전량 송신으로 이어짐. durable quote0 규칙과
  원시세 검증 면제는 다르므로 EOD 전 순수 신선도 검증을 유지한다.
- S2: 복구된 비접두사 full intent가 재생성 후60초 재시도 대기를 우회함.
  원 outbox/intent 연결 증거를 사용하되 audit outbox의 자동 재주문은 금지한다.
- S2: 제출 전 원결정 증거가 있는 pending을 재생성 sweep이 orphan으로 삭제함.
  증거를 보존하고 명시적인 복구 보류를 남긴다.
- S2: 이전 부분10주 체결 뒤 보류 full100이 잔량90 보호를 영구 차단함.
  충돌하는 새 EOD·일반 손절 결정 생성 시점을 막고, 원결정 수량을 임의 보정하지 않는다.
- S3: 호가 await 중 동일일 휴장 캐시가 갱신돼도 POST1건. 최종 lock에서 같은
  달력 함수로 거래일을 다시 확인하는 회귀를 고정한다.

외부 리뷰의 모든 제안을 그대로 수용하지 않았다. owner.state는 deepcopy 반환이므로
스냅샷 별칭 지적은 해당하지 않는다. lifecycle은 observed_amount를 초기화하며,
손상된 경제 증거를0으로 대체하지 않는다. 기존 지표 캐시는 float여서 Decimal 외 전부
거부하는 제안 대신 명시적 유한값 정규화로 처분했다. S2의 경계 어댑터 시험은 계속
실제 engine 배선 검증과 구분한다.

정상 ACK 대기/UNKNOWN·불변식 실패의 관측 구분도 보강한다. pending 동안 가격·고점
갱신이 멈추는 제한, 순서를 증명할 수 없는 과거 full 거절 이력의 재시작 보류는 자동
해제하지 않으며 운영 채택 전에 해결할 항목이다.

fix1은 원 RAM/admission 없는 **full** 감사 결정도 복구 보류로 남긴다. 감사 행의
종목·수량·intent 및 현재 보관 결정 연결이 다르면 자동 소비하지 않는다. 독립 추가 시험은
수동10주 중5주 경제 적용(보유95·예약5)에서는 새 손절을 막고,10주 적용 완료 후 다음
실제 틱에서90주 보호 결정을 생성함을 확인했다. S2에서는 엔진 경계만 시험 어댑터이며,
실제 engine→queue 전체 배선은 S4의 별도 인수다.

S3의 정규장 LIMIT 허용과 무효 보호 ID의 legacy 진입은 fix2에서 차단했다. 기존
종목 단위30초 cooldown은 보존한다. 이는 단발 지연뿐 아니라 반복 일반 신호와의 경합에서
보호 지연이 지속될 가능성이 있어, 반복 경로의 특성화 및 운영 전 우선순위 정책 해소가
필요하다. 이번 개발 배선 승인이 그 운영 위험의 수용을 뜻하지 않는다.

fix3는 실제 lock 대기자 등록 후 세션을 전환해 마지막 방어를 검증했다. limiter 대기 중
마감으로 넘어가 확정 미송신된 원 attempt를 보존한 채,31초 뒤 새 closing LIMIT 요청이
정상 ACK되는 경로도 통과했다. 반면 일반 신호를31초마다 넣으면 보호 신호가 계속
cooldown에 막히는 반례를 두 주기 고정했다. 일반 신호 중단 뒤31초 경과 시 보호 송신은
회복하지만 **반복 경합의 최대 보호 지연은 증명되지 않았다**. 운영 전환 차단 항목이다.

S2 fix2는 모든 SELL 행의 관측 실패를 먼저 집계하고, 정상 sweep 완료 시에만 현재
resume invariant 경보를 해소한다. author 관련343건(45.20초,rc0), 독립 focused13건과
submit 예외/기대 보류 경계1건(각 rc0)을 통과했다. 미식별 감사 종목의 손상은 영향 범위를
증명할 수 없어 전역 fail-closed를 유지한다. 전용 경보·승인된 수동 복구 절차는 미완이며,
이 수정에 자동 재수량 계산·TTL 폐기·원 감사 행 삭제 권한을 추가하지 않았다.

최종 범위 리뷰에 남길 경미 항목: 직접 대입된 non-dict Mapping/BUY 보호 표식은 현재
정상 SignalEvent 생성 계약 밖, asyncio Lock 사적 waiter 단언의 버전 의존성, closing
후속 attempt 예약90의 명시 단언 보강, 반복 cooldown 특성화 시험의 향후 정책 변경 주석.
이를 해결되지 않은 주문 안전 지적이 없는 것과 혼동하지 않는다.

호출7 이후에는 기존 None/빈값 감사 종목 시험으로 잡지 못한 공백·phantom 종목을
독립 재현했다. 잘못된 키에만 보류가 걸리고 실제 보유 종목의 매도100주가 송신돼 전역
손상 보류 정책과 충돌했다. 또한 실제 stale/abandoned 재개 뒤 생산자 하트비트가 성공을
기록했지만, runtime의 실패 래치와 abandoned의 degraded는 유지됐다. 두 반례만 fix3에서
수정하며 이를 기존 runtime 안전 장벽 전체가 사라진 문제로 확대하지 않는다.

추가 계약 확인: `_submit`의 예상 차단은 이유 기록 후 정상 반환이고, 순수 preview는
owner.state의 deepcopy를 사용한다. `effect_source`는 완료 표식이 아니라 intraday 출처
분류다. 일반 quote 감사와 intents는 삭제 없이 누적된다. 따라서 intent만 먼저 지우는
경로는 확인되지 않았으나, 감사 전량 순회의 장기 처리 비용·보존/압축 정책은 미검증이다.
짧은8종목 합성 성능 시험으로 이 운영 위험을 해소했다고 보지 않는다.

## 남는 경계

관측 필드의 수명도 구분한다. `blocked_reason`·`pending_reasons`는 현재 sweep/tick의
결과이며 여러 차단이 있으면 마지막 이유가 대표값이다. `recovery_required`의 종목별
상세와 기존 runtime 실패/degraded를 함께 읽어야 한다. `last_error`·누적 계수·마지막
틱/quote 시각은 과거 관측을 보존하고, `invariant_violation`은 정상 sweep 완료 때
해소된다. `producer_running`은 종료 여부이지 주기 성공이나 보호 가능성의 증명이 아니다.
반환 DTO의 deepcopy와 실제 경보 소비자 연결은 별개의 계약이다.

차단23의 A/C·repair-only·행 없는 결과 불명, 시간외 보호 매도(26), intraday_preemptive
소비자 부재(27), 미체결 BUY/late-fill·최초 인계·취소 최종성·정책 재게시는 이번 배선만으로
해소되지 않는다. 반복 cooldown에 의한 보호 기아(28), 수동 복구 절차 부재(29), 일반
heartbeat/API로의 관측 전달 부재(30), 감사 누적 비용·순간 고점 손실 평가(31)도 남는다.
설치 차단의 정본은 [운영 인계 표](../operations/claude-migration-handoff-2026-09-20.md)이며,
[다음 세션 작업 순서](../operations/p1-next-steps-2026-09-23.md)에 후속 인수 범위를 정리한다.
실제 설치 호출자는 계속0건이어야 하며, 승인받은 개발 기능을 운영 사용으로 확대하지 않는다.

## 재현 자료의 보존 경계

전체 출력·리뷰 전문·실행 기록은 정본 작업공간의
`.superpowers/sdd/2026-09-23-p1-producer-wiring/`에 보존한다(커밋 제외).
독립 재현 전용 미추적 시험이 있는 아래4개 작업공간은 강제 정리하지 않는다.
그 작업공간의 HEAD가 최종 통합 후보와 같다고 가정하지 말고 각 보고서의 소스 경로/
후보 SHA·실행 명령을 먼저 확인한다. 합성 시험 코드만 있고 실계좌 자료는 담지 않는다.

| 작업공간(.claude/worktrees 아래) | 보존하는 독립 자료 |
|---|---|
| p1-producer-review-20260923 | 최초·fix1·fix2·call7 scratch4개 |
| p1-engine-review-20260923 | engine 및 external evidence scratch2개 |
| p1-install-review-20260923 | 설치기 독립4인수 scratch1개 |
| p1-broad-review-20260923 | 전체 경계2인수 및 Opus 조건5인수 scratch2개 |

정본 feature의 ancestry·원격 푸시·각 트리의 무변경 상태를 확인한 뒤 이번 깨끗한
`p1-producer-20260923`·`p1-engine-20260923`·`p1-install-20260923`·
`p1-integration-20260923` worktree4개와 대응 로컬 브랜치4개를 `--force` 없이 제거했다.
커밋은 모두 `2a51d28` 이력과 원격에 보존되어 복구할 수 있다. 위 독립 리뷰4개/미추적
시험9개, 정본 작업공간, 다른 세션·운영 worktree는 보존했다.
