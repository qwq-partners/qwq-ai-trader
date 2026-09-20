# B2/B3 단계 원장 — request-bound qualification·최종 사이징

> 이 문서만 읽고 **리뷰하거나 이어받을 수 있게** 단계마다 같은 틀(Plan / Do / See / 체크리스트 / 잔여 / 다음 진입 조건)로 적는다.
> 계약·단계 정의의 정본은 `docs/superpowers/plans/2026-09-20-b2b3-request-bound-qualification.md`, 상위 인계는 `docs/operations/claude-migration-handoff-2026-09-20.md` §1 이다.
> **구분 규칙:** 구현 완료 ≠ 검증 완료 ≠ 운영 승격 · 요청 모델 ≠ 관측 모델 · 합성 시험 ≠ 실경로 검증. 모든 GREEN 은 fake HTTP + 주입 시계 + 시험용 합성 startup 허가(`monkeypatch` `trading_ready`) 위의 결과다.
> **운영 경계(전 단계 공통):** main 병합·배포·재시작·주문·설정·Toss grant 변경 없음. 제품 코드의 `trading_ready` 는 계속 False, MODIFY 미지원. 현금 고갈(펩트론 99.6%)로 정상 BUY 실경로 표본 0건.

## 상태 요약

| 단계 | 범위 | 상태 | 통합 SHA |
|---|---|---|---|
| Plan | 조사 3관점 + 계약·단계 고정 | 완료 | `47fa76b`·`03cc2d6`·`1f8e3ad`·`e8054b0` (문서만) |
| S1 (B2a) | facts DTO·게시 2종·final kernel 재검사 | **완료 — 한정 승인·운영 미설치** (실제 publisher·gateway 없음, 합성 시험 한정) | `01362db`(merge) + `78ca94f` |
| S2 (B2b) | 실제 CV/LLM/시간 규칙 publisher — 하위 S2-1~S2-5, wave A(S2-1∥S2-2)→B(S2-3∥S2-4)→C(S2-5)→통합 수정 | **완료 — 한정 승인·운영 미설치** (제품 소비자 0건: 게시·prepare·dispatch 호출은 S3. 실효 stale 축은 regime 1개. 세부 계획 `docs/superpowers/plans/2026-09-20-s2-qualification-publishers.md`) | `7177a8d`·`9330fbe`·`6f9108a`·`4829200`·`d703b34`·`072c51e`·`44e543a`(merge) + `6fa7fe5`·`578dc80`·`51a71e0`·`4018b79`·`83baa2a`·`85a65bc` |
| S3 (B3a) | SIGNAL→gateway→owner prepare/dispatch — 하위 S3-1~S3-6b, wave 1(S3-1∥S3-2)→2(S3-3∥S3-4)→3(S3-5)→4(S3-6a)→5(S3-6b) | **완료 — 한정 승인·운영 미설치**(2026-09-21). 제품에 `KRExecutionRuntime` 생성·`attach()`·`install_gateway()`·`recover_unsent()` 호출자 0건, `trading_ready=False` 그대로. HEAD `10d2ca7` 전체 UTC/KST 각 4758 passed. Codex 1차 APPROVE·2차/3차 CHANGES_REQUIRED → 처분(3차 처분은 Codex 미재확인 — S5 범위). 세부 계획 `docs/superpowers/plans/2026-09-21-s3-signal-gateway.md` 의 각 단계 "통합된 실제 인터페이스"가 구현 뒤의 정본 | merge `0975624`·`2c24aca`·`c48cb68`·`e42b014`·`cb1f554`·`5dac8da`·`4242b70` + coordinator `1a6e6d2`·`9a3fe19`·`9572ed2`·`b83b6e7`·`4356d43`·`aee69e3`·`9688d1d`·`486c7d3`·`10d2ca7` |
| S4 (B3b) | attach 에서 되살릴 수 있는 것만 owner 경로로 — 하위 S4-0(legacy 세 경로 특성화)·S4-1(미claim 자식 종료)·S4-1b(`_unsent`)·S4-2(eviction) | **Plan 완료(2026-09-21, 기준 `95029fe`)** — 세부 계획 `docs/superpowers/plans/2026-09-21-s4-owner-path-restoration.md`. **범위 축소:** 취소 최종성 증거가 제품에 없어 90초 SELL 에스컬레이션·10분 BUY 취소·owner 취소 배선은 attach 미지원으로 명시. Do 는 wave 1 부터 | — |
| S5 (See) | 독립 실큐 인수·최종 broad 리뷰·전체 직렬 | 미착수 | — |

## 공통 작업 방법 (이어받는 에이전트가 먼저 읽을 것)

- **작업 장소:** `/home/ubuntu/projects/qwq-ai-trader/.claude/worktrees/engine-safety-design-20260917`, 브랜치 `feature/engine-safety-design-20260917`. 세션이 다른 worktree 에서 시작되면 파일 쓰기가 거부된다 → `EnterWorktree(path=<위 경로>)` 로 전환(우회 금지). 격리 세션은 복합 git 명령(`&&`, `$(...)`, `-C`, 루프)을 거부하므로 단순 명령을 `;` 로 나열한다. cwd 가 하위 디렉터리에 남을 수 있어 경로는 `:/` 루트 pathspec 이 안전하다.
- **오프라인 시험 명령(접두):**
  `env -i PATH=/usr/bin:/bin LANG=C.UTF-8 TZ=UTC PYTHONDONTWRITEBYTECODE=1 PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 QWQ_DEPLOY_SSH_KEY=/tmp/qwq-offline-no-ssh-key /home/ubuntu/projects/qwq-ai-trader/venv/bin/python -m pytest -q -p no:cacheprovider --tb=short <파일…>` — KST 는 `TZ=Asia/Seoul`. 마지막 줄 위의 `[테스트 격리] … 0건` 을 함께 확인한다.
- **S1 대상 10파일:** `tests/test_execution_decision_facts.py tests/test_execution_command_owner.py tests/test_execution_command_flow.py tests/test_execution_command_shutdown.py tests/test_execution_policy_generations.py tests/test_execution_policy_generation_acceptance.py tests/test_execution_market_source.py tests/test_execution_resources.py tests/test_position_sizing_kernel.py tests/test_execution_sizing_characterization.py` (S1 이전 기준선: 앞 파일 제외 9개 339 passed)
- **호스트 자원이 병렬도의 실제 상한이다:** 이 호스트는 운영 서버(거래 봇·Toss observer 상주)이며 2 vCPU·RAM 3.8GB, 2026-09-20 15:18 KST 관측 가용 871MB·스왑 2GB 100% 사용. pytest 를 도는 에이전트는 동시 2명 이하, **전체 suite(UTC→KST)는 다른 worker 가 없을 때 단독 직렬**로 돌리고 시작 전 `free -m`·`uptime` 을 본다. 다른 세션의 장기 프로세스는 건드리지 않는다. **"단독"은 읽기 전용 에이전트도 포함한다(2026-09-21 실측):** 전체 suite 와 pytest 를 돌지 않는 조사 에이전트 3명을 겹쳐 돌렸더니 `tests/test_toss_client_boundary.py::test_expired_token_issuance_still_requires_remaining_retry[prefix2-1]` 1건이 실패했다 — 이 시험은 `RequestBudget(1)` 의 **실시간 1초 예산**(`rate_limit.py` 기본 `clock=time.monotonic`)을 쓰고 실제 토큰 발급(파일 잠금)을 그 안에 끝내야 해서 스왑 100% 호스트의 부하에 민감하다. 단독 재실행은 73 passed. 부하로 오염된 실행은 증거로 쓰지 않고 다시 돌린다. **호스트는 다른 사용자 세션과도 공유된다(같은 날 2차 실측):** 다른 Claude 세션이 자기 worktree 에서 pytest 를 도는 동안 돌린 전체 suite 는 426초(평소 ~290초)·`test_execution_account_lease.py` 의 프로세스 spawn·fd 시험 2건 실패(단독 62 passed)로 끝났다. 전체 suite 는 시작 전과 끝난 뒤 `uptime` 의 load average 를 함께 기록하고, 실행 시간이 평소보다 크게 길거나 무관한 파일이 실패하면 `ps -eo etimes,pcpu,pid,ppid,args --sort=etimes` 로 다른 세션의 pytest 를 확인한 뒤 조용해지면 다시 돌린다. 다른 세션은 건드리지 않는다.
- **하위 에이전트 worktree:** 수동 `git worktree add` + worker `EnterWorktree` 는 실패한다(Bash 격리 고정점이 부모 worktree 에 남음 — S1 1차 시도 커밋 0). Workflow `agent(..., {isolation:'worktree'})` 로 harness 가 만든 worktree 에서 시작해, 그 안에서 `git switch -c work/<이름> <base SHA>`(구현) 또는 `git switch --detach <SHA>`(리뷰)로 기준을 맞추고 **기준선 시험을 첫 단계**로 돌리게 한다. detached 로 바꾼 리뷰 worktree 는 자동 회수되지 않으므로 끝난 뒤 `git worktree remove <경로>`(dirty 면 스스로 거부)로 정리한다.
- **교차 provider 리뷰(Codex):** `scripts/dev/codex_review.sh` 는 모델·effort 를 지정하지 않아 전역 설정(astra/medium)을 따르고 기준이 main 이면 engine 전체가 범위가 된다. 단계 리뷰는 플러그인 companion 으로 돌린다:
  `node /home/ubuntu/.claude/plugins/cache/openai-codex/codex/1.0.6/scripts/codex-companion.mjs task --background --model gpt-6-astra --effort xhigh "<프롬프트>"` — `--write` 가 없으면 read-only sandbox. 완료 통지가 오지 않으므로 `status <id> --json`, `result <id>` 로 확인한다. 범위는 프롬프트에 `git diff <base>..<head>` 와 `git show <head>:<경로>` 로 명시하고, 실행 전후 `git status --short`·HEAD 를 대조한다.
- **⚠️ Codex 작업은 세션이 끝나면 죽는다(2026-09-20 실측 3회).** Codex 플러그인의 `SessionEnd` 훅(`scripts/session-lifecycle-hook.mjs`의 `cleanupSessionJobs`)이 이 세션에 등록된 실행 중 Codex 작업을 프로세스 트리째 종료하고 브로커도 내린다(`Stop` 훅은 안내만 한다). 사용자가 자리를 비운 사이 턴이 끝나 세션이 종료 처리되면 `--background` 작업이 시작 약 100초 만에 죽고, **종료 상태를 남기지 못해 `status` 가 계속 `running` 으로 보인다**(1차는 3시간 방치). harness 백그라운드 Bash 로 companion 을 포그라운드 모드로 돌려도 같은 지점에서 `exit 0` 으로 끊겼다 — companion 에 등록된 작업이면 정리 대상이다. 배제한 원인: 세션 CLI 재시작(같은 PID 10시간), OOM(커널 로그에 기록 없음), Codex 호출 자체(작은 대조 실행 정상).
  - **동작하는 방법:** companion `task` 를 **`--background` 없이 포그라운드 Bash 한 번**으로 돌려 끝날 때까지 턴을 열어 둔다(Bash 상한 10분). 10분 안에 끝나도록 프롬프트를 핵심 질문으로 좁히고 "플러그인·스킬 안내 파일·CLAUDE.md·CHANGELOG 는 읽지 말고 대상만 읽어라, 8분 안에 답하라"를 넣는다(모델·effort 는 그대로). 실측: 좁힌 S2 재리뷰가 xhigh 로 10분 안에 완료.
  - `codex exec` 직접 호출은 격리 세션의 git 검증기가 거부한다(프롬프트 안의 git 문구 때문에 "내 worktree 밖 git 이 아님"을 확인할 수 없다). 프롬프트를 파일로 돌려 검증기를 피하지 말 것 — 의도된 격리 경계다.
  - `--background` 를 쓸 수밖에 없으면 대기는 "상태가 running 을 벗어남"만 보지 말고 **worker pid 생존·로그 침묵·절대 시한**을 함께 본다(`kill -0 <pid>`, 로그 mtime). 죽은 작업은 `cancel <id>` 로 기록을 바로잡는다.
- **역할·한도:** coordinator 1 + active worker ≤3(전 provider 합산, 실제로는 위 자원 제약으로 ≤2), worker 재위임 금지, 파일당 단일 writer, coordinator 만 통합. 구현 = 요청 claude-opus-5/high, 독립 리뷰 = 구현자와 다른 실행의 opus/xhigh + Codex astra/xhigh. worker 금지: 운영 `.env`·토큰·`~/.cache/ai_trader*`·네트워크·systemctl·git push·허용 목록 밖 파일.

---

## S1 (B2a) — 불변 판단 사실과 final kernel 재검사

### Plan
- **범위:** `src/execution/safety/decisions.py`(신규) · `src/execution/safety/commands.py` · `tests/test_execution_decision_facts.py`(신규) + 기존 시험의 facts 게시 호출 최소 수정. `engine.py`·`position_sizing_kernel.py`(B1 동결)·`risk_policy.py` 판정 로직 무접촉.
- **고정 인터페이스(S3 gateway 가 의존):** frozen `EntryDecisionFacts`(`to_dict/from_dict/digest`, digest = `protection_recovery.digest(policy_generations.canonical(...))`), `RequestBoundCommands.publish_qualification_source(name, *, as_of, digest, expected_version) -> int`, `publish_decision_facts(facts, *, expected_version) -> int`, 소비는 **SUBMIT+BUY+AUTOMATIC 만**, `_evaluate`(prepare·final 공통 관문) 한 곳에서 재검사, binding 에 `decision_facts_digest`.
- **원칙:** 현재 경제(equity·cash·다른 예약·전략 노출·일손익·`policy.*`)는 매번 owner snapshot, 판단 시점 값은 불변 facts, mutable event/Order/metadata 에서는 읽지 않는다. stale 은 소비 출처 version·config·만료뿐(무관 fill/ACK 제외).
- **인수 조건:** 계획서 §3 S1 1~7 + 리뷰 처분 R1~R7(계획서 "S1 독립 리뷰 처분").

### Do (1차 구현)
- 기준 SHA `03cc2d6`. worker 1명, 요청 claude-opus-5/high, 실제 모델 metadata 미노출(시스템 프롬프트 자기보고 `claude-opus-5[1m]` — 실측으로 세지 않음). 1차 시도는 수동 worktree 방식으로 착수 전 중단(커밋 0), 2차에 harness 격리 worktree 로 완료.
- 커밋: `8a571b8` `test(red): S1 decision facts acceptance` → `33b7643` `feat(safety): request-bound decision facts and final kernel recheck (S1)`. 브랜치 `work/s1-decision-facts`(worktree `wf_37a6f0db-0ab-1`, 통합 후 정리 대상).
- 변경 7파일 +792/−7: `commands.py` +108, `decisions.py` +285, `test_execution_decision_facts.py` +372, `test_execution_command_owner.py` +30, `test_execution_command_flow.py` +2, `test_execution_policy_generations.py` +1, `test_execution_policy_generation_acceptance.py` +1.
- 구현자가 밝힌 계획 이탈: qualification source version 을 owner.version 이 아닌 **출처별 단조 counter** 로 정의(무관 fill/ACK 를 stale 로 오인하지 않기 위함) · `_evaluate` 반환 3→4-tuple(호출자 `_prepare`·`_bound` 둘뿐) · 계획에 없던 검사 2개(`decision_stop_changed`, risk 모드 stop 누락 시 `missing_decision_stop`) · hybrid parity 미포함 · 기존 시험 수정이 계획서의 8건보다 많음(같은 종류의 게시 호출 추가).

### See (1차)
- **coordinator 직접 재현** — RED 커밋(`git switch --detach 8a571b8`): 새 파일 22 failed / 3 passed. 행동 RED 1건(`test_only_automatic_buy_requires_published_decision_facts[automatic]` — `DID NOT RAISE`, 현재는 facts 없이 prepare 통과), 부재 RED 21건(`ModuleNotFoundError` 20·`AttributeError` 1)은 RED 증거로 세지 않음. GREEN(`33b7643`): UTC 364 passed·KST 364 passed, 각 exit 0·격리 0.
- 허용 파일 밖 변경 0. 기존 시험 4파일 `--numstat` 삭제 0줄(Codex 도 `assert` AST 225개 동일 확인) → 단언 약화 없음.
- **독립 리뷰 3건, 모두 CHANGES_REQUIRED·P0 0건:** Codex(요청 gpt-6-astra/xhigh, read-only, 8분 53초, job `task-mu9du0pc-833fgk`) · Claude 돈 경로 parity 렌즈(요청 opus/xhigh) · Claude 상태·시험 정직성 렌즈(요청 opus/xhigh). Claude 발견 11건은 전부 반증 시도(요청 opus/high) → 9건 확정·2건 기각.
- **변이 실험(리뷰어, 33b7643):** (a) recompose 의 예약 차감 제거 → 364 전부 통과(헛돎) · (b) daily_loss 제거 → 1건 실패 · (c) 전략 잔여 제거 → 전부 통과(헛돎) · (d) overlays 제거 → 전부 통과(헛돎) · (e) 수량 상한 검사 제거 → 2건 실패 · `_bound` digest 대조 제거 → 전부 통과(미검증 방어선).
- **비-hybrid parity 는 코드상 일치**(세 리뷰 공통): 가용 현금·core reserve·pool·단위(%/비율)·전략 잔여·일손실·overlay 순서·ATR skip·수수료 포함 risk cap·최소금액·3주·MARKET 1.3. 리뷰어가 직접 넣은 표본(일손실, overlay 0.5/1.1/1.2/1.3, 전략 예산 소진, 최소금액 바닥)에서 legacy == recompose. 즉 **코드는 맞고 시험이 없었다.**
- **처분표 R1~R7·기각 2건·계약 문언 정정·잔여 4건:** 계획서 "S1 독립 리뷰 처분" 절이 정본이다(`e8054b0`).

