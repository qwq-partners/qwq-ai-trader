# Claude 인계 — 브랜치 정리와 엔진 마이그레이션 (2026-09-20)

## 먼저 읽을 결론

운영 기준은 main, 계속 개발할 정본은 **`feature/engine-safety-design-20260917`** 한 개다. C4 제품 기준은 **`ab044c4edeb702911fee998973cb00a263a7b085`**다. 추가 확인 없이 정리하라는 후속 지시에 따라 다른 로컬54/원격7 branch heads와53개 worktree는 **복구 가능한 archive로 전환**했다. 최종 상시 branch/worktree는 main·engine 두 개이며 임시 문서 PR 작업공간은 병합 후 제거한다. 다른 worker를 다시 활성화하거나 중복 구현하지 않는다.

**전체 엔진의 main 병합·운영 전환은 아직 승인 가능한 상태가 아니다.** C4 한정 통과를 전체 마이그레이션 완료로 확대하지 않는다. 이번 정리는 개발선을 폐기하거나 거래 안전 장벽을 해제하지 않았다. 사용자가 요청한 다음 개발·설계는 이 문서를 읽은 새 Claude 세션에서 이어간다.

## 작업 장소와 기준

- 운영 checkout: `/home/ubuntu/projects/qwq-ai-trader` (main).
- 엔진 작업공간: `/home/ubuntu/projects/qwq-ai-trader/.claude/worktrees/engine-safety-design-20260917`.
- 1차 감사 시 main `465a029`, engine `ab044c4`(+18/-0). PR #77 이후 문서-only 동기화 기준은 main `4222f7442a63ded50dc2d96a3840a0f8acb041e6`, engine `792a4988c87049615bdda3689852f7b3acc0ac04`다. 이번 archive 인계도 문서만 추가한다. 현재 SHA는 Git으로 재확인하며 뒤처진 main 코드를 재구현하지 않는다.
- 이 인계 문서의 main 병합 이후에는 **문서만 바뀐 main을 engine에 merge**해 인계 문서도 가져온다. source/test 트리와 `ab044c4`의 차이를 대조한다. 강제 push/rebase로 기존 검토 이력을 바꾸지 않는다.
- 복구 정본: [추가 archive 보고서](../reviews/retired-workspace-archive-2026-09-20.md). dirty28·staged12개를 포함한53개 원본과 전체 Git 객체/index는 `/home/ubuntu/projects/qwq-retired-workspaces-20260920.c2dhUU`에 있다. **원래 worker 경로는 더 이상 존재하지 않으며 archive의 `.git` 포인터도 직접 사용하지 않는다.** 보고서의 복구 절차를 따른다. [1차 정리 보고서](../reviews/branch-consolidation-2026-09-20.md)의 보존 목록은 역사 기록이다.
- 이 문서보다 실제 Git 상태를 우선한다. 시작 직전 `git status --short`, `git log -5 --oneline`, `gh pr list`로 다른 세션 변경을 확인한다.

## 운영에 반영한 범위와 유지한 것

2026-09-20 10:36 KST, 기존 checkout `8ff2f55`를 이미 병합·별도 설치된 토스 서비스 기준 main `465a029`로 fast-forward했다. 변화는 별도 observer 모듈/설치 도구/테스트/문서이며, 기존 거래 엔진·스케줄러·브로커·위험·Toss provider·run_trader·설정·의존성 경로의 diff는 0이었다. **거래 프로세스의 새 코드 배포나 새 엔진 활성화를 했다는 뜻이 아니다.** 인계 문서 PR 이후 main이 더 전진하더라도 문서만 추가된다.

