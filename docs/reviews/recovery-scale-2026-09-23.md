# N4 누적 상태 성능·관측 연결 판단 — Plan → Do → See

## 범위와 현재 상태

사용자가 확정한 범위는 **주문·전략·위험 설정 유지, 검증된 수정본만 배포**다.
운영 main은 PR #90/#91/#92 반영 `a454277`, 실행 제품은 `e5ae602`와 동일하다.
09-23 23:18:02 KST PID2386785로 재시작했고192초 관찰에서 pending/stale/failing/EGW0을
확인했다. main과 별개인 전체 engine/N3 개발선 `5a2fab8`은 미배포다.
운영 결과 정본은 main의 `docs/operations/release-2026-09-23.md`이며, 이 개발선의 과거
미배포 문구는 당시 기록이다. Toss 만료 grant 연장·주문·전략·위험 변경은 없었다.
추가 읽기 전용 점검(09-24 00:02:25 KST): 같은 PID2386785 active/running, broker connected,
pending broker/risk/sell0, stale0, metrics available=true. 잔고 TR의 startup1+portfolio_sync10,
재시도/EGW00215는0이었다. Toss inactive/dead/PID0, 운영 main clean을 확인했다.
장중 호출 제한 해소나 자연 발생 매도 이유/freshness의 실관측 완료로 확대하지 않는다.

N4는 이 개발선에서 실제 runtime 타입과 합성 자료만 사용하는 **tests/docs 전용** 작업이다.
**직렬 측정에서 연결 게이트 실패: health/경보 배선 보류**다. 운영 owner 설치를 수행한 것이
아니며 설치·C/F/G/R·공식 최종성 차단은 유지한다. 최종 전체 검증 상태는 아래에서 구분한다.

## Plan — 변경 전에 고정한 기준

- [설계](../architecture/recovery-observability-scale-2026-09-23.md),
  [실행 계획](../superpowers/plans/2026-09-23-recovery-scale.md), base `5a2fab8`.
- 규모0/100/1000/5000; 실제 capture+builder+JSON, owner.state, producer cold/warm sweep.
- 기록당 intent/attempt/outbox 링크, 주 cohort는 종결 보관 이력+소수 live 기록.
  terminal 모양은 합성 lifecycle로 생성하며 공식 KIS finality 증거가 아니다.
- 일반 처리 시간과 별도 tracing peak를 분리한다. fixture/검증 비용은 타이머 밖이다.
  첫 event-loop 양보까지의 callback 지연은 전체 실행의 최대 loop stall이 아니다.
- 한 프로세스30초, 사례를 **직렬** 실행한다. 중단은 마지막 phase와 함께 censored이며
  setup/tracing/cleanup timeout을 제품 연산30초 초과로 단정하지 않는다.
- 연결 연구 게이트: 모든 규모 capture 지원, 각 일반 연산 최대 관측값≤50ms.
  수치가 작아도 snapshot unavailable이면 통과가 아니다. SLO/실시간 보장은 아니다.
- 실패하면 HTTP/경보 배선 보류. 상한 증대·원장 삭제·설정 완화로 게이트를 통과시키지 않는다.

## Do — 역할과 첫 리뷰 처분

격리 WT `feature/recovery-scale-20260923`에서 coordinator가 spec/plan을 작성(`16aa36e`),
Terra/high가 신규 테스트 두 파일만 구현했다(`a526cb0`). Sol/high는 작성자와 분리된
독립 리뷰다. native actual model/effective effort metadata는 미노출이므로 미검증이다.
N4 하네스는 외부 공급자 호출0, 제품 수정0, 실 API/자격/실 상태 자료 사용0이다.

첫 baseline은 실제 diagnostics3파일122 passed/7.79초/rc0/격리0이었다.
초기 TDD의 missing helper RED를 거쳐7 passed/16 opt-in skipped가 됐지만,
독립 리뷰는 **CHANGES_REQUIRED**였다. GREEN만으로 측정 타당성을 인정하지 않았다.