### Do (리뷰 수정 라운드)
- 기준 `33b7643`, 브랜치 `work/s1-fixes`, 단일 writer(요청 claude-opus-5/high, metadata 미노출). 커밋 `206376b` `test(red): S1 review fixes acceptance` → `720c913` `fix(safety): S1 review fixes R1-R7`. 변경 4파일: `decisions.py`·`commands.py`·`test_execution_decision_facts.py`·`test_execution_command_owner.py`(fixture `facts()` 에 `hybrid_enabled=False` 추가뿐).
- 항목별: **R1** `EntryDecisionFacts.hybrid_enabled: bool` 필수(기본값 없음·`type is bool`·digest 포함), `recompose_quantity` 가 True 면 `ValueError('unsupported_hybrid_sizing')`. **R2** 빈 `sources` 는 DTO 에서 `invalid_consumed_source`(falsy 검사가 아닌 길이 비교). **R3** `_consumed_sources` 가 게시본 `as_of` 동일성·당일(KST, `runtime._now()`)·`as_of <= decided_at` 을 추가로 요구(staticmethod → 인스턴스 메서드, 호출부 시그니처 무변, 사유 코드 `stale_qualification_source` 재사용). **R4~R7** 은 제품 변경 없이 시험만: status 를 `is NOT_SENT` + 저장 상태(`command_status=='not_sent'`, `state=='final_rejected'`)로 좁힘, UNKNOWN 별도 시험(주문번호 없는 ACK → 예약 507500.000/50주 유지·`blocked_unknown`·재 dispatch POST 추가 0), parity 구속 표본 7건, final `reservation` 140→130주·`cash` 는 "무관 종목 134주 보유 심기"(자산 200만 유지, 현금만 66만), binding digest 위조 시험, 무관 fill·무관 취소 ACK 대조, hashkey×reservation.
- parity 추가 표본의 기대값은 전부 **실제 legacy wrapper 호출값**이며 리뷰어 수치와 일치: pending 예약 153 · 전략 예산 소진 40 · volatility 0.5→125 · calendar 1.1→192 · conviction 1.2→210 · position 1.3→227 · 일손실 절반 축소 87 (기존 nominal 175 / risk 139 / core 100 / MARKET 1.3 100 포함 11표본).
- 수정 worker 가 밝힌 이탈: ① R3 의 "전일 게시본"은 시계 이동이 아니라 합성 state 주입으로 구성(시계를 다음 날로 옮기면 `_require_day_admission` 이 먼저 `day_transition_admission_closed` 로 막아 새 검사에 도달하지 못함 — 실측) ② R7 `other_fill` 만 요청 수량 50→49(무관 매수 수수료 14원으로 equity 2,000,000→1,999,986, kernel 상한 50→49), POST 단언은 절대값 1 → "직전 대비 +1" ③ DTO type 검사 사유 코드 `invalid_decision_hybrid` 신설.
- coordinator 보강 `78ca94f` `test(safety): pin the facts sector guard…`: 재검증에서 살아남은 변이(아래)에 대응해 시험 3건 추가. 제품 코드 무변경.

### See (수정 라운드)
- **독립 재검증 2명**(구현자와 다른 실행, 요청 opus/xhigh, `720c913` 기준): ① 수정 정확성 렌즈 **APPROVE_THIS_SLICE** — R1~R7·기존 단언 무변·면제 경로 무변 전부 확인, UTC·KST 각 375 passed·격리 0. ② 시험 강도 렌즈 **CHANGES_REQUIRED** — R1~R7 은 전부 확인했으나 자체 추가 변이 3종 중 1종 생존.
- **변이 kill(수정 worker 보고 → 재검증 2명이 각각 재현, 전부 일치):** (a) 예약 차감 제거 → 3건 실패 · (c) 전략 잔여 제거 → 1건 · (d) overlays 제거 → 1건 · (f) `_bound` digest 대조 제거 → 1건 · (g) as_of 검사 제거 → 3건 · (h) hybrid 거부 제거 → 1건 · (i) 빈 sources 거부 제거 → 2건. 재검증자 추가 변이: 수량 비교 `<=`→`<` 등 kill.
- **생존 변이 → 처분:** `commands._decision_facts` 의 `_require(sector is None or sector == facts.sector, 'decision_facts_sector_mismatch')` 를 `_require(True, …)` 로 바꿔도 375건 전부 통과(P1, 가드는 존재·시험 부재. 이 값이 `EntryPolicyInput` 의 섹터 집중 한도와 `attempt['sector']`·binding 에 쓰인다). → `78ca94f` 에서 `test_caller_sector_must_match_the_published_facts_sector[None|금융]`(정확한 사유·attempts `{}`·POST 0)와 `test_facts_sector_is_canonical_when_the_caller_passes_none` 추가. **coordinator 가 같은 변이를 적용해 앞의 2건이 실패함을 확인한 뒤 원복**(`git diff --stat -- src/` 비어 있음).
- **재검증 공통 P2 → 잔여 7 로 수용:** `hybrid_enabled` 는 게시자 자기신고이고 owner snapshot 에 대조할 값이 없다(거짓 False 는 검출 불가, 방향은 fail-open). 두 재검증자 모두 "지금 코드를 바꾸지 말고 실제 publisher 배선 시 policy 에 실어 대조"를 권고. `EffectiveRiskPolicy` 확장은 PolicyContext 직렬화·다수 시험 fixture 에 파급되고, policy publisher 자체가 아직 운영에 없다.
- **통합:** `01362db` `Merge S1 (B2a)…`(`--no-ff`, 리뷰된 SHA `8a571b8`·`33b7643`·`206376b`·`720c913` 보존) + `78ca94f`.
- **Codex 교차 재리뷰(요청 gpt-6-astra/xhigh, read-only, HEAD `78ca94f`, job `task-mu9gp9oc-h4etpz`, session `01a0bd99-35bb-7921-9b6c-6e522871ff68`): APPROVE — 이전 발견 8항목 전부 해소, 새 P0/P1/P2 0건.** pytest 는 돌리지 않고(호스트 메모리 제약으로 금지시킴) 메모리 내 재현으로 확인: ① final `cash` 사례는 기존 gate 가용 560,000원·owner 현금 660,000원으로 둘 다 통과하고 kernel 상한만 `floor(560000/13000)`=**43주**, `reservation` 은 580,500/680,500원·kernel **44주** (50주 요청의 주문가치 500,000·정책 gate 필요액 500,500·owner 예약 507,500원) ② `as_of` 검사 UTC/KST 자정 경계 8개 재현 일치(`runtime._now()` 가 항상 KST 정규화) ③ `other_fill` 49주는 타당(수수료 14원 → equity 1,999,986 → 25% 명목 499,996.50 → 49주; "경제적으로 유효한 요청이 무관 fill 로 stale 처리되지 않음"의 증명 목적에 맞음) ④ `hybrid_enabled` 키 없는 과거 행은 키 집합 검사에서 `invalid_decision_facts` 로 해당 자동 BUY 만 차단 ⑤ 기존 시험 단언 225개 AST 보존. **조건:** 실제 publisher 연결 전 `snapshot.policy.hybrid_enabled` 대조와 "설정 True·facts False → 거부" 시험은 필수(잔여 7). 이 판정은 소스 리뷰와 제한된 재현에 한정되며 전체 시험 통과·운영 승격을 뜻하지 않는다.

- **전체 suite(coordinator, HEAD `78ca94f`, 다른 worker 없이 단독 직렬, 2026-09-20 16:02~16:11 KST):** UTC **4442 passed / 2 xfailed / 경고 4 / 272.84초**, KST **4442 passed / 2 xfailed / 경고 4 / 266.91초**, 각 exit 0·`[테스트 격리] … 0건`. C4 기준선 4403 대비 +39 = `tests/test_execution_decision_facts.py` 의 39건(36 + sector 보강 3). 기존 xfail 2·경고 4(pykrx 1 + fork 3)는 그대로다. 실행 후 가용 메모리 916MB.
- **판정:** S1 은 **한정 승인**이다. 승인 범위 = "safety 패키지 안에서 자동 BUY 의 prepare·final 이 불변 facts 와 현재 snapshot 으로 수량·출처·설정·만료를 재검사한다"는 계약의 구현과 합성 시험. **승인 범위 밖:** 실제 CV/LLM 이 facts 를 만드는지(S2), SIGNAL 이 이 경로를 타는지(S3), 운영 설치·실송신·성능.
- **정리:** 임시 worktree `wf_37a6f0db-0ab-1`(1차 구현)·`wf_52cc12bf-265-*`(수정·재검증)와 로컬 브랜치 `work/s1-decision-facts`·`work/s1-fixes` 는 통합 후 제거한다(리뷰된 커밋은 merge 로 feature 브랜치에 보존). 리뷰용 worktree 13개는 1차 리뷰 직후 제거했다(전부 clean — 변이 원복 확인).

### 이어받는 에이전트 체크리스트 (S1)
- [ ] `git log --oneline 03cc2d6..<S1 통합 SHA>` 로 RED 커밋이 제품 커밋보다 앞에 있는지 확인.
- [ ] RED 커밋을 detached 로 체크아웃해 새 시험 파일만 실행 → 행동 RED(`DID NOT RAISE`)가 실제로 나오는지. 끝나면 feature 브랜치로 복귀.
- [ ] 통합 SHA 에서 대상 10파일 UTC·KST 실행 → 통과 수·격리 0.
- [ ] `git diff --numstat 03cc2d6 <통합 SHA> -- tests/` 에서 기존 시험 파일 삭제 줄이 0 인지(있다면 어떤 단언인지).
- [ ] 변이 재현: `decisions.py` 의 `- reserved` 제거 / `remaining = None` / `overlays=()` / hybrid 거부 제거 / 빈 sources 거부 제거, `commands.py` 의 `_bound` digest 대조 제거 / `_consumed_sources` as_of 검사 제거 → 각각 최소 1건 실패해야 한다. 실험 뒤 `git restore`.
- [ ] 의심해 볼 지점: `commands._dispatch` 가 `_bound` 안의 모든 `_require` 실패를 `claim_not_available` 하나로 뭉갠다(어느 검사가 막았는지 결과만으로는 구분 불가 — 시험은 변이로만 증명된다). `recompose_quantity` 의 `stop_pct` 는 risk 모드에서만 non-None 이 보장된다(Pyright 가 좁히지 못해 경고를 내지만 위쪽 `missing_decision_stop` 가드가 막는다).
- [ ] USER·SAFE_ASSET·SELL·CANCEL 이 facts 없이 현행대로 통과하는 대조 시험이 남아 있는지.

### 잔여·미검증 (S1 에서 고치지 않음 — 계획서 처분 절과 동일)
1. facts 의 설정 전용 값·배율은 **게시자 신뢰 범위**(owner 교차검증 없음, 노출은 policy 상한으로 묶임) → S2 publisher 시험에서 설정 출처에 고정.
2. `entry_decision_facts`·`qualification_sources` 를 비우는 writer 없음. 소비는 당일·만료로 fail-closed 이고 위험은 checkpoint 성장. `day_recovery.reset_daily` 를 건드리는 별도 작업. **출처 counter 는 일별 초기화 금지**(version 재사용 위험).
3. wrapper `get_available_cash()` 실산식(최소 현금·레짐 reserve)과 owner `available_cash(regime=True)` 의 등가는 parity 표본이 양쪽을 stub/0 으로 두어 **미대조**.
4. 과거 checkpoint 의 자동 BUY 는 `decision_facts_required`/`decision_facts_changed` 로 송신 차단(호환 우회 없음) — 실제 과거 checkpoint 재현은 하지 않음.
5. hybrid 사이징은 재현하지 않고 **명시 거부**한다(운영 설정 `hybrid.enabled: false`). 켜려면 policy 계약 확장과 parity 표본이 먼저다.
6. 실제 qualification publisher 가 없다 — facts 의 CV 점수·규칙 ID·LLM verdict·배율·출처 digest 는 전부 시험의 합성값(S2 범위).
7. `hybrid_enabled` 는 **게시자 자기신고**다. True 신고는 명시 거부되지만(변이 (h) kill), 거짓 False 를 대조할 owner 값이 없다(`EffectiveRiskPolicy` 에 hybrid 축 없음, `config_version` 은 불투명 문자열). 실제 policy publisher 를 붙일 때 `policy.hybrid_enabled` 를 실어 `_decision_facts` 에서 `facts.hybrid_enabled == snapshot.policy.hybrid_enabled` 를 대조하고, "config 에서 hybrid on + facts False → 거부" RED 를 먼저 세운다.
8. `test_published_facts_must_describe_the_actual_request` 는 `pytest.raises(ValueError)` 에 `match` 가 없어 어느 검사가 거부했는지 고정하지 않는다(수량 사례는 변이 (e) 로 구속됨). `_dispatch` 가 `_bound` 의 모든 실패를 `claim_not_available` 로 뭉개는 것과 같은 계열 — 사유별 구속은 변이로만 증명된다.

### S2 진입 조건
- S1 이 engine 브랜치에 통합되고 전체 suite UTC·KST 직렬 통과·격리 0, 이 원장과 CHANGELOG·계획서·인계 문서가 갱신·push 됨.
- S2 Plan 에서 먼저 정할 것: CV 의 4개 입력(trade_memory·expert_orchestrator·sector_council·panel_outlook)과 market_regime 의 **digest 산출 방법과 게시 시점**, CV 가 facts 를 "추가 반환"하는 형태(기존 `(bool, float, str)` 반환·감점 산식·임계값 불변, `event.score` in-place 변형은 legacy 경로에서 유지), LLM soft-reject 의 `position_multiplier` 이중 곱(event/signal 두 dict) 중 kernel 이 읽는 쪽(`signal.signal.metadata`) 기준 단일 값, 시간 규칙의 `expires_at` 산출(aware KST, `runtime._now()`), `hybrid_enabled`·`base_pct`·`strategy_allocation_pct`·`min_position_value`·`config_version` 의 설정 출처 고정.

---

## S2 (B2b) — 실제 qualification publisher

> 세부 계획·계약·하위 단계 명세의 정본: `docs/superpowers/plans/2026-09-20-s2-qualification-publishers.md`. 이 절은 진행·증거·체크리스트를 적는다.

### Plan (완료, 2026-09-20, 기준 `e5d0d93`)
- 방법: 읽기 전용 워크플로 — 조사 2건(CV 출처·갱신 지점 / facts 필드 출처·시간 규칙) → 독립 설계 2안(최소 침습 우선 / stale 실효성 우선) → 심사·종합 1건. 전부 요청 claude-opus-5(high, 심사만 xhigh), 실제 모델 metadata 미노출. 호스트 제약으로 동시 ≤2·**pytest 0건**(모든 주장은 정적 소스 대조). 심사자가 사실 주장 16건을 파일을 열어 확인(15 맞음·1 부분), 설계안1 의 오류 2건(증거 채널 교차 오염이 "구조적으로 불가능"하다는 주장, shadow 규칙11 을 소비 출처로 게시)을 기각.
- 핵심 발견: ① CV 가 받는 regime 은 owner 의 순수 함수 `effective_regime(state, now)` 와 같은 값 → **final 동기 구간에서 재유도 가능(자기신고가 아닌 유일한 stale 축)** ② panel_outlook·trade_memory 는 결정 시점에만 게시되어 version·digest 대조가 헛돈다(**replay 구속 전용**으로 정직하게 보고) ③ tests/ 에 CV 실인스턴스 0건 — "감점 산식 불변"의 증거가 없어 **특성화를 먼저** 세운다 ④ `cross_validator.py:258` 의 함수 내 datetime 재임포트로 기존 시계 동결 관례가 듣지 않는다 ⑤ CV 인스턴스를 실거래와 팀심의 shadow 가 공유하고 그 사이에 await 가 있어 증거 채널은 "진입 시 None + token 정확 대조"가 필요하다.
- coordinator 결정 8건 + 구조 변경(시험 파일 단계별 분리·wave 실행)은 세부 계획 §2. 상위 계획서 대비 이탈 2건을 거기 기록: S2 허용 제품 파일에 `safety/qualification.py`·`safety/commands.py` 추가, R2 의 "ruleset 출처" 대신 regime 상시 인용.
- **Codex 재리뷰 조건(S1)의 처리:** `hybrid_enabled` 의 policy 대조는 S3 로 미룬다(제품 PolicyContext publisher 0건이라 지금은 자기 자신과 비교, 필수 필드 추가는 기존 시험 동결 위반). S2 에서는 **부분 충족**(유도 + 명시 거부)으로 기록하고 완전 충족은 S3 인수 조건이다.

### Do·See — 하위 단계별 (진행하며 채운다)

