# Opus 독립 리뷰 실행기 진단·수정

## 범위

사용자가 모델 대체보다 실패 원인 진단·수정을 먼저 지시했다. 엔진 C2a 후보와 후속 caller 구현은 동결한다. 이번 범위는 개발용 외부 리뷰 실행기이며 운영 서비스·거래 설정·KIS/Toss·자격증명을 변경하지 않는다.

## Plan — 실패 증거와 가설

기존 두 시도는 각각 360.02초에 부모의 `subprocess.communicate(payload, timeout=360)` 제한으로 종료됐다. `TimeoutExpired` 처리에서 자식을 `kill()`했고 child rc는 -9였으나, 진단 래퍼는 실패 상태를 출력한 뒤 자체적으로 exit0했다. 최종 `result`/리뷰문은 없었다. 이 exit0은 성공 리뷰가 아니다.

첫 입력은 202,716자/SHA256 `a70daf25f14b3cfcc6149d108dd3d14391efec952ff15d1d053912e02ec906c8`, 두 번째는 137,827자/`839773479a0c2f6c149fa9087a87ded144d0b91b5a3ce3035862a9a786221804`다. 두 번째가 남긴 system268/assistant1만으로 당시 model reasoning인지 API 재시도인지 구별할 수 없다. 원 래퍼는 system subtype·시각을 보존하지 않았다.

확정 가능한 직접 원인은 **로컬 고정 시간 제한의 강제 종료**다. 그 제한을 넘긴 원인을 모델 장애로 단정하거나, 미완료 상태에서 공급자 대체를 먼저 제안할 근거는 부족했다. xhigh와 큰 다중 파일 리뷰의 작업량·시간 예산 불일치를 계측해서 확인한다. 돈 예산과 모델에게 쓴 자연어 시간 지시는 실행 시간 제한이 아니다.

복원한 호출 근거는 현재 Codex 세션의 2026-09-19T15:34:44.257Z/15:42:32.830Z tool 입력이다. 원본 세션 파일과 실패 기록은 수정하지 않는다. 외부에 원본 세션·자격·운영 로그를 내보내지 않는다.

## Do — 대조 실험과 수정 경계

### 대조 실험

1. 설치된 Claude Code 2.1.222의 로컬 `--help`에서 필요한 플래그를 확인했다. 같은 `claude-opus-5`/요청 xhigh/safe-mode/tools-empty 조건의 짧은 요청은 **4.13초, child exit0, terminal success, 실제 assistant Opus**로 완료됐다. 현재의 일반 로그인/모델 접근 실패 가설은 이 대조와 맞지 않는다. 보조 modelUsage의 Haiku 항목을 리뷰 모델 fallback으로 해석하지 않는다.
2. 첫 실패 입력을 바이트 해시까지 동일하게 재구성했다. 모델·effort·USD5 cap·안전 플래그·자격은 그대로 두고, 부분 이벤트/실시간 계측과 유한한 900초 진단 상한을 적용했다. 이 실험은 무한 재시도나 인증 우회가 아니다. `message_start`는 3.04초, `thinking_tokens`/`thinking_delta`는 3.89초부터 관측됐다. 이 실행에서는 입력 접수/기동보다 모델 계산 시간이 지연의 주된 구간이다. 최종 결과는 아래 See에 기록한다.

원래 두 실행의 상세 진행을 사후 복원할 수는 없다. 재실행 관측을 과거 두 호출의 토큰 이력으로 주장하지 않는다. hidden thinking 본문은 저장하거나 보고하지 않고 종류·계수·시각만 계측한다.

### 수정 계약

- 명시적으로 검토·비식별화한 stdin만 전달하며 저장소 전체나 `.env`를 자동 수집하지 않는다.
- stdin 전송과 stdout/stderr 수신을 동시에 처리하고 입력 SHA/완전 전송·실제 모델·도구 차단을 확인한다.
- startup/모델 진행 정체/전체 시간 상한을 구별한다. 자체 heartbeat와 일반 status를 모델 계산 진행으로 간주하지 않는다. 전체 상한은 어떤 이벤트로도 연장하지 않는다.
- 부분 출력은 완료가 아니다. 정확히 하나의 정상 terminal result·실제 모델 일치·child exit0·오류/도구 사용 없음 등을 확인한 뒤에만 실행 완료로 기록한다.
- timeout·불완전 결과·프로토콜 오류는 nonzero로 반환한다. 완료된 리뷰와 코드 승인·main/운영 승인은 별개다.
- 모델/effort의 조용한 강등, 권한 확장, 인증/전역 CLI 설정 변경 없이 고친다. 실제 유효 effort나 비용은 관측된 범위만 보고한다.

## See — 실제 Opus 재실행 결과

