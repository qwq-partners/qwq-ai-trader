# GitHub Quality Gate Design

## 목적

모든 PR에서 로컬과 동일한 검증을 자동 실행하고, 성공한 변경만 `main`에 병합되게 한다. 이 단계는 코드 품질 관문만 추가하며 Lightsail 배포와 운영 자격증명은 다루지 않는다.

## 접근 방식

단일 `verify` job을 권장안으로 사용한다. 문법·테스트·비밀 검사를 여러 job으로 나누면 실패 원인은 빨리 구분되지만 의존성을 중복 설치하고 로컬 `scripts/dev/verify.sh`와 흐름이 갈라진다. 단일 job은 가장 단순하며 로컬과 CI의 검증 명령을 하나로 유지한다.

## Workflow

`.github/workflows/verify.yml`은 다음 이벤트에서 실행한다.

- `main` 대상 pull request
- `main` push
- 수동 `workflow_dispatch`

job 이름은 브랜치 보호에서 참조할 수 있도록 `verify`로 고정한다. `ubuntu-24.04`, Python 3.12, `actions/checkout@v7`, `actions/setup-python@v7`을 사용한다. workflow 권한은 `contents: read`만 부여하고 checkout 자격증명은 유지하지 않는다.

실행 순서는 checkout, Python과 pip 캐시 준비, 의존성 설치, `bash scripts/dev/verify.sh`다. 전체 제한 시간은 15분이며 같은 PR/브랜치의 이전 실행은 취소한다. `.env`, GitHub secret, SSH 키, 운영 서버 주소를 workflow에 전달하지 않는다.

## Main 보호

CI가 실제 PR에서 성공한 뒤 `main`에 다음 보호를 적용한다.

- PR을 통해서만 변경
- 필수 상태 검사: `verify`
- 브랜치 최신 상태 요구
- 해결되지 않은 PR 대화가 있으면 병합 차단
- force push와 branch deletion 금지
- 1인 운영을 고려해 필수 승인 수는 0

보호 설정 API 권한이 부족하면 원격 feature 브랜치와 PR을 보존하고 GitHub Settings에서 적용할 정확한 값을 문서로 제공한다.

## 오류 처리

- 의존성 설치나 검증 실패 시 job은 즉시 실패한다.
- 취소된 과거 실행은 병합 조건으로 사용하지 않는다.
- CI는 외부 API, 실거래, SSH, systemd를 호출하지 않는다.
- 브랜치 보호는 `verify` check가 GitHub에 등록된 뒤에만 적용한다.

## 완료 기준

- PR에서 `verify` workflow가 자동 실행된다.
- workflow는 로컬 `scripts/dev/verify.sh`를 그대로 호출한다.
- CI에 쓰기 권한이나 운영 secret이 없다.
- 실제 workflow 실행이 성공한다.
- `main`이 성공한 `verify`와 PR을 요구한다. 권한 문제 시 수동 설정 절차가 검증 가능하게 문서화된다.