| 하위 단계 | 제품 파일 | 시험 파일 | 상태 | 커밋 | 재검증·리뷰 |
|---|---|---|---|---|---|
| S2-1 CV 특성화 + 증거 채널 | `src/core/cross_validator.py` (+63/−9, 판정·산식·문자열 무변) | `tests/test_cross_validator_characterization.py` (53건) | **통합(한정 승인)** | `5968220`(특성화)→`9185993`(red)→`a97dc1a`(feat)→`0988dbc`(보강) / merge `7177a8d` | 1차 재검증 CHANGES_REQUIRED(생존 변이 2) → 보강 → 독립 재현 APPROVE |
| S2-2 사이징 입력 반출 | `src/core/engine.py`(`_calculate_position_size` 내부만, +30/−0) | `tests/test_execution_sizing_inputs_export.py` (27건) | **통합(한정 승인)** | `7e26da7`(red)→`3832555`(feat)→`f53d57a`(보강) / merge `9330fbe` | 1차 재검증 APPROVE(생존 변이 3, P2) → 보강 → 독립 재현 APPROVE |
| S2-3 regime final 재유도 | `src/execution/safety/commands.py` (+23/−0: `read_qualification_source`·`_recheck_regime`·호출 1줄, `_decision_facts` 의 `_consumed_sources` 직후 = prepare·final 공통) | `tests/test_execution_regime_recheck.py` (8건) | **통합(한정 승인)** | `892905a`(red)→`53cbb08`(feat)→`73fe6af`(시계 시험) / merge `6f9108a` + `51a71e0`(schema 경계 단언) | 독립 재검증 APPROVE — 변이 8종 중 7 kill, 1종(deepcopy 제거)은 `owner.state` 가 이미 deepcopy 라 **동치 변이**로 확정. 구현자가 계획 변이 3(`decided_at` 재유도)의 생존을 스스로 발견해 시험 보강. coordinator 가 diff 직접 확인 |
| S2-4 순수 builder | `src/execution/safety/qualification.py`(신규 ~370줄: `QualificationRefused`·`PendingSource`·`RULE_IDS` 15쌍·`config_version`·`entry_expires_at`·`regime_digest`·`build_decision_facts`; I/O·시계 읽기 0) | `tests/test_execution_qualification_builder.py` (66건) | **통합(한정 승인)** | `4b085eb`(red)→`97b642d`(feat)→`9b09697`(red)→`d890c6f`(fix) / merge `4829200` + `4018b79`(SEPA 상한 경과 거부) | 1차 재검증 CHANGES_REQUIRED(자체 변이 26종 중 4 생존: atr_pct None 보존 P1·allocation None·overlay_status 내부 키·hybrid truthy) → 수정(가드 3건 시험 고정 + **SEPA 14:30 경계 교차 거부**, 행동 RED `DID NOT RAISE`) → 독립 재현 APPROVE(변이 8종 전건 kill) → 재현자 P2(양쪽 시계가 모두 14:30 이후면 상한이 소멸해 당일 말까지 유효 — fail-open)를 coordinator 가 `sepa_entry_deadline_passed` 거부로 닫고 가드 제거 변이로 확인 |
| S2-5 증거 캡처 + 게시 함수 (**범위 정정** — 세부 계획 "S2-5 범위 정정": on_signal 은 캡처만, 게시 호출·intent_id·config_version 은 S3 gateway) | `src/execution/safety/qualification_publisher.py`(신규) · `src/core/engine.py`(on_signal 의 캡처 구간만) | `tests/test_execution_qualification_publishers.py` (16건) | **통합, 통합 수정 라운드 진행 중** | `6a5f7d0`(red)→`f687f04`(feat) / merge `d703b34` | 독립 재검증 CHANGES_REQUIRED — 제품 9항목 전부 확인(허용 구간·legacy 0줄 실행·계약 13 캡처 순서·계약 14·publisher 순서·digest 동등성), 자체 변이 13종 중 3종 생존(token **동등** 비교 P1, 재사용 조건의 "당일" 절 P1, regime 재조회 P2) + 캡처 시점 얕은 복사·`invalid_qualification_evidence` 무시험 P2 → Codex wave B 발견과 함께 통합 수정(F1~F9) |

### wave A (S2-1 ∥ S2-2) — Do·See 기록 (기준 `58e5ef7`)

- **구현:** 단계별 단일 writer(요청 claude-opus-5/high, metadata 미노출), harness 격리 worktree. 허용 파일 밖 변경 0, 기존 시험 파일 수정 0줄.
- **S2-1 착수 전 실측(성공):** `cross_validator.py:258` 의 함수 내 재임포트 때문에 시계 동결은 **`datetime.datetime` 과 `src.core.cross_validator.datetime` 두 곳을 함께** Frozen(datetime 서브클래스, `now()` 만 고정)으로 교체해야 09:29/09:45/12:45/13:00/13:01 다섯 경계가 전부 언다(한쪽만 바꾸면 stats 리셋·패널 캐시·days_old·LLM 일일 한도 중 일부가 벽시계로 남는다). CV 시그니처·본문은 시험 편의로 바꾸지 않았다. validate 호출부 3곳(engine.py:2053, kr_scheduler.py:6153, us_scheduler.py:1090) 전부 keyword → 새 kwarg 후방 호환.
- **S2-1 구현자 이탈(수용):** 계약 9 의 `not_required` 에 생산자를 붙였다(validate 진입부에서 `last_llm_reason='not_required'` — llm_second_check 를 부르지 않은 판단이 앞 판단의 어휘를 물려받지 않게) · 제품 가산이 계획(~20줄)보다 큼(+63/−9; −9 는 llm_second_check 의 return 9곳을 `_llm()` 경유로 바꾼 것, **coordinator 가 diff 로 각 return 의 원래 bool 보존을 확인**).
- **S2-2 구현자 이탈(수용):** resolver 미실행(nominal·core)에서는 stop 3튜플을 False/'' 가 아니라 전부 None 으로 기록("해석기 미실행"과 "실제 False" 구분). **coordinator 확인:** overlay 3종은 조건 분기 없이 항상 호출되므로 `unavailable` 은 예외일 때만 생긴다("해당 없음"이 `unavailable` 로 오표식되지 않는다).
- **RED 의 한계(두 단계 공통, P2 수용):** 가산 인터페이스라 RED 커밋의 실패가 전부 부재 오류(TypeError/AttributeError)였다 — 단언 실패 0건. 행동 계약의 실질 증명은 **변이 kill** 이다. 이후 단계의 RED 인수 기준을 "나열 변이 전건 kill"로 읽는다.
- **변이:** 계획서 목록 S2-1 9종·S2-2 6종 전건 kill(구현자 보고 → 재검증자 재현 일치). 재검증자 자체 변이에서 **5종 생존 → 시험 전용 보강 → 독립 재현에서 전건 kill**: ① 적대검증 LLM 경로(운영 1순위 반환 경로)의 어휘 붕괴 ② 보너스 0 인 stale 패널 분기에서 `panel` 기록 ③ overlay 기록 줄을 `apply_overlay` 앞으로 이동 ④ `is not None` → falsy 판정(배율 0.0) ⑤ 금액 필드 `float()` 붕괴. 재현자가 추가로 찾은 2종(폴백 경로의 LLM 일일 한도 계수 제거, risk 경로 전용 key 추가)은 coordinator 가 단언 2줄(`6fa7fe5`)을 넣고 **같은 변이를 직접 적용해 각각 1건 실패 확인 후 원복**(`git diff --stat -- src/` 비어 있음, MUTATION 표식 0).
- **S2-5·S2-4 로 넘기는 계약 메모:** (a) `_last_sizing_inputs` 는 첫 사이징 호출 전에는 None 이 아니라 **속성 부재**다(기존 시험이 `object.__new__` 로 `__init__` 을 건너뛰므로 `__init__` 선언으로는 못 막는다) → 어댑터는 `getattr(rm, '_last_sizing_inputs', None)` 로 읽는다. (b) `last_llm_reason` 에는 token 이 없다 → 어댑터는 **같은 판단의 `last_decision['token']` 대조가 통과한 경우에만** `last_llm_reason` 을 읽는다. (c) 반출 `position_multiplier` 는 ATR skip **이전** 값이다(계약 8) — S1 의 `recompose_quantity` 가 `should_skip_atr_multiplier` 를 스스로 적용하므로 facts 에는 이 원값을 그대로 넣는다. `overlay_status` 는 position kind 를 덮지 않는다.
- **중간 전체 suite(coordinator, HEAD `6fa7fe5`, 단독 직렬, 2026-09-20 18:08~18:17 KST):** UTC **4522 passed / 2 xfailed / 경고 4 / 277.61초**, KST **4522 / 2 / 4 / 269.98초**, 각 exit 0·격리 0. S1 뒤 4442 대비 +80 = CV 특성화 53 + 사이징 반출 27. live 파일 2개(`cross_validator.py`·`engine.py`) 변경이 범위 밖 시험을 깨지 않았다.
- **Codex 교차 리뷰(요청 gpt-6-astra/xhigh, read-only·pytest 금지, HEAD `6fa7fe5`, job `task-mu9legcx-6dilp6`, session `01a0be11-a81f-76e3-a665-52d0738a0d75`, 7분 4초): CHANGES_REQUIRED — P0/P1 0건, P2 1건.** 제품은 깨끗: 증거 기록과 `_llm()` 래핑을 제거한 AST 가 두 제품 파일 모두 `58e5ef7` 과 일치, 메모리 one-off 에서 CV 19표본의 반환·통계와 LLM 11표본의 반환·계수 일치, LLM return 9곳의 원래 bool 보존, 세 호출부 keyword, `<=1300`(13:00 포함·13:01 제외)은 기존 구현과 일치, 0/빈값·Decimal·한국어 주석 위반 없음.
  - **P2(수정 완료):** 시계 동결 fixture 가 직접 바꾼 두 속성만 복원해, 동결 중 CV 가 처음 지연 import 하는 `expert_panel` 의 `from datetime import datetime` 이 `Frozen` 을 붙잡은 채 다음 시험까지 남는다(import 순서 의존 누수, Codex 가 메모리 재현으로 확인: 다음 시험 시계를 11:00 으로 바꿔도 패널 시계는 08:59). → fixture 종료 시 `sys.modules` 전체에서 Frozen 참조를 이름 무관하게 실제 `datetime` 으로 되돌리는 `_scrub_frozen` 추가 + 누수를 심는 시험/복원을 확인하는 시험 2건. **coordinator 가 scrub 호출을 제거하는 변이로 복원 확인 시험 1건 실패를 확인 후 원복.** CV 파일 55건 통과.
  - **S2-5·S2-4 로 승격한 지침(세부 계획 계약 13~15):** 증거 캡처 순서(자기 LLM await 직후 token 대조 → decision·reason 을 추가 await 없이 함께 복사; "B validate → 지연된 A 의 `_llm()`" 은 token B + A 사유를 만든다) · 사이징 입력은 현재 요청의 성공한 사이징 직후에만 읽는다 · 0 배율/hybrid 는 보정하지 말고 facts 미생성.
- **호스트 자원 사고 방지 기록:** 이 세션의 자식 `pyright-langserver` 가 643MB 까지 자라 가용 메모리가 481MB 로 떨어졌다(스왑 100%). coordinator 가 SIGTERM 으로 내려 1134MB 회복 후 전체 suite 를 돌렸다. 이 호스트에서 작업하는 세션은 `ps -eo rss,pid,comm --sort=-rss | head` 로 자기 세션의 LSP 서버 크기를 확인할 것.

### wave B·C — Do·See 기록 (기준 `3c51117` → `a67bbbf`)

- **S2-3·S2-4·S2-5 구현:** 단계별 단일 writer(요청 claude-opus-5/high) → 독립 재검증(요청 opus/xhigh, 계획서 변이 + 자체 변이). 세 단계 모두 **제품 가드는 전부 맞고 시험 공백이 변이 생존으로 드러나는** wave A 와 같은 양상이었다. S2-5 는 `engine.py` +39/−0(추가뿐, hunk 5곳 전부 허용 구간)·`qualification_publisher.py` 신규 99줄·시험 16건, 계획서 변이 8종 전건 kill, 재검증자 자체 변이 13종 중 3종 생존.
- **Codex wave B 교차 리뷰(요청 gpt-6-astra/xhigh, read-only·pytest 금지, 대상 `a67bbbf`, job `task-mu9nrwwh-e7zdbq`, session `01a0be4e-81da-7a13-9f48-12ce7f8685f9`, 10분 44초): CHANGES_REQUIRED — P0 0 · P1 1 · P2 4.** 확인된 것: `_recheck_regime` 경로에 새 await/I/O 없음·현재 now 사용·미소비 시 즉시 반환·policy 부재 시 거부, digest 식이 builder 와 동일, **실제 `penalties.append` 15곳과 `RULE_IDS` 15쌍 전수 대조 — 누락·중복 없음**, SEPA 경계 재현(14:29:59→14:30 만료 / CV 14:29·판단 14:30→`decision_clock_disagreement` / 둘 다 14:30→`sepa_entry_deadline_passed`), 합성 nominal·risk facts 가 실제 DTO 왕복·수량 재유도·`_decision_facts` 통과.
  - **P1(S3 로 이관, coordinator 가 소스로 확인):** prepare 뒤 **claim 이전**에 `_bound` 가 실패하면 `_dispatch` 가 결과를 기록하지 않고 `NOT_SENT/claim_not_available` 만 돌려준다(`commands.py:540-550`) → attempt 가 `prepared` 로 남아 **예약(현금·수량)과 pending sector 가 유지**된다. lifecycle 에 prepared 를 종료시키는 경로가 없다(`prepare_candidate`·`claim`·`record_result`·`reconcile` 뿐). claim 이후 최종 guard 의 같은 실패는 `final_rejected` 로 예약 0. S2-3 이 만든 결함이 아니라 Task 9 부터의 공백이지만 regime 거부가 새 발화 경로다. prepare 와 dispatch 를 실제로 잇는 S3 가 **"미claim prepared 의 원자적 미송신 종료(예약·pending sector 해제)"** 를 만들어야 한다.
  - **P2(통합 수정 라운드에서 처리):** ① 종목별 digest 를 전역 출처 한 행(`panel_outlook`)에 게시 → 두 번째 종목 게시가 첫 요청을 stale 로 만든다(설계 결함, R31 은 같은 digest 표본이라 놓침) → 출처 이름을 소비 범위로 분리 ② pending 출처의 `as_of` 가 CV 판독 시각이 아니라 `decided_at` → validate 직전에 `observed_at` 을 찍어 증거에 싣는다 ③ regime 시험이 network await 이후 재검사를 입증하지 못한다 ④ base_pct 표 대조 시험이 주장한 속성을 검증하지 않는다.
  - 참고: naive `panel_loaded_at` 을 KST 로 해석하는 가정은 실제 UTC 생산자에는 틀린다(운영 TZ=KST 에서는 무해). 새 시험 fixture 의 `date.today()`·`EngineStats` 의 `datetime.now()` 는 남아 있다 — 판정 입력의 벽시계 의존은 아니다.

### 통합 수정 라운드와 S2 마감 See (기준 `d703b34` → `83baa2a`)

- **통합 수정(F1~F9):** 단일 writer(요청 claude-opus-5/high) `a835502`(red)→`5a236f1`(fix), merge `072c51e`. 제품 3파일(`qualification.py`·`qualification_publisher.py`·`engine.py` 의 S2-5 캡처 구간)·시험 3파일. `commands.py` 무수정.
  - **F1 행동 RED 로 재현:** 같은 패널로 conviction 이 다른 두 종목을 잇달아 게시하면 첫 요청의 prepare 가 `CommandValidationError: stale_qualification_source` 로 막혔다(Codex 지적 그대로). 출처 이름을 `panel_outlook:<symbol>`·`trade_memory:<strategy>:<sector|'-'>` 로 분리, `regime` 은 전역. 조각 검사 `_scope`(사유 `invalid_source_scope`).
  - **F2:** on_signal 이 validate 직전에 `runtime._now()` 를 찍어 `QualificationEvidence.observed_at` 으로, `build_decision_facts(..., observed_at=)`(필수)가 `observed_at<=decided_at`·같은 KST 날짜 강제(`invalid_observation_time`), 출처 `as_of` = `observed_at`, `expires_at` 기준은 `decided_at`.
  - **F3~F9(제품 무변경·시험 고정):** token 보유 침입자 / 전일 게시본 재게시(합성 state 주입 — 시계를 다음 날로 옮기면 day admission 이 먼저 막는다) / regime 재조회 금지 / 캡처 시점 deepcopy(제품 1줄) / `invalid_qualification_evidence` / **network await 대기 중** regime 변경 → NOT_SENT·POST0·`final_rejected`·예약 0 / base_pct 표 대조 시험의 거짓 주장 제거(주장 범위 축소 + `_entry_risk_config_hash` 의 해시 입력이 `self.config` 뿐임을 소스 대조).
  - 계약 변경으로 **기대값을 갱신한 기존 단언**(출처 이름·`as_of`)은 구현자가 줄 단위로 보고했고 독립 재현자가 "계약 변경에 따른 갱신이며 약화 아님"으로 확인.
- **독립 재현(요청 opus/xhigh): CHANGES_REQUIRED** — 확인 19항목 전부 OK, 지정 변이 8종 전건 kill, 자체 변이 5종 중 1종 생존: 관측 시각 stamp 를 LLM·섹터 조회 await 뒤로 옮겨도 354건 통과(주입 시계가 상수라 "언제 읽었는가"가 관측 불가). → coordinator `83baa2a`: 섹터 조회 await 안에서 시계를 30초 미는 **전진 시계 시험** 추가, 같은 변이(증거 조립 시점에 시계를 다시 읽기)를 직접 적용해 **그 시험만 실패**함을 확인 후 원복(`git diff --stat -- src/` 비어 있음, MUTATION 표식 0).
- **S2 마감 전체 suite(coordinator, HEAD `83baa2a`, 단독 직렬, 2026-09-20):** UTC **4628 passed / 2 xfailed / 경고 4 / 301.43초**, KST **4628 / 2 / 4 / 282.50초**, 각 exit 0·격리 0. S1 뒤 4442 대비 **+186 = builder 69 + CV 특성화 55 + 사이징 반출 27 + publishers 25 + regime 재검사 10**(수집 수로 대조). 기존 xfail 2·경고 4 불변. 시작 전 세션 자식 `pyright-langserver` 가 다시 548MB 로 자라 있어 SIGTERM(가용 654→1204MB).
- **정리:** S2 의 임시 worktree 18개(wave A 8 + wave B·C·수정 라운드 10)와 work 브랜치 9개(4 + 5)를 전부 제거(모두 clean·merged — `worktree remove`/`branch -d` 가 거부 없이 통과). 남은 worktree 는 main·세션 기본·engine 3개.