| 실험 | 입력/요청 | 결과 |
| --- | --- | --- |
| 짧은 연결 대조 | 동일 Opus5/xhigh, 짧은 고정 응답 | 4.13초, 실제 Opus, terminal success/child0 |
| 원 장문 입력 재현 | 원 SHA 동일·202,716자, 동일 Opus5/xhigh | 본문 시작577.05초, 최종692.32초, terminal success/child0, 파일 지문 동일 |
| 작은 실제 모듈 대조 | 8,844자, 동일 Opus5/xhigh | 본문 시작109.20초, 최종120.91초, terminal success/child0 |

장문은 365.32초 시점에도 364.08초까지 진행 이벤트를 수신했고, API retry/error·stderr 없이 정상 완료됐다. 이 재현에서 **360초 예산은 정상 장문 리뷰를 자르는 제한**이었음이 확인됐다. 작은 모듈의 빠른 완료는 입력/검토 범위의 영향을 뒷받침하지만, 샘플 하나로 일반 성능이나 시간 상한을 보장하지 않는다.

장문 최종 usage의 output_tokens는56,632(추론 포함), client 추정 비용은USD2.19885다. 작은 모듈은9,613/USD0.299045다. 이 값은 실제 청구액이 아니며 과거 timeout 두 건의 미관측 비용을 대체하지 않는다. `thinking_tokens`의 중간 estimated 값도 spinner용 추정치이지 최종 과금 token 증거가 아니다. 설치 바이너리의 해당 이벤트 설명도 이 구별을 명시한다.

장문 리뷰의 코드 판정은 **CHANGES_REQUIRED**다. B1(미등록 selector seal이 owner 전면 차단/종료 실패로 확대되는 경로)을 차단 의견으로, B2–B6와 V1–V8을 추가 의견으로 제출했다. 아직 부모가 실제 재현·범위 판정을 하지 않았으므로 확정 결함 수로 세지 않는다. 작은 모듈 대조도 호출자 문맥이 없으므로 전체 slice 승인/결함 확정에 쓰지 않는다. 원 장문 리뷰 본문은 ignored SDD의 `task-10a2c-generation-opus-review-completed-20260920.md`에 보존했다.

원 장문 재현은 계측용 스크립트로 실행했으며, 별도 신규 공용 실행기와 같은 코드라고 주장하지 않는다. C2a 후보/후속 엔진 구현·main/운영은 동결 상태다.

## 공용 실행기 — 독립 검토와 실패 경로

원인 증거 검토는 Sol/high, 실행기 구현은 별도 worktree의 Terra/high, 독립 프로세스 경계 검토는 Astra/xhigh가 맡았다. Astra는 **개발 실행기**를 검토했으며 C2a의 Opus 리뷰를 대체하지 않았다. 외부 Claude 호출과 최종 통합은 부모가 수행한다.

첫 후보의 작성자 시험15개와 실제 작은 모듈 호출114.69초가 통과했어도 다음 결함이 독립 재현됐다. 정상 호출만으로 실행기 안전성을 승인하지 않았다.

| 독립 지적 | 재현된 실패 | 수정 인수 조건 |
| --- | --- | --- |
| R1 | 잘못된 이벤트 필드가 예외로 탈출해 자식이 계속 실행됨 | 모든 post-spawn 실패에서 소유 프로세스 그룹 정리·회수, 민감 원문/traceback 미노출 |
| R2 | leader와 stdio가 먼저 종료되면 실패 경로에서 자손이 남음 | leader 종료 여부와 무관하게 소유 그룹 정리 |
| R3 | wrapper SIGTERM 후 Claude가 계속 실행됨 | SIGINT/SIGTERM을 정리 경로로 연결 |
| R4 | 임의 nested event가 outer 오류·모델·도구 검사를 우회함 | 공식 envelope만 허용하고 외부/내부 검사를 우회하지 않음 |
| R5 | thinking 블록별 추정치 초기화를 무시해 정상 진행을 정체로 판정함 | 새 thinking 블록의 추정치를 구별하되 동일값·일반 heartbeat는 진행 아님 |

독립 원본28개는 처음27개 중8실패/19통과와 추가 블록 시험1실패로 고정했다. 1차 수정 후 작성자19·원본28은 통과했지만 별도3개가 R1의 같은 근본 원인을 재현했다: `delta.type=[]`, 과도한 중첩, 극단 비용 정수. 후속 검토는 이벤트 루프 이전 selector 초기화에 실제 SIGTERM을 주입해 자식이 남는 좁은 경로도1개 재현했다. 중간 GREEN은 완료/승인으로 처리하지 않았으며 초기화·예외·종료를 포함하는 소유 프로세스 수명 경계로 보완한다. 독립 원본32개는 수정하지 않고 별도 보존하며, 같은 assertion의 경로/인터프리터만 이식한 시험을 정규 CI에 포함한다.

R5의 per-block 전제는 로컬 CLI2.1.222 바이너리 설명으로 확인했다(`/usr/lib/node_modules/@anthropic-ai/claude-code/bin/claude.exe`, byte offsets257407965/282068838). 중간 추정치가 해당 thinking 블록 기준이라는 내용이며 과금 토큰으로 해석하지 않는다.

