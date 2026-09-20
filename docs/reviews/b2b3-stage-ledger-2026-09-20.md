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
| S2 (B2b) | 실제 CV/LLM/시간 규칙 publisher — 하위 S2-1~S2-5, wave A(S2-1∥S2-2)→B(S2-3∥S2-4)→C(S2-5) | **Plan 완료, Do 착수 전** (세부 계획 `docs/superpowers/plans/2026-09-20-s2-qualification-publishers.md`) | — |
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
| S2-1 CV 특성화 + 증거 채널 | `src/core/cross_validator.py` | `tests/test_cross_validator_characterization.py` | 미착수 | — | — |
| S2-2 사이징 입력 반출 | `src/core/engine.py`(`_calculate_position_size` 내부만) | `tests/test_execution_sizing_inputs_export.py` | 미착수 | — | — |
| S2-3 regime final 재유도 | `src/execution/safety/commands.py` | `tests/test_execution_regime_recheck.py` | 미착수 | — | — |
| S2-4 순수 builder | `src/execution/safety/qualification.py`(신규) | `tests/test_execution_qualification_builder.py` | 미착수 | — | — |
| S2-5 engine 어댑터 배선 | `src/core/engine.py`(어댑터 구간만) | `tests/test_execution_qualification_publishers.py` | 미착수 | — | — |

### 이어받는 에이전트 체크리스트 (S2 공통 — 하위 단계마다 반복)
- [ ] 세부 계획 §4 의 그 단계 "고정 인터페이스"와 실제 시그니처·dict 키가 일치하는가(S2-4·S2-5·S3 가 의존).
- [ ] 허용 파일·허용 구간 밖 변경 0(`git diff --stat <base> <head>`; engine.py 는 hunk 위치까지 확인).
- [ ] 기준선 시험 파일 수정 0줄(`git diff --numstat <base> <head> -- tests/` 에서 새 파일만 나와야 한다).
- [ ] RED 커밋이 제품 커밋보다 앞, 행동 RED 와 부재 RED 를 구분했는가. S2-1 의 특성화 C1~C7 은 "착수 시 GREEN 이 정상"이므로 RED 로 세지 않았는가.
- [ ] 세부 계획에 나열된 변이 각각 ≥1건 실패를 **직접 재현**(실험 뒤 원복·`git status --short` 비어 있음).
- [ ] 새 시험이 `synthetic_home` autouse 를 자체 선언했는가(CV `__init__` 과 규칙11/12 가 `~/.cache/ai_trader` 아래에 쓴다 — 운영 캐시 오염 위험). 벽시계 의존 0, UTC·KST 양쪽 통과.
- [ ] legacy 불변: 특성화·기준선 시험 전건 통과, runtime 미설치에서 게시 호출 0.