- **Codex S2 재리뷰(요청 gpt-6-astra/xhigh, read-only·pytest 금지, 대상 `83baa2a`): CHANGES_REQUIRED — 새 P0/P1 0건, 새 P2 1건.** 1·2차(`task-mu9qmtk7-sp2cwl`·`task-mu9x4m7f-vb88oh`, `--background`)와 3차(harness 백그라운드)는 세션 종료 훅에 정리돼 결과 없이 죽었고(위 "공통 작업 방법"의 경고 참조), **4차에 포그라운드·좁힌 프롬프트로 10분 안에 완료**했다. 좁힌 범위: 이전 발견 5건의 해소 여부 + 새 P0/P1 위주(출처 행 수 증가·시험 정직성 전수·falsy/Decimal 점검은 이번 재리뷰에서 뺐다 — wave A·B 리뷰와 독립 재현이 덮은 항목이다).
  - 이전 발견: **P1(claim 이전 실패 시 prepared 예약 잔류) → S3 이관 수용**("현재 `src/` 에 prepare/dispatch 제품 호출자가 없으므로 S3 연결 전 인수 조건으로 처리할 수 있다") · 전역 출처 충돌 **해소** · `as_of` **해소** · regime await 시험·base_pct 주장 축소는 **처분 수용**(regime 시험 원문은 이번 범위 밖이라 독립 확인하지 않았다고 명시).
  - 확인된 것: legacy 경로의 판정·반환 변경 없음, 캡처 사이 추가 await 없음, 사이징 dict 는 호출마다 교체, 거부 뒤 남는 증거는 S2 에 소비자가 없어 즉시 주문 위험 아님, 게시 순서·매 게시 시 현재 `owner.version`·예외 전파, 출처만 게시된 부분 상태는 기존 판단을 stale 로 만들 수 있으나 새 송신 허가를 만들지는 않는다.
  - **새 P2(수정 완료 — 바로 아래 "S2 마감 수정" 참조):** metadata 에 sector 가 없으면 CV 는 `get_score_adjustment(strategy, "")` 로 메모리 보정을 계산하는데, 증거에는 그 뒤 `_sector_lookup` 이 돌려준 섹터가 들어가 `trade_memory:<strategy>:<조회 후 섹터>` 로 게시된다 — CV 가 쓰지 않은 섹터에 귀속되고, 서로 다른 소비 범위가 같은 행을 덮어 정상 판단을 stale 로 만들 수 있다(현행 메모리 stub 이 섹터와 무관한 상수라 놓침). → CV 가 규칙9 에서 실제로 넘긴 섹터를 `last_decision['memory_sector']`(13번째 키)로 보고하고 builder 가 그 값으로 출처 이름·digest 를 만든다. 단일 writer + 독립 재현으로 처리했다(아래).

### S2 마감 수정 — trade_memory 출처의 섹터 귀속 (기준 `fcc2a27` → `85a65bc`)

- **Plan:** Codex S2 재리뷰의 새 P2 한 건만. 고정 인터페이스(`last_decision` 키 집합)가 바뀌므로 S3 전에 닫는다. 허용 파일 5개(`cross_validator.py` 는 **가산만**·`qualification.py`·시험 3파일), `engine.py`·`qualification_publisher.py`·`commands.py`·`decisions.py` 금지. 인수 조건은 변이 M1~M4 kill.
- **Do (단일 writer, 요청 claude-opus-5/high, 관측 모델 미노출):** `0ac5dd8`(red) → `36d8dc6`(fix), merge `44e543a`. CV 가 규칙9 에서 `get_score_adjustment` 에 넘긴 값을 같은 지역변수로 들고 있다가 **보정이 실제로 붙었을 때만** `last_decision['memory_sector']`(13번째 키)로 싣는다 — 기존 `sector or ""` 의미·판정·점수·penalties·반환·기존 12키 불변(삭제 줄 1줄은 같은 인자 호출을 2줄로 분해한 것). builder 는 `_CV_KEYS` 13키 정확 대조, trade_memory 이름·digest 를 `memory_sector` 로(`''` → `'-'`), `memory_adj != 0` 인데 str 아니면 `unexpected_cv_decision`. `_pending_sources` 에서 조회 후 `sector` 인자를 제거해 그 값이 출처 이름에 닿을 경로 자체를 없앴다.
  - **행동 RED:** `test_f8_memory_source_is_attributed_to_the_sector_the_validator_used` — 실제 CV·실제 on_signal·실제 owner 로 빈 섹터(−7)와 '반도체'(−3) 두 판단을 게시하면 `assert 'trade_memory:sepa_trend:-' in {'regime', 'trade_memory:sepa_trend:반도체'}` 로 실패(잘못된 귀속 그대로 재현). 같은 RED 커밋의 builder 실패 57건은 공유 표본이 13키로 바뀐 데 따른 **구조적 RED**(행동 단언 미도달) — 행동 RED 단독 증거는 F8 하나이고, 고친 뒤에는 M1·M6 이 builder 시험을 단언으로 죽인다.
  - 메모리 stub 을 상수 → 섹터 의존(`{'반도체': -3, '': -7}`, 그 외 −5)으로 교체(상수 stub 이 귀속 오류를 숨겼다는 Codex 지적의 직접 해소).
  - **기대값을 갱신한 기존 단언 7건**은 구현자가 줄 단위로 보고, 독립 재현자가 "계약 이동에 따른 갱신·약화 아님"으로 확인. **실질 완화 1건(공개):** `facts.sector` 의 ':'(예 `'반도체:2차전지'`)는 이제 통과한다 — 출처 이름 조각으로 더는 쓰이지 않기 때문이며 소비처는 `commands.py` equality 대조와 `risk_policy` 섹터 비교뿐이다. `''`·`' 반도체'` 는 계속 거부되고 사유만 `invalid_source_scope` → `invalid_decision_sector` 로 바뀐다.
  - 절차 이탈(구현자 자진 신고·결과 무영향): 1차 변이 실행 때 `git restore` 가 미커밋 fix 를 함께 되돌렸다 → fix 재적용·커밋 후 M1~M4 를 커밋 기준으로 재실행. 독립 재현자가 커밋 `36d8dc6` detached 에서 전부 다시 돌려 영향 없음을 확인.
- **See (독립 재현, 요청 claude-opus-5/xhigh, 관측 모델 미노출): APPROVE_THIS_SLICE** — 확인 13항목 전부 OK(허용 파일만·가산만·13키 정확 대조·순수성 유지·행동 RED 재현·기존 단언 갱신 7건 판정), 지정 변이 M1~M4 + 자체 변이 M5(보정 0 에도 섹터 채움)·M6(`'-'` 치환·`_scope` 제거) **6종 전건 kill**, 각 변이 뒤 트리 원복 확인. 새 P2 1건: 반대 방향 계약(`memory_adj == 0` 이면 `memory_sector` 도 None) 미검사 — 오늘 행동 영향 0 이나 CV 기록 위치가 바뀌면 조용히 통과할 틈.
  - → coordinator `85a65bc`: 대칭 가드 1개(+3줄)와 단언 1줄 추가. 가드를 무력화하는 변이를 직접 적용해 `test_memory_source_without_a_reported_sector_is_refused` **그 시험만 실패**(1 failed / 70 passed) 확인 후 원복.
- **S2 최종 전체 suite(coordinator, HEAD `85a65bc`, 단독 직렬, 2026-09-21):** UTC **4632 passed / 2 xfailed / 경고 4 / 293.74초**, KST **4632 / 2 / 4 / 282.27초**, 각 격리 0. `83baa2a` 의 4628 대비 **+4 = CV 특성화 1 + builder 2 + publishers 1**. 기존 xfail 2·경고 4 불변. 그에 앞선 1차 UTC 실행(4631 passed / **1 failed**)은 S3 조사 에이전트 3명과 겹쳐 돌린 **부하 오염**이라 증거로 쓰지 않았다 — 실패한 `test_toss_client_boundary.py::…[prefix2-1]` 은 이번 diff 와 무관한 파일이고 실시간 1초 예산을 쓰며 단독 73 passed(위 "공통 작업 방법"의 기록, 별도 작업 칩 `task_df7c594a`).
- **정리:** 임시 worktree 2개(`wf_df27d92f-809-1·2`)와 `work/s2-memory-sector` 제거(clean·merged). 남은 worktree 는 main·세션 기본·engine 3개.
- **(갱신 2026-09-21) 교차 provider 재확인 완료:** S3 의 Codex 교차 리뷰 1차(아래 S3 절, 대상 `8ef3b99`)가 이 diff(`fcc2a27..85a65bc`)를 범위 B 로 보고 "이전 P2 를 닫는다·새 결함 미발견"으로 판정했다. 아래는 그 전 시점의 기록이다.
- **교차 provider 재확인은 하지 않았다.** Codex 의 CHANGES_REQUIRED 는 이 P2 한 건이었고 수정은 같은 provider(Opus) 독립 재현으로만 검증됐다 — "Codex 재승인"으로 세지 않는다. S3 단계 리뷰에서 Codex 가 이 diff(`fcc2a27..85a65bc`)를 함께 보게 한다.

### S2 의 성과와 한계 (보고 문장 — 이대로 인용한다)

- S2 가 만든 것: 실제 `CrossStrategyValidator`·실제 `_calculate_position_size` 가 낸 값으로 **불변 `EntryDecisionFacts` 를 만드는 부품**(증거 채널·사이징 입력 반출·순수 builder·증거 캡처·게시 함수)과, owner 가 final 에서 **체제를 스스로 재유도해 대조하는 검사**. tests/ 에 0건이던 CV 실인스턴스 특성화 55건.
- **실효 있는 stale 축은 regime 1개다.** `panel_outlook:*`·`trade_memory:*` 는 결정 시점에만 게시되므로 판단→final 사이 version·digest 대조가 헛돈다 — **replay 구속 전용**(전일·다른 결정의 게시본 재사용과 `as_of` 당일성만 잡는다). config 축은 제품 PolicyContext publisher 가 0건이라 자기 일관성뿐이고 **S3 에서 실효가 생긴다.**
- **제품 소비자는 여전히 0건이다.** `publish_qualification` 의 호출자도, `_last_qualification_evidence` 를 읽는 코드도, prepare/dispatch 를 부르는 코드도 없다(S3 gateway). 즉 운영 경로에서 게시·송신은 0건이고 이 단계는 설치가 아니다. 모든 GREEN 은 fake HTTP·주입 시계·시험용 합성 startup 허가 위의 결과다.

### S3 인수 조건 (누적 — S1·S2 의 리뷰에서 S3 로 넘긴 것. S3 Plan 은 이 목록에서 시작한다)

1. **`hybrid_enabled` 의 policy 대조**(S1 잔여 7·Codex S1 재리뷰 조건): `EffectiveRiskPolicy`(또는 PolicyContext)에 hybrid 축을 실어 `_decision_facts` 에서 `facts.hybrid_enabled == snapshot.policy.hybrid_enabled` 대조, RED "설정 True·facts False → 거부". 필수 필드 추가는 `tests/test_execution_risk_policy.py` helper 등 기존 시험 fixture 에 파급된다 — 동결 예외 범위를 Plan 에서 명시한다.
2. **PolicyContext 제품 publisher 와 `config_version` 실제 게시:** 5축(validator = `kr.validator` 블록 / llm 상수 / sizing = `engine.py` 의 `strategy_position_pct` 리터럴 표·allocation·min_position_value·hybrid / stops = `run_trader._strategy_exit_params` + exit_manager 의 REGIME·INTRADAY_CRASH 표 / experts.shadow_mode)을 factory 가 모은다. 지금은 제품 호출자 0건이라 facts 의 config 축이 자기 일관성뿐이다.
3. **미claim prepared 의 원자적 종료 경로**(Codex wave B P1, 위).
4. **증거 인계:** gateway 는 성공한 on_signal 호출의 증거를 **다음 await/큐 적재 전에 요청 지역값으로** 받는다. sector 조회 뒤에도 거부 return 이 있으므로(`engine.py` G15·G17) `_last_qualification_evidence` 속성의 존재만으로 성공을 판단하지 않는다. `publish_qualification` 호출·`intent_id` 발급은 gateway.
5. **레거시 게이트 순서:** legacy `KRSession` 과 owner `_session_at` 의 경계 표가 다르다 — 기존 후보 판단(거래시간·쿨다운·G15~G17)을 먼저 통과시킨 뒤 owner 로 넘긴다. on_signal 의 상대 쿨다운(30초/300초/90·600초)은 facts 로 재현하지 않는다.
6. **runtime attach 시 legacy 사이징이 owner 재유도보다 커지는 문제:** legacy `_reserved_by_order`·`_pending_*` 가 비어 있어 가용 현금이 과대 → 정상 주문이 `decision_quantity_unjustified` 로 거부될 수 있다. 사이징이 owner snapshot 의 가용 현금을 읽게 할지 Plan 에서 정한다.
7. **schema3 에서 "재유도는 현재 now" 계약 재수립**(S2-3): horizon 의 `classified_at` 당일 게이트가 현재 now 로 평가된다는 축으로.
8. sector 조회 실패가 None 으로 뭉개져 섹터 집중 한도를 "제한 없음"으로 만드는 fail-open(`run_trader.py:1846-1848`).
9. `_dispatch` 가 `_bound` 의 모든 실패를 `claim_not_available` 로 뭉개 사유를 추적할 수 없다(S1 잔여 8).
10. owner 의 `stop_resolver` 가 engine 과 같은 ExitManager 인스턴스인지(판단~final 사이 급락 레벨 변화 시 `decision_stop_changed`), `adapter.regime` 이 owner 미준비 시 None 이 아니라 예외를 전파하는 경로, stale regime 을 인용한 facts 를 게시 단계에서도 막을지.
11. `entry_decision_facts`·`qualification_sources` 정리 writer(S1 잔여 2) — 출처 이름을 종목·전략 범위로 나누면 행 수가 더 빨리 는다. 출처 counter 일별 초기화 금지.
12. 상위 계획의 S3·S4 범위 그대로: `_process_event` 의 SIGNAL 폐기 분기(engine.py:526-533) 대신 gateway 경로, ORDER 는 command ID 만, on_signal 내부 직접 SELL 폴백·취소0건 예약 해제·eviction SELL 의 owner 경로 이관, UNKNOWN 예약 유지·취소 ACK≠최종성.

### 이어받는 에이전트 체크리스트 (S2 공통 — 하위 단계마다 반복)
- [ ] 세부 계획 §4 의 그 단계 "고정 인터페이스"와 실제 시그니처·dict 키가 일치하는가(S2-4·S2-5·S3 가 의존).
- [ ] 허용 파일·허용 구간 밖 변경 0(`git diff --stat <base> <head>`; engine.py 는 hunk 위치까지 확인).
- [ ] 기준선 시험 파일 수정 0줄(`git diff --numstat <base> <head> -- tests/` 에서 새 파일만 나와야 한다).
- [ ] RED 커밋이 제품 커밋보다 앞, 행동 RED 와 부재 RED 를 구분했는가. S2-1 의 특성화 C1~C7 은 "착수 시 GREEN 이 정상"이므로 RED 로 세지 않았는가.
- [ ] 세부 계획에 나열된 변이 각각 ≥1건 실패를 **직접 재현**(실험 뒤 원복·`git status --short` 비어 있음).
- [ ] 새 시험이 `synthetic_home` autouse 를 자체 선언했는가(CV `__init__` 과 규칙11/12 가 `~/.cache/ai_trader` 아래에 쓴다 — 운영 캐시 오염 위험). 벽시계 의존 0, UTC·KST 양쪽 통과.
- [ ] legacy 불변: 특성화·기준선 시험 전건 통과, runtime 미설치에서 게시 호출 0.

## S3 (B3a) — SIGNAL → gateway → owner prepare/dispatch

정직한 성과 문장은 세부 계획 §0 에 있다: **S3 는 설치가 아니다.** 제품에 `KRExecutionRuntime` 생성·`attach()`·`install_gateway()` 호출자는 S3 뒤에도 0건이고, 모든 GREEN 은 fake HTTP·주입 시계·시험용 합성 startup 허가 위의 결과다.

### Plan (완료, 2026-09-21, 기준 `85a65bc`)

- **방법:** 읽기 전용 조사 3관점(engine 흐름 / safety API / 하네스·RED 후보, 요청 claude-opus-5/high) → 설계(요청 opus/high) → **적대적 설계 심사(요청 opus/xhigh, 설계자와 다른 실행): REVISE — must-fix 8건** → coordinator 가 핵심 줄을 직접 대조한 뒤 결정 17건으로 확정. 관측 모델은 전부 metadata 미노출(미검증). 제품·시험 수정 0, pytest 0. workflow `wf_40cf4baa-380`(에이전트 5, 조사·설계·심사 원문은 그 journal).
- **산출물:** `docs/superpowers/plans/2026-09-21-s3-signal-gateway.md` — §1 확정 사실 19 · §2 coordinator 결정 ①~⑰ · §3 계약 8 · §4 하위 단계 7개(고정 인터페이스·RED·변이) · §5 legacy/US 불변 증명 · §6 하지 않는 것 · §7 위험·미확인 · §8 역할.
- **심사 must-fix 8건의 처분(전부 수용, 계획 §2 에 반영):** ② 인계점의 예외 흡수(없으면 owner 예외 한 건이 엔진 루프를 내린다) · ④ `_last_signal_time` 기록 유지(지우면 30초 쿨다운이 attach 에서 무장되지 않는다) · ⑥ (must-fix 2건을 한 결정으로) 사이징 divergence 는 `_reserved_cash` 하나가 아니라 `_pending_strategy_notional` 까지 두 지점이고, `_reserved_cash` 의 소비자에 G5_cash 조기 차단(2169)이 빠져 있었다 — 소비자는 모두 다섯 곳 · ⑩ 정리 writer 의 축약 행이 `commands.py` 인덱싱을 KeyError 로 깬다 → **coordinator 가 범위를 더 줄여 facts 행만 정리**(출처 행은 이름 수로 유계임을 직접 확인) · ⑤ eviction 은 S3 가 송신 경로를 실제로 열어 버리므로 attach 에서 호출 자체를 막는다(설계의 "S4 이관" 기각) · ⑦ `claimed=False` 경로도 같은 prepared 누수 → abandon 대상에 포함 · ⑮ `_pending_sector_map` 이 attach 에서 체결 후 영구 잔류 → 결과와 무관하게 pop.
- **coordinator 가 설계와 다르게 정한 것:** ③ attach 모드에서는 ORDER 이벤트를 큐에 싣지 않는다(prepare·dispatch 를 같은 command_scope 에서) — 상위 계획 문구와 다른 해석이라 **S5 최종 broad 리뷰의 확인 항목**. ⑩ 위. ⑪ engine 은 safety 패키지를 `runtime.gateway` 한 길로만 만나고 예약 읽기 helper 도 gateway 가 제공(engine 에 owner state 해석 복제 금지). ⑯ engine.py 배선을 S3-6a(경로)·S3-6b(정합)로 분할.
- **누적 인수 조건 12항의 처분:** 1 → S3-2+S3-3 · 2 → 대조만 S3-3, 조립은 10A3 · 3 → S3-1+S3-3(**wave 1 의 전제 조건** — `trading_ready` 가 항상 False 라 gateway 를 붙이는 순간 모든 dispatch 가 claim 이전에 실패해 예약이 100% 누수된다) · 4 → S3-6a(계약 3) · 5 → S3-6b · 6 → S3-5 helper + S3-6b · 7 → S3-3 · 8 → 소비 지점 S3-6b, lookup 은 10C · 9 → S3-3 · 10 → S3-5 · 11 → S3-4(facts 만) · 12 → SIGNAL 분기·식별자는 S3, 직접 SELL 폴백·취소0건·eviction 이관은 S4(S3 는 attach 에서 셋이 발화하지 않음만 고정), 실큐 인수는 S5.

