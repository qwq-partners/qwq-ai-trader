# 10A3 — 설치 준비 (세부 계획)

> 상위: `docs/operations/claude-migration-handoff-2026-09-20.md` 의 "attach 설치 전에 닫아야 할 것"(15항)·§2 · 기준 SHA `f0593d9` · 2026-09-21
> Plan 산출 과정: 읽기 전용 조사 3관점(factory·설치/종료 순서 / 정책·설정 출처 / S3~S5 이월 항목, opus/high) → 단계 설계(결정 18·하위 단계 5, opus/high) → 적대적 심사 2관점(safety·scope, opus/xhigh, **둘 다 REVISE — must-fix 13건**) → coordinator 가 핵심 주장을 코드로 재확인하고 이 문서로 처분. pytest 0·제품 수정 0. 요청 모델 claude-opus-5(관측 모델 미노출 — 미검증).

## 0. 한 줄 요약과 coordinator 의 범위 결정

- 설계는 5단계(정책 builder·관측 배선·시계/표시·owner 게이트 인수·설치 factory)였다. **심사가 보여준 것은 "설치 factory 본체는 증거 계약과 사용자 결정 없이는 명세할 수 없다"는 것이다**(§2). 그래서 10A3 을 둘로 나눈다.
  - **10A3a (지금 한다):** ① 로드된 설정에서 owner 정책을 만드는 부품(신규 모듈 1개·제품 호출자 0) ② owner 각 게이트의 단독 결정·누적 정합·#12 범위·면제 충돌을 세우는 인수 시험(제품 0줄). **기존 제품 파일은 한 줄도 고치지 않는다.**
  - **10A3b (미룬다 — 전제: 아래 §3 의 사용자 결정 + 공식 KIS 증거):** 설치 factory 본체 · attach 인지형 관측(대시보드·SIGNAL 표시) · CV/교체 쿨다운의 시계 주입 · 사이징 표의 모듈 상수화. 넷 다 **attach 가 실제로 설치될 때에만 의미가 생기는** 변경이고, 뒤의 셋은 live 파일(`engine.py`·`cross_validator.py`·`data_collector.py`)을 만진다 — 호출자가 없는 지금 만들면 소비자 없는 변경이다.
- **10A3a 는 차단 사유를 "닫지" 않는다.** 제품 어디에서도 정책을 조립하거나 게시하지 않으므로 #11·#15 는 "부품 완성 — 설치 단계에서 호출자와 함께 닫힘 확인"이고 15항 표에서 열린 채로 둔다(심사 must-fix: 잘못된 닫힘 표기는 다음 세션의 설치 판단을 낮춘다).
- 불변: main 병합·배포·재시작·주문·설정·Toss grant 무변경, `trading_ready=False`, MODIFY 미지원, 독립 인수 37건 기대값 변경 0.

## 1. Plan 이 코드로 확정한 사실 (인계 문서를 고친다)

