# E04 comparison policy lifecycle — 구현 계획

> 사용자 승인: PR #336 병합, 기존 기록·월 예산·설정·매수 중지를 보존하며 후속 구현·실제 WSL 검증·커밋/푸시. API400/800 제한 실호출도 이번 요청으로 승인됨. 이전 만료 승인/실험을 재사용하지 않는다.

**목표:** 같은 공유 월 원장에서 기존 불변 정책과 과거 기록을 보존하며, 명시 승인된 새 실험 정책을 추가할 수 있게 한다. 새 ID로 기존 전체 한도나 기존 실험의 사용량을 초기화하지 않는다.

**기준:** PR #336 merge `aa29b636374b1d916f56cc0913d3f0e45d3abfea` (PR tree 동일). 운영 `a399c297`, evolved 미커밋·KILL 보존. 완료된 PR336 검증은 재실행하지 않는다.

## Global Constraints

- 기존 `comparison_policy(id=1)` 정책/identity_version, requests/attempts/pairs 및 요청/결과 JSON 바이트·ID·월 예약·대사 기록을 덮어쓰거나 삭제하지 않는다.
- 같은 MonthlyBudgetLedger만 사용한다. 월 설정/활성화 기준선·월USD100·log_and_continue·80/95/100% 가드·기존 unknown은 보존한다. 별도 예산 DB로 분리하지 않는다.
- legacy anchor의 전체 일일 시도·쌍 상한을 보존하고 새 정책의 실험별 일일 상한을 추가한다. 비용 상한은 기존 role=experiment_id 전체 기간 집계를 재사용한다. 새 ID로 전역 한도를 우회하지 않는다.
- 일반 guard 생성은 기존 anchor exact 또는 명시 등록된 정책 exact만 허용한다. 같은 ID의 정책·날짜·한도 변경, 불명 버전/부분 스키마/손상 자료는 fail-closed다.
- 명시 등록은 승인자와 승인 참조(준비 plan SHA256)에 결합한다. 자동 정책 교체·기간 연장·실패 후 새 실험·자동 재시도는 없다.
- 진단의 모델 claude-opus-5-5/low·입력/system/suffix·5.75초·출력400→800·최대2시도·재시도0·추정USD0.50·2개 소비표식과 중단 조건을 유지한다. Bull/CLI·운영 worker/R1·Gemini·주문·배포·재시작·발송·예약 변경 없음.
- 개발 검사는 합성/임시 DB만 사용한다. 전체 최종 검증은 실제 사용자 WSL에서 verify.sh 직렬 전체를 푸시 전에 수행한다. 운영 자격증명·원문·로그·캐시·상태를 커밋/리뷰 자료에 넣지 않는다.

## 설계 결정

기존 singleton을 불변 전역 기준으로 남기고 additive versioned experiment registry를 추가한다. singleton 교체는 과거 가드/복구 신뢰를 깨므로 제외하며, 월 DB 분리는 예산을 복제하므로 제외한다. 명시 등록 함수가 transaction 안에서 지원하는 legacy schema와 anchor를 검증하고 새 정책+승인 참조를 INSERT한다. 기존 ID는 같은 정책·같은 승인만 멱등이고 변경은 거절한다. 기존 guard 생성은 새 정책을 등록하지 않는다.

기존 request_json과 pair manifest 내부의 policy 문자열로 소유 정책을 확인한다. 과거 행 재작성/side mapping을 피하고, 같은 BEGIN IMMEDIATE에서 전역 count와 실험 count를 함께 적용한다. 익명/orphan/잘못된 정책 행은 한도를 낮게 세지 않고 거절한다. read_day_attempts의 전역 보고와 과거 inspect/recovery 의미는 보존한다.

## Task 1: 정책 등록·입장·진단 연결과 합성 수락 검사