### Do·See — 하위 단계별 (진행하며 채운다)

| 단계 | 제품 파일 | 상태 | 구현 SHA | 독립 재현 | 통합 SHA |
|---|---|---|---|---|---|
| S3-1 | `lifecycle.py` | **통합(한정 승인·호출자 0건)** | `52a6ffc`(red)→`86ccc7e`→`58d530f` | CHANGES_REQUIRED(시험 공백 2·설계 쟁점 1) → coordinator `1a6e6d2` 로 해소 | merge `0975624` |
| S3-2 | `risk_policy.py`(+helper 2줄) | **통합(한정 승인·제품 생성자 0건)** | `af09a24`(red)→`f66258d` | CHANGES_REQUIRED(시험 공백 1) → coordinator `1a6e6d2` 로 해소 | merge `2c24aca` |
| S3-3 | `commands.py` | **통합(한정 승인·dispatch 제품 호출자 0건)** | `3a28993`(red)→`d339cd7`→`17725fa` | APPROVE_THIS_SLICE(P2 3건) → coordinator `9a3fe19` 로 해소 | merge `c48cb68` |
| S3-4 | `day_recovery.py` | **통합(한정 승인·돈 경로 아님)** | `feb4349`(red)→`968a097` | CHANGES_REQUIRED(시험 공백 2) → coordinator `9a3fe19` 로 해소 | merge `e42b014` |
| S3-5 | `gateway.py`·`runtime.py` 설치점 (+`commands.py` P1 수정) | **통합(한정 승인·설치 호출자 0건)** | `cadd67a`·`fd03c12`(red)→`737da95`→`05e3104` | CHANGES_REQUIRED(**P1 1**·P2 5) → coordinator `83e187a`(red)·`b83b6e7`·`4356d43` 로 해소 | merge `cb1f554` |
| S3-6a | `engine.py` H1·H4·H5 | **통합(한정 승인·live 파일이나 attach 가드 안 — 제품 attach 호출자 0건)** | `cf8720b`(red)→`0b5ac18` | CHANGES_REQUIRED(P1 시험 공백 1·P2 3) → coordinator 시험 보강으로 해소 | merge `5dac8da` |
| S3-6b | `engine.py` H2·H3·H6 | **통합(한정 승인·attach 가드 안 — 제품 attach 호출자 0건)** | `e8039a6`(red)→`6e5c621` | CHANGES_REQUIRED(P1 시험 공백 1·P2 4·P3 1) → coordinator `9688d1d` 로 해소 | merge `4242b70` |

### wave 1 (S3-1 ∥ S3-2) — Do·See 기록 (기준 `3f30fcc` → `1a6e6d2`)

- **방법:** workflow `wf_82d74f07-3a1` — 단계마다 단일 writer(요청 claude-opus-5/high, harness 격리 worktree) → 독립 재현(요청 opus/xhigh, 다른 실행). 관측 모델 전부 미노출(미검증).
- **S3-1 `abandon_candidate`(`lifecycle.py` +33/−0, 순수 가산):** 단일 `owner.mutate` reducer. 가드 = attempt 존재 · `state=='prepared'` · `claim_id is None` · `command_status is None` · `order_ref is None` · `observed_quantity==applied_quantity==0` · `evidence_conflict` 없음 → 하나라도 어긋나면 무변경 False. 통과 시 `final_rejected`/`not_sent`/`reason_code=<사유>`/version+1 뒤 **기존** `_release_resources`(→`clear_settled_pending_sector`) 재사용. 새 OrderState·새 해제식 없음. 제품 호출자 0건(배선은 S3-3).
  - **RED 의 성격(정직하게):** 새 전이라 RED 13건이 전부 **부재 RED**(AttributeError)다 — 증거로 세지 않는다. 인수 근거는 변이 kill 이다. 유일한 특성화 1건은 "기존 `record_result` 로는 미claim prepared 를 끝낼 수 없다"를 현행 행동으로 고정.
  - 구현자가 자기 변이에서 **`claim_id is None` 가드 제거가 살아남는 것**을 발견(claim 하면 state 가 `submitting` 이 되어 state 가드가 먼저 걷어낸다) → "state 는 prepared 인데 claim_id 가 남은 복구 행" 파라미터를 더해 kill(`58d530f`).
  - 명세와 다른 점(수용·계획서 S3-1 절에 기록): 사유 키는 기존 `reason_code`, 빈 사유는 `ValueError`, `_require_admission()` 미호출(`record_result`·`reconcile` 과 같은 관용구).
- **S3-2 `EffectiveRiskPolicy.hybrid_enabled`(`risk_policy.py` +3/−0):** 마지막 필수 필드(기본값 없음) + `_bool()`(`type is bool`). `policy_snapshot.py` **수정 0줄** — `from_dict` 의 키 집합이 `fields(kind)` 파생이고 `build_owned_snapshot` 이 `replace` 라 자동 전파(둘 다 시험으로 고정). 기존 시험 수정은 결정 ⑰의 helper 2곳 각 1줄 치환(`hybrid_enabled=False` 인자 추가), 단언·기대값 변경 0. `EffectiveRiskPolicy(` 생성 지점은 그 2곳뿐(제품 0건).
  - 행동 RED 5건(기본값·키 누락 거부 1 + bool 아닌 값 4) · 부재 RED 2건(왕복·전파 — 오늘은 hybrid on 인 policy 를 만들 수 없다). **이 축의 실질 행동 RED 는 S3-3 의 `decision_hybrid_mismatch`** 다. 제품에 이 축에 실제 설정값을 싣는 경로는 아직 없다(10A3).
- **독립 재현: 두 단계 모두 CHANGES_REQUIRED — 제품 코드는 명세대로, 살아남은 변이 3종은 전부 시험 공백.** 지정 변이는 전건 kill. 자체 변이 중 생존: ① abandon 가드에서 `state != 'prepared'` 절 제거 ② `attempt['version'] += 1` 제거 ③ `build_owned_snapshot` **비-regime 분기**에서 hybrid 축을 조용히 off 로(그 분기가 S3-3 의 hybrid 대조 상대다).
  - → coordinator `1a6e6d2`: ① state 만 어긋난 복구 행 파라미터 ② version+1 단언 ③ regime owner 없는 state 의 전파 시험(기존 `test_execution_policy_snapshot.baseline` 을 import 만). **세 변이를 직접 다시 적용해 각각 그 시험만 실패**함을 확인 후 원복·트리 clean.
  - **설계 쟁점(재현자 발견 → 결정 ⑧ 확정):** 자식 명령(cancel/modify) 행은 `prepare_candidate` 가 항상 부모 `order_ref` 를 싣기 때문에 `order_ref is None` 가드에 걸려 **구조적으로 abandon 될 수 없다.** 가드를 풀면 "이미 보낸 주문"의 증거를 가진 행을 지우는 길이 되므로 풀지 않는다 → **S3-3 의 abandon 호출은 SUBMIT 한정**, 미claim 자식 명령의 잔류(같은 부모의 이후 취소를 막는다)는 취소를 owner 경로로 옮기는 **S4 의 설계 항목**으로 이월. S3 gateway 는 SUBMIT 만 내므로 S3 범위의 누수는 없다.
- **전체 suite — 아직 깨끗한 증거 없음:** HEAD `1a6e6d2` UTC 1회 = **4654 passed / 2 failed**(426초, 평소 ~290초). 실패 2건은 `tests/test_execution_account_lease.py` 의 프로세스 spawn·fd 시험으로 이번 diff 와 무관한 파일이고 단독 62 passed. 원인은 **다른 사용자 세션**(PID 175570, 새 worktree `serene-wright-7abecf` — 분리해 둔 Toss flaky 작업으로 보인다)이 같은 시각에 pytest 를 돌려 load average 가 10 까지 오른 것. 합계는 4654+2 = 4656 = 4632 + 16(abandon) + 8(hybrid)로 맞는다. **오염된 실행이라 증거로 쓰지 않고, wave 2 경계에서 호스트가 조용할 때(`uptime` 의 load average 확인) UTC→KST 를 다시 돌린다.** 다른 세션은 건드리지 않는다.
  - **정정(wave 2 경계에서 확인):** 위 2건 중 `test_repeated_close_does_not_retain_lease_objects_or_descriptors` 는 **부하 탓이 아니었다.** 조용한 호스트(load 1.2, 292초)의 재실행에서도 그 1건만 실패했고, 원인은 S3-1 의 새 restore 시험이 SQLite store 를 닫지 않아 남긴 fd 였다 — 새 파일 `test_execution_abandon_candidate.py` 가 알파벳 순서상 `test_execution_account_lease.py` **바로 앞**에서 돌고, 그 시험의 `gc.collect()` 가 남은 연결을 거두면서 fd 계수가 틀어진다(두 파일만 함께 돌려 재현). `9572ed2` 에서 `finally: await store.close()` 로 수정, S3 의 새 시험 파일 4개 전부를 lease 시험 앞에 같은 프로세스로 돌려 110 passed 확인. **교훈: "단독으로는 통과"는 부하성의 증거가 아니다 — 순서 의존일 수도 있다. 무관해 보이는 실패는 "직전 파일과 함께" 다시 돌려 본다.** 나머지 1건(`test_two_spawn_processes…`)은 조용한 재실행에서 통과했다(부하성).
- **정리:** 임시 worktree 4개·work 브랜치 2개 제거(clean·merged).

### wave 2 (S3-3 ∥ S3-4) — Do·See 기록 (기준 `ffca70c` → `9a3fe19`)

- **방법:** workflow `wf_d8295400-b95` — wave 1 과 같은 틀(단일 writer 요청 opus/high → 독립 재현 요청 opus/xhigh, 관측 모델 미노출).
- **S3-3 `commands.py`(+36/−2 → coordinator 로거 교체 포함):** ① `_dispatch` 의 claim 이전 `except Exception` 하나를 네 갈래로 — `ApplicationBlocked` 재던짐(바깥이 `command_admission_closed`) / `CommandValidationError` → **사유 보존** + `_unsent` / 그 밖 → `dispatch_failed`(예약 무변) / `claimed=False` → `claim_not_available` + `_unsent` ② `_unsent`: **SUBMIT 일 때만** `abandon_candidate`, False·예외는 로그로 남기고 NOT_SENT 보존 ③ `_decision_facts` 에 `decision_hybrid_mismatch`(`recompose_quantity` 뒤 — 진리표와 이유는 계획서 S3-3 절). 성공 경로·claim 이후 실패 경로는 0줄 변경.
  - **행동 RED 11건(부재 RED 0):** 전부 단언 불일치 — 예) `assert 'claim_not_available' == 'startup_reconciliation'`, `== 'decision_facts_expired'`, `DID NOT RAISE`(설정 hybrid on·신고 off 가 오늘 통과), 그리고 누수 그 자체: dispatch 실패 뒤 `attempts['A']` 가 `prepared`·`reserved_quantity=50`·`reserved_cash='507500.000'`·`pending_sectors == {'005930': '반도체'}` 로 남는다.
  - **기존 시험 기대값 갱신 2파일(좁은 예외 — 줄 단위 보고, 재현자가 "약화 아님" 확인):** `tests/test_execution_market_source.py`(일시 차단 뒤 같은 attempt 가 다시 ACK 된다는 단언 → dispatch 분기는 NOT_SENT·POST 0)·`tests/test_execution_two_minute_regime_owner.py`(claim 이전 실패 뒤 행이 prepare 직후와 글자 하나까지 같다는 단언 → `final_rejected`·사유·예약 0·binding 보존). 둘 다 **이 단계가 닫는 누수를 현행으로 고정했던 단언**이다. 계약 귀결(일시 차단도 attempt 를 끝내고 재송신은 새 prepare)은 계획서 S3-3 절에 기록.
  - 명세대로 세울 수 없었던 것(수용): `claimed=False` 의 "sibling 미해결" 형태는 도달 불가(`_evaluate` 가 먼저 예외) → 대조 시험으로 · schema3 의 now 계약은 동치 변이 → schema1 축에서 고정(인수 조건 7 은 그렇게 닫는다).
  - **독립 재현: APPROVE_THIS_SLICE** — 지정 변이 8 + 자체 7 = 15종 중 1종 생존(시험 공백). P2 3건 → coordinator `9a3fe19`: ⓐ 새 로거가 stdlib `logging` 이라 프로젝트 loguru sink 에 실리지 않는다 → `from loguru import logger`(실행 로그에서 `[실행] 미송신 시도 예약 유지` 출력 확인) ⓑ `_unsent` 의 `ApplicationBlocked` 재던짐에 시험이 없다 → abandon 경합 시험 1건 ⓒ 갱신된 market_source 단언의 특정성 → 사유 코드 `reservation_changed` 단언 추가(실측값).
- **S3-4 `day_recovery.py`(+4/−0):** `reset_daily` 가 `entry_decision_facts` 만 비운다(키가 없으면 만들지 않는다, `qualification_sources` 무접촉). 행동 RED 3건(부재 0): 실제 4단계 전환을 APPLIED 로 통과한 뒤 facts 3건 잔존 / `rollover_day` 직전 7886B → 직후 8636B / 전환 후 재 prepare 의 사유가 `decision_facts_required` 가 아니라 `decision_facts_expired`. 명세의 RED 두 건이 도달 불가·거짓이었던 사정은 계획서 S3-4 절.
  - **부수 발견:** 미claim prepared attempt 가 하나라도 있으면 일자 전환이 `unresolved_submit` 로 **시작조차 되지 않는다** — S3-3 이전의 누수는 예약뿐 아니라 다음 날 rollover 도 막는 결함이었다.
  - **독립 재현: CHANGES_REQUIRED — 제품은 옳고 시험 공백 2건**(자체 변이 4종 중 3종 생존): ⓓ 정리를 `not state.get("attempts")` 에 조건부로 걸어도 통과(전환 시나리오가 백지 state 뿐) ⓔ 전환 writer 가 `entry_policy_effects`·`entry_quotes`·`inbox` 를 함께 비워도 통과(무변경 대조가 손열거 6뿌리). → coordinator `9a3fe19`: 거래가 있었던 날(S3-3 의 abandon 이 남긴 터미널 attempt·intent)의 전환 시험 + 무변경 뿌리를 "전환 직전의 전 뿌리 − 전환 소관"으로 유도·뿌리 키 집합 단언(전환이 `recovery_receipts` 를 더한다는 것도 이때 실측).
- **coordinator 변이 재적용:** 생존했던 4종(ⓑ `_unsent` 의 재던짐을 `pass` 로 · ⓓ · ⓔ 두 뿌리)을 직접 다시 넣어 **각각 해당 시험만 실패**함을 확인 후 원복·트리 clean.
- **wave 1·2 전체 suite(coordinator, HEAD `9572ed2`, 단독 직렬·다른 세션의 pytest 없음 확인, 2026-09-21 02:25~02:35 KST):** UTC **4680 passed / 2 xfailed / 경고 4 / 290.00초**(종료 시 load 1.51), KST **4680 / 2 / 4 / 291.24초**(load 1.29), 각 격리 0. `85a65bc` 의 4632 대비 **+48 = abandon 16 + hybrid 축 8 + 사유 보존 17 + facts 정리 7**. 기존 xfail 2·경고 4 불변. 그 직전의 `9a3fe19` UTC 실행(4679 passed / **1 failed**)은 wave 1 기록의 "정정"에 적은 fd 누수였고 `9572ed2` 에서 고쳤다.
- **정리:** 임시 worktree 4개·work 브랜치 2개 제거(clean·merged).

### Codex 교차 리뷰 1차 (wave 2 경계, 대상 `8ef3b99`) — **APPROVE**

