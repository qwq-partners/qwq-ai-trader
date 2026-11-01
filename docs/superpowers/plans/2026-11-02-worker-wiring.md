# E04-WORKER-WIRING — worker 클라이언트 조립 경로·생산자 현행 프로필 발행 (합성 연결 검증, 운영 비활성)

> 사용자 지시: E04-CURRENT-FLOW(PR #326 병합, main `c8b38798`) 위에 "worker의 GPT/Claude 클라이언트 생성·조립 경로 구현, 생산자의 현행 비교 프로필 발행 연결, 실제 호출 없이 합성 입력으로 생산자→worker→상위 비교 흐름→시도별 guard→복구까지 연결 검증". R1·운영 worker OFF 유지, 배포/재시작·실제 모델 호출·유료 비교·실주문·새 예약 없음.

**기준:** main `c8b38798`(PR #326, main CI run41321976552 10,303 passed / 2 skipped / 2 xfailed). 개발은 별도 worktree 브랜치 `feature/e04-worker-wiring-20261102`.

## 연결 경로 확인 (소스)

| 경로 | 현행 | 이번 변경 |
|---|---|---|
| 생산자 | `CrossValidator._offer_comparison`이 `_comparison_handoff`(attach로 주입)가 있을 때만 `mci.operating_role_controls(llm_manager, fallback_prompt_suffix, SINGLE_FALLBACK_TIMEOUT)`로 제어 봉투를 만들어 `producer_identity(experiment_id, config_sha256)`와 함께 넘긴다. 제어에는 이미 `bear_empty_retries`·`bear_internal_timeout_seconds`·`bear_transport_order`가 있다 | 변경 없음(발행 조건은 attach). 새 `current_profile_manifest(operating_controls, …)`가 같은 생산자 제어에서 현행 프로필 manifest를 동결(sha256)해 worker가 기대하는 버전·역할·모델·effort·출력 상한·retry·시한·전송 순서·채널과 바이트 단위로 맞춘다 |
| worker 설치 | `install_from_env`는 `COMPARISON_WORKER=1`+manifest 파일이 있어도 Gemini 스캔 뒤 `_raw_clients()`가 `provider_disabled`를 올려 항상 None | 변경 없음(환경변수만으로 승인 불가). 새 `assemble_comparison_worker(cross, manifest, ledger, transports, approval)`가 명시 승인 기록·동결 manifest sha·날짜 창·전송 집합을 대조한 뒤에만 runner/worker를 조립·attach한다 |
| 클라이언트 생성 | `SingleAttemptAdapter`는 OpenAI/Gemini 인스턴스 필수, Claude 어댑터는 runner에 선택 주입 | `ComparisonTransports(openai, claude_cli, anthropic_api, owned)`를 주입받아 `build_comparison_runner`가 조립. Gemini 인스턴스 없이 조립 가능(`gemini=None`이면 Gemini 요청은 전송 전 `provider_disabled`). 실제 클라이언트 생성은 호출자(미래 승인 경로)가 하며 이 모듈은 import·기본 생성·조립에서 자격을 읽거나 연결하지 않는다 |
| 자원 수명 | worker `_close_clients`가 adapter의 OpenAI/Gemini 세션과 주입된 Claude 전송을 닫는다 | worker는 자기 스레드 루프라 aiohttp 세션을 캐시하는 공유 전송(`owned=False`)은 **조립을 거부**한다(쓰지도 닫지도 않음, 독립 리뷰 P1). worker 전용 전송(`owned=True`)만 받아 stop 때 닫는다. 조립 실패는 소유권 이전 전에 끝나 호출자 자원을 건드리지 않는다 |

## 설계 (최소 단위)

- 새 `src/analytics/model_comparison_wiring.py`: `ComparisonTransports`, `ComparisonActivationApproval`(명시 승인 기록 — 파일·환경변수에서 자동 생성하지 않음), `manifest_sha256`, `current_profile_manifest`, `build_comparison_runner`, `assemble_comparison_worker`. 전부 순수 조립이며 네트워크·자격·CLI 실행 없음.
- fail-closed: manifest `enabled` False, 승인 experiment/sha/전송 집합 불일치, 승인 날짜 창이 execution_policy 창을 포함하지 않음(가드가 매 시도 policy 창을 검사하므로 승인 만료 뒤 새 전송 0), Gemini 포함, 레거시 프로필(Gemini Bear), 필요한 전송 누락/초과, 공유 전송, `max_input_age_seconds` 인자 override(manifest가 소유·지문 포함), 최상위 `pairs_per_day`와 policy 불일치 → `ValueError`로 조립 거부(어댑터·worker 미생성). manifest는 일별 보고 스크립트가 읽는 `max_input_age_seconds`·`pairs_per_day`를 최상위에 갖는다.
- `SingleAttemptAdapter(gemini=None)` 허용(Gemini 요청은 전송 전 거절). `_raw_clients`/`install_from_env`의 거부는 그대로.

## 순서와 수락 기준

- [ ] 합성 검사(`tests/test_model_comparison_wiring.py`): import/기본 생성/승인 없음/잘못된 manifest에서 생성·자격·전송 0, 올바른 입력에서 역할·채널·제어가 정확한 어댑터로 전달, 생산자 제어→manifest 동결과 봉투 대조 일치, 실제 `CrossValidator` 발행→attach된 worker→CLI→API→영수증·복구(`inspect_pair`) E2E, owned/공유 전송 종료 경계, 기존 활성화 차단 회귀.
- [ ] 관련 회귀 0 → Codex 독립 리뷰(고정 SHA/tree) → 실제 WSL 직렬 전체 → 커밋/푸시·PR·필수 CI. 결과는 원장.
- [ ] See: 원장·CHANGELOG·검토 문서. 실호출 인수안(승인 범위·호출 횟수/비용 상한·시한·중단 조건)은 준비만 한다.