**소유 파일:** `src/analytics/model_comparison_policy_registry.py`(필요 시 새 helper), `model_comparison_execution.py`, `model_comparison_runner.py`, `bear_api_cap_diagnostic.py`; 해당 기존 검사와 새 `tests/test_model_comparison_policy_lifecycle.py`. 다른 소스/설정/문서는 구현자 변경 금지.

1. 테스트부터 작성해 새 명시 등록/두 실험 coexist 부재로 RED를 남긴다. 기존 mismatch 검사는 승인 없는 guard 생성에서 계속 통과해야 한다.
2. immutable legacy anchor + 엄격한 version/schema 검증 + 명시 승인 등록을 구현한다. SQL transaction 중 원장 설정/역사적 행 불변을 확인한다. 실패 시 새 스키마/등록도 원자 rollback.
3. guard에서 등록 exact를 수락하고 전역+실험별 일 시도 상한을 같은 입장 잠금으로 확인한다. 재시작/월 전환에도 기존 실험 비용/소비는 부활하지 않는다. ID 충돌은 전역 request PK 및 정책 지문으로 거절한다.
4. runner의 일 pair cap/admitted_pair_ids도 전역+실험 의미를 일관되게 한다. 전체 보고 함수는 전체 결과를 유지한다. 기존 manifest/행 바이트는 그대로다.
5. diagnostic RO 사전점검은 등록 가능/기등록 exact/충돌/손상을 구분한다. run은 plan/소스/제어/창→RO→local 소비→ledger→전역 소비→창 재확인→명시 등록→guard→기존 시도 없음→창 재확인→client 순서. 승인 없는 prepare/check-ledger는 DB 쓰기 0. 실패 소비표식 유지.
6. 합성 검사: legacy 행·월 설정/활성화/unknown 보존, legacy guard 계속 동작, 2정책 재개/조회, 같은 ID 변조/불명 contract/부분 registry 거절, 여러 ID의 global cap 유지, 좁은 local cap 유지, 전역 attempt_id 충돌·경쟁·등록 rollback, old/new concurrent admissions와 monthly reservation 공통80%/실험 ceiling, pair caps/recovery, 진단 실제 fake API400/800 + old rows 동일·approval 없으면 client0/registry0.
7. 관련 검사·문법·diff·비밀검사 후 소스 커밋(푸시는 coordinator WSL 뒤). RED/GREEN 명령·rc·수치·실패를 보고서에 남긴다.

## Task 2: 독립 리뷰·문서·WSL·통합·승인된 제한 실호출

Coordinator가 구현 diff/검사 영수증을 읽고 별도 Astra/xhigh 최종 리뷰(spec+quality)를 수행한다. 지적은 현재 범위에서 재현·최소 수정·재리뷰한다. 원장/CHANGELOG/CLAUDE/README/진단 문서에 구현·검증·제약·NEXT를 반영한다.

실제 사용자 WSL 기존 호스트 핀 확인, 별도 고정 SHA worktree/bundle 해시, 직렬 `QWQ_VERIFY_PYTHON=/home/user/projects/qwq-ai-trader/venv/bin/python bash scripts/dev/verify.sh`; 전후 tree/원본 상태/로그 해시/격리0/비밀검사 결과 기록. 그 뒤 커밋/푸시·PR·필수CI. main/운영 자동 배포 없음.

실호출은 검증된 소스·새 ID·같은 공개 입력·새 지문·실행 KST 창과 이번 사용자 승인을 비공개 기록으로 묶어 한 번만 진행한다. 새 정책 등록 전 legacy 비교/월 설정 보존 지문을 대조하고, 전체 일 한도 소진이면 창을 임의 연장하거나 새 ID로 우회하지 않는다. 최종 영수증에서400/800 완료·절단·신원·비용/정산·종료를 판독한다. 실패/불명은 중단이며 수익성 주장 금지. 별도 실호출을 실행할 수 없으면 정확한 차단 상태와 필요한 다음 조건을 남긴다.