- 요청 gpt-6-astra/xhigh, companion 포그라운드(`--background` 없음)·read-only·pytest 금지, 전체 suite 가 끝나 호스트가 조용할 때 실행. 범위 A = `3f30fcc..8ef3b99 -- src/`(S3 wave 1·2 제품 변경 4파일), 범위 B = `fcc2a27..85a65bc -- src/`(S2 마감 수정). 좁힌 질문 5개(송신됐을 수 있는 attempt 의 예약 해제 경로 / claim 이후 실패의 분류 / hybrid 대조 배치의 fail-open / facts 정리의 fail-open / S2 의 이전 P2 해소).
- **판정: "P0/P1/P2: 지정 diff 에서 확인된 신규 결함 없음."**
  1. `abandon_candidate`: claim 과 `submitting` 을 함께 세우고(lifecycle.py:353) 377-383 이 둘 중 하나만 남아도 거부, abandon 이 먼저 끝나면 `final_rejected` 라 claim 조건(336)을 통과하지 못한다 — 송신 가능 시도의 예약 해제 경로 미발견.
  2. claim commit 뒤에 `CommandValidationError` 가 전파돼 `_unsent` 로 가더라도 저장된 claim 때문에 abandon 이 거부된다(예약 유지). 일반 예외는 `dispatch_failed` 로 끝나고 상태는 claim 유지·`submitting`·예약 유지(permit·전송·결과 기록에 미도달). 같은 attempt 의 중복 dispatch 에서 패자가 받는 NOT_SENT 는 "이 호출이 보내지 않았다"이지 attempt 전체의 미송신 증명이 아니다 — **기준 커밋에도 있던 동작**이고 예약은 유지된다.
  3. hybrid 대조의 배치가 만드는 fail-open 조합 없음(on·off, off·on 모두 불통과).
  4. facts 를 비운 뒤에도 자동 BUY 의 facts 검사는 생략되지 않고(`decision_facts_required`) final guard 가 재검사한다.
  5. **범위 B 는 이전 P2 를 닫는다**(실제 메모리에 전달한 섹터로 digest·이름 생성, 보정 0 과 섹터 불일치도 거부). 새 결함 미발견. → S2 의 "Codex 재확인을 받지 않았다"는 단서는 이 리뷰로 해소된다.
- **Codex 가 "미확인"으로 남긴 것(승인 근거에 미포함):** 부분 상태(`observed_amount`·`status` 불일치)의 유입 가능성 · checkpoint restore 에서 claim 보존 · `owner.mutate` 의 원자성(지정 파일 밖) · transport 내부의 POST 이후 상태 분류 · 실제 rollover 직렬화와 guard→POST 간격 · `recompose_quantity` 내부의 신고 on 거부 구현. 이 중 restore 뒤 가드 성립은 S3-1 시험이, mutate 의 deepcopy-commit 의미는 기존 application 시험이 덮는다. 나머지는 S5 최종 broad 리뷰의 범위다.

### wave 3 (S3-5 gateway) — Do·See 기록 (기준 `d1faf91` → `4356d43`)

- **방법:** workflow `wf_e783d47f-151` — 단일 writer(요청 opus/high) → 독립 재현(요청 opus/xhigh), 관측 모델 미노출.
- **S3-5 `src/execution/safety/gateway.py`(신규 121줄 → coordinator 수정 뒤 120줄)·`runtime.py`(+9/−0: `self.gateway = None` 과 `install_gateway` 뿐):** `SignalGateway.submit(event, order, evidence)` 가 (BUY) 진입 시세 게시 → `publish_qualification` → 감사 로그 → `prepare` → `dispatch` 를 같은 태스크에서 수행한다. engine import 0, owner 판정 복제 0, 같은 attempt 재 dispatch 0(구조적). 실제 인터페이스·예외 계약은 계획서 S3-5 절의 "통합된 실제 인터페이스".
  - **RED 의 성격:** 새 모듈이라 24건 전부 **부재 RED**(ModuleNotFoundError) — 증거로 세지 않는다. owner 측 거부(`decision_facts_required`·`current_entry_quote_required`·`stale_regime_decision` 등)는 S1~S3-4 가 이미 GREEN 으로 만든 계약이라 "부를 주체가 없다"는 형태로만 실패한다. 인수 근거는 변이 kill(구현자 9종 전건).
  - 세우지 못한 것(구현자 보고·수용): ExitManager 가 다를 때의 `decision_stop_changed` 대조(재사용 fixture 가 nominal 이라 손절 축이 비어 있다 — commands 층의 기존 시험이 덮는다) · 대조 3종(무관 fill·무관 취소 ACK·미소비 출처)은 `test_execution_regime_recheck` 의 기존 4건에 의존 · stale 6축은 실제 증거가 정당화하는 수량(약 47주)에 맞춰 크기를 키운 자체 `apply_change`.
- **독립 재현: CHANGES_REQUIRED — P1 1건(실제 누수)·P2 5건.** 지정 변이 전건 kill, 자체 변이 3종 생존.
  - **P1 (직접 재현): prepare 와 dispatch 사이에 세션 경계가 닫히면 예약이 영구 잔류한다.** `_dispatch` 가 `_request`(세션 재검사 포함)를 try **바깥**에서 불러 `CommandValidationError` 가 `dispatch()` 를 탈출 — 재현: `market_closed`·state `prepared`·claim None·예약 101,500원·`pending_sectors` 잔류·abandon 0·POST 0. S3-1·S3-3 이 닫으려던 바로 그 누수이고, gateway 가 prepare **뒤에** dispatch 를 부르므로 창이 실재한다(장 마감 경계·session_guard 거부). → coordinator: 행동 RED `83e187a`(`CommandValidationError: market_closed` 가 탈출함을 실측) → 수정 `b83b6e7`: `_dispatch` 는 `_request(..., session=False)` 로 **신원·권한만** try 밖에서 검증(계속 raise — 기존 시험 `untrusted_entry_context` 계약 유지, 위조 요청이 남의 attempt 를 끝내는 길은 열지 않는다), 세션은 `_bound`→`_evaluate` 의 기존 재검사에서 걸려 `_unsent` 로 끝난다. dispatch 를 구동하는 기존 7파일 252건 회귀 0.
  - **P2-a config 축 자기 인증:** gateway 가 게시본의 config 축을 주입값으로 다시 찍어 재게시 → owner 의 `stale_decision_config_version` 이 한 값을 자기 자신과 비교(변이 생존). → coordinator `4356d43`: **gateway 는 정책 맥락을 쓰지 않는다.** BUY 는 게시 전에 게시본 config ≠ 주입값이면 같은 사유로 거부(owner.version 불변·facts 0), SELL 은 config 로 막지 않는다. 결정 ⑫의 순서를 계획서에서 정정.
  - **P2-b/c 시험 공백:** LIMIT 주문의 평가가격 출처(`valuation = event.price` 변이 생존)·intent 키의 side/전략 성분(`key = (symbol,)` 변이 생존) → 대조 시험 2건.
  - **P2-d 전송 객체:** prepare 뒤에 만들던 `GuardedKISTransport` 를 예약 이전으로 당김. 부수 실측: `engine.broker` 가 None 이어도 누수는 없다 — NOT_SENT/`preparation_failed`·`final_rejected`·예약 0(시험으로 고정).
  - **P2-e 합산식 복제(수용):** helper 두 줄이 `risk_policy.py:716`·`decisions.py:283-284` 와 같은 식을 다시 적었다. 공용 함수로 뽑으려면 B1·S1 파일을 건드려야 해 수용하고, 갈라짐은 S3-6b 의 parity RED 가 행동으로 잡게 한다. **P2-f 인계:** engine 의 `_reserved_cash` 는 property, gateway 쪽은 예외를 내는 메서드 · core_reserve 합산 기준 차이 → 계획서 S3-6b 의 H2 에 명시.
- **coordinator 변이 재적용:** 평가가격(B)·intent 키(C)·config 사전 대조 무력화(A′) 세 변이를 직접 넣어 **각각 해당 시험만 실패**함을 확인 후 원복·트리 clean. P1 은 수정 전 RED 실패를 실측.
- **wave 3 전체 suite(coordinator, HEAD `09dcf77`, 단독 직렬·다른 세션 pytest 없음, 2026-09-21 03:53~04:03 KST):** UTC **4709 passed / 2 xfailed / 경고 4 / 303.16초**(종료 시 load 1.68), KST **4709 / 2 / 4 / 301.97초**(load 1.26), 각 격리 0. `9572ed2` 의 4680 대비 **+29 = gateway 28(구현 24 + coordinator 4) + 세션 경계 RED 1**. 기존 xfail 2·경고 4 불변.
- **제품 호출자 재확인:** `src/`·`scripts/` 에서 `KRExecutionRuntime(`·`.attach(`·`install_gateway(` 호출 0건(grep).
- **정리:** 임시 worktree 2개·work 브랜치 1개 제거.

### Codex 교차 리뷰 2차 (wave 3 경계, 대상 `1442f82`) — **CHANGES_REQUIRED (P0 0 · P1 2 · P2 1)** → coordinator 처분

- 요청 gpt-6-astra/xhigh, 포그라운드·read-only·pytest 금지, 전체 suite 가 끝난 뒤 실행. 범위 `d1faf91..1442f82 -- src/`(gateway·`install_gateway`·`_request(session=False)`). 질문 5개(prepare 뒤 예약이 남는 탈출 경로 / `session=False` 의 fail-open / 증거·facts·config 없이 BUY 가 prepare 에 닿는 경로 / 중복 dispatch·POST / helper 의 과소 집계).
- **확인된 것(결함 없음):** `session=False` 로 인한 fail-open 없음 — prepare(`commands.py:427`)·claim 직전과 claim reducer 의 candidate guard·final guard 가 모두 `_bound`→`_evaluate`→`_session` 으로 이어진다 · 증거/판단 사실 없이 자동 BUY 예약이 승인되는 경로 없음, SELL 이 BUY 검사를 우회해 BUY 를 송신하는 경로 없음 · gateway 내부에 같은 attempt 재 dispatch·POST 재시도 경로 없음(POST 호출은 `transport.py:138` 한 곳, 재시도 루프 없음) · 건강한 owner 에서 helper 는 예약이 남은 터미널·UNKNOWN·부분체결을 빠뜨리지 않는다.
- **P1-a claim 이전 취소가 예약을 남긴다**(prepare 성공 뒤 claim 의 owner lock 대기 중 submit 태스크가 취소되면 `CancelledError` 가 그대로 전파) · **P1-b prepare 도중 shutdown 이 시작되면 dispatch 의 새 `command_scope` 가 거부돼 `command_admission_closed` 만 돌려주고 abandon 은 부르지 않는다.**
  - **coordinator 판단:** 둘은 결정 ⑧이 10A3 의 startup 정리로 미뤄 둔 **같은 부류**(종료·취소 중에는 abandon 자체가 다시 던진다)다. 그리고 프로세스가 prepare 와 dispatch 사이에 **죽는** 경우는 어떤 in-process 정리 경계로도 덮을 수 없으므로, 필요충분한 해법은 "취소에도 완료되는 정리 경계"가 아니라 **다음 기동의 sweep** 이다. 다만 그 부품이 아예 없으면 재시작 뒤 잔류가 영구가 되므로 **부품은 지금 만든다**: `SignalGateway.recover_unsent()` — `kind=='submit'`·`state=='prepared'` 인 행마다 `abandon_candidate(reason='startup_unclaimed')` 를 부르고(누가 끝낼 수 있는지는 그 가드가 정한다 — 재판정 없음), **엔진이 도는 중에는 `gateway_recover_requires_stopped_engine` 로 거부**한다. 제품 호출자는 0건이며 **factory(10A3)가 restore 뒤·엔진 루프 시작 전에 불러야 한다**(계획서 §6 에 명시).
  - 시험(`tests/test_execution_signal_gateway.py`): dispatch 가 `CancelledError` 를 내면 attempt 가 `prepared`·claim None·예약 10주로 남음을 **재현**(Codex P1-a 그대로) → 엔진 실행 중 sweep 거부 → 정지 후 sweep 이 `final_rejected`/`startup_unclaimed`·예약 0 으로 만들고 두 번째 sweep 은 빈 목록 · ACK 된 주문은 sweep 이 글자 하나 건드리지 않는다.
- **P2 복구가 필요한 owner 에서 helper 가 낡은 메모리 state 를 읽는다**(SQL commit 은 됐는데 게시 전 취소 → durable 예약은 있고 메모리는 이전 값) → `_pending()` 이 먼저 `commands._owner_ready(state)` 를 부른다(`store_or_publication_unhealthy`). 실제 dispatch 는 원래 `_owner_ready` 로 막혀 초과 주문 경로는 아니었으나, 사이징이 "예약 없음"으로 읽는 것은 막는다.
- **coordinator 변이 재적용:** 두 가드(엔진 실행 중 거부·helper 의 준비 검사)를 제거하면 **각각 해당 시험만 실패**(2 failed / 29 passed) 확인 후 원복. 수정 커밋 `aee69e3`.
- Codex 가 덧붙인 관찰(조치 없음·기록): `_dispatch` 의 claim **이후** `version` 조회·permit 등록(try 밖)에서 예외가 나면 claim 된 행이 `submitting` 으로 남는다 — POST 전이고 재시작 대사가 "송신됐을 수 있음"으로 보수적으로 읽는다(기준 커밋에도 있던 동작). 송신 단계 취소는 UNKNOWN 기록 후 재전파하며 예약 보존이 타당하다. 실행 재현·실제 KIS 동작은 Codex 도 "미확인".
- **이 처분은 Codex 의 재확인을 받지 않았다** — wave 5 뒤의 3차 리뷰 범위에 `1442f82..<wave 5 HEAD> -- src/execution/safety/` 를 포함한다.

### wave 4 (S3-6a engine 경로 배선) — Do·See 기록 (기준 `d2e899c` → 이 절의 마지막 SHA)

- **방법:** workflow `wf_d7265c3c-a9e` — 단일 writer(요청 opus/high) → 독립 재현(요청 opus/xhigh), 관측 모델 미노출.
- **`src/core/engine.py`(numstat 52 추가 / **1 삭제**) — 운영에서 도는 live 파일:** H1(`_process_event` 의 거부 분기 직전 6줄 + 새 메서드 `_submit_signal` 33줄)·H4(종착부 `_pending_lock` 블록 안의 조기 반환 7줄)·H5(eviction `if` 에 조건 한 줄 — **기존 줄이 바뀐 유일한 곳**). coordinator 가 diff 를 줄 단위로 읽어 확인: 추가 실행 줄은 전부 `_execution_runtime is not None`(H1 은 +`gateway is not None`) 가드 안, H4 의 조기 반환이 건너뛰는 것은 7개 장부 기록뿐(그 뒤에 다른 로그·훅 없음), FILL·ORDER 거부와 gateway 미설치 시의 SIGNAL 폐기는 글자 그대로. legacy 경로에서 새로 평가되는 것은 가드의 첫 피연산자(`is not None`) 하나다. SIGNAL 에 등록된 핸들러는 `on_signal` 하나뿐(grep).
- **행동 RED 13건(부재 RED 0):** 오늘은 527-533 이 SIGNAL 을 폐기해 핵심 시험이 `assert 0 == 1`(POST 0)로 실패 — 핵심 BUY·SELL 송신 2 · 결정 ② 예외 흡수 3(gateway·candidate·`CancelledError`) · 결정 ④ 쿨다운·이중 장부 2 + S4 경계(진입부 stale 루프 무발화) 1 · 결정 ⑮ 2 · 계약 3 1 · SignalEvent 불변 1 · ready=False 예약 0 1. 착수 시에도 통과하는 6건은 의도된 현행 고정·대조(FILL/ORDER 거부 2·gateway 미설치 폐기 1·**legacy 불변 쌍 2**·eviction 미호출 1).
- **독립 재현: CHANGES_REQUIRED — 제품 결함 0, 시험 공백.** §5-4 의 "attach 가드를 항상 참으로" 변이 3종(H1·H4·H5)에서 legacy 대조 시험이 전부 죽는 것은 재현됨. 발견:
  - **P1 계약 3 이 행동으로 고정되지 않았다.** 구현자는 "증거를 submit 시점에 다시 읽음" 변이를 동치로 처분했으나 **틀렸다** — 재현자가 `result[0].order` 접근에 부수효과를 심는 12줄 probe 로 kill 가능함을 보였다. `_last_qualification_evidence` 는 공유 RiskManager 의 가변 속성이라, 반환과 읽기 사이에 await 이 하나라도 끼면 **다른 요청의 증거가 이번 intent 로 게시**된다. → coordinator: `Trap` 객체로 읽기 순서를 관측하는 시험 추가.
  - **P2 결정 ⑮ 가 예외 경로에서 미고정**(pop 을 `finally` 밖으로 옮겨도 19건 통과) — 예외는 이 경로의 정상적인 실패 채널이다(S3-5 계약상 게시~prepare 실패는 raise). → parametrize 에 `'raised'` 추가(+`errors_count` +1 단언).
  - **P2 스텁 공개 불완전:** 재사용한 `_order_env` 가 세션뿐 아니라 `engine.can_open_position`(항상 통과)·`_risk_validator`·`_sector_lookup` 도 스텁한다 — 통과 경로에서 실제 게이트는 돌지 않는다. → 파일 독스트링과 계획서에 명시. **실제 게이트 통과 경로는 S3-6b 가 처음 태운다.**
  - **P2 (S3 범위 밖의 구조적 전제) `bind_execution_runtime` 이 legacy 장부 잔류를 보지 않는다** — 잔류한 `_pending_timestamps` 가 있으면 attach 뒤에도 진입부 90초 stale SELL 이 owner 를 우회해 직접 POST 한다. → **S3-6b 에 H6 로 추가**(bind 시 legacy 장부 3종이 비어 있지 않으면 RuntimeError).
- **coordinator 변이 재적용:** m5(증거를 `order` 접근 뒤에 읽음)·n3(pop 을 `finally` 밖으로) → **각각 새 시험만 실패**(1 failed / 20 passed). m4(인계점의 예외 흡수 제거)는 재현자 기록에 `killed=false` 로 적혀 있었으나 실패 시험 4건을 함께 적은 **표기 오류**였다 — 직접 적용해 5건 실패(kill) 확인. 전부 원복·트리 clean.
- **wave 4 전체 suite(coordinator, HEAD `5d80df2`, 단독 직렬·다른 세션 pytest 없음, 2026-09-21 04:52~05:02 KST):** UTC **4733 passed / 2 xfailed / 경고 4 / 311.35초**(종료 시 load 1.76), KST **4733 / 2 / 4 / 308.37초**(load 1.33), 각 격리 0. `09dcf77` 의 4709 대비 **+24 = Codex 2차 처분 시험 3 + 실큐 배선 21(구현 19 + coordinator 2)**. live 파일 `engine.py` 의 변경이 범위 밖 시험(legacy on_signal 소비 5파일·T11 기준선 포함)을 깨지 않았다. 기존 xfail 2·경고 4 불변.
- **남은 것(이 단계에서 고정하지 않음):** UNKNOWN 의 실큐 접합 쪽 대조 · attach 모드의 체결 메타·pending 교착 감시가 빈 값을 본다는 결정 ④의 이월 사항(10A3/10C) · dispatch 의 network await 동안 엔진 루프가 멈추는 지연 상한.
- **정리:** 임시 worktree 2개·work 브랜치 1개 제거.

