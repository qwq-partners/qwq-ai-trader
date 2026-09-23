# N4 관측 연결 전 누적 상태 비용 검증

## 의도·범위

사용자는 검증된 운영 수정 배포 후 health·경보 연결과 장기 누적 상태 성능 검증을 요청했다.
주문·전략·위험 설정을 유지한다. main `e5ae602` 운영 반영 후 N3 개발선 `5a2fab8`에서
먼저 **측정만** 수행한다. 사용자의 독자 판단 위임에 따라 coordinator가 이 분리 순서를
선택한다. 현재 제품·runtime 설치·청결 판단·주문·설정·보존 정책은 바꾸지 않는다.

N3 exporter는 owner RAM 전체를 두 번 복사하고 node100,000/text2MB 상한을 적용한다.
현재 크기를 모른 채 HTTP 요청마다 직접 호출하거나 unsafe private RAM을 to_thread로
옮기지 않는다. 모든 실자료는 제외하고 실제 runtime 타입+합성 상태만 사용한다.

## 대안과 선택

1. HTTP마다 즉시 캡처: 항상 최신이지만 전체 복사가 요청 수만큼 중복된다. 채택하지 않음.
2. 60초 단일 sampler+공통 캐시: HTTP와 경보를 같은 표본에 고정할 수 있어 다음 설계 후보다.
   단 한 번의 동기 캡처도 보호 루프를 지연시킬 수 있어 먼저 아래 게이트를 검사한다.
3. revision별 불변 projection/증분 요약: 캡처 상한이나 지연 게이트가 실패하면 필요한
   후속 재설계다. owner commit/replay/미확정 증거 계약을 바꾸므로 이번 측정에 섞지 않는다.

## 사전 고정한 측정 계약

- 기준 `0/100/1000/5000`개의 합성 누적 기록. 한 사건에 intent/attempt/outbox가 연결되는
  자료를 만들고 실제 fixture에서 얻은 row 모양을 사용한다. setup 비용은 측정과 분리한다.
  여러 top-level 행을 한 기록으로 묶었다면 행 수를 따로 표시한다.
- 읽기 캡처+builder+JSON, owner.state 복사, producer의 실제 sweep을 별도 측정한다.
  sweep은 test-owned producer RAM만 변경 가능하고 owner/store/gateway/네트워크 writer는
  실패 spy로 막는다. 비어 있는 보호 pending/admission으로 자동 재시도를 만들지 않는다.
- wall time/최대 관측값, asyncio event-loop 대기 지연, 별도 tracemalloc peak와 JSON bytes,
  snapshot_stable/counts_complete/고정 finding code를 각각 표시한다. tracing 비용을 일반
  latency와 합산하지 않는다. warm/cold producer sweep을 구분한다.
- 성능 실행은 명시 opt-in 시험으로 두고 평상시 CI에 긴 벤치를 섞지 않는다. 각 규모/종류는
  별도 pytest 프로세스·외부 timeout30초·합성 tmp 저장소만 사용한다. timeout은 PASS가
  아니라 censored/failure로 기록한다. 미완 사례를0ms/0byte로 만들지 않는다.
- 단위/하네스 시험은 일반 suite에서 실행해 자료 링크·출력 schema·비식별·writer0·
  반복 측정의 원 상태 불변을 인수한다. 수치 타이밍은 CI의 flaky assertion으로 쓰지 않는다.

## 연결 게이트

연결을 시작하기 위한 연구 기준은 모든 제안 규모에서 진단 표본이 유실 없이 지원되고,
일반(비 tracing) 캡처 및 owner.state/sweep의 최대 관측 동기 지연이 각각50ms 이하인 것이다.
이는 사용자 위험 한도 변경이 아닌 사전 고정한 **관측 개발 게이트**이며 실시간 상한의
수학적 보장/운영 성능 보장은 아니다. 작은 표본의 p95를 모집단 SLO로 주장하지 않는다.
본 측정에서 기준을 못 채우면 HTTP/경보 배선을 보류하고 원인과 재설계 조건을 남긴다.
추후 기준을 완화하려면 결과를 덮어쓰지 말고 새 사전 등록을 별도로 기록한다.

`snapshot_unavailable/volatile`, counts_complete=False, mutation_in_flight, unknown risk는
정상0이나 자동 장애/복구 허가가 아니다. 초기 fixture의 partial_install은 설치 미완이므로
별도 보고하며, 대형 상태의 캡처 거부와 혼동하지 않는다. 상한을 올리거나 audit/attempt를
삭제·압축해서 통과시키지 않는다. KIS 최종성/전체 C/F/G/R·설치 차단을 유지한다.

## 후속 연결 계약(아직 구현 아님)

게이트 통과 후 `src/monitoring/recovery_health.py`의 단일 cached sampler를 설계한다.
`HealthMonitor` 60초 tick과 `DataCollector.get_system_health`는 같은 표본만 소비한다.
일반 PERIODS 등록으로 legacy 유령 경보를 만들지 않는다. runtime 부재는 not_observed/
mode unknown이며 legacy 인증이 아니다. 정상 SELL/in-flight/미측정 위험/상시 durable
미지원은 critical 또는 재발행 신호가 아니다. closed/holiday/closing/no-holdings/stopped와
관측 freshness를 구분하고 raw 식별자·예외는 내보내지 않는다. 종료 상태를 private RAM에서
재추정하지 않고 exporter의 가법 schema 계약으로 먼저 해결한다. 경보는 복구 writer0이다.

## 역할·검증

테스트/벤치만 Terra/high, 독립 ordinary review Sol/high, integration coordinator.
actual model/effort metadata 미노출이면 미검증으로 남긴다. 외부 모델 호출은 없고 작성자
자가 승격0. 제품 변경이 생기면 이 범위를 종료하고 critical 설계/리뷰로 별도 전환한다.
격리 worktree·파일 비중복, 작업자 총3명 이하, full suite는 모든 작업자 종료 후 coordinator만
UTC→KST 직렬 실행한다. 본 N4는 main의 전체 엔진 배포 허가가 아니다.
