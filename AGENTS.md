# QWQ AI Trader — Codex 작업 규칙

## 세션 시작

작업 전에 `CLAUDE.md`, `CHANGELOG.md`, `docs/README.md`와 작업 분야의 문서를 읽는다. `CLAUDE.md`를 프로젝트 상세 규칙의 기준으로 삼고 이 문서는 Codex 전용 안전 경계만 보충한다.

현재 작업 순서는 최신 main의 [`docs/operations/execution-roadmap.md`](docs/operations/execution-roadmap.md)를 정본으로 삼는다. 운영 checkout의 과거 계획/미완료 체크박스를 그대로 재개하지 않는다. 시작 시 작업 ID·완료 조건을 확인하고, 완료 시 Plan→Do→See 근거·독립 리뷰·운영/예약 상태·NEXT ID를 갱신한다. 승인 범위와 의존성을 충족한 다음 작업은 같은 세션에서 이어간다.

## 로컬 안전 경계

- 사용자가 현재 요청에서 명시하지 않은 실거래 주문을 실행하지 않는다.
- 사용자가 현재 요청에서 명시하지 않은 운영 서버 SSH, `systemctl`, 재시작, 배포를 실행하지 않는다.
- 로컬 기본 검증에서는 외부 API와 운영 자격증명을 사용하지 않는다.
- `.env`, 로그, 캐시, PID, 거래 상태 파일을 커밋하지 않는다.

## 변경과 검증

- `feature/*` 브랜치에서 작업하고 관련 테스트와 문서를 함께 갱신한다.
- 구현 후 관련 테스트, 전체 테스트, 비밀정보 검사를 수행한다. 전체 검증은 CI·운영 배포와 동일한 `bash scripts/dev/verify.sh` 로 **푸시 전에 로컬에서** 통과시킨다. 로컬(WSL)에서는 `QWQ_VERIFY_PARALLEL=auto`(pytest-xdist) 병렬과 `QWQ_VERIFY_TESTS=<경로>` 부분 실행을 써도 되지만, 최종 확인과 CI·서버는 직렬 전체다. 준비는 `scripts/dev/bootstrap.sh` 를 쓴다([로컬 개발](docs/operations/local-development.md) 2·6절).
- 구현자와 최종 리뷰어 역할을 가능하면 분리하고 동일 파일을 동시에 수정하지 않는다.
- Claude Code가 구현한 브랜치의 교차 리뷰는 `bash scripts/dev/codex_review.sh`(읽기 전용)로 수행한다.