### wave 5 (S3-6b engine 정합 배선) — Do·See 기록 (기준 `1d9bed6` → `9688d1d`)

- **방법:** workflow `wf_b958e489-068` — 단일 writer(요청 opus/high) → 독립 재현(요청 opus/xhigh), 관측 모델 미노출.
- **`src/core/engine.py`(numstat **37 추가 / 0 삭제**):** 모듈 함수 `_attached_gateway(engine)`(단일 가드) · H2a `_reserved_cash`(**property 유지**) · H2b `_pending_strategy_notional` — 둘 다 첫머리에서 gateway helper 를 그대로 돌려준다(예외 무흡수) · H3 sector 조회 `except` 안의 attach 거부(`block_gate='G3_sector'`) · H6 `bind_execution_runtime` 의 legacy 장부 검사. coordinator 가 diff 를 줄 단위로 읽어 가드 밖 변경 0 확인. `_calculate_position_size` 본문과 소비 지점 다섯 곳은 불변.
- **행동 RED 10건(부재 RED 0):** B 가 owner `strategy_remaining`(322,950원)을 넘어 `decision_quantity_unjustified`·POST 0(`assert 1 == 2`) · 전략 예산·현금 조기 차단이 owner 예약을 못 봐 발화 0 · `can_open_position(reserved_cash=)` 가 owner pending 을 통째로 누락(600,000 vs 477,050+600,000) · 예약 읽기 실패가 `gateway.submit` 이후에야 드러남 · H6 3건 · sector 조회 예외가 None 으로 통과 등. 표본·core_reserve 결론·on_signal 밖 소비자(`dashboard/data_collector.py:1572`)는 계획서 S3-6b 절.
- **독립 재현: CHANGES_REQUIRED — 제품은 fail-closed 가 맞고(결함 0) 시험 공백.** 지정 변이 전건 kill, 자체 변이 중 3종 생존:
  - **P1 X1** `_reserved_cash` 의 attach 분기**만** 예외를 0 으로 삼켜도 19건 통과 — 바로 뒤의 `_pending_strategy_notional` 이 같은 예외를 다시 내 가려졌다. 전략 배분이 0 이거나 전략이 없는 BUY 는 전략 예산 게이트를 건너뛰므로, 그때 현금 축이 0 을 돌려주면 **한도가 조용히 넓어진다.** → 전략 축을 조용히 둔 채 현금 축만 예외원이 되는 시험.
  - **P2 X2** attach 분기의 전략 필터 상실(전 전략 합 반환)이 생존 — 표본의 미해결 BUY 가 늘 같은 전략 1건뿐이었다. → 다른 전략은 0 임을 단언.
  - **P2 X4** 사이징의 `available = … - self._reserved_cash` 에서 차감을 지워도 parity 19건 + 기준선 239건 전부 통과 — 현금이 넉넉한 표본에서는 전략 축이 먼저 묶인다. → 현금이 실제로 수량을 깎는 구간(무관 보유 115주)을 만들어 owner 예약을 뺀 수량 < 안 뺀 수량을 단언.
  - P2 "네 시계" 서술이 세션 차단 표본에서는 사실과 다름 · P3 걷어내지 못한 스텁(`_risk_validator`·`_check_factor_budget`) 미공개 → 독스트링 정정. P2 대시보드 디버그 통계 → 10A3/10C 이월.
- **coordinator 변이 재적용:** X1·X2·X4 를 직접 넣어 **각각 새 시험만 실패**(1 failed / 21 passed) 확인 후 원복·트리 clean.
- **전체 suite 가 잡은 legacy 회귀 1건(구현자·독립 재현자·coordinator 의 지정 파일 실행은 모두 놓쳤다):** HEAD `bd4cc8c` 의 UTC·KST 두 실행이 똑같이 **1 failed / 4754 passed** — `tests/test_review_fixes_2026_09.py::test_pending_strategy_notional_sums_reserved_cash`. 그 시험은 `object.__new__(RiskManager)` 로 **`engine` 속성이 없는** 인스턴스를 만들어 `_pending_strategy_notional` 을 부르는데, H2 가 첫머리에서 `self.engine` 을 새로 읽어 AttributeError 가 났다 — **legacy 경로에 새 의존을 만든 회귀**다("runtime 없는 legacy 의 실행 줄 차이 0" 은 맞았지만 "engine 없는 인스턴스"라는 축을 아무도 보지 않았다). → `486c7d3`: H2a·H2b 의 가드를 `_attached_gateway(getattr(self, "engine", None))` 으로(helper 는 None engine 을 이미 받는다), 같은 축을 `_reserved_cash` 까지 고정하는 시험 1건 추가. 깨졌던 파일 + parity 45 passed. **교훈: live 파일을 만지는 단계는 지정 파일 GREEN 만으로 통합하지 않는다 — 전체 suite 가 유일한 안전망이었다.**
- **wave 5 전체 suite(coordinator, HEAD `486c7d3`, 단독 직렬·다른 세션 pytest 없음, 2026-09-21 05:56~06:07 KST):** UTC **4756 passed / 2 xfailed / 경고 4 / 321.14초**(시작 load 1.10·종료 1.17), KST **4756 / 2 / 4 / 324.50초**(종료 load 1.08), 각 격리 0. `5d80df2` 의 4733 대비 **+23 = parity 23(구현 19 + coordinator 4)**. 기존 xfail 2·경고 4 불변. **S2 마감(4632) 대비 S3 전체 +124.**
- **제품 호출자 재확인:** `KRExecutionRuntime(`·`.attach(`·`install_gateway(`·`recover_unsent(` 호출 0건(grep).
- **정리:** 임시 worktree 2개·work 브랜치 1개 제거.

### Codex 교차 리뷰 3차 (wave 5 경계, 대상 `3ff04c8`) — **CHANGES_REQUIRED (P0 0 · P1 1 · P2 1)** → coordinator 처분 `10d2ca7`

- 요청 gpt-6-astra/xhigh, 포그라운드·read-only·pytest 금지, 전체 suite 뒤 실행. 범위 `1442f82..3ff04c8 -- src/`(engine.py H1~H6 + Codex 2차 처분인 `recover_unsent`·`_pending` 의 준비 검사).
- **결함 없음으로 확인된 것:** ① **legacy 불변** — runtime 없는 운영 경로에서 실행 결과를 바꾸는 곳 미발견(`_attached_gateway` 와 중첩 `getattr(..., None)` 은 `engine` 없는 부분 생성 RiskManager 에서도 기존 합산식으로 돌아가고, 새 SIGNAL 분기는 단락 평가, H3 는 기존 `_sector=None` 유지) ② `on_signal` 반환~증거 지역값 확보~`gateway.submit` 사이에 끼어드는 await 없음, `CancelledError` 재전파, OrderEvent 를 큐에 넣지 않음 ③ **H2 에서 예외를 0 으로 바꾸거나 한도를 넓히는 경로 없음**(소비 지점 다섯 곳 모두 무흡수) ④ **Codex 2차 P1·P2 의 처분은 타당** — `recover_unsent` 는 `running=True` 를 거부하고 transaction 내부 가드가 재확인하며, claim 은 송신 전에 저장되므로 sweep 이 송신됐을 수 있는 주문의 예약을 푸는 경로 없음. `_owner_ready` 사전 검사도 낡은 집계를 막는다. (자동 기동 호출이 `src/` 에 없으므로 실제 기동 순서의 보장은 "미확인" — 10A3.)
- **P1 H6 은 attach 시점만 본다.** `engine.risk_manager is None` 인 상태로 attach 한 뒤 legacy pending 이 남은 RiskManager 가 연결되면, 다음 SIGNAL 의 진입부 stale 루프가 owner 를 거치지 않고 직접 취소·재주문한다(H4·H5 는 그보다 뒤라 못 막는다). → **행동 RED**(`assert ['cancel_all_for_symbol'] == []` 실패 — 직접 호출이 실제로 일어남) → **H7:** on_signal 의 stale 루프 **직전**에서 attach 이고 legacy 장부 3종 중 하나라도 비어 있지 않으면 RuntimeError(`_submit_signal` 이 흡수 → 그 SIGNAL 만 거부·`errors_count` +1·브로커 직접 호출 0·게시 0).
- **P2 H1 의 `finally` 가 다시 던질 수 있다.** `symbol` 없는 `Event(type=SIGNAL)` 이면 `event.symbol` 접근이 AttributeError 를 내 흡수한 예외를 덮고 run 루프 밖으로 샌다. → 행동 RED(AttributeError 가 `_process_event` 를 탈출) → `getattr(event, "symbol", None)`. 다음 SIGNAL 이 정상 POST 됨까지 단언.
- **S3 최종 전체 suite(coordinator, HEAD `10d2ca7`, 단독 직렬·다른 세션 pytest 없음, 2026-09-21 06:14~06:25 KST):** UTC **4758 passed / 2 xfailed / 경고 4 / 324.28초**(시작 load 0.81·종료 1.56), KST **4758 / 2 / 4 / 319.85초**(종료 load 1.05), 각 격리 0. `486c7d3` 의 4756 대비 +2(Codex 3차 RED 2건). **S2 마감(4632) 대비 S3 전체 +126.** 기존 xfail 2·경고 4 불변.
- engine.py 누적 numstat 은 **98 추가 / 1 삭제**, H7 도 attach 가드 안이다. **이 처분은 Codex 의 재확인을 받지 않았다**(Codex 가 제시한 수정안 그대로이며 RED→GREEN 과 전체 suite 로 확인) — S5 최종 broad 리뷰의 범위에 `3ff04c8..10d2ca7 -- src/core/engine.py` 를 포함한다. **첫 리뷰 범위에 S2 마감 수정 diff `fcc2a27..85a65bc` 를 포함**한다(그 수정은 아직 같은 provider 재현만 받았다).

### 이어받는 에이전트 체크리스트 (S3 공통 — 하위 단계마다 반복)
- [ ] 세부 계획 §4 의 그 단계 "고정 인터페이스"와 실제 시그니처가 일치하는가. §2 의 결정 번호와 어긋나는 구현이 없는가.
- [ ] 허용 파일 밖 변경 0. `engine.py` 는 허용 hunk(H1~H5) 밖 변경 0 이고, 추가된 실행 줄이 전부 `_execution_runtime is not None`(및 `gateway is not None`) 가드 안인가(§5-1).
- [ ] 기존 시험 파일 수정 0줄(`git diff --numstat <base> <head> -- tests/`). 예외는 S3-2 의 helper 2줄뿐이고 기대값·단언 변경 0.
- [ ] RED 커밋이 제품 커밋보다 앞. 행동 RED 와 부재 RED 를 구분해 보고했는가.
- [ ] 세부 계획에 나열된 변이 **전건**과 독립 재현자의 자체 변이 ≥3 이 kill 되는가. "attach 가드를 항상 참으로" 변이에서 legacy 대조 시험이 죽는가.
- [ ] 합성 허가 없는(`ready=False`) 대조가 쌍으로 있고 그때 **예약 0** 인가.
- [ ] 네 시계(legacy `KRSession`·engine 모듈·CV 모듈·runtime 주입)가 같은 순간으로 고정됐는가. UTC·KST 양쪽 통과, 벽시계 의존 0, `synthetic_home` autouse 자체 선언.
- [ ] 실제 SQLite store·runtime 을 여는 새 시험이 `finally` 에서 `await store.close()`(필요하면 `runtime.shutdown()`)를 부르는가 — 남긴 fd 는 같은 프로세스에서 다음에 도는 fd 계수 시험(`test_execution_account_lease.py`)을 깨뜨린다(wave 1 에서 실제 발생). 새 시험 파일들을 그 파일 **앞에 같은 프로세스로** 돌려 확인한다.
- [ ] `src/`·`scripts/` 에서 `KRExecutionRuntime(`·`.attach(`·`install_gateway(` 제품 호출자 0건을 grep 으로 재확인했는가.
- [ ] 전체 suite 는 **어떤 에이전트도 돌지 않을 때** 단독 직렬(UTC→KST)로 돌렸는가.

### S3 의 성과와 한계 (보고 문장 — 이대로 인용한다)

- S3 가 만든 것: runtime 이 붙은 엔진에서 실제 큐의 SIGNAL 이 기존 후보 판단(`on_signal`)을 그대로 거친 뒤 **owner 의 prepare → final 재검사 → dispatch 한 길로만** 송신되는 경로(정상 MARKET BUY·LIMIT/MARKET SELL), 송신하지 못한 요청이 예약을 남기지 않게 하는 종료 전이와 기동 sweep 부품, 그리고 attach 모드의 사이징·한도가 owner 의 예약을 읽게 하는 정합.
- **S3 는 설치가 아니다.** 제품에 runtime 을 만들고 붙이는 코드는 0건이다. `trading_ready` 는 항상 False 이므로 **정상 송신의 표본은 시험 안에만 있다**(fake HTTP·주입 시계·합성 startup 허가). 현금 고갈(펩트론 99.6%)로 정상 BUY 의 실경로 표본도 0건이다.
- **시험이 아직 태우지 않는 것:** 실제 `risk/manager.py` 의 `can_open_position`(`_risk_validator` 가 하네스에서 None)·팩터 버킷 게이트(`_check_factor_budget` 스텁) · UNKNOWN 의 실큐 접합 쪽 대조 · 3건 이상 동시 미해결 BUY 의 누적 정합 · dispatch 의 network await 동안 엔진 루프가 멈추는 지연 상한.
- **진입 가격의 provenance 는 증명되지 않는다** — gateway 가 게시하는 entry quote 의 출처는 SIGNAL 자체(`source='signal'`)다. 실제 market source 결합은 10A2/10A3.
- 독립 재현(같은 provider, 다른 실행)은 7단계 중 6단계에서 CHANGES_REQUIRED 였고 그 가치는 컸다 — 실제 누수 1건(세션 경계)과 살아남은 변이 15종. Codex 는 그 위에서 P1 3건을 더 찾았다(취소·종료 잔류 2, attach 뒤 연결된 legacy 장부 1). **같은 provider 의 독립 재현과 교차 provider 리뷰는 서로 다른 것을 잡는다.**

### S4 진입 조건 (S3 에서 S4 로 넘긴 것 — S4 Plan 은 이 목록에서 시작한다)

1. **on_signal 진입부의 직접 SELL 폴백**(90초 미체결 SELL → `cancel_all_for_symbol` → MARKET SELL `broker.submit_order` 직접 호출)과 **취소 0건을 최종성으로 읽는 예약 해제**(10분 미체결 BUY)를 owner 경로로. S3 는 attach 에서 둘이 **발화하지 않게** 했을 뿐이다(H4 로 순회 대상이 없고, H7 로 잔류가 있으면 SIGNAL 자체를 거부). 그 결과 **attach 모드에는 지금 미체결 SELL 의 시장가 폴백이 없다** — S4 가 owner 경로로 되살려야 하는 기능이다.
2. **eviction**(만석 + 고득점 BUY → 가장 약한 포지션 SELL)의 owner 경로 이관. S3 는 attach 에서 호출을 막았다(H5). 권한 표식을 `metadata['source']='replacement'` 문자열에서 `EntryAuthority` 발급 context 로.
3. **미claim 자식 명령(cancel/modify)의 종료 간선** — `prepare_candidate` 가 자식 행에 부모 `order_ref` 를 싣기 때문에 `abandon_candidate` 로 끝낼 수 없다. 남으면 같은 부모의 이후 취소를 `previous child command unresolved` 로 막는다. 일자 전환도 막는지 S4 에서 확인.
4. **SELL 지정가의 재확인** — SELL 은 evidence·facts·진입 시세를 요구하지 않아, LIMIT SELL 의 지정가(`broker.get_best_bid` await 로 얻은 값)가 판단~송신 사이에 여전히 타당한지 아무도 다시 보지 않는다.
5. **계약 귀결의 전제:** claim 이전의 일시 차단(in-flight market source 등)도 그 attempt 를 끝낸다 — 재송신은 같은 intent 의 새 prepare. 보호 SELL 의 재시도 설계는 이 위에서 한다.
6. UNKNOWN 예약 유지·취소 ACK≠최종성은 lifecycle 에 이미 있다 — S4 는 그것을 실큐 접합에서 대조로 고정한다.
7. 범위 밖(10C): KOFR(`kr_scheduler.py:6754·6818`)·수동 매수(7597)·CLI 2개·`kr_scheduler.py:978-1004` 의 취소0건 예약 해제·`run_trader.py` 의 sector lookup 예외 뭉갬.

## S4 (B3b) — attach 모드에서 되살릴 수 있는 것만 owner 경로로

정직한 성과 문장은 세부 계획 §0 에 있다: **S4 는 "이관"이 아니라 부분 복원이고, 설치가 아니다.** S4 뒤에도 attach 모드에는 미체결 SELL 의 시장가 에스컬레이션과 미체결 BUY 의 타임아웃 취소가 없다 — 보호 SELL 에 관해 legacy 보다 계속 덜 안전하며, 그것이 attach 설치의 차단 사유다.

### Plan (완료, 2026-09-21, 기준 `95029fe`)

