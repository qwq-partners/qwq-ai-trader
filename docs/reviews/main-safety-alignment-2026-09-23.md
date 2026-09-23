# N2 — main 안전 동작 정합화·수동 복구 진단

## 범위

기존 개발선78a253b에 mainafa6e1e와 오늘 관측 수정3cfcdca를 통합하는 **feature 후보**다.
main에 새 owner 엔진을 설치하거나 기존 운영 체크아웃을 바꾸는 작업이 아니다.
주문·설정·실 API·배포·재시작0. readiness=False와 공식 증거/전체 C/F/G/R 차단은 유지한다.

## Plan → Do

1. main5파일을 byte-identical test-only commit0f581f5로 가져왔다. 원 feature에서
   77 failed/18 passed/수집오류1을 확인했다. [RED 원장](main-safety-contracts-red-2026-09-23.md)은
   당시 결과이며 최종 후보가 RED라는 뜻이 아니다. 기존31개는 당시 모두 통과했다.
2. coordinator 전용 worktree에서 main+관측 후보를 no-commit merge했다. 양쪽 이력을
   보존하고7개 충돌을 직접 정리했다. engine의 _attached_gateway와 _SellKeep 모두 유지,
   broker의 QueryResponse/HTTP·헤더 증거/획득별 lease와 main TR·페이지·관측을 결합했다.
   기본 legacy 첫 페이지 빈 tr_cont는 생략, execution 수집기의 명시 빈 헤더는 유지한다.
3. 새 main heartbeat가 attached owner 밖에서 취소를 호출하는 회귀를 실제 engine/owner
   하네스의 직접 호출·실큐2건으로 RED 재현했다. runtime이 있으면 gateway 유무와 무관하게
   legacy retry 앞에서 반환하도록 했다. 기존 legacy heartbeat 동작은 그대로다.
4. [수동 복구 절차](../operations/protection-recovery-diagnostics.md)를 작성했다.
   진단 exporter/CLI 구현 완료를 주장하지 않는다. 별도 Sol/high 검토의5개 지적을
   코드로 대조해 반영했다: partial_install, 안정성과 게시 정합성 분리, 복합 owner 건강,
   독립 게시 receipt 미지원, A/C의 종목별 durable 증거 부재. null effect_source 계수 차이와
   raw state/health 공유 금지도 명시했다. 재검토는 문서 한정 승인, 테스트 실행0이다.

## 기존 stale/eviction26개 처분

아래 건수는 parametrized node 기준이다. 수정된 main5파일의 assertion은 완화하지 않았다.

| 기존 특성화 | 건수 | 이번 처분 |
| --- | ---: | --- |
| 정규장 취소 후 시장가 폴백 | 1 | 유지 |
| 89/90초 임계 | 2 | 유지 |
| 원 주문 수량/수량 부재 폴백 | 2 | 유지 |
| 3차 폴백 pending 해제 | 1 | 기존 legacy 한계로 유지; owner에 허용하지 않음 |
| 동시호가 취소 후 무주문 | 1 | **취소 없이 지정가 유지**로 기대 교체 |
| 거절0/1회 횟수·포기 | 2 | 유지 |
| submit 예외 pending 해제 | 1 | 기존 legacy 한계로 유지; UNKNOWN 최종성 증명 아님 |
| cancel 예외 pending 유지 | 1 | 유지 |
| exit_exempt 시장가 재매도 | 1 | **취소만 시도, 조회 불가면 pending 유지**로 교체 |
| 이벤트 없이 최초 stale 미처리 | 1 | 유지·설명 정정; cancel_keep 하트비트와 구분 |
| 추적/조회 없는 fake BUY 취소0/3 | 2 | 결과 유지·호환 한정으로 이름/설명 정정 |
| BUY 취소 예외 | 1 | 유지 |
| 취소 수단 없는 BUY 두 경우 | 2 | 기존 legacy 호환으로 유지 |
| 가장 약한 포지션 축출 | 1 | 유지 |
| core/pending/exempt/winner/cooldown 보호 | 5 | 유지 |
| 5점 미만 격차 | 1 | 유지 |
| 같은 배치 두 희생자 축출 | 1 | 기존 legacy 한계로 유지 |

실제 추적/조회 가능한 BUY의 live/unknown/vanished·부분체결·상한 계약은
main의 test_engine_stale_pending_fixes가 별도로 고정한다. legacy의 판단불가 시간
예산 해제를 owner의 UNKNOWN 해제로 옮기지 않았다. 이번 통합이 legacy 모든 안전
결함을 해결했다는 뜻은 아니다.

## See — 중간 검증 및 수정 근거

모든 시험은 env-i·외부망/운영 상태 차단·합성 broker로 실행했다. 이전 후보 GREEN을
새 tree의 결과로 재사용하지 않는다.