| 지적 | 확인 사실 | 처분 |
| --- | --- | --- |
| P1 측정 오염 | 검증용 deepcopy/동등성 비교가 타이머 안, capture JSON은 밖 | 검증을 밖으로 옮기고 제품 연산만 측정 |
| P1 이력 대표성 | 전부 서로 다른 종목의 미종결 SELL | 실제 종결 모양·반복 종목·작은 live cohort로 분리 |
| P2 timeout 귀속 | setup/warm/tracing을 포함한 전체 프로세스 한도 | flushed phase 증거를 남기고 미완/미측정 보존 |
| P2 명칭·인수 | 첫 양보 대기를 최대 stall로 표현, writer/mutation 음성 대조 부족 | 정확한 이름과 강제 위반 회귀 추가 |

초기 병렬100건 실행의 숫자는 측정 경계·실행 조건이 모두 부적합하므로 **폐기**했다.
아래에 기록할 수정 후 직렬 결과와 합치지 않는다.50ms/규모/30초 기준은 바꾸지 않았다.

첫 수정 `9a8821a`는10 passed/16 skipped였으며 측정 경계·JSON·종결 cohort·첫 양보
명칭은 재리뷰에서 확인됐다. 다만 reset 앞 phase 부재와 실제 설치 seam이 아닌 상수만
비교하는 guard 시험 때문에 **CHANGES_REQUIRED**가 유지됐다. async tracing 결과 전달도
함께 정정했다. `53c22e5`는 관련4 RED→13 passed/16 skipped였고, coordinator가 fixture
setup 표식의 남은 위치를1 RED로 확인한 뒤 `435b53f`로 고쳤다. 최신 focused는
**14 passed/16 skipped/2.20초/rc0/격리0**다. 작은 시험 하네스라도 측정/차단 주장의
증거가 부족하면 승인을 확대하지 않는다.

## See — 독립 재검토와 직렬 측정

Sol/high는 최종 두 테스트 파일 `435b53f`를 **APPROVE**했다. 측정 경계·종결 cohort·
phase 선행·실제 seam 집합·async 반환값 수정에 대한 정적 승인이다. 리뷰어는 최종 시험을
재실행하지 않았고 coordinator의14 passed를 구분했다. 제품/운영/dirty 문서 승인으로 확대하지 않는다.

coordinator 단독으로 **2026-09-23 23:54:57~23:57:44 KST**에16개 사례를 직렬 실행했다.
제품 base `5a2fab8`, 테스트 후보 `435b53fd10918fb721df42cb457adc06bf244195`.
**12개 rc0, 4개 timeout rc124, 그 외 실패0**. 아래 ms는 완료 사례의 비 tracing3회 최대값이다.
timeout은 빈 값이며, 최대 실행 시간의 엄밀한 하한으로 해석하지 않는다.

| 합성 사건 수 | capture+builder+JSON(ms) | owner.state(ms) | cold sweep(ms) | warm sweep(ms) | 캡처 지원 |
| --- | --- | --- | --- | --- | --- |
| 0 | 1.87 | 0.14 | 0.62 | 0.58 | 지원 |
| 100 | 59.37 | 3.28 | 710.98 | 680.18 | 지원 |
| 1000 | 322.36 | 33.15 | 미완·normal phase | 미측정·warm 준비 phase | 지원 |
| 5000 | 181.53 **거부 반환** | 221.98 | 미완·normal phase | 미측정·warm 준비 phase | **거부/unknown** |

- 주 cohort의 기록 수는 사건 수다. 각각 intent/attempt/outbox 행 수가 이 숫자와 같으므로
  5000건은 해당 top-level 행15000개다. live=min(size,2), terminal=size-live, 반복 종목≤5.
- 5000건의 빠른 거부를1000건보다 성능이 좋다고 해석하지 않는다. `snapshot_stable=null`,
  `counts_complete=false`, `snapshot_unavailable/evidence_invalid`였다. 구체적 상한을
  늘리거나 증거 행을 삭제하는 실험은 하지 않았다.
- 100건에서 이미50ms 게이트를 넘었다. 크기별 capture tracing peak는 각각30,256 /
  1,625,552 /16,835,392 /8,973,086 bytes이며 일반 지연에는 tracing을 넣지 않았다.
  JSON 크기는524/768/768/761 bytes다. 작은 JSON 결과만으로 작은 캡처 비용을 추정할 수 없다.
- timeout4개는 전체 프로세스30초 예산 종료다. cold는 마지막 표식 normal, warm은 warm
  precompute였다. cleanup/최종 schema/전체3회/peak가 완료되지 않았으므로 null로 보존한다.
  no marker라면 phase=unknown으로 기록한다. warm 준비를 warm sweep 지연으로 둔갑시키지 않는다.