1. **UNKNOWN 1건의 정지 범위는 "자동 매수"가 아니라 attach 의 모든 새 명령이다.** `commands._evaluate` 의 `unresolved_execution_evidence` 검사는 side·kind 를 가리지 않는다(`commands.py:346-348`) — `blocked_unknown` 인 **SUBMIT** 행이 하나라도 있으면 **보호 SELL 과 CANCEL 도 거부된다.** S5 의 B1 은 다른 종목 BUY 로만 확인했다. 차단 사유 12 를 이 범위로 고친다. **범위 한정(Codex 6차):** 가리지 않는 것은 **새 요청**의 side·kind 이고, 전역 정지를 일으키는 **기존 행**은 SUBMIT 뿐이다(`commands.py:342` 가 기존 비-SUBMIT 행을 건너뛴다). UNKNOWN 인 자식 명령은 전역 정지가 아니라 같은 종목의 새 SUBMIT 만 `unresolved_child_attempt` 로 막는다 — 그 대조 표본은 아직 없다(취소를 켜는 작업의 인수 조건).
2. **`runtime.restore()` 는 live 를 덮어쓴다.** 마지막 줄의 게시가 `engine.portfolio` 의 현금·포지션과 ExitManager 의 상태를 checkpoint 값으로 교체한다 — 제품 기동이 KIS 잔고·포지션 로드·면제 복원·`register_position` 으로 만든 값이 전부 사라진다. 그래서 "restore 뒤에 live 와 대조"는 항진명제이고(게시본 대 게시본), 대조는 restore **앞**이어야 한다. 설치 도중 실패하면 live 가 저장본 값으로 남는다는 것도 계약으로 적어야 한다.
3. **일자 전환 없이는 아침 재기동이 설치 불가다.** checkpoint 의 risk day 가 오늘이 아니면 `day_transition_admission_closed` 로 RegimeOwner 생성·정책 게시·모든 prepare 가 죽는다. 전환 의식(평가 증거의 출처 포함)을 누가 언제 하는지는 아직 정해지지 않았다.
4. **레짐 baseline 을 factory 가 만드는 것은 포트폴리오 baseline 과 같은 성격의 "증거 없는 최초 인계 주장"이다.** checkpoint 에 이미 있으면 재등록은 `regime_baseline_conflict`(시각이 달라 canonical 이 같을 수 없다), 없으면 `regime_baseline_required`.
5. **진입 시세의 "실제 source 요구"는 소스 주석이 A3 factory 에 배정해 둔 일이다**(`market_source.py:203` — `source_is_current` 는 `market_sources` 행이 없으면 True 를 돌려준다). 지금 attach 의 BUY 는 gateway 가 스스로 찍은 SIGNAL 가격만으로 성립한다. 10A3b 가 닫거나, 10A2 로 넘기고 그 주석을 고쳐야 한다(차단 사유 6 에 명시).
6. **`recover_unsent()` 는 미claim 자식 명령을 쓸지 않는다**(`kind=='submit'` 만 순회). 남은 prepared CANCEL 은 같은 종목의 모든 새 SUBMIT 을 `unresolved_child_attempt` 로 영구 차단한다 — 설치 직전의 잔존 검사는 **kind 무관**이어야 한다. ("자식은 구조적으로 abandon 불가"는 틀렸다 — `abandon_candidate` 는 조건이 맞는 자식도 끝낸다, S4-1.)
7. **CV 의 벽시계는 `now_hm` 하나가 아니다.** `_panel_loaded_at`(`cross_validator.py:145·168·171`)도 결정 dict 로 반출돼 owner 의 만료 계산(`qualification.entry_expires_at`)에 들어가고 naive 를 KST 로 해석한다. 운영(실제 시각·KST 호스트)에서는 일치하므로 **운영 영향은 낮고** 재생·시험·시계 주입 설계의 전제다 — 차단 사유 13 의 서술을 그렇게 고친다.
8. 제품 생산자 0건인 것들: `EffectiveRiskPolicy(`·`qualification.config_version(`·`publish_policy_context(`. 전부 시험 리터럴이다. `_build_risk_config` 로더는 `max_core_positions`·`daily_exit_cooldown_threshold`·`hybrid` 를 읽지 않아 그 축은 dataclass 기본값으로 돈다(운영 동작 — 10A3 에서 바꾸지 않는다). 매수 수수료율은 `FeeConfig`(0.000140527)와 `TradingConfig.buy_fee_rate`(0.00014)가 공존한다.
9. 인수 하네스에서는 `attach()` 가 `engine.risk_manager` 설정보다 앞이라 legacy 미체결 장부 가드(`engine.py:301-304`)가 무하중이다. 제품 순서에서는 그 가드가 처음으로 발화한다.

## 2. 설치 factory 본체를 미루는 이유 (심사 must-fix 의 요지)

| 심사 지적 | 뜻 |
|---|---|
| restore 뒤 live 대조는 항진명제 · 실패 시 live 가 저장본 값으로 남는다 | 대조의 기준(KIS 잔고 대 checkpoint)이 곧 차단 사유 2(startup 대사)다 |
| 고정 순서에 일자 전환이 없다 | 전환 의식의 증거 출처가 미정 |
| RegimeOwner·baseline 공급이 시그니처에 없고 재기동에서 구조적으로 충돌 | baseline 의 작성 주체가 미정(사실 4) |
| config 축의 실효는 호출자가 있어야 생긴다 | 주기 재게시의 주체(스케줄러 배선)가 10C |
| 최초 checkpoint 를 누가 만드는가 | 차단 사유 2 그 자체 |

다섯 모두 **사용자 결정(§3)이나 공식 증거를 기다린다.** 지금 factory 를 쓰면 이 자리들을 추측으로 채우게 된다.

## 3. 10A3b 에 앞서 필요한 사용자 결정 (coordinator 의 보수적 기본값 — 뒤집을 수 있다)

