# B2/B3 단계 원장 — request-bound qualification·최종 사이징

> 이 문서만 읽고 **리뷰하거나 이어받을 수 있게** 단계마다 같은 틀(Plan / Do / See / 체크리스트 / 잔여 / 다음 진입 조건)로 적는다.
> 계약·단계 정의의 정본은 `docs/superpowers/plans/2026-09-20-b2b3-request-bound-qualification.md`, 상위 인계는 `docs/operations/claude-migration-handoff-2026-09-20.md` §1 이다.
> **구분 규칙:** 구현 완료 ≠ 검증 완료 ≠ 운영 승격 · 요청 모델 ≠ 관측 모델 · 합성 시험 ≠ 실경로 검증. 모든 GREEN 은 fake HTTP + 주입 시계 + 시험용 합성 startup 허가(`monkeypatch` `trading_ready`) 위의 결과다.
> **운영 경계(전 단계 공통):** main 병합·배포·재시작·주문·설정·Toss grant 변경 없음. 제품 코드의 `trading_ready` 는 계속 False, MODIFY 미지원. 현금 고갈(펩트론 99.6%)로 정상 BUY 실경로 표본 0건.

## 상태 요약

| 단계 | 범위 | 상태 | 통합 SHA |
|---|---|---|---|
| Plan | 조사 3관점 + 계약·단계 고정 | 완료 | `47fa76b`·`03cc2d6`·`1f8e3ad`·`e8054b0` (문서만) |
| S1 (B2a) | facts DTO·게시 2종·final kernel 재검사 | **리뷰 수정 라운드 진행 중** (미통합) | — |
| S2 (B2b) | 실제 CV/LLM/시간 규칙 publisher | 미착수 | — |
| S3 (B3a) | SIGNAL→gateway→ORDER command ID→dispatch | 미착수 (사전 확인만, 계획서 "S3 사전 확인") | — |
| S4 (B3b) | on_signal 내부 직접 SELL·취소0건 해제·eviction | 미착수 | — |
| S5 (See) | 독립 실큐 인수·최종 broad 리뷰·전체 직렬 | 미착수 | — |

## 공통 작업 방법 (이어받는 에이전트가 먼저 읽을 것)

- **작업 장소:** `/home/ubuntu/projects/qwq-ai-trader/.claude/worktrees/engine-safety-design-20260917`, 브랜치 `feature/engine-safety-design-20260917`. 세션이 다른 worktree 에서 시작되면 파일 쓰기가 거부된다 → `EnterWorktree(path=<위 경로>)` 로 전환(우회 금지). 격리 세션은 복합 git 명령(`&&`, `$(...)`, `-C`, 루프)을 거부하므로 단순 명령을 `;` 로 나열한다. cwd 가 하위 디렉터리에 남을 수 있어 경로는 `:/` 루트 pathspec 이 안전하다.
- **오프라인 시험 명령(접두):**
  `env -i PATH=/usr/bin:/bin LANG=C.UTF-8 TZ=UTC PYTHONDONTWRITEBYTECODE=1 PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 QWQ_DEPLOY_SSH_KEY=/tmp/qwq-offline-no-ssh-key /home/ubuntu/projects/qwq-ai-trader/venv/bin/python -m pytest -q -p no:cacheprovider --tb=short <파일…>` — KST 는 `TZ=Asia/Seoul`. 마지막 줄 위의 `[테스트 격리] … 0건` 을 함께 확인한다.