- 수동 통합 직후 main/query/특성화7파일:228 passed/기존 기대2 failed.
- attached heartbeat RED2 → guard 뒤 위 관련4파일186 passed/19.49초.
- owner/계좌lease/query/main18파일:586 passed/하네스1 failed. object.__new__로 만든
  RM에 새 main _pending_cancel_keep 초기값이 없었다. 제품 기본값과 REQUIRED_RM_ATTRS를
  보강했고 실제 acceptance37 passed/17.95초. 제품 getter 폴백으로 오류를 숨기지 않았다.
- 독립 Astra/xhigh가 요청-legacy 비교 시험의 새 use_new 필수 인자 누락을 발견했다.
  기존 요청과 비교하는 시험이므로 use_new=False를 명시, assertion은 그대로 유지했다.
  requests+acceptance+heartbeat3파일250 passed/34.69초, 격리0.
- native Astra/xhigh 최종 검토는 범위 승인(미해결 P0/P1/P2 없음). 요청 생성·실제 GET
  어댑터·기존26 특성화·실큐 heartbeat를 독립 실행해220 passed/3.90초/격리0.
  main7개 helper와 feature POST/query/limiter 함수의 AST 보존도 확인했다.
  native 실제 모델/실효 effort는 metadata 미노출로 미검증이다.
- external 최종 검토 및 UTC/KST 전체 결과는 아래 최종 검증 절에 기록한다.

### 외부1차 검토 뒤 보완

Opus5/xhigh는464.067초/$1.332495, exit0으로 리뷰를 끝냈지만 REQUEST CHANGES였다.
실행 성공과 승인을 구분한다. 입력112,863bytes, SHA256
`2d6e30254e0d0017043644716415cc63efd1b43351a009a471332e4505abb00b`.

- P1-1: object.__new__ fixture의 _exempt_block_logged 누락을4개 시험의 준비단계
  실패로 확인, 실제 생성자 기본값을 추가했다. 제품 생성자는 이미 초기화하므로
  제품 getattr 폴백을 추가해 잘못된 하네스를 숨기는 권고는 채택하지 않았다.
- P1-2: fixture는 원래 engine 시계를11:00 KST로 동결한다. 시험 안에 시각/정규장
  단언을 추가하고 attached/gateway부재/legacy 양성 대조 × 직접/큐6개로 강화했다.
  legacy 대조만 실제 취소 경계에1회 도달한다.
- P1-3: ORDER는 기존 조기 차단으로 안전하지만 일반 attached 면제 SELL은 새 main
  사유 캐시/경고 스로틀 writer에 도달했다(경제 owner 상태 쓰기는 아님). direct/큐2건
  RED로 재현했다. **면제 SELL 거부는 유지**하면서 attached이면 legacy 장부 수정
  전에 반환한다. legacy 동작/보호 SELL 생산자 판단을 완화하지 않는다.
- P1-4: KR _rate_limit은 기존 feature처럼 return await acquire를 보존한다.
  src/scripts의 추가 override는 없고 별도 US는 무변경이다. None을 반환하는 일부
  테스트 no-op은 acquire도 호출하지 않는다. 실제 limiter·fakeHTTP 통합으로
  첫페이지/취소/늦은 응답/후속lease 보존을 검증했다(독립 query3파일90 passed).
- P2 진단: 실제 health 읽기에서 mutation/network 금지 spy·owner/RAM/예약/게시 버전
  전후동등성·반환 중첩 복사본 변경 독립성을 시험한다. exporter 자체는 여전히 미구현이다.
- 나머지 legacy 면제 재시도·BUY 해제 경합·타이머/cache·고아keep 권고는 main의
  7helper+clear_pending과 AST가 같다는 것을 대조했다. 기존 main의 검토 과제로
  남기며 이번 scope에서 위험 정책을 다시 바꾸지 않는다. 무인자 limiter 해제도
  기존 호환으로 남아 있으므로 lease 보존을 모든 호출자/프로세스의 상호배제로 확대하지 않는다.
- QueryRequest의 tr_cont 기본값은 빈 문자열, 수집기의 첫페이지도 빈 문자열이다.
  실제 adapter/HTTP 인수의 첫페이지''→다음N/legacy헤더부재 시험으로 확인했다.

두 번째 외부 리뷰에는 빠졌던 함수 머리·시계/하네스·실제 조회 증거와 위 변경만
제공한다. 새 전체 suite는 최종 제품 tree를 대상으로 다시 실행한다.

보완 후 실제 검증: root5파일225 passed/40.39초. 독립 native는 신규11+main면제17을
실행해28 passed/7.18초, 앞선 query90건 포함118건 통과·격리0·추가 source4줄 승인.
readiness/면제 거부 유지와 legacy 불변을 직접 대조했다. 이후 native 작업자는 종료했다.