1. factory 의 제품 호출자: **기본값 = 두지 않는다**(CLI 플래그·환경변수·config 키 모두 없음. config 키는 진화 시스템이 `evolved_overrides.yml` 을 쓰므로 어느 경우에도 금지).
2. factory 가 최초 checkpoint·레짐 baseline 을 만드는가: **기본값 = 만들지 않는다**(없으면 거부). 허용은 곧 "최초 계좌 인계 주장"이다.
3. 런타임 `add_exit_exempt`(`kr_scheduler.py:7537·7614`)의 처분: **기본값 = 10C 의 owner command 와 한 덩어리로.** 조기 반환 한 줄은 "면제가 등록되지 않는 조용한 실패"를 만들어 펩트론류 종목에 위험하다.
4. 설정이 프로세스 수명 중 바뀌면(진화 apply) BUY 가 재시작까지 `stale_decision_config_version` 으로 막히는 것: **기본값 = 수용(fail-closed)하고 runbook 에 절차를 적는다.** 대안은 gateway 재설치 API(새 상태 전이).
5. 매수 수수료율의 정본: **기본값 = `FeeConfig`**(10A3a 의 builder 가 이것을 읽는다). 이원화 해소는 별도 과제.
6. 일자 전환 의식과 설치 순서의 결합 방식 — 공식 증거 뒤.

### 3-1. 결정 확정 (2026-09-21 15:55 KST — 사용자 위임)

사용자 지시 원문: "니가 추천 및 확인해서 진행해줘". coordinator 가 **여섯 건 모두 위 보수적 기본값으로 확정**한다 — 전부 운영을 건드리지 않고(`scripts/run_trader.py` 0줄·설정 키 0) 나중에 뒤집을 수 있는 선택이다. 6번은 "설치기는 일자 전환을 하지 않고, 입장이 닫혀 있으면 전용 사유로 거부한다"로 확정한다(전환 의식 자체는 증거 계약 뒤).

함께 확정한 **진행 순서와 10A3b 의 범위:**

- **① KIS 증거 좁히기(병행):** 차단 사유 1·2·12 는 공식 증거로만 열리고 coordinator 가 그 증거를 직접 얻을 수 없다(실 API 호출·실주문·운영 로그 열람은 위임 범위가 아니다). 공개 공식 자료를 한 번 더 훑어 **필드 정의 수준의 근거**를 고정하고, 남는 질문을 **KIS 에 그대로 보낼 질문지**와 **비식별 응답 수집 체크리스트**로 바꾼다 — 막연한 차단을 사용자가 실행할 수 있는 요청으로.
- **② 10A3b 는 "거부형 설치기" 하나로 좁힌다.** 증거 결과가 무엇이든 변하지 않는 것은 설치 **순서**와 **명명된 거부**다: checkpoint 를 먼저 읽고 restore **앞**에서 live 와 대조 · 일자 입장이 닫혀 있으면 거부 · 레짐 baseline 이 없으면 거부 · RegimeOwner 설치를 명시 guard 로 · `recover_unsent()` 뒤 **kind 무관** 잔존 검사 · gateway 의 digest 는 기동 시 고정. 증거가 뒤에 더하는 것은 "checkpoint·baseline 을 **어떻게 만드는가**"라는 설치기 **앞** 단계뿐이다. 설치기는 `factory.py` 에만 추가되고(live 파일 0) 제품 호출자는 0건으로 둔다. 제품에는 그것을 통과시킬 checkpoint 를 만드는 코드가 없으므로 **운영에서는 항상 거부로 끝나는 것이 정상**이다.
- **미룸(실제 설치 단계로):** CV·레짐 어댑터·교체 쿨다운의 시계 주입, attach 인지형 관측(대시보드·SIGNAL 표시), 사이징 표의 모듈 상수화. 셋 다 live 파일(`cross_validator.py`·`engine.py`·`data_collector.py`)을 만지는데 설치가 없는 지금은 소비자가 없다 — 특히 사이징 표는 돈 경로의 리팩터다. §5 의 설계 메모는 그대로 유효하다.

## 4. 10A3a 하위 단계

### S10A3a-1 — 로드된 설정에서 owner 정책을 만드는 부품 (신규 `src/execution/safety/factory.py`·`tests/test_execution_policy_factory.py`, 기존 제품 파일 0줄)