- 거래 봇 PID **3534327**, 시작 **09-19 06:14:40 KST**. 이는 이번 작업 이전의 시작이며 원인/당시 실행 SHA를 재구성했다고 주장하지 않는다. checkout SHA와 프로세스 시작 SHA를 혼동하지 않는다.
- Toss 별도 서비스 PID **3335469**, 시작 **09-17 21:47:37 KST**, active 유지. 봉인 릴리스는 기존 `877768e` 기록을 따르며 이번에 재설치/재시작하지 않았다.
- 10:36 재확인: broker connected, broker/risk pending 0, stale loops 0. 설정3파일·킬스위치4경로 지문 불변. 주문·계좌·잔고는 계속 **KIS 단독**이고 Toss는 관측 전용이다.
- 기존 거래 소스에 적용할 변경이 없고 Toss는 같은 grant로 재시작하면 안 되므로 **서비스 재시작0**. 신규 발급·실 API 검증·설정 변경도 하지 않았다.
- 읽기 전용 최근2시간 집계: ERROR/Traceback0, EGW00215/토큰 오류0, EGW00201 12건. 게이트웨이 경고 원인은 미확정이며 이번 정리에서 고쳤다고 보고하지 않는다.
- 승인된 Toss 관측은 **09/18·09/21·09/22**, **09/22 18:00 KST 만료** 그대로다. 미래 날짜를 완료 처리하거나 기간을 자동 연장하지 않는다. 이후 표본/만료/락 반환 인수는 [관측 보고서](../reviews/toss-observer-service-2026-09-17.md)와 [점검표](monitoring-checkpoints.md)를 따른다.

## 완료된 개발과 아직 미완인 개발

아래 engine 문서는 **engine 브랜치에만 존재**한다. main에서 상대 링크가 깨지지 않도록 C4 고정 커밋 링크를 사용했다.

