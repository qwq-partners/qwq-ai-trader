# GitHub Quality Gate Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** PR마다 로컬과 동일한 검증을 자동 실행하고 성공한 검사만 `main` 병합 조건으로 설정한다.

**Architecture:** 읽기 전용 단일 GitHub Actions job이 Python 3.12 환경에서 의존성을 설치한 뒤 `scripts/dev/verify.sh`를 호출한다. 실제 CI 성공 후 `verify` 상태 검사를 요구하는 `main` 보호 정책을 적용한다.

**Tech Stack:** GitHub Actions, Ubuntu 24.04, Python 3.12, Bash, pytest, GitHub REST API

**Spec:** `docs/superpowers/specs/2026-08-19-github-quality-gate-design.md`

## Global Constraints

- workflow 권한은 `contents: read`만 사용한다.
- 운영 secret, `.env`, SSH, systemd, 주문 또는 배포 명령을 사용하지 않는다.
- CI와 로컬은 모두 `scripts/dev/verify.sh`를 검증 진입점으로 사용한다.
- 브랜치 보호는 실제 `verify` check가 성공한 뒤 적용한다.

---

### Task 1: Verify Workflow

**Files:**
- Create: `.github/workflows/verify.yml`

- [ ] `main` PR/push와 수동 실행 trigger를 정의한다.
- [ ] 읽기 전용 권한, concurrency, 15분 timeout을 정의한다.
- [ ] `actions/checkout@v7`과 `actions/setup-python@v7`으로 Python 3.12와 pip cache를 준비한다.
- [ ] `pip install -r requirements.txt` 뒤 `bash scripts/dev/verify.sh`를 실행한다.
- [ ] YAML 구조와 로컬 검증을 실행한다.
- [ ] `git commit -m "ci: PR 검증 workflow 추가"`로 커밋한다.

### Task 2: Operations Documentation

**Files:**
- Create: `docs/operations/github-quality-gate.md`
- Modify: `docs/README.md`
- Modify: `CHANGELOG.md`

- [ ] workflow trigger, check 이름, 실패 확인법과 재실행 절차를 문서화한다.
- [ ] GitHub UI와 API의 `main` 보호 설정값을 문서화한다.
- [ ] 문서 인덱스와 변경 이력을 갱신한다.
- [ ] `bash scripts/dev/verify.sh`를 실행한다.
- [ ] `git commit -m "docs: GitHub 품질 관문 운영 절차 추가"`로 커밋한다.

### Task 3: Remote Verification and Protection

- [ ] feature 브랜치를 push한다.
- [ ] `main` 대상 PR을 생성한다.
- [ ] `gh pr checks --watch`로 `verify` 성공을 확인한다.
- [ ] `main` 보호 API에 PR 필수, `verify` 필수, strict 최신화, 대화 해결, force push·삭제 금지를 적용한다.
- [ ] 보호 설정을 다시 조회해 실제 값을 검증한다.
- [ ] 권한 부족 시 PR과 브랜치를 유지하고 수동 설정 절차를 인계한다.
