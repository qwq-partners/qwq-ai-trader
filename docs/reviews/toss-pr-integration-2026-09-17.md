# Toss 열린 PR 통합·운영 반영 — 2026-09-17

## Plan — 사용자 범위

열린 PR 전부 검토·main 병합 → 독립 리뷰/문서/검증/커밋·푸시 → 운영 배포/재시작 → Toss 활성화 요청이다. 주문·전략·위험 사이징·KIS 돈 경로 변경은 포함하지 않는다. 사용자에게 단일 토큰 발급 주체·외부 발급 중지·조회 및 관측 원장 저장 허용을 확인받았다. 실제 자격 값은 문서나 출력에 남기지 않는다.

시작 시 열린 PR은 #70(`5a834ff`)과 #68(`0b978e0`) 두 개였다. #70은 검증된 source `984dbdf` 이후 문서만 변경했고, 새 KST 전체1684 passed/2 known xfailed/1 기존 pykrx warning·격리0·문법/비밀정보 검사 통과 후 main `1c9531cbb98af5a34d7e3f60ce9b33e1038aa55c`에 병합했다. 당시 운영 checkout은 아직 갱신하지 않았다.

## Do — #68 정합화

원본을 그대로 합치거나 충돌 파일만 해결하지 않는다. 독립 Astra/xhigh 대조에서 `.env.example`, CLAUDE, KR 스케줄러, heartbeat에 구형 기본 ON·작업이 충돌 없이 자동 병합됨을 확인했다.

- provider 5파일(`__init__`, `client`, `market_data`, `rate_limit`, `token`), KR 스케줄러·heartbeat, 최신 설계/API 문서는 #70 blob을 유지한다.
- `src/analytics/toss_parity.py`와 구형 `test_toss_client_phase1.py`, `test_toss_parity_phase1.py`는 새 구현·계약 테스트로 대체되어 최종 트리에서 제외한다. 원본 Git 이력은 merge 부모에 남는다.
- 원본 리뷰 프롬프트는 역사 배너로 보존하고 최신 리뷰 문서로 연결한다. `.env.example`은 기본0과 빈 자격 필드만 남긴다. 실제 `.env`는 변경하지 않는다.
- 호가·상하한가·지수, 비일봉 캔들 래퍼, 미배선 일봉 실자료 대조는 원본에만 있던 기능이지만 승인 범위 밖이므로 옮기지 않는다. 기능 누락을 숨기거나 live 완료로 표시하지 않는다.

원본의 기본ON, revoked 동일 캐시 재발급, durable intent 없는 발급, 보안 저장/송신 경계, 원문 오류 유출, 시각·시장 기준 없는 비교 표본, 쓰기 실패/전량 실패 성공 처리 문제는 [기존 고정 SHA 리뷰](toss-phase1-handoff-2026-09-16.md)에 근거가 있다. 새 독립 리뷰도 이를 확인했다. 원본 테스트45 passed는 위험 동작을 정답으로 고정한 항목을 포함하므로 병합 승인 근거가 아니다.

## See — 검증과 운영 상태

정합화 인수: `1c9531c` 대비 `src/`, `tests/`, `scripts/`, `config/`, 의존성 diff0, 구형 parity 작업/모듈 부재, OFF 예제, 역사 프롬프트 표시를 확인했다. 정합화 트리 KST 전체 **1684 passed/2 known xfailed/1 기존 warning(82.88초)**, 격리0·문법/비밀정보 검사 통과. 별도 Astra/xhigh가 6개 변경 파일과 전체 실행 트리 동등성을 검토해 신규 P0/P1/P2 0·승인했다. 최종 head/CI/병합 결과는 후속 절에 기록한다.

지정 `bash scripts/dev/codex_review.sh --branch`는 원본 #68의 별도 읽기 전용 작업공간에서 Astra/xhigh·read-only sandbox로 실행했다. 이번에는 저장소 접근과 코드 대조가 정상 동작했지만 **360초 제한으로 최종 판정 전 종료(exit124)** 됐다. 샌드박스 실패나 리뷰 승인으로 기록하지 않는다. 중간 관측에서 반열림·시각 없는 비교·토큰/캘린더/정밀도 문제를 지적했으며, 병합 판단은 완료된 독립 대조·정합화 리뷰와 새 전체 검증에 근거한다. 원본 작업공간/HEAD는 유지했다.

읽기 전용 운영 사전점검(00:30 KST): 장외, 서비스 active/PID3082563/09-15 22:37 시작, pending0·stale0, root main/a3187a8 clean, sudo 비대화식 가능. 작업공간 SHA가 실행 코드 SHA를 증명하지는 않는다. Toss 키 이름의 존재·비어 있지 않음만 확인했고 값·지문·토큰 캐시는 출력/조회하지 않았다.

실관측은 아직 시작하지 않았다. 현재 서비스는 mutable checkout의 `run_trader.py`를 직접 실행한다. launcher/StartupAttestation·operator registry·실제 plan/grant·분리 상태 경로 연결이 없으므로 플래그만 켜거나 현재 Git HEAD로 실행 증거를 만들지 않는다. 정확한 실행 방식·관측 정책 설계/검증 후 활성화해야 한다.

배포 직전에는 장외·pending0·clean·최종CI·설정/킬스위치 보존을 다시 확인한다. 배포 성공과 실관측 활성화/3영업일 인수/소비자 승격은 별개이며, 모든 관측 보고서의 `production_eligible=False`는 유지한다.

## 병합 완료·배포 직전 인계

- #68 정합화 head **`93570df673ba03ccfeba9f074a77b743557b18cc`**. 부모는 원본 `0b978e0`과 #70 main `1c9531c`다. 원본 브랜치로 정상 fast-forward push했고 이력을 강제로 재작성하지 않았다.
- 독립 Astra/xhigh 최종 커밋 재확인 승인, 신규 P0/P1/P2 0. [필수 verify run35116712908](https://github.com/qwq-partners/qwq-ai-trader/actions/runs/35116712908) SUCCESS 후 #68을 **main `6ac6ce02daaa6a2429fc5cacad1f9c585731772c`**에 병합했다. 00:40 KST 열린 PR은 0개로 확인했다.
- 병합 main의 소스/테스트/스크립트/설정/의존성은 검증된 #70과 동일하다. main의 병합 이력과 실제 운영 프로세스의 로드 상태는 다르므로, 배포 완료 결과는 이 문단으로 대체하지 않는다.
- 운영 설정3개(.env/default/evolved)·킬스위치4경로의 사전 지문을 값 노출 없이 비교용으로 확보했다. 미체결0과 브로커 연결을 배포 직전에 다시 검사한다.
- 활성화는 현재 mutable 거래 checkout을 immutable이라고 주장하지 않는 별도 연결 설계가 필요하다. 기존 거래 서비스를 유지하는 독립 관측 프로세스안과 전체 거래 서비스의 고정 릴리스 이전안을 비교했고, 사용자에게 전자를 권고했다. 아직 선택/상세 정책을 확정하거나 서비스를 설치하지 않았다. 기존 `run_trader.py`를 관측용 두 번째 프로세스로 실행하면 singleton 동작이 기존 거래 프로세스를 중단할 수 있으므로 금지한다.