| 영역 | 현재 판정 | 정본 |
| --- | --- | --- |
| legacy 조회·실제 limiter/응답 어댑터 | 오프라인 실제 broker 연결 시험·한정 리뷰 완료 | [후속 진행 원장](https://github.com/qwq-partners/qwq-ai-trader/blob/ab044c4edeb702911fee998973cb00a263a7b085/docs/reviews/engine-execution-followup-2026-09-18.md) |
| 요청·예약·경제·보호·core receipt·복구·outbox ACK·계좌 lease/drain | 구현 조각별 인수 완료, 전체 운영 설치 아님 | 같은 진행 원장 |
| 정책 generation·입력 변경 전파·지속 source 권한 | 실제 결함 RED→수정·재리뷰 완료 | [source 권한](https://github.com/qwq-partners/qwq-ai-trader/blob/ab044c4edeb702911fee998973cb00a263a7b085/docs/reviews/source-authority-followup-2026-09-20.md) |
| 실제 2분 레짐 루프 | owner 분기·버전/결측/저장 경계 한정 완료 | [2분 루프](https://github.com/qwq-partners/qwq-ai-trader/blob/ab044c4edeb702911fee998973cb00a263a7b085/docs/reviews/two-minute-regime-owner-2026-09-20.md) |
| C3 정오·JSON LLM·보호 replay | 실제 스케줄러 인수·한정 승인 완료 | [C3](https://github.com/qwq-partners/qwq-ai-trader/blob/ab044c4edeb702911fee998973cb00a263a7b085/docs/reviews/noon-regime-protection-replay-2026-09-20.md) |
| C4 장전 text diagnosis·소비자 정합성 | actual caller·성공일 dedupe·정책/표시·게시 실패 장벽 완료 | [C4](https://github.com/qwq-partners/qwq-ai-trader/blob/ab044c4edeb702911fee998973cb00a263a7b085/docs/reviews/morning-regime-owner-2026-09-20.md) |
| qualification·최종 sizing·실제 SIGNAL/ORDER | **다음 시작점**, request-bound 전체 연결 미완 | [10B2/10B3](https://github.com/qwq-partners/qwq-ai-trader/blob/ab044c4edeb702911fee998973cb00a263a7b085/docs/superpowers/plans/2026-09-18-engine-writer-migration.md) |
| factory·나머지 writer/sender·공식 증거·전체 인수 | 미완, 운영 전환 차단 | 같은 계획 10A3/10C와 아래 순서 |

브랜치 전체는 166파일 +44,697/-389줄이며 **전부 비활성 코드가 아니다**. WS 46필드 parser, 공용 limiter, 기존 sizing/regime wrapper와 DB DDL도 바뀐다. 따라서 owner가 아직 자동 설치되지 않는다는 이유로 통째 merge/restart하지 않는다. 실제 `KRExecutionRuntime.trading_ready`는 False, factory 자동 생성/attach는 미배선이며 임의 attach는 legacy SIGNAL/ORDER/FILL을 차단한다.

## 다음 세션의 Plan → Do → See

### 1. B2/B3 — 실제 요청에 qualification·최종 sizing 연결 (최우선)

**Plan**

1. engine 작업공간의 `CLAUDE.md`, 최근 `CHANGELOG.md`, `docs/README.md`, 위 writer 이행 계획 10B와 C4 보고서를 읽는다.
2. `src/core/engine.py`의 실제 `on_signal/on_order`와 같은 파일 `RiskManager._calculate_position_size`, `src/execution/safety/`의 command/runtime/정책 소비 경로를 다시 찾아 파일별 소유권을 고정한다. 별도 `src/risk/manager.py` 클래스와 혼동하지 않는다. 경로/함수는 수정 전에 `rg`로 확인한다.
3. CV·LLM·시간 규칙·시장 자료·sector·손절·score·정책 version을 immutable decision facts로 묶는 계약과 갱신 주체를 정한다. 현재 allow bool이나 broker 체결 metadata를 원 판단의 증거로 대신 쓰지 않는다.
4. 구현, 독립 실제 큐 인수, 최종 리뷰를 분리한다. 같은 파일을 동시에 수정하지 않는다.

**Do**

1. 실제 EventBus/큐/on_signal/on_order/SQLite/Portfolio/RiskManager를 사용하고 외부 HTTP·시계만 합성한 실패 시험부터 추가한다.
2. CV·LLM·현금·다른 예약·정책이 network await 중 바뀌면 stale/reject 및 **POST0**이 되는지 고정한다. 무관 fill/ACK가 무조건 전역 stale을 만들지 않는 대조도 둔다.
3. 정상 MARKET BUY·LIMIT/MARKET SELL을 보존한다. 실제 request의 수량·가격·계좌·origin·부모와 prepare/claim/final을 결합하고, 동일 pure sizing kernel로 최종 경제/예약을 재검사한다.
4. UNKNOWN 예약 유지, 취소 ACK≠최종성, 중복 dispatch0, 부분체결·늦은 응답·caller cancel·SQL/게시 실패·새 runtime restore를 확인한다. 강제 `trading_ready=True`나 합성 startup 허가로 운영 장벽을 우회하지 않는다.

**See**

- 실제 정상/거부 대조·RED/GREEN·독립 spec/quality 리뷰와 exact SHA를 기록한다.
- 전체 UTC/KST는 **직렬** 실행하고 격리 위반0·문법·비밀정보 검사를 통과한다.
- “순수 helper 통과”, “raw writer 차단”, “모델 합의”를 정상 거래 파이프라인 이행 완료로 세지 않는다.

### 2. 10A3/10C — 단일 owner와 남은 writer/sender

B2/B3의 정상 거래 연결 후에 명시 factory와 프로세스 종료 순서를 묶는다. scheduler SAFE/USER, KOFR, 별도 수동 CLI/IPC, broker/effect 송신, WS quote, fill 수집→지원 증거→core receipt, sync·면제·초기 인계·일일 reset을 **한 경로씩** 이행한다.

각 경로의 현재 writer 검색 목록, 성공/실패 실제 caller 시험, 제거된 중복 경제·보호·원장 쓰기를 남긴다. IPC가 없으면 raw broker fallback하지 않는다. 장부 snapshot 부재/빈 응답/timeout은 예약 해제나 초기 인계 증거가 아니다.

### 3. 공식 증거 — 코드와 별도 작업

[공식 근거 요청표](https://github.com/qwq-partners/qwq-ai-trader/blob/ab044c4edeb702911fee998973cb00a263a7b085/docs/integrations/kis-execution-evidence-request-2026-09-18.md)의 최초 잔고/체결 cutoff 또는 검증된 재조정, 취소 최종성, 원주문/자식 누적량 의미, 다중 페이지 일관성을 공식 명세나 승인된 비식별 실응답으로 확인한다.

증거가 없으면 **미지원/시작 차단 유지**다. 합성 시험은 외부 계약 증명이 아니다. **모든 MODIFY는 현재 미지원**이며 수량/가격 증가분의 실제 추가 예약·위험 상한 인수 없이 지원 상태로 바꾸거나 송신하지 않는다.

### 4. 최종 통합 판정

실제 전체 C/F/G/R, 남은 legacy 분석 원장 projection, 과거일 회계, 장기 이력/부하 시험 및 독립 broad 리뷰 이후 main/운영 전환을 별도로 판정한다. 사용자 목표는 **기존 위험 한도 유지 + 비용 차감 KODEX200 초과수익 검증**이며 아키텍처 안전성 통과가 투자 엣지 증명은 아니다.

### 5. 독립 운영 수정 후보는 별도 release로

`bdda0e9`의 limiter 취득별 lease·GET finally 정리는 별도 운영 개선 후보이나 커밋 전체가 safety collector에 의존한다. 필요하면 main 기준에서 관련 hunk만 추출하고 취소/timeout/late-response·기존 dict GET·US/unscoped 호환 인수와 독립 리뷰를 새로 한다. 이번에 추출·배포한 것으로 보고하지 않는다. `scripts/dev/claude_review.py`는 개발 도구이며 그 배포 때문에 거래 봇을 재시작할 이유가 없다.

## 모델 배정·실패 처리

- 전역 정본: `/home/ubuntu/.config/ai-agents/model-routing.md` (`ai-routing-v1-2026-09-20`).
- 일반 구현/테스트 Terra medium~high, 다중파일 감사 Sol/high, 핵심 상태·돈 경로 구현 Astra/high 또는 검증된 Opus/high, 독립 최종 critical 리뷰는 구현자와 다른 Astra/Opus xhigh. 문서/기계적 확인 Luna low~medium.
- coordinator1+worker3 **전체 공급자 합산** 상한. 별도 worktree·동일 base·파일당 단일 작성자·worker fanout0.
- Fable은 실제 Opus fallback이 관찰돼 자동 배정 제외다. Sonnet/Haiku 가용성을 검증됐다고 가정하지 않는다. 실제 모델 미노출은 요청값과 구분한다.
- Claude가 구현한 브랜치의 Codex 교차 리뷰는 `bash scripts/dev/codex_review.sh`의 read-only 경로를 사용한다.
- 외부 Opus 정적 리뷰는 engine 브랜치의 `scripts/dev/claude_review.py`를 사용한다. tools-off·명시 세션·한정 입력, 모델/result 검증, startup/idle/absolute deadline을 유지한다. 900초 제한을 숨기거나 무제한 대기로 바꾸지 않는다.
- 긴 리뷰의 timeout은 승인도 모델 장애 증명도 아니다. 작은 대조→범위 분해→실제 지적 재현/수정/재리뷰 순으로 처리한다. C4의 실패와 최종 승인은 별도 원장에 남아 있다.
- 다른 세션·전역 auth/config·운영 자격·토큰을 수정하지 않는다.

## 재현 명령 (오프라인)

engine 작업공간에서 실행한다. 외부/운영 자격을 전달하지 않고 프로젝트 테스트 가드를 유지한다.

```bash
cd /home/ubuntu/projects/qwq-ai-trader/.claude/worktrees/engine-safety-design-20260917
git status --short
git log -5 --oneline
git diff ab044c4 -- src scripts tests config requirements.txt
env -i PATH=/usr/bin:/bin LANG=C.UTF-8 TZ=UTC \
  PYTHONDONTWRITEBYTECODE=1 PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 \
  QWQ_DEPLOY_SSH_KEY=/tmp/qwq-offline-no-ssh-key \
  /home/ubuntu/projects/qwq-ai-trader/venv/bin/python -m pytest \
  -q -p no:cacheprovider --tb=short tests
```

다음 실행은 같은 명령의 `TZ=Asia/Seoul`로 **앞 suite 종료 후** 수행한다. C4 원 완료 기록은 양TZ 각각4403passed/기존xfail2다. 새 변경 후에는 이 숫자를 복사하지 말고 실제 결과를 기록한다.

## Claude에게 전달할 시작 문장

> 이 문서와 engine 브랜치의 최신 writer 이행 계획을 읽고, 기존 C4 완료 부분을 재구현하지 말아 주세요. 먼저 현재 HEAD/dirty 상태/다른 PR을 확인하고 B2/B3의 실제 qualification·최종 sizing request-bound 인수를 RED부터 진행해 주세요. Plan→Do→See, 역할별 모델·격리 병렬·독립 리뷰를 유지해 주세요. main 전체 병합·배포·재시작·주문·설정/토스 grant 변경은 이번 인계만으로 실행하지 마세요. 공식 startup·취소 증거와 전체 C/F/G/R이 미완이면 차단을 유지하고, 완료/미완/실제 검증을 구분해 보고해 주세요.
