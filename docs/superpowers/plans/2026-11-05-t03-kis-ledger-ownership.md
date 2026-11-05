# T03 KIS 원장 조회 소유권 경계 수정

기준: origin/main `98513f6e`, 운영 `4117371c`. 사용자 후속 진행·최소 수정·WSL 검증 후 커밋/푸시·정상 배포 범위 안에서 수행한다. E05는 순수익 통합 리뷰이며 이 KIS 후속과 구분한다.

## 목표와 범위

실제 limiter와 `_api_get`의 합성 지연/취소에서 재현된 세 상태 오류만 고친다: stale 해제 분기의 최소 간격 우회, 이전 응답의 후속 요청 busy 해제, 취소된 소유자의 busy 잔존. 실제11/5 공급자 거절의 원인은 미확정이다. wire/도착 간격은 입장 간격과 같다고 보장하지 않는다.

숫자 제어 변경은 원인 증거가 없어 제외한다. 명시 lease 인자를 추가하는 대안보다 현재 asyncio 작업 소유권을 리미터 내부에 기록하는 방식을 선택한다(coordinator 판단). 현재 호출자들은 같은 task에서 입장·응답을 처리하며 외부 인터페이스를 유지할 수 있다. 새 기능·계측·재시도·실호출·관측기간은 추가하지 않는다.

## 불변 조건

- MAX_RPS10, MIN_GAP0.1, LEDGER_MIN_INTERVAL1.05, TTTC8434R2.1, stale 10초와 기존 거절 backoff를 유지한다.
- HTTP timeout·GET최대3시도·응답/실패 dict·헤더 도착 시 조기 해제 계약을 유지한다.
- 매수 중지·월USD100/log_and_continue·R1/worker OFF·Gemini 차단·기존 설정/예약/원본 기록 유지.
- 완료된 전체 검증은 반복하지 않는다. 새 변경은 실제 WSL 직렬 전체가 푸시 선행 조건이다.

## 구현·검사

1. `tests/test_kis_ledger_ownership.py`에 실제 `KISBroker._api_get`/리미터와 가짜 세션·통제 시계를 사용한다. 새 검사에서 stale 직후0초 간격, A후속 B 입장 뒤 A의 늦은 해제, 취소 소유자 잔존을 먼저 실패로 고정한다. 네트워크/운영 자격은 사용하지 않는다.
2. `src/utils/kis_rate_limit.py`: ledger 입장 때 현재 task 소유권 기록, 해제는 동일 소유자만 수행한다. stale/reset에서 소유권을 비우고 stale 분기는 전체 가드를 재검사한다. 소유자 없는 중복 해제는 시간 기준을 갱신하지 않는다.
3. `src/execution/broker/kis_kr.py`: GET시도 종료의 finally에서 원장 소유권을 해제한다. 기존 헤더 조기 해제 유지; 취소는 전파하며 대기만 한 요청은 다른 소유권을 해제하지 않는다.
4. 기존 `test_review_fixes_2026_09.py`의 별도 task가 다른 task소유권을 해제하는 fixture는 소유자 task가 응답하는 실제 호출 구조로 바꾼다. stale 단독 검사에서 TR간격을 명시 격리하고 기존 정책 검사는 유지한다.
5. 새 검사/관련 KIS 검사 → 독립 중요 리뷰 → 실제 WSL `QWQ_VERIFY_PYTHON=/home/user/projects/qwq-ai-trader/venv/bin/python bash scripts/dev/verify.sh` 직렬 전체 → 문서/원장·커밋/푸시·PR/CI. 운영 배포는 새 SHA·장외 창·WSL 정본 인수 조건 충족 시만 한다.

## 리뷰 초점

- 취소 시점이 입장 전/HTTP 대기/헤더 이후일 때 다른 소유자의 상태를 바꾸지 않는가.
- 오래된 응답·중복 release가 busy와 응답 기준시각을 바꾸지 않는가.
- stale 해제 후 일반/원장 간격·전역 hold·윈도우를 모두 다시 지키는가.
- 정상 응답, 기존 backoff/재시도/오류 분류와 GET 이외 주문 경로가 불변인가.
- 기존 10초 stale fallback은 살아 있으므로 느린 이전 wire요청과의 중첩 가능성을 해결했다고 주장하지 않는가.

## See

RED 12검사 중5 failed/7 passed를 확인했고, 수정 후 관련12파일 199 passed·18.98초·격리 위반0·문법/diff/비밀패턴 PASS다. 독립 리뷰·실제 WSL은 대기다. 합성 재현은 비공개 `~/.local/state/qwq-task-audit/20261105/t03-kis-boundary/`에 보존하며 실제 거절 인과·선정 성과/순수익은 미확정이다. O02는 오늘 기존 실행 영수증만 판독하고 새 정리/참조 스캔을 만들지 않는다.

리뷰 보완: 실제 Opus5.5/high의 P2에 따라 루프 밖 release의 RuntimeError를 새 실패로 재현해 무변경 반환으로 보완했다. 경합 검사는 active 소유자 응답 뒤 후속 요청이 실제 진행하는 양성 대조를 추가했다. 송신 후 취소/예상 밖 종료에서도 서버 처리 중첩은 가능함을 명시한다. 보완 2파일 38 passed/7.17초·격리 0. 재리뷰·실제 WSL은 대기다.

최종 소스 리뷰: 실제 Claude Opus5.5/high 재리뷰가 P2-1~5 ADDRESSED, P0/P1 0·소스 조건부 APPROVE로 판정했다. 신규 P2 숫자 띄어쓰기는 문서만 정정했다. 실제 WSL 전체가 남았으며 작성자/리뷰어는 분리했다.

실제 WSL See: 스냅샷 `cdebeec1`/tree `f2426a8b`, 직렬 전체10,545 passed/2 skipped/2 xfailed·440.30초(전체461초)·rc0·격리0·문법/비밀정보 PASS. bundle/로그 해시와 원본 main4117371c clean·실행 전후 tree 동일을 확인했다. 테스트 이후 문서만 갱신했으며 통합/배포는 해당 단계 영수증으로 마감한다.
