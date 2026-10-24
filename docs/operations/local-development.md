# 로컬·원격 개발환경 — Codex·Claude

1~9절은 Windows의 WSL2 Ubuntu에서 Claude Code와 Codex로 QWQ AI Trader를 개발하고 PR까지 올리는 절차다. 아래 [CLI 협업과 엔진 활용 검토](#cli-collaboration)는 현재 원격 개발 호스트의 확인 결과와 공통 역할 분담을 다룬다. 로컬 환경은 테스트, 백테스트, 명시적으로 선택한 dry-run 전용이다. 실거래 주문, 운영 서버 재시작, 배포는 이 흐름에 포함하지 않는다.

## 1. 작업 위치

Windows 드라이브(`/mnt/c`)가 아니라 WSL의 Linux 파일시스템을 사용한다.

```bash
mkdir -p ~/projects
cd ~/projects
git clone git@github.com:qwq-partners/qwq-ai-trader.git
cd ~/projects/qwq-ai-trader
git remote -v
```

기존 작업본이 있다면 새로 복제하지 말고 상태부터 확인한다.

```bash
cd ~/projects/qwq-ai-trader
git status --short --branch
```

운영 서버의 `.env`, `logs/`, `~/.cache/ai_trader/`, PID 또는 거래 상태 파일을 로컬로 복사하지 않는다.

## 2. 개발 도구 준비

다음 명령은 Git, Python 3.11 이상, Node.js, npm, GitHub CLI, Claude Code, Codex가 WSL 내부에서 실제로 실행되는지 검사한다. 기존 `venv`와 `.env`는 덮어쓰지 않는다.

```bash
cd ~/projects/qwq-ai-trader
bash scripts/dev/bootstrap.sh
```

현재 상태만 확인하고 패키지를 설치하지 않으려면 다음과 같이 실행한다.

```bash
QWQ_BOOTSTRAP_CHECK_ONLY=1 bash scripts/dev/bootstrap.sh
```

### Codex가 Windows 경로를 가리킬 때

`codex --version`이 WSL에서 실행되지 않으면 Windows 앱의 경로가 먼저 잡혔을 수 있다. [공식 OpenAI Codex CLI 문서](https://learn.chatgpt.com/docs/codex/cli)에 따라 WSL 터미널 안에서 Linux용 CLI를 설치한 뒤 새 셸을 연다.

```bash
curl -fsSL https://chatgpt.com/codex/install.sh | sh
hash -r
codex --version
```

설치 스크립트를 검토해야 하는 환경에서는 먼저 파일로 내려받아 내용을 확인한 뒤 실행한다. 부트스트랩은 이 전역 설치를 자동으로 수행하지 않는다.

## 3. 인증 상태 확인

비밀값 자체를 출력하지 말고 사용 가능 여부만 확인한다.

```bash
gh auth status
claude --version
codex --version
```

Claude Code와 Codex의 최초 로그인은 각 명령을 직접 실행해 대화형 안내를 따른다.

```bash
claude
codex
```

## 4. 환경변수

단위 테스트와 문법 검사는 `.env` 없이 실행되어야 한다. 조회용 외부 API가 필요한 작업에서만 예제 파일을 복사하고 필요한 빈칸만 채운다.

```bash
cp .env.example .env
```

`.env.example`의 `KIS_ENV=dev`는 안전한 기본 방향을 나타낼 뿐 주문 실행을 허가하지 않는다. 운영 자격증명 전체를 로컬로 복사하지 않는다.

## 5. 작업 브랜치

`main`에서 직접 개발하지 않는다.

```bash
git switch main
git pull --ff-only origin main
git switch -c feature/<작업명>
```

Claude Code와 Codex는 세션 시작 시 `CLAUDE.md`, `AGENTS.md`, `CHANGELOG.md`, `docs/README.md`와 작업 분야 문서를 먼저 읽는다. 한 에이전트가 구현한 변경은 다른 에이전트가 최종 검토하도록 역할을 분리한다.

## 6. 로컬 검증

다음 한 명령은 Git이 추적하는 Python 파일의 문법, `tests/` 테스트 모음, 고확률 비밀정보 패턴을 순서대로 검사한다.

```bash
bash scripts/dev/verify.sh
```

이 명령은 `.env`를 읽지 않으며 SSH, `systemctl`, `scripts/run_trader.py`, `scripts/liquidate_all.py`를 호출하지 않는다. 실패하면 원인을 수정하고 같은 명령을 다시 실행한다.

## 7. Push와 PR

검증이 통과한 뒤 feature 브랜치만 push하고 PR을 만든다.

```bash
git status --short
git push -u origin HEAD
gh pr create --fill
```

이 단계에서 `main` 병합은 운영 서버 배포나 재시작을 자동으로 유발하지 않는다.

## 8. Dry-run

dry-run도 외부 조회 API를 사용할 수 있으므로 사용자가 현재 작업에서 명시적으로 선택한 경우에만 수행한다. 반드시 `--dry-run`을 포함하고 실행 전 시장 인자를 확인한다.

```bash
source venv/bin/activate
python scripts/run_trader.py --market kr --dry-run
```

`--dry-run`이 없는 `run_trader.py`, `liquidate_all.py`, 운영 서버 SSH, `systemctl` 명령은 이 로컬 개발 절차에서 실행하지 않는다.

## 9. Codex 교차 리뷰

한 에이전트가 구현한 변경은 다른 에이전트가 최종 검토한다. Claude Code가 구현한 feature 브랜치는 PR 생성 전에 Codex로 교차 리뷰한다.

```bash
bash scripts/dev/codex_review.sh                # 현재 feature 브랜치 vs main
bash scripts/dev/codex_review.sh --uncommitted  # 커밋 전 변경만
```

리뷰의 읽기 전용 계약은 **프롬프트**로 강제된다(파일 수정·git 쓰기·설치·네트워크 금지, 조회 명령만 허용). 샌드박스는 기본 **read-only** 이며(운영 서버는 아래 AppArmor 프로파일로 다시 동작), 프로파일이 없는 호스트에서 `~/.codex/config.toml` 의 `sandbox_mode` 를 그대로 쓰려면 `QWQ_REVIEW_SANDBOX=` (빈 값)으로 실행한다 — 샌드박스를 켤 때는 스크립트가 먼저 1회 프로브(종료 코드·출력 분리 판정)해 bwrap 이 못 뜨면 조용히 "미검증"으로 끝나는 대신 즉시 실패한다. 샌드박스 없이 도는 경우의 완화책으로 리뷰 전후 작업 트리·HEAD 를 대조해 변경이 있으면 경고와 함께 exit 3 으로 끝낸다(격리를 대신하진 못하고 위반을 탐지한다).

> **운영 서버(Ubuntu, `kernel.apparmor_restrict_unprivileged_userns=1`)에서는 Codex 번들 bwrap 이 사용자·네트워크 네임스페이스를 만들지 못한다**(`bwrap: loopback: Failed RTM_NEWADDR: Operation not permitted`). 2026-08-02 에 config 에 `danger-full-access` 우회를 뒀으나 이 스크립트가 `--sandbox read-only` 를 하드코딩해 그것을 덮어써 8~9월 교차 리뷰가 전부 "환경 오류·미검증"으로 끝났다(2026-09-16 확정·수정). **2026-09-16 저녁 해결**: 번들 bwrap 에 `userns` 만 허용하는 AppArmor 프로파일(`scripts/ops/apparmor/codex-bwrap`, 설치 `bash scripts/ops/apparmor/install.sh`)을 운영 서버에 로드했다 — Ubuntu 가 chrome/code 에 쓰는 `flags=(unconfined) { userns, }` 패턴이고 경로는 nvm 노드 버전·플랫폼 글롭. 이후 `codex exec --sandbox read-only` 가 정상 완주(실측: 번들 bwrap 으로 명령 실행·`SANDBOX_OK`). 롤백은 프로파일 머리말 참조. Codex 가 "bubblewrap 이 PATH 에 없다"고 경고하지만 번들본을 쓰므로 무해하다(배포판 `bubblewrap` 패키지 설치는 하지 않았다). `~/.codex/config.toml` 의 `danger-full-access` 우회는 다른 Codex 세션이 진행 중이라 **그대로 뒀다** — 리뷰 스크립트는 `--sandbox read-only` 를 명시하므로 config 와 무관하게 격리된다.

지적 사항은 P0(치명적)/P1(중요)/P2(경미)로 분류된다. P0·P1을 수정한 뒤 `bash scripts/dev/verify.sh`를 다시 통과시키고 push한다. 반대로 Codex가 구현한 변경은 Claude Code가 리뷰한다. 기준 브랜치는 `QWQ_REVIEW_BASE`로 재정의할 수 있다.

## 문제 해결

- `venv`가 손상되었다면 자동 삭제하지 말고 먼저 이름을 바꾸어 보존한 뒤 부트스트랩을 다시 실행한다.
- 전체 저장소에서 `pytest`만 실행하면 `scripts/test_new_tr.py`의 수동 KIS 진단 함수가 수집될 수 있다. 표준 검증은 외부 API 진단을 제외하기 위해 `tests/`만 실행한다.
- 인증 오류가 나면 토큰 값을 화면에 출력하지 말고 `gh auth status`, Claude Code 또는 Codex의 로그인 흐름으로 갱신한다.


## CLI collaboration

2026-10-24 사용자 요청: 다시 로그인한 Claude도 개발·운영 준비·배포 검토·독립 리뷰에 활용한다. 엔진 LLM API를 호스트에서 사용 가능한 CLI로 대체하는 안은 별도 타당성 검토다. 작업 상태는 [실행 원장 D01/L01](execution-roadmap.md#d01l01)을 따른다.

### 확인한 실행 환경

- 현재 도구가 실행되는 호스트에서 Claude Code 2.1.310의 `auth status`는 `loggedIn=true`, `authMethod=claude.ai`, Max를 보고했고, Codex CLI 0.164.0은 ChatGPT 로그인을 보고했다. 계정 이메일·인증 토큰은 출력하거나 복사하지 않았다. 별도 원격 컨테이너에 SSH해 검증한 것으로 주장하지 않는다.
- Claude 개발 설계 검토 1회: `claude-opus-5` / high 요청, 실제 assistant 응답 및 terminal `modelUsage`에서 `claude-opus-5` 확인. 실효 effort는 미노출이다. 도구0·MCP0, 프로세스 exit0·terminal success, 76.610초. 이는 이번 설계 검토 시간이며 거래 판단의 대표 지연이나 p95가 아니다.
- CLI 표시 비용은 약 $0.19739의 추정값이며 구독의 실제 증분 청구액은 미확인이다. 실제 비용을0이나 이 추정값으로 확정하지 않는다. 기존 엔진 USD100 원장에 개발 검토 비용을 임의 합산하지 않았다.
- 검토는 비공개 작업 디렉터리의 stdin 자료만 사용했고 `--safe-mode`, `--tools ""`, 빈 strict MCP, `--no-session-persistence`, 180초 절대 제한·90초 모델 진행 제한·$1.50 표시 비용 상한을 사용했다. 진행 이벤트는 소비하되 숨은 추론 내용은 저장하지 않았다. safe mode가 지침을 생략하므로 호스트의 승인된 라우팅 정책을 명시적으로 전달했다. 운영 설정·예약·엔진 코드 변경은 없다.
- 비공개 근거: `~/.local/state/qwq-task-audit/20261024/cli-agents/`의 `claude-review-receipt.json`, `claude-review.md`, `engine-code-audit.md`. 실제 요청 비용·추가 사용료 설정·남은 구독 한도는 이 시험으로 확정하지 않았다.

### 개발·운영 작업의 역할 분담

| 작업 | 기본 역할 | 실행·검증 책임 |
|---|---|---|
| 복잡한 설계·재현·통합 | Codex Astra/high 또는 검증된 Claude Opus/high | coordinator가 범위와 근거를 정하고 통합 |
| 범위가 명확한 구현·테스트 | 기본 Terra/medium~high; 난도가 높은 독립 묶음은 Claude Opus | 병렬 작성자는 같은 base의 별도 worktree와 겹치지 않는 파일 사용 |
| 일반 분석·문서 검토 | 기본 Sol/high, 기계적 정리는 Luna | 무조건 두 모델에 같은 일을 반복시키지 않음 |
| 주문·권한·원장·배포 관련 최종 리뷰 | 작성자와 다른 검증된 Claude Opus 또는 Codex Astra, high/xhigh | 실제 모델·폴백·테스트 증거 확인; 사용자 승인된 독립 Codex 대체도 유효 |
| 운영 장애 분석·배포 준비 | 한쪽이 원인/패치를 만들고 다른 쪽이 검토 | 비식별 자료로 분석; 실제 설치·재시작·롤백은 coordinator가 기존 검증 스크립트와 해당 작업의 사용자 권한으로 수행 |

호스트 정본은 `/home/ubuntu/.config/ai-agents/model-routing.md`다. coordinator1명+worker최대3명은 Codex/외부 Claude 합산이며, worker의 추가 fan-out을 허용하지 않는다. 별도 사용자 세션을 중단하거나 재사용하지 않는다. worktree는 보안 격리가 아니므로 Claude의 도구 사용 구현은 개발 샌드박스가 검증된 경우에만 맡긴다. 그렇지 않으면 이번처럼 도구 없이 정제한 소스/자료를 전달해 패치 제안·리뷰를 받는다. 이번 호출 성공은 도구 사용 샌드박스 검증 완료가 아니다.

### 엔진 API를 CLI로 대체할 때의 판단

CLI도 클라우드 모델을 호출한다. 설치된 실행 파일을 쓴다고 로컬 추론이나 무제한 무료 호출이 되는 것은 아니다.

| 후보 | 현재 판단 | 이유·기존 계약 |
|---|---|---|
| 개발·독립 리뷰·운영 준비 | 지금 활용 | 호출 결과를 coordinator가 검증하는 작업이며 이번 Claude 호출 성공 |
| 기존 장후 연구의 별도 재생 | 제한된 비교 후보 | `POST_EXIT_RESEARCH`는 gpt-6.1-sol/medium, 출력4096·90초·one-shot. 기존 LLM을 실제 사용하는 이 역할을 우선 비교; 자동 보고 발송·Wiki·전략 반영 없이 운영 밖에서 평가 |
| 뉴스·테마/레짐·배치 선정 | 아직 대체하지 않음 | 텍스트 요약처럼 보여도 점수·후보·레짐에 영향을 줌. 자료 신선도와 실패 동작을 함께 검증해야 함 |
| 진입·장마감 포지션 판단 | API 유지 | 진입8~12초, 포지션15초·SELL/trailing 영향. CLI 기동·인증·쿼터·취소·자식 프로세스·응답 시각 계약이 미검증 |
| 기존 숫자/도표 보고 | CLI를 추가해 절감으로 세지 않음 | 결정론적 보고는 애초 LLM 호출이 없을 수 있어 추가 호출은 비용 증가 |

코드 근거: `src/utils/llm.py`의 `LLMResponse`/`complete`/`complete_with`/`complete_role`, `src/utils/llm_budget.py:reserve`, `src/utils/llm_roles.py:ROLE_PROFILES`, `src/analytics/llm_decision_record.py`, `src/analytics/post_exit_review.py:_generate_llm_insights`, `src/core/adversarial_validator.py`, `src/schedulers/kr_scheduler.py`의 진입 검증·장마감 포지션 점검. 확인한 것은 소스 계약이며 실제 운영 호출량/지연 분포를 이번에 측정하지 않았다.

전송 경로를 바꾸더라도 원문·입력 hash, requested/resolved model, 요청/응답/소비 가능 시각, 각 시도, JSON/종료 상태, 토큰·과금 확인 여부를 보존해야 한다. `decision_available_at`은 CLI 프로세스 시작이나 마지막 출력 시각이 아니라 검증된 판단을 소비할 수 있게 된 시각이다. 문자열에서 최종 답만 추출해 기존 API 응답으로 가장하지 않는다. CLI 종료가 원격 요청 중단·과금0을 증명하지도 않는다.

### 공식 지원 범위와 비용 해석

- [Codex 비대화형 실행](https://learn.chatgpt.com/docs/non-interactive-mode)은 `codex exec`, JSONL·출력 schema, 저장된 로그인 재사용과 자동화를 지원한다. 공식 문서는 자동화의 기본 인증으로 API key를 권장한다. [인증](https://learn.chatgpt.com/docs/auth)·[가격/한도](https://learn.chatgpt.com/docs/pricing)에서 구독 인증과 API 과금을 구분하며, CLI 개발 작업과 엔진이 같은 계정의 사용량을 나눠 쓸 수 있다. 지원 사실은 엔진의 지연·가용성 보증이 아니다.
- [Claude 프로그램 실행](https://code.claude.com/docs/en/headless)은 `claude -p`·구조화 출력을 지원한다. 현재 `--bare`는 구독 OAuth를 읽지 않는다. 인증을 유지하는 `--safe-mode`도 도구 차단을 대신하지 않는다. 이번에는 별도 도구/MCP 비활성화와 응답 metadata로 확인했다. CLI 버전 변경 때 이 조합을 다시 확인한다.
- [Claude 인증/이용 범위](https://code.claude.com/docs/en/legal-and-compliance)는 본인 계정으로 수정하지 않은 CLI를 사용하는 경우와 타인을 위한 제품/서비스가 구독 자격을 중개하는 경우를 구분한다. 본인 개발 CLI 자동화를 일괄 금지라고 해석하지 않으며, 제품/상시 엔진에 내장하는 방식은 실제 사용 형태에 맞는 인증·계약을 따로 확인한다. OAuth 토큰 추출·다른 API 엔드포인트로의 재사용·한도 우회는 하지 않는다.
- [Claude 비용 안내](https://code.claude.com/docs/en/costs)는 구독 사용량과 표시된 토큰 정가 추정액·실제 추가 사용료를 구분한다. 비교표에는 구독 고정비/증분 사용료/공유 쿼터/개발 작업 경합을 별도로 남긴다. CLI $0 표시나 기존 구독 보유만으로 경제적 비용0을 주장하지 않는다.

기존 사용자 방침인 `missing_cost_policy=log_and_continue`, 실제 비용 `null`은 그대로다. Claude 리뷰의 “실제 비용 미확정이면 전면 중단” 권고는 이 방침과 충돌하므로 채택하지 않는다. 새 CLI 경로를 도입한다면 별도 한도·알림·호출 수/시간 제한을 두고 불명 비용을 기록하며, USD100 API 원장을 우회하거나 예산을 몰래 늘리지 않는다. 유료 API 폴백은 필요성이 확인된 역할에서 기존 예산 예약을 통과한 경우에만 별도로 설계하며, 현재 one-shot 역할에 폴백을 임의 추가하지 않는다.

### 다음 선택과 최소 검증 범위

권고는 개발 협업부터 적용하고 엔진은 단계적으로 비교하는 혼합 방식이다. 전면 API→CLI 교체와 무조건 CLI 금지 모두 현재 근거보다 강한 결론이다. 이번에는 엔진 adapter·타이머·유료 R1·보고 발송을 구현/활성화하지 않는다. T03/T04→T06→E01의 주 과제는 유지한다.

향후 L01 후속을 선택하면 비식별 고정 `POST_EXIT_RESEARCH` 입력1건으로 framing·schema·실제 모델·종료/늦은 결과 배제를 먼저 확인한다. 이 한 건은 수익이나 지연 분포의 증거가 아니다. 이어 제한된 소수 표본으로 오류·출력 적합성·개발 쿼터 경합을 점검하되, 5건 정도로 안정적인 p95/SLA를 입증했다고 하지 않는다. 확대 전에 비교 입력/프롬프트·반복 수·시간/호출 상한·품질 기준을 정하고, 신뢰할 자료가 모였을 때만 동일 자료의 API/CLI 품질·지연·비용을 비교한다. 별도 API 비교는 기존 유료 실험 승인/예산 조건을 따른다.

도구 실행 시도·예상 밖 모델·출력 무효·인증/쿼터 실패·반복 timeout·시간 상한 초과는 해당 실험을 멈추고 원인을 기록한다. 안전 거절을 다른 모델로 우회하지 않는다. 운영 엔진은 기존 API 경로를 유지하므로 실험 중단이 매매나 보고 장애로 전파되지 않는다. 거래 판단·Wiki·보고 본문으로 연결하려면 이 오프라인 확인과 별도의 도입 결정이 필요하다.