### 최종 실행기 검증

최종 실행기 SHA256은 `71c32a34cdb7c6f453a8ce8e220f821480c2bc9a9deae7ed8cd68996bd79cdf7`, 작성자 시험은 `41578c5885c0d692d4c93db4fa422589662d23e5c05f67303a80f66f360f10de`, 정규 이식한 독립32개 시험은 `d5806fb915854a622e25220cba38330404c28eea237495067320e899cb6c06cf`다. 부모는 실행기/작성자 시험을 이 해시 그대로 엔진 feature worktree에 통합했다.

- Astra/xhigh 최종 독립 재리뷰: **APPROVE_THIS_SLICE**. 변경하지 않은 원본32개15.43초·작성자21개14.86초 통과, 작성자 시험의 격리 감지0. 승인 보고서 SHA256 `20995734ae416e22a372121d67defbf88a5a98a5a9f849956c5944ab019a2cfc`를 ignored SDD에 보존했다. 이는 해당 개발 CLI 구간 승인이지 엔진 코드나 실제 공급자 동작 전부의 승인이 아니다.
- 부모 통합 집중 시험: 신규53개(작성자21+독립32)와 기존 Codex 실행기9개, **62passed/29.05초**, 격리0.
- 같은 최종본의 실제 Opus smoke: 요청 Opus5/xhigh, 실제 `claude-opus-5`, **2.72초/child0/정상 terminal success**, 도구·stderr·오류 없음. `RUNNER_SMOKE_OK`는 통신/실행 완료 대조이지 코드 승인 문구가 아니다. 입력272바이트/SHA `7c6114e9860de8bffc2ed3dcf9fc7c4565e5d68ea2ea56228032b437a5013141`를 전부 전달했다. 이 짧은 대조만 startup30/idle30/total60초로 한정했고, 공용 기본120/180/900초는 바꾸지 않았다. usage output16·client 추정USD0.02726이며 실제 청구액으로 주장하지 않는다.
- 통합 feature 전체: **UTC4021passed/2 known xfailed/4기존 warnings,224.01초**, **KST4021/2/4,222.78초**, 각각 exit0·운영 상태/외부 네트워크 접근 시도0. 앞선 엔진 후보3968개에 실행기 신규53개가 추가된 수치다. 독립 원본32개를 별도로 다시 더하지 않는다. tracked Python 문법·비밀패턴·staged diff 공백 검사를 통과했으며 `verify.sh`의 시험 생략 모드를 전체 시험 근거로 쓰지 않았다(위 명시 `tests` 실행이 근거다).

시험 범위는 실제 로컬 자식/파이프/프로세스 그룹과 합성 CLI 이벤트다. 모든 명령 경계의 신호 경합, SIGKILL, 악의적인 detached 자손, OS 보안 격리, 입력 생산자/출력 소비자의 무한 blocking까지 검증한 것은 아니다. 사전 비식별화된 완성 입력 파일과 정상 출력 소비자를 사용한다. 공용 실행기는 safe-mode 인증 호환 allowlist를 사용하지만 별도 OS sandbox를 제공하지 않는다.

개발 도구의 인수는 엔진 C2a 코드 승인, 전체 C/F/G/R, main 병합 또는 운영 전환을 승인하지 않는다. 다음 엔진 작업은 **원 Opus B1부터 실제 재현 → 범위 판정/수정 → 독립 재리뷰**, 그 뒤 지속 source 권한·실제2분 루프·정오/LLM/보호 replay 순서다. 현재 턴에는 엔진7파일 source/test patch SHA `e62f1c53e267dac313bd880f32fc0b6b11b339e60af02b755011486ee785cd81`가 그대로이고 main `8ff2f55`는 clean이다. 수정본은 기존 feature worktree에 보존했으며 이번 턴 commit/push/main 병합/운영 SSH·배포·재시작/브로커 API·주문·거래 설정 변경은 없다.

## 참고

- [공식 programmatic 실행 문서](https://code.claude.com/docs/en/headless): 부분 stream 이벤트, terminal result, exit code, API retry 구분. 최신 문서의 일부 기능은 로컬 2.1.222보다 이후 버전이므로 로컬 help와 실제 이벤트를 우선한다.
- [공식 effort 설정 문서](https://code.claude.com/docs/en/model-config#adjust-effort-level): 높은 effort의 추가 계산/토큰 비용과 요청 수준·실제 적용 수준의 구별.
- 기존 실패 기록: `.superpowers/sdd/2026-09-17-engine-execution-safety/task-10a2c-generation-opus-review-attempts-20260920.md`.
- 읽기 전용 Sol/high 진단: 같은 디렉터리의 `opus-runner-diagnosis-independent-20260920.md`. 판정 근거 점검용이며 C2a 코드 리뷰는 아니다.
