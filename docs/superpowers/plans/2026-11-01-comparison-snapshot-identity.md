# E04-SNAPSHOT-IDENTITY 구현 계획

> 실행: `superpowers:subagent-driven-development`. 사용자 지시: PR #322 통과 확인·병합 뒤 지금 가능한 엔진 개선을 계속한다. 기존 설정·매수 중지는 유지한다.

**목적:** 현재 GPT 응답의 공식 날짜형 모델명을 잘못된 모델로 분류하지 않고, 비교에 쓸 수 있는 호출 증거와 실제 성능/경제성 증거를 계속 구분한다.

**설계/근거:** 기준 main `e71ebc3255f710f331ccb777c420f8edb29397c9`(PR #322). 기존 안전 집계의 GPT24건에서 요청 `gpt-5-mini`, 응답 `gpt-5-mini-2025-08-07`을 확인했다. 2026-11-01 [공식 모델 문서](https://developers.openai.com/api/docs/models/gpt-5-mini)는 이 ID와 snapshot을 명시한다. 오프라인 v1의 문자열 완전 일치 의미는 보존하고, 명시적으로 선택하는 input/report v2에서만 코드에 고정된 방향성 있는 `(openai_chat, gpt-5-mini, gpt-5-mini-2025-08-07)` 관계를 인정한다. 접두사·날짜 정규식·사용자 whitelist로 확장하지 않는다. v2에서 후보 모델명만 같은 공식 snapshot으로 바꾸는 A/A 비교는 비교 변수 변경으로 인정하지 않는다. profile `entry-independent-review-v1`의 판단/제어는 그대로다.

**인터페이스:** 기존 `evaluate_contract(payload)`와 명시 파일 CLI를 사용한다. v2는 `model-comparison-offline-contract-v2`/`model-comparison-offline-report-v2`; 출력에 고정 `identity_policy_version=documented-model-identity-v1`, 각 관측에 `identity_match`(`exact`, `documented_snapshot`, `unknown`, `mismatch`)를 추가한다. v1 출력에는 필드를 추가하지 않는다. 요청/응답 이름은 기존 안전 출력 규칙대로 별도로 보존하며 정규화해 덮어쓰지 않는다. 이미 명시된 refusal/identity_mismatch/실패를 승인으로 복구하지 않는다. 실제 모델 호출·입력 진위·모델 우위·현재 alias의 동적 포인터·실효 effort를 인증하지 않는다.

**소유/경계:** Terra6.1/high(정책 Terra5.6 런타임 대체)가 같은 base의 별도 개발 worktree에서 `src/analytics/model_comparison_contract.py`, `tests/test_model_comparison_contract.py`만 구현한다. coordinator는 문서/통합/WSL을 소유한다. 런타임·모델 설정·가격표·입장/정산·worker/R1 OFF·Gemini 차단·주문·예약·배포·관측 기간은 변경하지 않는다. 외부 모델 리뷰에는 소스/합성 검사만 전달한다.

## 순서와 수락 기준

- [x] RED: 공식 snapshot의 v1 hold/v2 정상, 임의 날짜/다른 모델/누락/반대 방향/잘못된 transport, 첫 시도 mismatch 후 정상 마지막 시도, 명시 실패 상태를 검사한다. v1 출력 불변과 입력 비변경·출력 정제·기존 비용/기한 경계도 검사한다.
- [x] 최소 구현: 두 신원 검사 위치에 같은 버전별 판정을 적용하고 관측의 분류를 일관되게 출력한다. 알 수 없는 버전·caller whitelist는 계속 거절한다.
- [x] 관련 검사: AWS 개발 worktree의 정본 부분 실행은 보조 검증으로 기록한다. 실제 앱/API/운영 자격을 사용하지 않는다.
- [x] 독립 리뷰: 명시 Claude Opus5.5/high의 도구/MCP 없는 소스 리뷰, 실제 응답 모델 검증. 미해결 지적은 수정 후 검증한다.
- [x] 실제 WSL: 고정 소스 tree에서 선택/병렬/skip 없이 `QWQ_VERIFY_PYTHON=/home/user/projects/qwq-ai-trader/venv/bin/python bash scripts/dev/verify.sh`. 문법/비밀정보/격리/전체 결과와 위치·SHA를 기록한 뒤 커밋/푸시한다.
- [x] See: PR #322 병합/CI, 이번 구현/리뷰/검증, 운영/예약 보존 및 NEXT를 원장에 갱신한다. E02/E01 새 유효 입력이 생기면 우선하며 유료 비교·실제 adapter 연결은 별도 조건 대기다.

검토 초점: 모든 시도의 불일치 보존, 실패 상태 우회 방지, v1 출력의 재현성, unknown을 exact로 승격하지 않음, 코드 소유 관계 외 입력 확장 금지. 네트워크 기반 자동 갱신은 없다.

결과: 실제 WSL 직렬 전체10,183PASS·격리0와 실제 Opus5.5/high 재리뷰 미해결0을 확인했다. 상세 근거·소스 인계·현재 NEXT는 [원장](../../operations/execution-roadmap.md#e04-snapshot-identity-20261101)을 따른다.