### 첫 전체 검증에서 확인한 하네스 누락

UTC 전체 첫 실행은2 failed/5654 passed/기존2 xfailed/4 warnings,466.21초였다.
실패2건은 `test_execution_p04_stale_pending.py`의 비면제 legacy 대조에서 fake
ExitManager에 main의 `is_exit_exempt` 인터페이스가 없어 생긴 AttributeError다.
실행 종료 후 fake에 `is_exit_exempt(...): return False`만 추가했다. 제품 코드와
기존 assertion은 변경하지 않았으며 실패를 xfail로 바꾸지 않았다.
관련 stale/면제/cleanup4파일 재실행은133 passed/4.04초, 격리 위반0이다.
별도 native Astra/xhigh가 비면제 표본/제품 조회 인터페이스/기존 단언 보존을
읽기 전용으로 재확인해 승인했다. 추가 시험은 중복 실행하지 않았고 실제 모델/
실효 effort metadata는 미노출이다. 제품 동일인 시험 대역 보완이므로 외부 검토
예산을 새로 열지 않았다. 이후 전 작업자 종료 상태에서 전체를 다시 측정한다.

## 최종 후보 검증

첫 실패를 고친 뒤 모든 native/외부 작업자 종료 상태에서 coordinator가 직렬 실행한다.
시험 중 제품·테스트는 고정하고 문서만 편집했다.

- UTC 전체: **5656 passed / 기존2 xfailed /4 warnings**,454.31초, exit0·격리 위반0.
- 한국시간 전체: **5656 passed / 기존2 xfailed /4 warnings**,450.80초, exit0·격리 위반0.
- 경고4건은 pykrx1건과 계좌 lease의 fork 시험3건이다. 별도 회귀 실패로 숨기지 않는다.
- 전체 추적 Python 문법 검사 통과. private-key/AWS/GitHub 제한 패턴 매치0,
  diff --check 통과. 기존 민감정보 전체 부재 보증이나 자격 교체 완료 주장이 아니다.
- main5개 계약 파일은 afa6e1e와 여전히 byte-identical이다.

시험한 Git subtree OID: src `1d239dbbe2df4f81913d95b809a608ada90c691d`,
scripts `fc4cc08b6d605616c27b18974184d8a9e932902b`,
tests `d598ff3764876e9d7a3aa1ac6185587f0063f153`.
후속 문서 통합에서 이 제품/시험 tree가 바뀌지 않는지 다시 대조한다.

## 남기는 경계

외부2차(예산 마지막)는 실제 metadata claude-opus-5/requested xhigh,
211.967초/$0.71328/exit0·**APPROVE(한계 기록 조건부)**. 입력64,605bytes,
SHA256 `e2bfc1fee2a9c3cb65d3ef8dcb201021425c536afd59a06edbd4fcfd56ade4f0`.
P1 처분과4줄 제품 수정·시험·절차 범위 승인이지 설치/전체 기존 엔진 재승인은 아니다.
두 번 합계 확인 비용$2.045775. tools0 공급 텍스트만 검토했고 실효 effort는 별도 미노출이다.

비차단 잔여: attached 면제 WARNING의 별도 스로틀, curated RM 속성 목록의 향후 drift,
health의 shallow-copy 하위 값/향후 exporter의 중첩 probe 확장, protection 태그 SELL의
설치 전 발행 경계 재확인. 신규 불변 단언은 사유/주문/예약/타임스탬프/면제 경고 장부에
한정하며 기존 일반/주문실패 cooldown 만료 정리까지 금지했다는 주장은 하지 않는다.
신규11은 heartbeat6+면제SIGNAL/ORDER4+health1이다. 발췌 말미 기존 market_data 시험은
11에 포함하지 않는다. 현행 producer의 면제 발행 시험은 별도로 존재하나 설치 증거로
승격하지 않는다. 이 조건들을 남기는 범위에서 native/외부 양쪽 승인을 확인했다.

- main/운영 승격 없음. KIS 거래·잔고 정본/Toss 조회 전용 구분 불변.
- 운영 관측 수정의 실제 호출주체별 표본은 배포 후 별도 관측이 필요하다. 이번 합성
  계측 시험으로 운영 초당 한도 문제의 원인을 확정하거나 감소량을 주장하지 않는다.
- 수동 복구는 증거 분류 절차다. 원장 삭제, 예약 해제, 수량 보정, 재시작으로 잠금 우회,
  진단을 위한 mutating audit/resume/repair와 raw state 공유를 금지한다.
- [다음 개발 순서](../operations/p1-next-steps-2026-09-23.md)의 exporter·관측 배선,
  장기 처리 비용, 누락 writer/consumer, 공식 최초 인계/취소 최종성 근거가 남는다.