- `effective_risk_policy(risk: RiskConfig, *, regime: str, fee_config) -> EffectiveRiskPolicy` — 전 필드를 **인자 인스턴스**에서 유도한다(YAML dict 오버로드 금지). `core_allocation_pct = risk.strategy_allocation['core_holding']`(키 부재는 0 으로 뭉개지 않고 거부), `regime_min_cash_reserve_pct = MarketRegimeAdapter.REGIME_PARAMS[regime][...]`, `buy_commission_rate = fee_config.buy_commission_rate`, `hybrid_enabled = risk.hybrid.enabled`.
- `execution_config_version(*, validator_config, risk, position_pct, stop_params, exit_config, experts_shadow_mode) -> str` — `qualification.config_version` 을 감싸는 얇은 어댑터. 새 해시 체계를 만들지 않는다. 5축의 정본은 그 함수의 docstring.
- `async publish_entry_policy_context(commands, *, risk, sidecar, regime_adapter, fee_config, validator_config, position_pct, stop_params, exit_config, experts_shadow_mode, now) -> str` — **`config_version` 을 인자로 받지 않고 안에서 유도해 게시하고 그 digest 를 돌려준다**(심사 must-fix: 인자로 받으면 owner 의 대조가 한 값을 자기 자신과 비교하는 지금 상태 그대로다). `macro` 축은 모듈 함수 `src.utils.macro_calendar.is_macro_event_day()`(sidecar 메서드가 아니다). sidecar 의 naive 시각(`_sync_unhealthy_since`)은 qualification 과 같은 규칙(naive = KST 지역시각)으로 aware 로 만들고 그 규칙을 docstring 에 적는다. `versions` 의 execution/quote/risk/protection 은 게시 직전 `owner.version` 으로 채우고 `expected_version` 과 같은 값을 쓴다. 어느 축이 owner 에게 실제로 읽히고 어느 축이 재유도로 덮어써지는지 docstring 에 표로 남긴다.
- RED(요지): dataclass 기본값이 아니라 **인스턴스 값**에서 나온다(운영값을 세운 RiskConfig 로) · 로더가 안 읽는 2축은 인스턴스 값 그대로 · 수수료 두 값의 공존을 단언으로 고정 · hybrid 출처 하나 · core 키 부재 거부 · 레짐별 최소 현금 · config_version 5축 각각 한 값만 흔들면 digest 변화(+축 밖 값은 불변) · 정규화 없는 Decimal/datetime/enum 은 거부 · **게시본 정책 전 필드가 게시 시점 객체에서 다시 유도한 값과 전건 일치** · 일자 불일치 거부 · sync 축 · `from_dict(to_dict())` 왕복.
- 변이: 인자를 무시하고 `RiskConfig()` 를 새로 만듦 · 수수료를 `TradingConfig` 에서 · core 에 `.get(..., 0)` 폴백 · 레짐 최소 현금을 `risk.min_cash_reserve_pct` 복사 · config_version 축 제거 3종 · publisher 가 정책 사본 재사용 · `expected_version` 고정값.
- legacy 불변: `git diff --numstat <base>.. -- src/ scripts/` 가 `factory.py` 한 행. 세 함수의 제품 호출자 0건(grep). 기존 경로는 이 모듈을 import 하지 않는다.

### S10A3a-2 — owner 게이트의 단독 결정·누적 정합 인수 (신규 `tests/test_execution_owner_gate_authority.py`, 제품 0줄)