- 완료12개는 격리 위반0 요약을 확인했다. timeout4개는 종료 요약이 없으므로0으로 세지
  않는다. pytest 격리 가드는 전 사례에, 추가 writer guard는 실제 `_sweep` 측정 구간에
  적용했다. warm 사전 계산은 그 guard 이전의 별도 준비 단계이며 실 API 사용0이다.
- first_yield_delay는 단회 callback이 첫 양보까지 기다린 시간이며 연속 stall 측정이 아니다.
  이 호스트의 동시 실행 부하는 통제된 성능 실험실 수준이 아니며 운영 SLO/최악 상한 보장이 아니다.
- pending_sell/remaining_reservation은 bounded live SELL2건의 자료다. 해당 finding을 자동
  critical/주문 재시도/예약 해제 신호로 연결하지 않는다. `disposition_not_durable`도 유지했다.

비식별 [직렬 측정 원본 JSON](data/recovery-scale-2026-09-23.json)에 모든16행의 exit code,
phase, 지원성/완전성, 별도 peak/JSON 크기/첫 양보 지연을 보존했다. raw ID·계좌·가격·실자료는 없다.
예를 들어 개발 worktree에서100건 capture 한 사례를 실행한 명령은 다음과 같다.
kind는 capture/owner/sweep-cold/sweep-warm, size는0/100/1000/5000을 사용했다.
긴 opt-in16개는 일반 suite에서 skip하고 계약14개는 항상 실행한다.

```bash
timeout --signal=TERM --kill-after=5s 30s env -i \
  PATH=/usr/bin:/bin LANG=C.UTF-8 TZ=UTC PYTHONUNBUFFERED=1 \
  PYTHONDONTWRITEBYTECODE=1 PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 \
  QWQ_RUN_RECOVERY_SCALE=1 QWQ_DEPLOY_SSH_KEY=/tmp/qwq-offline-no-ssh-key \
  /home/ubuntu/projects/qwq-ai-trader/venv/bin/python -m pytest \
  'tests/test_recovery_diagnostics_performance.py::test_opt_in_measure_case[capture-size-100]' \
  -q -s -p no:cacheprovider --tb=short --show-capture=no
```

### 전체 검증·개발 통합

모든 native/외부 작업자 종료 후 coordinator가 UTC→KST 전체 suite를 직렬 실행한다.
UTC는 **5792 passed/16 opt-in skipped/기존2 xfailed/기존4 warnings/462.91초/rc0/격리0**다.
KST도 **5792 passed/16 opt-in skipped/기존2 xfailed/기존4 warnings/460.38초/rc0/격리0**다.
warnings는 pykrx1·기존 fork 관련3이며 새 하네스 경고가 아니다. 같은 시험을 다른 TZ에서
재실행한 것이므로11584개 독립 시험으로 합산하지 않는다. Python 문법·제한된 비밀 패턴
검사·diff-check도 rc0이다. 저장소 전체/과거 이력에 비밀이 없다는 보장은 아니다.
제품/scripts/config의 base 대비 diff는0이다.
고정 tree: src `7f994a918020c1f35517258a05b02e4414f4091c`,
tests `083839f002282571473d778138d25e87cfaf510f`, scripts `fc4cc08b6d605616c27b18974184d8a9e932902b`.
통합 직전 정본의 clean·local/remote `5a2fab8`을 확인한 뒤, 테스트/문서 후보 `dce43f7`을
`feature/engine-safety-design-20260917` 한 곳에 fast-forward했다. 통합본 diagnostics4파일
**136 passed/16 skipped/7.89초/rc0/격리0**, source/test/script tree 불변을 재확인한 뒤
push 성공 및 원격 `dce43f7` 일치를 확인했다. 이 마감 기록은 문서만 갱신하며 제품 변경은 없다.
새 main PR/전체 engine 배포·재시작·주문·설정 변경은 하지 않았다. 작업/리뷰 증거가 있는
격리 worktree는 삭제하지 않고 보존한다. 다음 재개 위치는 아래 projection/index 계약이며
완료한 하네스나 N3 exporter를 중복 구현하지 않는다.

## 유지하는 한계와 후속

### 게이트 실패 시 다음 Plan — revision별 projection·producer index

