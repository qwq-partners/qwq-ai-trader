# P1 후속 N1 — 운영 반영 판정과 보호 신호 대기 분리

## 현재 범위와 Plan

사용자 요청은 "운영에 반영하고, 다음 작업 이어가자"다. 앞선 자율 판단 위임에 따라
기존 위험 한도·주문/설정·Toss 관측 전용 경계를 유지하고, 운영에 안전하게 적용할 수
있는 변경과 미완 migration을 분리한다. 이번 제품 범위는 보호 신호 대기 시계의 bounded
수정이다. 새로운 복구 권한/시작 허가/실주문을 만드는 작업이 아니다.

- 개발 기준: `69b43e271089f1aa319667260efe165bdb9b3732`.
- 운영 기준: `d337494` → 이미 병합된 main `afa6e1e`의 문서만 동기화.
- 두 개발선의 공통 조상: `93c2fbd959d250e524d3d99447110aee594d880a`.
- 조회 시 main 전용48 / 개발선 전용261커밋. 공통 조상 이후 개발선233파일
  +73,735/-452줄이며 미설치 코드만 있는 브랜치가 아니다.
- 원칙: 전체 C/F/G/R와 설치 차단이 남은 개발선을 통째 운영에 넣지 않는다. main의
  독립 legacy 개선(PR #81/#83/#84 등)을 덮어쓰지 않는다.

### 선택한 동작

일반 신호가31초마다 갱신하는 `_last_signal_time`을 보호 경로까지 읽는 것이 기아의
원인이다. 보호 전용 종목 시계를 분리하고, 보호 후보의 최종 lock에서는 전용/공통
시계를 함께 갱신한다. 일반 신호는 보호 시계를 갱신하지 못한다.

보호끼리30초, 확정 무체결 거부 후 생산자60초 재발행은 유지한다. 원 intent/가격/수량,
owner 예약, UNKNOWN·미해결 주문·관측미적용 체결·면제·세션/휴장 검사를 바꾸지 않는다.
완전 cooldown 면제는 재시도 정책을 바꾸고, 새 우선권 latch는 복구 상태를 늘리므로
이번 범위에서 제외한다. 수동 복구 장벽을 TTL 삭제나 자동 수량 보정으로 열지 않는다.

보장 목표는 **일반 신호가 보호 자신의 대기 만료 시점을 연장하지 못한다**는 것이다.
피드/큐가 진행되고 다른 장벽이 없으며 시계가 정상일 때, 보호 자신의30초가 지난 뒤
첫 처리 기회에 진행한다. 실시간 절대 송신 상한·직전 익절 뒤 즉시 손절·운영 전환을
증명하는 것이 아니며 차단28 전체/29~31 해소로 보고하지 않는다.

## 운영에서 실제로 한 것

2026-09-23 17시대 KST, fetch 뒤 운영 checkout의 청결과 변경 경로를 확인했다.
`d337494` → `afa6e1e`는 `CHANGELOG.md`·`CLAUDE.md` 2파일(+13/-1)뿐이고
src/scripts/tests/config/requirements/pyproject 차이는0이다. 최신 main의 문서를
detached checkout에 반영했다. **제품 배포/재시작은 하지 않았다.**

- 서비스: active/running, PID2259506, 시작09-23 06:28:05 KST. 동기화 전후 동일.
- 이 시작 시각의 원인이나 실제 프로세스 로드 SHA를 재구성했다고 주장하지 않는다.
- localhost 읽기 전용 점검: broker.connected=true, 미체결 API `[]`, stale_loops0.
- 최근10분 최대2,500줄의 표본337줄: ERROR 0, Traceback 0, EGW00215 0,
  EGW00201 24건. 호출 한도 경고의 원인은 이 집계만으로 확정되지 않았다. 별도 뒤따른
  10분 TR 집계24건은 FHKST03010100 12/FHPST02300000 10/FHKST01010100 2였다.
  연속 두 시점의 겹치는 표본이므로 총48건으로 합산하지 않는다.
- Toss 별도 서비스 inactive/dead. 09-22 18:00 KST 만료 권한을 연장/재사용하지 않았다.
- 마지막 점검의 retention oneshot은 failed/rc1(09-22 19:00:14~15 KST)였다. 관측
  서비스의 장애와 구분하며 아래 후속 분류의 한계를 적용한다.
- 배포 스크립트의 기존 평문 인증 폴백은 사용하지 않았다. 인증정보 교체·권한 변경도
  실행하지 않았다. 조사 때 기존 값이 도구 출력에 포함돼 사용자에게 알렸다. 해당
  인증정보 교체와 평문 폴백 제거가 필요하며 값을 보고서/외부 리뷰에 옮기지 않는다.

실행 코드 차이가 없는 main 동기화 때문에 거래 봇을 재시작하지 않았다. 미완 runtime은
실제 설치 호출자0·`trading_ready=False`를 유지한다. 운영 반영 요청이 안전 게이트를
강제 개방할 근거는 아니다.

### main을 먼저 보존해야 하는 구체 근거

독립 Sol 조사와 Git/코드 대조에서 다음 운영 개선이 개발선의 조상이 아님을 확인했다.

| main 변경 | 보존할 행동 | 개발선 통째 교체 위험 |
| --- | --- | --- |
| PR #81 | 미체결 BUY 해제와 미체결 조회 페이지 처리 | 이전 해제/조회 경로 복원 |
| PR #83 | exit-exempt 최종 방어·면제 복원/열린 매도 확인 | 면제 종목 보호 회귀 |
| PR #84 | stale SELL 상태·재시도·살아 있는 주문 보존 | 취소0을 소멸로 읽는 구 경로 복원 |
| PR #80/#88 | TR 전환 스위치·연속조회 `tr_cont` 요청 헤더 | 운영 KIS 프로토콜 강화 소실 |

제품 충돌 대상은 `batch_analyzer.py`, `engine.py`, `kis_kr.py`, `kr_scheduler.py`,
`kis_rate_limit.py` 다섯 곳이다. 개발선의 legacy 특성화26건은 과거 결함도 의도적으로
고정하므로, 단순히 그 시험을 통과했다고 main 동등성을 주장할 수 없다. 향후 정합화는
main 행동 시험을 먼저 기준선으로 가져와 기존 기대 변경을 각각 처분하는 별도 작업이다.
본 N1에서 이 충돌을 기계적으로 합치거나 오래된 코드로 덮지 않았다.

## 역할·자원과 Do

Plan은 Sol/high 배포 차이 조사와 Astra/high 보호 계약 조사를 병렬로 분리했다. Do는
Astra/high가 같은 base의 별도 `feature/protection-priority-20260923` WT에서 engine과
시험만 작성한다. coordinator는 이 문서/운영 판정과 통합을 소유한다. 전역 정책
`ai-routing-v1-2026-09-20`, root+작업자2 이하, fanout0를 지킨다. native actual model과
effective effort는 metadata 미노출로 미검증이다.

See는 작성자가 아닌 Astra/xhigh와 tools0 실제 모델 검증 Opus/xhigh를 사용한다.
**N1 새 범위의 외부 예산은 최대2회·각$5·600초**로 고정하며 이전 P1의8/8회 예산을
초기화하지 않는다. 재시도도2회에 포함한다. 인증/모델/필수 검증 실패는 우회하지 않는다.
전체 suite는 모든 작업자 종료 후 coordinator만 UTC→KST 직렬 실행한다.

### 검증 진행

- 기준선 `69b43e2`: 기존 `tests/test_execution_p1_engine.py` **43 passed /13.57초 /
  rc0 /격리 위반0**(UTC, 운영 자격을 제외한 env -i).
- 구현자 RED: 새 행동7 failed / 대조3 passed /7.16초 /rc1 /격리0. 일반 신호의
  공통 시계 때문에 POST0이고 보호 전용 경쟁은 기존 코드가 읽지 않아 POST1이었다.
  초기화 우회 하네스2곳은 실제 생성자와 맞추기 위한 빈 속성 각1줄만 소유 확대했다.
- 후보 `53c8bd605ece1418b49911e671c8aefb6881f90d`, tree
  `aa70ba719be7822f5d2780cdcf0da8060f1d6eca`: 관련8파일319 passed/69.99초,
  legacy 특성화26 passed/2.30초, 각각rc0·격리0. 초점 시험 수를 전체 수로 합산하지 않는다.
- coordinator가 AST를 재대조해 engine 새 초기화1줄·보호 helper 밖의 본문 동일성을
  확인했다. 해당 후보의 제품 diff는15삽입/3삭제, 전체5파일185삽입/21삭제다.
- 독립 native는 신규10개를 포함한74 passed/25.85초 및 복구/legacy146 passed/14.64초,
  각rc0·격리0. 두 시계 읽기/쓰기 메모리 변이4종을 각각 행동 단언으로 검출했다.
  혼합 일반 SELL 호가 대기→보호90주→일반 재개에도 POST1·예약/intent 불변이다.
- native Spec/Quality `APPROVE_THIS_SLICE`, 신규P0/P1/P2없음. raw 상태나 다중 루프/
  스레드 안전성을 검증한 것은 아니다. 해당 리뷰에서도 기존 민감행의 도구 출력 노출이
  한 차례 있어 보고서에 기록했다. 실제 자격 사용·값 재기록·외부 전달은 없었다.
- 외부1/2: **실제 claude-opus-5**, 요청xhigh/유효effort미노출, **317.044초·$1.214·rc0**,
  `APPROVE_THIS_SLICE`. 입력149,305bytes·SHA256
  `5b17acf5a3ac63d0c4a2ff2342b0804b0571cdd6d88e30a76dd1e97a7440b403`.
  도구0 정적 source 리뷰이며 시험 실행/운영 승인이 아니다. 출력24,586tokens,
  입력2/cache creation59,934/cache read0. 새 제품 결함0, 통합 전 확인1·비차단 제안6.
- 시험-only 보완 후보 `06fc7ed1dc29dafc7ea27b7771673bf205237ff6`, tree
  `f9b5ae5a9e9401fcdf7628797302c45ffe4a1130`: 제품은 `53c8bd6`과 동일하고
  시험2파일만31삽입/1삭제다. 구현자65 passed/23.92초, 독립 native 재검토65
  passed/23.20초, 각각rc0·격리0. native Spec/Quality 범위 승인과 신규P0/P1/P2없음.
  외부 승인 제품을 바꾸지 않았으므로 두 번째 외부 호출을 추가하지 않았다(사용1/2).
- coordinator 단독 전체: **UTC/KST 각각5386 passed /기존2 xfailed /경고4 /rc0 /
  격리0**, UTC442.74초·KST443.20초. 모두 최종 후보 `06fc7ed`에서 직렬 실행했다.
  경고는 pykrx resources.path 폐기 예정1건·계좌 lease의 fork 시험3건이다.
- 별도 `QWQ_VERIFY_SKIP_TESTS=1`로 전체 tracked Python 문법·프로젝트의 제한된
  비밀정보 패턴 검사를 실행해 rc0을 확인했다. 이미 실행한 전체 시험을 생략한 정적
  단계이며 세 번째 전체 suite 통과로 세지 않는다. `git diff --check` rc0.

전체 검증은 운영 자격을 이용하지 않고 아래 명령으로 직렬 실행했다. `TZ`만
UTC→Asia/Seoul로 바꾸며 시험 디렉터리를 명시한다. 결과 수를 합산해 서로 다른
10,772개 시험이나 실 API 인수로 보고하지 않는다.

```sh
env -i PATH=/usr/bin:/bin LANG=C.UTF-8 TZ=UTC \
  PYTHONDONTWRITEBYTECODE=1 PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 \
  QWQ_DEPLOY_SSH_KEY=/tmp/qwq-offline-no-ssh-key \
  /home/ubuntu/projects/qwq-ai-trader/venv/bin/python -m pytest tests \
  -q -p no:cacheprovider --tb=short
```

리뷰 입출력·독립 시험 원장·전체 실행 출력은 정본 개발 WT의 gitignored
`.superpowers/sdd/2026-09-23-protection-priority/`에 보존한다. 이 로컬 증거 폴더와
운영 원장을 혼동하지 않으며 실제 계좌·비밀정보·운영 상태 파일을 커밋하지 않는다.

### Opus 의견 처분

| 의견 | 코드 대조와 처분 |
| --- | --- |
| P1-1 생성자 우회 하네스 | `_last_signal_time` 초기화/`object.__new__` 전수 검색, 관련2개 초기화 보완 확인. 제품 lazy fallback 없이 최종 전체 suite로 누락 검사 |
| P2-1 전용 시계500개 cutoff | 미반영. 기존 cutoff도 미래시각 항목을 제거하지 않고 최근 항목 수를 실제로500 이하로 제한하지 않는다. 이미 경과30초 만료 정리, 보호 중단 뒤 작은RAM 잔류와 시계역행 한계를 명시; 미래시각 항목 삭제로 중복 장벽을 바꾸지 않음 |
| P2-2 로그 구분 | 현재 "보호 매도…신호 쿨다운"은 전용 시계라는 점을 명시. pending/전용 cooldown 사유 세분은 다음 관측 소비자 단계로 이월, 제품 동작은 유지 |
| P2-3 다른 시험 모듈 시계 | 미반영. 실제 `sell_signal`은 자체 `NOW_KST`/`_CLOCK`를 읽지 않고 Signal/SignalEvent만 구성한다. 같은 body의 지역 복제는 새 보장 없음 |
| P2-4 session_guard 복원 | 수용. 일반 거부 뒤 원 guard로 복귀하는 tests-only 보완. 원 guard 역시 synthetic True이므로 실제 거래소 세션 허가 증명으로 확대하지 않음 |
| P2-5 보호→일반 차단 사유 | native의 공통시계 쓰기 제거 변이가 실제 행동 실패로 잡힘. 제품은 같은 candidate_at을 양쪽에 기록함을 코드로 대조. 추가 중복 단언은 넣지 않고 None 반환 단독으로 근거화하지 않음 |
| P2-6 closing 호가 전 조합 | 수용. 공통시계를 미리 무장한 closing LIMIT의 호가1회/POST1 인수 추가 |

cooldown 자체는 engine 시험/변이가, 관측미적용·ACK/UNKNOWN 보존은 producer/owner
인수가 각각 검증한다. 후자의 GREEN만으로 cooldown 분리를 입증했다고 하지 않는다.
와이어에 나가지 않은 보호 후보도 기존30초를 무장하는 정책과 벽시계 역행 한계는 남는다.

### 운영 경고의 별도 처분

Sol/high의 읽기 전용 코드 조사와 coordinator 대조에서 위3개 TR은 모두 공용 KR
limiter를 거치는 비원장 조회임을 확인했다. `_api_get`은 한 논리 요청을 최대3회
시도하므로24경고를24개 논리 요청이나24개 독립 장애로 읽을 수 없다. 기존 ledger lease
변경은 이 TR들의 직접 경로를 바꾸지 않아 경고 해결책이라고 주장할 수 없다.

일봉 최대5페이지/페이지별 재시도, 시간외20초 보유·약40초 watch 순회, 여러 task의
겹침이 존재한다. US는 같은 환경 키를 쓰지만 별도 limiter이고 외부 프로세스/서버 조건도
가능하나 해당 시간의 원인은 미입증이다. 현 MAX_RPS나 주문·시세 임계값은 변경하지 않았다.

후속 운영 개선은 **main 기준 비식별 호출원·논리요청/재시도·in-flight 계측**을 먼저
검토한다. params·종목·계좌·headers·token은 기록하지 않고, 변경 전후 송신/재시도/반환
동등성을 오프라인 대조한 뒤 별도 리뷰한다. EGW00201 feedback 공통화나 KR/US 공용
pacing은 계측-only가 아니라 행동 변경이므로 같은 단계의 무동작 변경으로 섞지 않는다.
필요한 근거를 얻기 전 호출 한도를 임의로 높이거나 낮추지 않는다.

### retention 서비스 상태의 후속 분류

Sol/high의 추가 읽기 전용 조사(5회)에서09/18~22 매일19시 실행5회 모두
`observer_retention_incomplete`·rc1이었다. 새 회귀로 단정하지 않는다. 이 프로그램은
날짜/권한/경로 등 모든 예외를 같은 sentinel로 숨기므로 journal만으로 내부 원인을
확정할 수 없다. 실제 배치의 인증/상태 파일은 이번 조사에서 열지 않았다.

coordinator가 main 소스 `run_retention`의 `now < retention_at` 거부와 포괄 예외
처리, 승인 문서의 **삭제 기한10/22 18:00 이후·타이머 매일19:00**을 대조했다.
따라서 기한 전 `retention_not_authorized`가 가장 강한 설명이지만 다른 fail-closed
원인을 배제한 직접 예외 증거는 아니다. 관측 grant 만료09/22와 원장 보존 기한을
혼동해 원장을 지우지 않는다. 거래 서비스는 별도 PID로 유지됐다.

삭제·재실행·reset-failed·권한 연장·서비스 설정 변경은0이다.10/22 19:00 이후 기존
체크포인트에 따라 승인 cohort의 삭제/영수증을 별도 확인한다. 기한 전 유휴를 안전한
reason code로 식별하는 관측 개선은 다음 main 후보이며, unsafe/unknown의 실패를
일괄 성공으로 바꾸거나 이번에 조용히 경고를 지우지 않았다.

## 통합 확인

원격 fetch에서 정본 feature가 여전히 `69b43e2`, main이 `afa6e1e`임을 확인한 후
후보 `06fc7ed`를 정본 feature로 fast-forward했다. 별도 문서 수정 외에 source/tests/
scripts/config·의존성 파일은 검증 후보와 동일하다. 통합 WT에서 engine/install 관련
시험을 재실행해 **65 passed /21.75초 /rc0 /격리0**을 확인했다. 이 통합은 main이나
운영 runtime 설치가 아니다. 운영 checkout은 `afa6e1e`, 거래 서비스 PID2259506·
09-23 06:28:05 KST 시작·active/running이18시대 최종 점검에서도 같았다.

후보 `06fc7ed`를 `origin/feature/engine-safety-design-20260917`에 정상 push한 뒤 원격
포함을 확인하고 이번 N1 작업용 WT/로컬 브랜치2개만 정리했다. 리뷰 scratch1파일은
위 증거 폴더로 이동해 보존했고 전후 SHA256은
`001596e9b85e1ad8333d36f47700cf5a09eacd4669aada4d93647de5e538bd42`로 같다.
그 밖의 이전 미커밋 리뷰 WT와 다른 세션 WT는 건드리지 않았다. source/tests는 원격
커밋으로 복구할 수 있고 신규 scratch는 로컬 증거 폴더에 남아 있다.

## 다음 작업과 유지할 차단

운영선과 개발선의 다음 작업을 섞지 않는다.

1. **운영선 main:** 노출된 인증정보는 안전한 운영자 절차로 교체한다. 기존 값이 들어간
   문서/배포 스크립트를 다시 출력하지 않고, 별도 main 기반 작업에서 평문 폴백 제거·
   무권한 상황의 안전 실패를 시험한다. 이번 작업의 제한된 비밀정보 정규식 통과는
   기존 평문 자격의 부재나 교체 완료를 뜻하지 않는다.
2. **운영선 main:** 위 EGW00201 비식별 계측 후보를 별도 검토한다. 실제 호출량 근거
   없이 제한값·재시도·공용 pacing을 바꾸지 않는다.
3. **개발선:** 아래 수동 복구 진단 계약과 운영 main 정합화 계획을 진행한다. 최신 main의
   안전 행동 시험을 가져올 때 기존 특성화26건의 변경을 각각 설명하며 전체 브랜치를
   운영 checkout으로 덮지 않는다.

N1 뒤에는 원 증거/경제 적용 상태를 읽는 **수동 복구 진단 계약**부터 설계한다.
원 RAM/admission 없는 미제출 결정, full 거절 순서 모호함, late-fill/수동 SELL 뒤
보유수량 불일치, A stale/C abandoned/repair-only/commit·게시 결과 불명은 자동 해소하지
않는다. 진단에서 지원하는 증거·불일치·허용 절차를 구분한 뒤 별도 복구 변경을 검토한다.

관측 소비자/heartbeat, 장기 감사 비용·고점 누락, entry policy 지속 게시, 선제 보호
소비자, open BUY/late-fill, cold start와 main 정합화는 별도 단계다.
[설치 차단 표](../operations/claude-migration-handoff-2026-09-20.md)와
[후속 순서](../operations/p1-next-steps-2026-09-23.md)를 계속 적용한다.