- 하네스는 `tests/test_execution_runtime.setup` 위에 인수 파일과 **같은 순서**로 새로 세운다. `tests/test_execution_signal_gateway_acceptance.py` 를 import 하지 않는다(autouse fixture 가 모듈 경계를 넘지 않는다). S3·S4 시험 모듈도 import 하지 않는다. 정책은 하네스 안의 명시 구성으로 만든다(S10A3a-1 과 병렬로 돌 수 있게 — builder 에 의존하지 않는다): **축마다 owner 의 값만 sidecar 보다 엄격하게** 두어 owner 게이트가 단독으로 결정하는 상황을 만든다.
- RED: 단독 결정 4건(최소 현금·최대 포지션 수·당일 손절 재진입·섹터 한도 — 섹터는 두 판정 지점이 같은 사유 문자열이라 구분 불가임을 단언으로 남긴다) · 누적 3건(3종목·3섹터·2전략의 미해결 BUY → 예약 합·pending 포함 최대 포지션·pending 으로 채워지는 섹터 한도·전략별 `pending_strategy_notional`) · **#12 범위**(`blocked_unknown` 1건 뒤 보호 SELL SIGNAL 과 CANCEL prepare 가 `unresolved_execution_evidence`) · **면제 충돌**(런타임 `exit_manager.add_exit_exempt` 뒤 다음 명령이 어떤 사유로 끝나는지, 다음 게시가 그것을 지우는지 — 제품이 실제로 하는 대로 고정) · legacy 장부가 남은 채 attach 거부(제품 순서 — `engine.risk_manager` 를 attach 앞에 설정).
- 변이: owner 의 네 게이트를 각각 통과로(4건) · pending 합산에서 다른 attempt 제외 · `unresolved_execution_evidence` 에 BUY 한정 분기 추가 · `_owner_ready` 의 보호 등식 제거.
- 제품 결함 후보가 나오면 시험으로 덮지 않고 보고한다(파일에 RED·xfail 금지).

### wave

S10A3a-1 ∥ S10A3a-2 (만지는 파일이 겹치지 않고 live 파일 0). 각각 구현(요청 opus/high) → 독립 재현(요청 opus/xhigh, 변이 kill 실측·자체 변이 ≥5) → coordinator 통합 → 전체 suite UTC→KST 단독 직렬 → Codex Astra/xhigh 교차 리뷰(포그라운드).

## 5. 10A3b 로 미룬 것의 설계 메모 (다음 Plan 의 출발점 — 심사 처분 반영)

- **설치 순서:** 대조는 restore **앞**(`store.load()` → `_owner_ready` 와 같은 정규화로 live 와 비교 → 불일치면 `startup_reconciliation_required`, checkpoint 부재는 `startup_checkpoint_required`). `day_admission_closed` 면 전용 사유로 거부. `regime_policy` 가 없으면 `startup_regime_baseline_required`, 있으면 등록을 건너뛰고 RegimeOwner 만 구성. RegimeOwner 설치와 `regime_policy` 존재를 명시 guard 로(첫 평가 시점의 강제에 기대지 않는다). `recover_unsent()` 뒤의 잔존 검사는 **kind 무관**. 설치 도중 실패는 live 를 저장본 값으로 남긴다 — 호출자는 legacy 로 계속 가면 안 된다는 것을 계약으로.
- **시계:** CV 의 `clock=None` 기본 주입은 `now_hm` 과 `_panel_loaded_at` 을 **같이** 옮긴다(하나만 옮기면 축이 둘로 남는다). **세 번째 벽시계(10A3a 재현자가 실측):** `MarketRegimeAdapter.effective_regime` 이 `src/core/market_regime.py` 의 `_now()`(naive `datetime.now()`)로 장중 위험의 "당일" 게이트를 판정한다 — publisher 가 읽는 `regime_adapter.regime` 이 이 경로다. engine 내부 `_now()`(naive KST)를 CV 와 교체 쿨다운이 공유. 교체 상한의 재시작 경계는 owner 이관 대신 기동 stamp 한 줄이 후보.
- **관측:** gateway 에 예외를 내지 않는 읽기 하나 + 대시보드 2곳. SIGNAL 표시 블록을 attach 조기 반환 앞으로. `_reserved_cash` 의 fail-closed 의미는 바꾸지 않는다. health monitor 의 pending 교착 검사는 체결 공급이 생기기 전에는 배선하지 않는다(상시 경보가 된다).
- **사이징 표**를 모듈 상수로(`config_version` 의 sizing 축이 복사본이 아닌 실물을 읽도록) — 호출자가 생길 때.

## 6. 10A3b — 거부형 설치기 (§3-1 의 확정 범위, 2026-09-21)

> 산출 과정: 설계(요청 opus/high) → 적대적 심사 2관점(요청 opus/xhigh, **둘 다 REVISE — must-fix 14건, 서로 겹치며 방향이 같다**) → coordinator 처분. 심사의 요지는 하나다 — **거부할 수 있는 것은 전부 live 를 건드리기 전에 거부하고, 되돌릴 수 없는 줄(attach)은 맨 끝으로.**

### 6-1. 인터페이스 (`src/execution/safety/factory.py` 에 추가, 다른 제품 파일 0줄·제품 호출자 0건)