- **S1 대상 10파일:** `tests/test_execution_decision_facts.py tests/test_execution_command_owner.py tests/test_execution_command_flow.py tests/test_execution_command_shutdown.py tests/test_execution_policy_generations.py tests/test_execution_policy_generation_acceptance.py tests/test_execution_market_source.py tests/test_execution_resources.py tests/test_position_sizing_kernel.py tests/test_execution_sizing_characterization.py` (S1 이전 기준선: 앞 파일 제외 9개 339 passed)
- **호스트 자원이 병렬도의 실제 상한이다:** 이 호스트는 운영 서버(거래 봇·Toss observer 상주)이며 2 vCPU·RAM 3.8GB, 2026-09-20 15:18 KST 관측 가용 871MB·스왑 2GB 100% 사용. pytest 를 도는 에이전트는 동시 2명 이하, **전체 suite(UTC→KST)는 다른 worker 가 없을 때 단독 직렬**로 돌리고 시작 전 `free -m`·`uptime` 을 본다. 다른 세션의 장기 프로세스는 건드리지 않는다.
- **하위 에이전트 worktree:** 수동 `git worktree add` + worker `EnterWorktree` 는 실패한다(Bash 격리 고정점이 부모 worktree 에 남음 — S1 1차 시도 커밋 0). Workflow `agent(..., {isolation:'worktree'})` 로 harness 가 만든 worktree 에서 시작해, 그 안에서 `git switch -c work/<이름> <base SHA>`(구현) 또는 `git switch --detach <SHA>`(리뷰)로 기준을 맞추고 **기준선 시험을 첫 단계**로 돌리게 한다. detached 로 바꾼 리뷰 worktree 는 자동 회수되지 않으므로 끝난 뒤 `git worktree remove <경로>`(dirty 면 스스로 거부)로 정리한다.
- **교차 provider 리뷰(Codex):** `scripts/dev/codex_review.sh` 는 모델·effort 를 지정하지 않아 전역 설정(astra/medium)을 따르고 기준이 main 이면 engine 전체가 범위가 된다. 단계 리뷰는 플러그인 companion 으로 돌린다:
  `node /home/ubuntu/.claude/plugins/cache/openai-codex/codex/1.0.6/scripts/codex-companion.mjs task --background --model gpt-6-astra --effort xhigh "<프롬프트>"` — `--write` 가 없으면 read-only sandbox. 완료 통지가 오지 않으므로 `status <id> --json`, `result <id>` 로 확인한다. 범위는 프롬프트에 `git diff <base>..<head>` 와 `git show <head>:<경로>` 로 명시하고, 실행 전후 `git status --short`·HEAD 를 대조한다.
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

### Do·See (리뷰 수정 라운드) — 진행 중
- 기준 `33b7643`, 브랜치 `work/s1-fixes`. 단일 writer(요청 opus/high) → 독립 재검증 2명(요청 opus/xhigh: 수정 정확성 / 시험 강도). 인수 조건: 변이 (a)(c)(d)(f)(g)(h)(i) 각각 ≥1건 실패.
- 결과는 완료 후 이 절에 채운다: 수정 커밋 SHA, 항목별 상태, 변이 kill 표, 재검증 판정, Codex 재리뷰 판정, 통합 SHA, 전체 suite UTC/KST 수치.

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

### S2 진입 조건
- S1 이 engine 브랜치에 통합되고 전체 suite UTC·KST 직렬 통과·격리 0, 이 원장과 CHANGELOG·계획서·인계 문서가 갱신·push 됨.
- S2 Plan 에서 먼저 정할 것: CV 의 4개 입력(trade_memory·expert_orchestrator·sector_council·panel_outlook)과 market_regime 의 **digest 산출 방법과 게시 시점**, CV 가 facts 를 "추가 반환"하는 형태(기존 `(bool, float, str)` 반환·감점 산식·임계값 불변, `event.score` in-place 변형은 legacy 경로에서 유지), LLM soft-reject 의 `position_multiplier` 이중 곱(event/signal 두 dict) 중 kernel 이 읽는 쪽(`signal.signal.metadata`) 기준 단일 값, 시간 규칙의 `expires_at` 산출(aware KST, `runtime._now()`), `hybrid_enabled`·`base_pct`·`strategy_allocation_pct`·`min_position_value`·`config_version` 의 설정 출처 고정.