- **방법:** S3 와 같은 틀 — 읽기 전용 조사 3관점(legacy 세 경로의 실제 동작 / owner 의 취소·SELL API / 하네스·RED 후보, 요청 claude-opus-5/high) → 설계(요청 opus/high) → **적대적 설계 심사(요청 opus/xhigh, 다른 실행): REVISE — must-fix 5건** → coordinator 결정 9건. 관측 모델 전부 미노출(미검증). 제품·시험 수정 0, pytest 0. workflow `wf_7a788856-a08`.
- **산출물:** `docs/superpowers/plans/2026-09-21-s4-owner-path-restoration.md` — §1 확정 사실 14 · §2 결정 ①~⑨ · §3 계약 7 · §4 하위 단계 4개 · §5 legacy 불변 · §6 하지 않는 것과 그 전제 · §7 위험.
- **Plan 이 뒤집은 것(상위 계획·원장 "S4 진입 조건" 대비 범위 축소 — coordinator 결정, 인계 지시 "공식 취소 증거 미완이면 차단 유지"와 같은 방향):**
  - **취소 최종성은 제품에서 도달 불가능하다**(증거 파서가 취소 체인을 `supported_finality=False` 로 못박고 `lifecycle.reconcile` 제품 호출자 0건·`EXECUTION_FILL` 제품 생산자 0건). 그래서 legacy 의 "취소 → 같은 종목 시장가 재주문"은 owner 위에서 성립하지 않는다 → **90초 SELL 에스컬레이션은 attach 미지원으로 명시**(진입 조건 1 의 절반).
  - **owner 취소의 제품 배선도 하지 않는다**(설계의 cancel sweep 기각) — 심사 must-fix: 취소를 켜는 순간 부모가 영구히 최종화 불가가 되고, claim 이후 transport 실패는 자식을 `RECONCILING` 으로 굳혀 그 종목과 sweep 을 영구히 잠근다. 설계안 그대로면 stale BUY 한 건이 매 sweep 의 `previous child command unresolved` 예외로 **attach 의 모든 SIGNAL 을 무기한 죽였다**.
  - **SELL 지정가 재확인(진입 조건 4)은 "하지 않는다"로 닫는다** — 심사 must-fix: "불리한 방향만 막는다"는 안전장치가 곧 손절 방향이라 하락장에서 보호 SELL 을 체계적으로 거부한다.
  - **연쇄 축출은 owner 필터로 닫히지 않는다**(큐 순서 — 발행된 SELL 은 이미 큐에 있던 BUY#2 뒤에 처리된다) → attach 에서만 전역 600초 1건 상한.
- **S4 가 하는 것:** S4-0 legacy 세 경로의 특성화(오늘 0건·현행 결함까지 그대로 고정·exit_exempt 가드의 첫 시험) · S4-1 미claim 자식 명령의 종료 간선(kind 인지형 가드) · S4-1b `_unsent` 가 자식 명령도 끝낸다 · S4-2 eviction 을 owner 경로로(H5 복원·H9 owner 미해결 필터·H10 상한).
- **"S4 진입 조건" 7항의 처분:** 1 → 10분 BUY·90초 SELL 모두 **미지원 명시**(전제: 취소·체결 최종성 증거 계약 = 10A2/10C) · 2 → S4-2 · 3 → S4-1+S4-1b(ACK/UNKNOWN 자식은 풀지 않는다 — 같은 전제) · 4 → 하지 않는다 · 5 → 전제로 유지(S4 가 보호 SELL 의 재시도 경로를 새로 만들지 않으므로 충돌 없음) · 6 → S4-1b 의 대조 시험이 claim 이후 자식의 상태값까지 고정 · 7 → 10C 그대로.

### Do·See — 하위 단계별 (진행하며 채운다)

| 단계 | 제품 파일 | 상태 | 구현 SHA | 독립 재현 | 통합 SHA |
|---|---|---|---|---|---|
| S4-0 | 없음(특성화) | **통합(제품 0줄)** | `1e0581a` | CHANGES_REQUIRED(시험 공백 — 임계값 미고정·공전 단언) → coordinator 보강 | merge `60a19af` |
| S4-1 | `lifecycle.py` | **통합(한정 승인·제품 호출자 0건)** | `952028c`(red)→`b8a8b7e` | CHANGES_REQUIRED(**제품 결함 P2 1**·시험 공백 2) → coordinator RED→수정 | merge `9afa8e9` |
| S4-1b | `commands.py` | **통합(한정 승인·취소 제품 호출자 0건)** | `a1f0a3e`(red)→`2efb01e` | **APPROVE_THIS_SLICE**(변이 7종 전건 kill, info 2) | merge `d929167` |
| S4-2 | `engine.py`·`gateway.py` | **통합(한정 승인·attach 가드 안 — 제품 attach 호출자 0건)** | `ce1e710`·`75ffb0b`(red)→`3fa9b0e` | CHANGES_REQUIRED(제품 결함 0·시험 공백 2·낡은 독스트링 1) → coordinator 보강 | merge `892112f` |

### wave 1 (S4-0 ∥ S4-1) — Do·See 기록 (기준 `6a6ca30`)

- **방법:** workflow `wf_fbc2a382-957` — 단일 writer(요청 opus/high) → 독립 재현(요청 opus/xhigh), 관측 모델 미노출.
- **S4-0 특성화(제품 0줄, 26건):** runtime 없는 실제 `UnifiedEngine`+실제 inner `RiskManager`+engine 시계 동결, 본문 monkeypatch 0(브로커의 `cancel_all_for_symbol`·`submit_order` 만 심는다). RED 가 아니라 **변이 kill 8/8** 이 인수 근거(폴백 수량 클램프·exit_exempt·폴백 상한·동시호가 분기·+5점·`cancel_ok = cancelled`·승자 보호·코어 보호). 고정한 동작 18항은 계획서 S4-0 절. **드러난 legacy 현행 결함(고치지 않고 고정만 했다):** 동시호가(15:20~15:30)에 취소만 보내고 재주문 없음 · 폴백 상한 뒤 원 지정가 방치 · `submit_order` 예외 시 접수 여부를 모른 채 `clear_pending` · 취소 0건도 최종성으로 읽어 예약 해제 · **`_exit_exempt_ref` 종목도 90초 폴백 루프에서는 그대로 시장가 SELL**(7개 청산 가드 중 이 경로만 누락) · 연쇄 축출.
  - 독립 재현 CHANGES_REQUIRED(자체 변이 6종 중 3종 생존, 전부 시험 공백): ⓐ `_SELL_TIMEOUT 90→150` 생존 — 표본이 전부 200초라 "90초"가 고정돼 있지 않았다 → 경계 시험(89초 미발화/90초 발화) ⓑ 폴백 성공 분기의 `_pending_sides` 재기록 삭제 생존 — 시험이 같은 값을 미리 심어 단언이 공전했다 → 공전 단언 삭제 ⓒ eviction 제외 검사의 순서 교환 생존 — 구현자는 "제품 probe 없이는 관측 불가"로 처분했으나 재현자가 시험 쪽 프로퍼티 probe 로 관측 가능함을 보였다. 다만 다섯 검사가 모두 부수효과 없는 `continue` 라 **재정렬은 결과를 바꾸지 않는다** → 고정하지 않고 사유를 정확히 기록(계획서). ⓓ "신호 없으면 폴백 없음" 시험은 엔진을 시작하지 않아 구조상 실패할 수 없다 → 독스트링에 그 성격을 명시.
  - coordinator 변이 재적용: `_SELL_TIMEOUT = 150` → 경계 시험 `[90-True]` 만 실패 확인·원복.
- **S4-1 `abandon_candidate` 의 kind 인지형 가드(`lifecycle.py`):** 행동 RED 9건(`assert False is True` — 오늘은 미claim 자식을 끝낼 수 없다), 자식 종료는 부모 행을 글자 하나 안 바꾼다. 기존 `test_execution_abandon_candidate.py`(SUBMIT 가드) 0줄 수정·전건 통과, `_unsent` 는 아직 SUBMIT 한정이라 S3 의 해당 시험도 그대로 통과.
  - **독립 재현이 찾은 제품 결함(P2):** 가드가 `if submit: <엄격> else: <완화>` 라 **완화가 기본값**이고 부모를 검증하지 않았다. ACK 된 SUBMIT 을 "미송신 3축"만 되돌린 복구 행이 `kind` 와 `parent_attempt_id` 두 칸만 어긋나면(자기 자신, 또는 같은 order_ref 를 정상적으로 공유하는 자기 취소 자식) 종료되고 예약 (10, 1,000,200, 1,000,000) → 0. 제품 경로에서는 생기지 않지만 이 가드가 방어하기로 선언한 대상이 바로 복구/위조 행이다. → coordinator: **행동 RED 7건**(예상 밖 kind 4·부모가 자기 자신·부모가 다른 자식·부모와 자식의 order_ref 가 둘 다 None) → 수정: positive 형 `elif kind in (cancel, modify)` + 부모 존재·부모≠자신·부모 kind=submit·부모 order_ref 존재 + `else: return state`. 부모 소실·`parent_attempt_id=None` 거부는 대조로 추가(그 절 삭제 변이가 살아남던 공백).
  - coordinator 변이 재적용: 가드를 1차 구현 모양으로 되돌리면 **7건 실패** 확인·원복. 관련 6파일 192 passed(lease fd 시험 포함).
  - 관측해 고정한 현행(판정 보류): 부모가 터미널이고 자식만 남은 표본에서 자식 종료 뒤에도 pending sector 가 풀리지 않는다 — 제품에 취소 호출자가 없어 생길 길이 없고, 취소를 켜는 작업(증거 계약 뒤)의 인수 조건으로 넘긴다.
- **wave 1 전체 suite(coordinator, 단독·다른 세션 pytest 없음, 2026-09-21 07:38~07:43 KST):** UTC **4813 passed / 2 xfailed / 경고 4 / 323.86초**(시작 load 0.56·종료 1.32), 격리 0. `10d2ca7` 의 4758 대비 +55 = 특성화 26 + 자식 종료 29. **KST 는 wave 2 경계에서 함께 돌린다**(이 wave 는 live 파일 `engine.py` 를 만지지 않았다).
- legacy 현행 결함 5건은 운영 경로의 별도 작업으로 분리했다(작업 칩 `task_1d1ae719` — S4 는 고치지 않는다).
- **정리:** 임시 worktree 4개·work 브랜치 2개 제거.

### wave 2 (S4-1b ∥ S4-2) — Do·See 기록 (기준 `982bc17`)

- **방법:** workflow `wf_6c600add-b85` — 단일 writer(요청 opus/high) → 독립 재현(요청 opus/xhigh), 관측 모델 미노출.
- **S4-1b `commands._unsent`(한 hunk):** `if request.command is CommandKind.SUBMIT:` 한 줄을 지우고 본문을 한 단계 dedent — **어떤 명령이든** claim 이전에 실패하면 `abandon_candidate` 를 부르고, 누가 끝날 수 있는지는 lifecycle 의 가드 한 곳이 정한다. `ApplicationBlocked` 재던짐·abandon 예외의 `logger.exception`+NOT_SENT 보존·False 시 경고·`dispatch_failed` 의 abandon 미시도는 글자 그대로. S4-1 로 거짓이 된 독스트링 2곳도 고쳤다.
  - **행동 RED:** `assert [] == [('C', 'request_binding_changed', True)]` — 오늘은 claim 이전에 실패한 cancel dispatch 가 abandon 을 한 번도 부르지 않아 자식이 prepared 로 남고 같은 부모의 두 번째 취소가 `previous child command unresolved` 로 막힌다.
  - **결정 ⑨의 시험 1건 개정**(`test_an_unclaimed_cancel_is_left_for_s4_and_never_abandoned` → `…_is_ended_while_the_acknowledged_submit_is_not`): 단언 반전 + 사유 보존 + **부모 행 전체 dict 동일성**(명세의 "예약 4항"보다 강하다) + 같은 부모에 두 번째 취소 prepare 통과. 줄 단위 보고는 workflow journal.
  - **대조(상태값까지 고정):** claim **이후** 실패한 자식(transport 실패·guard 거부 2종)은 `reconciling`·`unknown` 이고 abandon 이 거부되며 부모 예약이 그대로다 — 이 잔류는 S4 가 풀지 않는다.
  - 독립 재현 **APPROVE_THIS_SLICE** — 지정 변이 + 자체 변이 7종 전건 kill. info: 자식 행의 예약 단언 한 줄은 구조적으로 공허하다(자식 예약은 항상 0 — 원본에서 옮겨온 줄) · 자식의 claim-before 표본이 합성 binding 위조 1종뿐(넷 다 같은 `_unsent` 한 길로 모인다 — 취소를 실제로 켜는 작업의 인수 조건으로).
- **S4-2 eviction 을 owner 경로로(`engine.py`·`gateway.py`):** **H5 복원**(S3-6a 가 붙인 attach 차단 조건 한 줄 삭제 — engine.py 에서 기존 줄이 바뀐 유일한 곳을 원래대로) · **H9** `_try_evict_weakest_position` 의 try **앞**에서 owner 의 미해결 종목을 한 번 읽고(`_attached_gateway(getattr(self, "engine", None))` — 예외 무흡수), try 안 루프 앞에서 `_pending_ref = self._pending_orders if … else …` 로 고정(legacy 장부 읽기는 try 안에 남겨 `object.__new__(RiskManager)` 인스턴스에서 종전처럼 흡수된다) · **H10** attach 에서만 `_REPLACEMENT_LAST_EVICT_TS` 에 쿨다운(기존 상수 600초) 안의 기록이 하나라도 있으면 축출하지 않는다 · `gateway.unresolved_symbols()` = `_pending()` 의 종목 집합(구현 전 확인: `build_owned_snapshot` 의 pending 은 kind=='submit' 만 거르고 side 는 거르지 않아 **미해결 SELL 도 들어온다** — 실큐 probe 로 관측).
  - **행동 RED 10건:** 핵심은 `assert 0 == 1`(H5 가 막아 SELL 0) — 만석 + 고득점 BUY → SELL SignalEvent 1건이 큐에 들어가고 구동하면 owner 경로로 POST 정확히 1건(`broker.submit_order` 직접 호출 0), 원 BUY 는 같은 사이클에 `G3_risk` 거부. exit_exempt·승자·코어 보호, owner 미해결 SELL 종목 제외, **같은 배치의 고득점 BUY 두 건 → 축출 SELL 1건뿐**(상한 경계 599/600초), owner 의 최종 방어선(`unresolved_symbol_attempt`), helper 예외 → 그 SIGNAL 은 `errors_count` +1, ready=False, legacy 불변(H9 가 같은 객체 — `__contains__` 기록 set 으로 조회 순서까지, H10 미적용).
  - **S4-0 의 특성화 26건은 한 글자도 안 고치고 전건 통과**(legacy 의 연쇄 축출 2건 그대로). 결정 ⑨의 S3 시험 1건(`test_eviction_is_not_reached_in_attach_mode` → `…_is_reached_…`) 개정.
  - 독립 재현 CHANGES_REQUIRED — **제품 결함 0**, 시험 공백 2: ⓐ `unresolved_symbols()` 본문이 예외를 삼켜 빈 집합을 돌려주는 변이가 생존(기존 시험은 helper 를 스텁으로 갈아끼워 engine 쪽 전파만 봤다) ⓑ `+5` → `+20` 변이가 생존(attach 하네스의 CV 가 99→86 으로 깎아 13점 여유) + RED 시절 독스트링 1건. → coordinator: helper 자신의 fail-closed 시험(unhealthy owner → `store_or_publication_unhealthy`) + 실효 점수 86 기준 경계 시험(희생 81 → 축출 / 82 → 스킵). **두 변이를 직접 다시 넣어 각각 새 시험만 실패**(2 failed / 18 passed) 확인·원복.
- **S4 전체 suite(coordinator, wave 2 통합 HEAD, 단독 직렬·다른 세션 pytest 없음, 2026-09-21 08:29~08:47 KST):** KST **4835 passed / 2 xfailed / 경고 4 / 344.91초**, UTC **4835 / 2 / 4 / 328.32초**(시작 load 1.54·종료 1.11), 각 격리 0. wave 1 의 4813 대비 +22 = 사유 보존 2 + eviction 20(구현 17 + coordinator 3). **S3 마감(4758) 대비 S4 전체 +77.** 기존 xfail 2·경고 4 불변.
  - 그에 앞선 UTC 1회(`nice -n 10` 으로 돌린 실행)는 **4834 passed / 1 failed** — `tests/dev/test_claude_review.py::test_status_spam_cannot_extend_model_progress_idle_deadline`. 이번 diff 와 무관한 개발용 리뷰 실행기 시험이고 하위 프로세스를 0.12~0.8초의 실시간 시한으로 돌린다. 장 시작 직전(08:30 KST)이라 운영 봇에 양보하려고 낮은 우선순위로 돌렸더니 그 시험이 굶었다. **단독과 같은 디렉터리(`tests/dev`, 직전 파일 포함) 71 passed, 같은 HEAD 의 KST 실행과 기본 우선순위 UTC 재실행 모두 통과** — 순서 의존이 아니라 우선순위·부하성이다. 교훈: 전체 suite 에 `nice` 를 쓰지 않는다(실시간 시한 시험이 있다).
- **제품 호출자 재확인:** `KRExecutionRuntime(`·`.attach(`·`install_gateway(`·`recover_unsent(` 호출 0건(grep).
- **정리:** 임시 worktree 4개·work 브랜치 2개 제거.

체크리스트는 S3 공통 체크리스트를 그대로 쓰고 세 항목을 더한다:
- [ ] S4-0 의 특성화가 **현행 결함까지 그대로** 고정했는가("고치고 싶은" 동작을 섞지 않았는가), 그리고 S4-2 뒤에도 한 글자 안 고치고 통과하는가.
- [ ] exit_exempt·승자·코어 보호가 legacy 와 attach 양쪽에서 변이 kill 로 고정됐는가.
- [ ] S3 시험의 기대값 변경이 계획 결정 ⑨의 **2건뿐**인가(`test_the_entry_stale_loops_…`·`test_a_risk_manager_with_legacy_orders_connected_after_attach_…` 는 변경 금지).