```python
async def install_attached_runtime(runtime, commands, *, sidecar, regime_adapter, fee_config, risk,
                                   validator_config, position_pct, stop_params, exit_config,
                                   experts_shadow_mode, now, vix_fetcher) -> SignalGateway
```

- 인자는 전부 명시값이다(`KRTradingBot`·설정 파일·환경변수·config 키를 모른다). 클래스·opt-in·새 추상화 없음. **`vix_fetcher` 는 기본값 없는 필수 키워드**다 — 기본값 None 은 "VIX 없음"이 아니라 실제 네트워크 조회를 설치한다.
- 제품에는 이 함수가 요구하는 checkpoint(포트폴리오·보호 대사 + 레짐 baseline + policy generation 등록)를 만드는 코드가 없다 — **운영에서는 항상 거부로 끝나는 것이 정상**이다. 기동 시 **단일 호출자**가 전제다(계좌 lease 를 잡지 않는다).
- 예외 관례: 인자 모양은 `ValueError`, 상태 거부는 `ApplicationBlocked(<snake_case 사유>)`, store 장애(`StoreError`)와 `attach()` 의 `RuntimeError` 는 재작명하지 않고 그대로 올린다.

### 6-2. 고정 순서

**구간 1 — 아무것도 바꾸지 않는다(live·owner·기존 store 내용 무변경, 순수 읽기).** 여기서 끝나면 호출자는 legacy 로 계속 가도 된다.

1. 인자 모양: aware `now` · `type(commands) is RequestBoundCommands` · **`commands.runtime is runtime and commands.owner is runtime.owner`**(아니면 `ValueError('invalid_command_binding')` — 잘못 배선된 commands 는 남의 store 에 게시한다) · `execution_config_version(...)` 과 `effective_risk_policy(risk, regime=regime_adapter.regime, fee_config=fee_config)` 를 **미리 한 번 불러**(결과는 버린다) 정책 인자의 모양 결함을 앞당긴다.
2. 바인딩 선검사(전부 `ApplicationBlocked`): `engine.running` · `engine._execution_runtime` 존재 · **`engine._event_queue` 비어 있지 않음** · **legacy 미체결 장부 3종 중 하나라도 있음**(`legacy_pending_orders_present` — 제품 순서에서 처음 하중되는 가드를 restore 앞으로) · `runtime.gateway` 존재 · `_regime_writer` 존재/sidecar·adapter 가 runtime·engine 에 묶인 객체가 아님(`regime_owner_binding_conflict`) · **owner 가 이미 복구됨**(`runtime.owner.version != 0 or runtime.owner.state` → `execution_runtime_already_restored` — 설치 실패 뒤의 재호출에서 아래 8번 대조가 항진명제로 되살아나는 것을 구조로 막는다).
3. 면제 별칭: `engine.risk_manager._exit_exempt_ref is exit_manager._exit_exempt`(동일성) 아니면 `exit_exempt_alias_required`. 주입은 호출자 몫이고 설치기는 확인만 한다.
4. checkpoint 읽기: **store 파일이 없으면 만들지 않고** `startup_checkpoint_required`(`store.load()` 는 없는 파일을 생성·초기화한다 — 읽기가 아니다). 있으면 `store.load()` → 필수 root 집합이 없으면 같은 사유. **store 의 수명은 호출자 소유다 — 모든 종료 경로에서 `await store.close()`.**
5. 계좌 scope: 저장본·레짐 baseline 의 scope 가 `runtime.account_scope` 와 다르면 `startup_account_scope_conflict`(제품의 `_publish` 에는 scope 대조가 없다 — 다른 계좌의 checkpoint 가 그대로 live 에 게시될 수 있다).
6. 일자 선필터: 저장본 risk day ≠ **`runtime._now()`** 의 날짜 → `startup_day_transition_required`(제품의 입장 검사와 **같은 시계·같은 식**. 주입 `now` 는 정책 게시 시각 전용). 설치기는 일자 전환을 하지 않는다.
7. 레짐: `regime_policy` 없음 → `startup_regime_baseline_required` · POLICY_READS 미등록 → `startup_policy_generations_required`. 만들지 않는다.
8. **restore 앞 대조:** 저장본에 `_owner_ready` 와 같은 정규화를 적용해 live 포트폴리오·보호 상태와 비교, 다르면(또는 live 인코딩이 실패하면) `startup_reconciliation_required`.
9. 잔존 prepared 선필터: 저장본 `attempts` 에 기동 sweep 이 끝낼 수 없는 prepared 행이 있으면 `startup_unresolved_prepared_attempt`.