독립 Sol/high의 제품 읽기 전용 조사(시험 실행0)와 coordinator 소스 대조에서 다음을
확인했다. `application.py::state`는 전체 deepcopy이고, `recovery_capture::_read_sample`
두 번이 각각 owner 전량을 복사한다. `protection_producer::_restart_cooldowns`는 cold에
intent별 감사 전체를 탐색하고, `_episode_status`→`_attempts`도 전체 state를 재복사한다.
commit/store도 전체 상태 복사/인코딩이 있으므로 비용을 관측에서 주문 경로로 옮기는
것만으로 개선이라고 보고하지 않는다.

다음 제품 변경은 별도 critical Plan→Do→See로 진행하며 아래 인수를 선행한다.

1. 불변 진단 projection에 정확한 owner revision을 붙인다. owner/published/engine version
   불일치·commit/게시 실패는 unavailable이며 이전 정상 표본을 현재 정상으로 내보내지 않는다.
2. owner 직렬 게시 수명주기에서 갱신하거나 checkpoint와 원자적으로 저장한다. mutable
   private RAM을 worker thread로 복사하지 않고 소비자에게 변경 가능한 alias를 주지 않는다.
3. restore는 durable 전체 사실로 재구축·검증한 뒤에만 current가 된다. day/fence/account
   검사와 replay를 보존하며 옛 저장본의 projection 부재는 재구축/unknown이지0이 아니다.
4. attempts-by-symbol/intent, protection audit/admission/pending-by-intent index를 검토한다.
   producer episode/recovery는 기존 lock 소유를 유지한다. 반복 종목·순서 모호함은 해소로 추정하지 않는다.
5. intent/attempt/audit/원 replay 사실을 삭제·요약 대체하지 않는다. index는 종결성·정정·
   복구 권한을 새로 만들지 않고, 기존 UNKNOWN/잔존 예약/관측미적용 체결 차단을 보존한다.
6. 설치 판정과 관측 건수는 분리한다. runtime/engine/producer/gateway/handler/reconciler
   binding 변경은 표본을 무효화한다. partial_install/미설치 표본을 완전 설치로 재사용하지 않는다.
7. commit 추가 비용·restore 재구축·cold/warm index·cached read·JSON·cache age/revision/
   bytes를 따로 계측하고 주문 경로의 새 예산을 **변경 전에** 고정한다. N4의 첫 양보
   지연만으로 최대 loop stall을 입증하지 말고 필요한 연속 probe를 별도 설계한다.
8. 이번 rejected-history cohort뿐 아니라 실제 합성 체결·부분 체결·미확정 취소·거래일
   전환·commit/게시 실패 이력도 사전 등록한다. 새 인덱스 결과와 기존 전량 계산의 일치,
   같은 종목의 복수 사건·unknown/잔존 예약·처리 중 입력을 인수한다. 데이터 분포를 바꿔
   기존 실패 수치를 숨기거나 성능 개선을 broker 최종성 증거로 바꾸지 않는다.

이후에만 한 sampler의 동일 revision/신선도 표본을 health와 경보가 공유하도록 배선한다.
오래됨·결측·장외/휴장/closing/무보유/종료는 서로 다른 상태이며 정상 SELL/미측정 위험을
critical로 승격하거나 경보가 복구 writer를 호출하지 않는다. 전체 C/F/G/R·독립 broad
리뷰·공식 증거/미지원 판정 전 main 전체 이행은 금지한다.

### 범위 한계

- 합성 작은 live cohort는 모든 주문 상태·이력 분포·거래일·실계좌 성능을 대표하지 않는다.
- 행 복제는 test-owned RAM만 증식한다. 이 합성 대량 상태의 durable commit/reopen·실제
  경제 보존·최초 잔고 인계를 인수한 것이 아니며, latency 결과를 전체 runtime 인수로 쓰지 않는다.
- N3의 partial_install, counts_complete=False, None 계획 위험은 자동 장애/복구 허가가 아니다.
- health는 사용자 요청마다 전체 상태를 재캡처하거나 unsafe RAM을 thread로 복사하지 않는다.
  exporter를 소비하는 단일 캐시 후보도 성능·freshness·종료 schema 계약을 통과해야 한다.
- 별도 보안 후속: 운영 배포 스크립트의 평문 인증 폴백은 제거됐으나, 노출된 기존 인증정보
  교체와 Git 이력/문서 정리는 완료되지 않았다. 값 재출력·임의 권한 변경은 금지한다.
