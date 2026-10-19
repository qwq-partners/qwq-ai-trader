# Pre-gate Candidate Observation Implementation Plan

> **For agentic workers:** Use superpowers:subagent-driven-development for the bounded independent tasks below. The coordinator alone integrates and publishes.

**Goal:** 시장 방어에서 멈춘 후보의 후속 가격을 오프라인에서 판독하고 수집 자원 공백을 정량 확인한다.

**Architecture:** 기존 first-scan-v4 저널·선정·게이트·프레임 검증기를 재사용한다. 읽기 전용 가격 판독과 순수 자원 점검을 별도 모듈로 만들며 실제 수집/설치 경로에는 연결하지 않는다.

**Tech Stack:** Python, 기존 strict journal/market-state/markout helpers, pytest.

**Spec:** `docs/research/pregate-candidate-observation-2026-10-19.md`

## Global Constraints

- Base `bcc397531635a371bd79208456fc3503b3ac3101`; 별도 worktree, 파일 소유권 분리.
- 운영 파일·API·자격증명·주문·서비스·예약 변경 없음. 기존 LLM readout 변경 없음.
- 전체 반환 후보 분모, 고정 상위5·3채널, 900초/5초, 순수익/실거래 승격 금지.
- 작성자는 중요 변경을 스스로 승인하지 않으며 실제 모델 metadata 미노출은 미검증으로 기록.

## Review Focus

- 첫 스캔이 빈 경우나 후보가 5개보다 많은 경우 분모가 바뀌지 않아야 한다.
- 불량 첫 호가·미래 시각·재연결 전 상태를 좋은 자료로 교체하지 않아야 한다.
- 미완전/손상 저널과 study 불일치를 정상 관측으로 세지 않아야 한다.
- 구독 여유·레코드/저널 한도의 경계값과 bool/비정상 숫자를 명확히 검증한다.
- CLI는 symlink/FIFO/과대 파일/중복 JSON 키를 거부하고 입력을 수정하지 않는다.

### Task 1: 후보 가격 판독

**Files:** `src/analytics/pregate_candidate_readout.py`, `scripts/report_pregate_candidate_observation.py`, `tests/test_pregate_candidate_readout.py`.

**Interface:** `build_pregate_candidate_report(study_bytes, observations, *, study_sha256, analysis_as_of)` returns a diagnostic dict. CLI reads a bounded study and strict journal, prints JSON only; no fabricated order/signal/LLM records.

- [x] RED: 7후보/5대상, 첫 빈 스캔, 불량 첫 호가, 같은 연결/세대 상태, +900초 첫 호가, 늦은 기준점, 손상/미완전 저널 반례 작성·실패 확인.
- [x] DO: 기존 검증·분류 함수를 재사용해 원본 가격/시각/상태/선정/게이트와 결측 사유를 출력.
- [x] SEE: 관련 검사 통과, CLI 파일 경계와 입력 불변 확인, 독립 리뷰.

### Task 2: 자원 점검

**Files:** `src/analytics/pregate_capture_capacity.py`, `scripts/assess_pregate_capture.py`, `tests/test_pregate_capture_capacity.py`.

**Interface:** `assess_pregate_capture_resources(study, *, operational_registrations, external_registrations, readout_max_bytes=268435456)` returns requirements/checks/blockers with `installation_ready=false`. No study/manifest/once/systemd files generated.

- [x] RED: top5×3=15, 운영/외부/예비 포함 상한 경계, 버퍼·큐·레코드 예산, 905초 후미/lease, 잘못된 숫자·경로/프로필 시험 작성·실패 확인.
- [x] DO: first-scan-v4 설정의 요구량/통과 여부와 메모리·준비/교체 미구현 상태를 별도 표시. 날짜/달력/실제 운영 수요는 미확인으로 유지.
- [x] SEE: CLI 읽기 경계·결정성·입력 불변 검사와 독립 리뷰.

### Task 3: 통합

**Files:** 위 연구 문서, `CLAUDE.md`, `CHANGELOG.md`, `docs/README.md`.

- [x] 독립 구현 diff 검사·순차 cherry-pick; 부작용 없는 합성 연결 검사.
- [x] 관련 검사와 전체 `scripts/dev/verify.sh` 통과, 독립 최종 리뷰 지적 반영.
- [x] 검증 결과·남은 설치 작업 기록.

게시 후 commit/push/PR/CI/merge와 main CI의 최종 결과는 PR 본문/checks에 기록한다. 이 계획의 체크 표시는 로컬 구현·검증 완료 범위다.
- [x] 오프라인 도구는 운영 재배포 대상이 아님을 구분하여 보고.

검증: 신규47·관련346·전체8076 passed/2 skipped/2 xfailed, 격리0, 문법/비밀검사 통과. 독립 Sol/high 최종 APPROVE; Astra 용량 오류/Claude 인증 오류와 명시적 검토 경로 변경은 연구 정본에 기록했다.