**구간 2 — 여기부터 live 가 저장본 값이 된다. 이 뒤의 실패에서 호출자는 legacy 로 계속 가면 안 된다**(프로세스를 세우거나 거래를 멈춘다. `restore()` 의 게시는 롤백 없이 순차 대입이라 중간 실패는 혼합 상태를 남긴다).

10. `await runtime.restore()` → `runtime._require_day_admission()`(전체 입장 검사).
11. `RegimeOwner(runtime, adapter=…, sidecar=…, vix_fetcher=…)` — 명시 guard. 성공 뒤 `runtime._regime_writer` 와 state 의 `regime_policy` 를 다시 확인한다.
12. `digest = await publish_entry_policy_context(…)` → `gateway = SignalGateway(…, config_version=digest)` — digest 는 **기동 시 한 번** 고정(재게시와 함께 덮어쓰면 다시 자기 인증이 된다).
13. `await gateway.recover_unsent()` → **kind 무관** 잔존 prepared 검사(backstop).
14. **`runtime.attach()` → `runtime.install_gateway(gateway)`** — 인접한 동기 두 줄. attach 만 되고 gateway 가 없는 구간(그 사이의 SIGNAL 은 조용히 폐기된다)을 다섯 단계에서 두 줄로 줄인다. *제품 가드가 attach 를 더 앞에 요구하면(구현자가 file:line 으로 확인) 그 직전까지만 당기고 이유를 기록한다.*

### 6-3. 시험 (신규 `tests/test_execution_install_factory.py`)

- 하네스는 **restore/attach 를 하지 않는 자체 조립**이다(`tests/test_execution_runtime.setup` 은 restore·attach 까지 하므로 그 위에서는 모든 표본이 "이미 설치됨"으로 끝난다). 1차 seed 런타임으로 checkpoint(레짐 baseline·policy generation 포함)를 만들고 닫은 뒤, 새 engine·exits·store·runtime 으로 설치기를 부른다. `account_scope` 를 명시한다. 독립 인수 파일·S3/S4 시험 모듈을 import 하지 않는다.
- 표본: 성공 1건(설치 뒤 SIGNAL 이 gateway 로 가고 `trading_ready=False` 라 `startup_reconciliation` 으로 끝난다 — 예약 0·POST 0) · 구간 1 의 거부 각각이 **다른 거부에 가려지지 않는** 표본(거부 뒤 live·owner 무변경과 `engine._execution_runtime is None` 을 함께 단언, store 파일이 없을 때 **생성되지 않음**) · 구간 2 의 실패 계약(실패 뒤 live 가 저장본 값임을 고정) · 설치 실패 뒤 재호출이 `execution_runtime_already_restored` · 잘못 배선된 commands 가 두 store 를 건드리지 않음 · 시계 둘(`clock` 과 `now`)의 날짜가 다를 때 일자 선필터가 제품과 같은 판정.
- 변이: 대조를 restore 뒤로 · 각 선검사 제거 · attach 를 앞으로 · 잔존 검사를 `kind=='submit'` 으로 한정 · digest 를 재계산해 gateway 에 넘김(같은 값이라 죽지 않는 변이는 목록에서 뺀다 — 죽지 않는 변이를 "죽는다"고 적지 않는다).
- 기존 시험(독립 인수 37·owner gate 11·policy factory 34)은 기대값 변경 0. `git diff --numstat <base>.. -- src/ scripts/` 는 `factory.py` 한 행.

### 6-4. 하지 않는 것

최초 checkpoint·레짐 baseline·일자 전환을 만드는 앞 단계(공식 증거 뒤) · 추세 출처 갱신(설치 뒤 첫 prepare 가 `regime_source_not_current` 로 막힐 수 있다 — 갱신 주체는 스케줄러 배선, 10C) · 진입 시세의 실제 source 요구(차단 사유 6) · 시계 주입·관측·사이징 표(§3-1 에서 미룸) · 런타임 면제 추가의 처분(10C) · 계좌 lease 배선 · 실패 뒤 해체 절차(`runtime.shutdown()` 은 owner 가 blocked 면 다시 실패할 수 있다 — 호출자는 store 를 닫고 프로세스를 세운다).
