# 봇 실행 중 주문 CLI 거부 · OrderRef 형식 영속 — 설계 (절충안 3단계, 2026-09-29)

> 상태: 설계 v2 · 구현 완료(미배포, §8) — 설계 리뷰 1회차(Codex REQUEST_CHANGES P1 3·P2 3, 독립 Claude 운영 안전 REQUEST_CHANGES P1 2) 처분 반영(§7). 기준 main 은 `4b8e146` 이고, 운영 코드(15:33 배포 `974a71f`)와 같다.
> 절충안 순서: (1) 초과수익 원장 ✅ → (2) 주문 POST 접수 불명 분리 ✅(PR #100) → **(3) 이 문서**.
> 근거 원문(설계 B, `origin/feature/engine-b-minimal-kis-owner-design-20260928`):
> - §0(B:23-25): "(3) 봇 기동 중 CLI 매도 스크립트 거부 + 주문 신원을 W `OrderRef` 형식으로 영속(나중의 attach 입양 입력)"
> - §5 표(B:124), 인수 시나리오 19(B:325)
> - (2)단계 설계 §2 D2·§7 P1-6 이 CLI 문제를 이 단계로 넘겼다.
>
> 사용자 원칙:
> - 과도한 방어 로직을 지양한다.
> - 어느 답이 참이어도 안전해야 한다.
> - 브로커 불변식(I1 매도가능수량 초과 거부, I2 당일 주문 소멸)에 기댄다.
> - 미국 거래는 제외한다.

## 1. 문제

봇이 떠 있는 동안에도 운영자 CLI 두 개가 **별도 프로세스·별도 `KISBroker`** 로 같은 계좌에 주문을 낼 수 있다.

| CLI | 동작 |
|---|---|
| `scripts/liquidate_all.py` | 보유 전량을 매도한다(1호가 지정가 → 15초 뒤 잔량 시장가). `--dry-run` 이어도 `connect()`·잔고 조회를 한다 |
| `scripts/sell_specific.py` | 인자로 받은 `symbol:qty` 를 매도한다. 단 **최초 주문만 지정 수량이고, 15초 뒤 폴백은 보유 전량**을 시장가로 낸다(`sell_specific.py:78-83`·`:93-96` — 기존 결함, 별도 과제). dry-run 이 없다 |

대시보드 POST API 와 수동 풀매수는 봇 프로세스 안에서 같은 엔진·브로커를 쓰므로 해당하지 않는다(`run_trader.py:274-277`, `kr_scheduler.py:7368`).

CLI 의 취소는 자기 인스턴스의 추적만 순회하므로 봇 주문에 닿지 않는다. 따라서 프로세스 간 위험은 `submit_order` 하나이고, 결과는 세 가지다.

1. 봇 장부가 CLI 주문·체결을 동기화 전까지 모른다.
2. CLI 의 분할 SELL 은 I1 로 막히지 않는다.
3. 레이트 리미터가 프로세스별이라 원장 TR 이 합산되어 EGW00215 가 날 수 있다. 09-28 EGW00215 의 원인도 외부 조회였다.

(2)단계의 접수 불명 장부 역시, CLI 쪽에서 생긴 불명은 봇이 재시작하기 전까지 보지 못한다.

설계 B 의 OrderRef 는 main 에 없다. W 의 정의(`lifecycle.py:69-104`)는 `account_scope, market, order_date, exchange, order_no, org_no, parent_order_no` 다. 지금은 접수된 주문의 조직번호(`KRX_FWDG_ORD_ORGNO`)·주문일·출처가 어디에도 남지 않는다. 감사 원장 EV_ACCEPT 에는 ODNO 만 있다(`kis_kr.py:650-654`).

## 2. 결정

### D1. CLI 거부는 봇이 이미 쥐고 있는 flock 하나로 한다

봇은 기동부터 종료까지 `~/.cache/ai_trader/unified_trader.lock` 에 `LOCK_EX|LOCK_NB` 를 쥔다(`run_trader.py:93-94`, `:2198-2200`). 비정상 종료하면 커널이 락을 푼다. 그래서 PID 파일·systemd·`/api/health` 보다 믿을 만하다.

**새 모듈** `src/utils/trader_lock.py`(약 15줄)
- `lock_path()`
  - 호출 시점의 `Path.home()` 으로 경로를 계산한다. 그래야 시험의 home 패치로 격리된다(`order_unknown.default_path()` 와 같은 방식).
- `hold_or_exit(tool)`
  - 락 파일을 **`"a"` 모드**로 연다. `"w"` 로 열면 봇이 쓴 PID 내용이 잘린다.
  - 부모 폴더를 `mkdir(parents=True, exist_ok=True)` 한다(없으면 트레이스백이 아니라 정상 동작).
  - `flock(LOCK_EX|LOCK_NB)` 이 실패하면 **다음 행동을 그대로 출력**하고 `sys.exit(2)` 한다(휴대폰 SSH 에서 runbook 을 찾지 않게):
    ```
    [{tool}] 봇 또는 다른 주문 CLI 가 실행 중 — 주문 CLI 거부
      누가 쥐었나: fuser -v ~/.cache/ai_trader/unified_trader.lock
      봇이면(급할 때 1순위): touch ~/.cache/ai_trader/KILL_SWITCH 후 MTS/HTS 에서 미체결 일괄취소·매도 (30초 뒤 미체결 재확인)
      CLI 로 하려면: KILL_SWITCH → sudo systemctl stop qwq-ai-trader → HTS 미체결 취소 확인 → 이 명령 재실행
    ```
  - 성공하면 fd 를 모듈 전역에 붙잡아 **프로세스가 끝날 때까지** 쥔다. 그러면 CLI 가 도는 동안 봇이 재기동하려 해도 기존 코드대로 거부된다(`run_trader.py:2198` exit 1 → systemd 재시도).
  - 락 파일은 지우지 않는다.

**삽입 지점**(KIS 호출·토큰 발급보다 먼저)
- `sell_specific.py`: `args = parser.parse_args()` 직후.
- `liquidate_all.py`: `args = parse_args()` 직후. **`--dry-run` 도 거부한다.** 별도 프로세스의 원장 조회가 EGW00215 를 부를 수 있기 때문이다. 봇이 떠 있으면 조회는 대시보드로 한다.

**쓰지 않는 방법**
- `run_trader.acquire_singleton_lock` 재사용: 1단계에서 PID 파일에 적힌 **봇 프로세스에 SIGTERM/SIGKILL** 을 보낸다(`:67-88`).
- `run_trader` import: 무겁고, `LOCK_FILE` 이 import 시점 상수다.
- `KISBroker`·`submit_order` 공통 게이트: flock 은 열린 파일 설명 단위라서 봇 자신도 거부된다. 막으려면 소유자 표식이 따로 필요해 범위가 넓어진다.

**봇 쪽 경합 닫기**
- `release_singleton_lock` 에서 락 파일을 unlink 하는 3줄(`run_trader.py:128-131`)을 **삭제**한다.
- 봇의 락 획득 실패 로그(`run_trader.py:97`)에 `fuser -v ~/.cache/ai_trader/unified_trader.lock` 안내 한 줄을 더한다(재시작 루프의 원인이 CLI 인지 바로 보이게).
- 이유: 해제 순서가 LOCK_UN → close → unlink 다. 그 틈에 CLI 가 옛 inode 를 잡은 뒤 파일이 지워지면, 다음 봇이 새 inode 로 락을 잡아 CLI 와 봇이 동시에 돈다.
- 파일이 남아도 무해하다. 다음 기동의 `open(...,'w')`+flock 이 그대로 동작한다.

**한계**
- 나중에 추가되는 주문 스크립트는 자동으로 덮이지 않는다. "새 주문 CLI 는 `hold_or_exit` 를 부른다"는 규칙을 runbook·CLAUDE.md 에 한 줄 적는다.
- `sudo` 로 실행하면 home 이 달라져 검사가 통과해 버린다(추정). runbook 에 명시한다.

### D2. runbook "긴급 전량 매도" 절차를 바꾼다 (리뷰 1회차 P1 반영)

**1순위 — 봇을 멈추지 않는 방법**(가장 빠르다. 봇 내부 청산 경로는 없다):
1. `touch ~/.cache/ai_trader/KILL_SWITCH` — 봇 신규 매수 차단(2초 안 반영). **`KILL_SWITCH_ALL`·`KILL_SWITCH_ALL_KR` 금지** — CLI·봇 매도까지 막힌다.
2. MTS/HTS 에서 **미체결 일괄취소**(봇의 살아 있는 BUY 가 청산 뒤 체결되는 것 방지) → 보유 전량 매도.
3. **30초 뒤 미체결을 다시 확인·취소한다** — 킬스위치 직전 검사를 통과한 BUY 가 hashkey·rate-limit 대기 뒤 늦게 전송될 수 있다. (구현 리뷰 1회차 P1)
   킬스위치를 만들면 그 뒤(최대 2초 캐시) 봇 BUY 는 전송 직전에도 막힌다. 이미 전송 중이던 요청만 남으므로 30초 뒤 재확인으로 닫는다. `_api_post` 의 매수 TR 전송 직전 재확인(매 시도·401 재전송 포함 rate-limit 직후)이 접수 불명 보류와 함께 `kill_switch.check("buy", market="KR")` 도 본다 — SELL 은 재검사하지 않는다. (구현 리뷰 2회차 P1)
4. 그 재확인 뒤에 잔고·미체결 0 을 확인해 청산 완료로 판정한다. 봇은 30초 동기화로 결과를 반영한다. KILL_SWITCH 는 재개를 판단할 때까지 유지한다.

**2순위 — CLI 로 할 때**(tmux 안에서 실행 — SSH 가 끊겨 SIGHUP 이 15초 대기 중인 CLI 를 죽이면 자기 SELL 이 매도가능수량을 잡아 재실행이 I1 로 거부된다):
```bash
touch ~/.cache/ai_trader/KILL_SWITCH                                   # 1) 신규 매수 차단
sudo systemctl stop qwq-ai-trader                                     # 2) 봇 정지 (운영 unit 의 TimeoutStopSec 확인 — 저장소 unit 30초, 과거 90초 기록)
# 3) MTS/HTS 에서 봇이 남긴 BUY·SELL 미체결 확인·취소 (봇 종료는 주문을 취소하지 않고, CLI 는 봇 주문을 취소할 수 없다)
cd /home/ubuntu/projects/qwq-ai-trader && venv/bin/python scripts/liquidate_all.py --market kr   # 4) 청산
# 5) 잔고·미체결 0 확인 (CLI 는 실패해도 끝에 '완료'를 출력한다)
```
- `liquidate_all` 은 자동매도 금지 종목(087010)까지 판다(기존 동작 — 한 줄 경고).
- 청산 뒤 봇 재기동은 운영자 판단이다. **CLI 실행 중에는 배포·수동 재기동을 하지 않는다** — 봇 기동이 락 실패로 exit 1 을 반복하고, `local_deploy.sh` 는 헬스체크 실패로 롤백을 시도하며, CLI 가 끝나면 systemd 재시도(RestartSec=10)로 봇이 운영자 판단 없이 올라온다. KILL_SWITCH 가 남아 있으면 매수는 막힌다.

**"싱글톤 락 충돌" 절**: `rm -f ~/.cache/ai_trader/*.lock` 을 **삭제**한다(Codex·Claude 공통 P1). unlink 를 없앤 이상 락 파일을 지울 이유가 없고, CLI 가 옛 inode 를 쥔 채 지우면 봇·CLI 가 동시에 돈다(같은 glob 이 `order_unknown.json.lock` 도 지운다). 대신 `fuser -v ~/.cache/ai_trader/unified_trader.lock` 으로 쥔 프로세스를 확인하고, CLI 면 끝나기를 기다린다. PID 파일(`unified_trader.pid`) 정리는 봇과 주문 CLI 가 모두 없을 때만 한다.

### D3. OrderRef 는 감사 원장 EV_ACCEPT 행에 필드로 영속한다(새 파일 없음)

`kis_kr.submit_order` 의 EV_ACCEPT 기록(`:650-654`) 한 곳에 필드를 더한다. 브로커 한 곳이므로 봇·수동 매수·CLI 가 모두 덮인다.

| 필드 | 값 |
|---|---|
| `odno`, `org_no`, `order_date`, `account_scope` | 원시 신원 — **항상** 기록(TEMP_ 면 `odno` 도 그대로 남겨 식별 불가임을 보이게) |
| `order_ref` | `[account_scope, "KR", 주문일 ISO, "KRX", ODNO, ORGNO, ""]` — W `OrderRef` 필드 순서. **거래소가 확정(정규·동시호가 세션)이고 ODNO 가 TEMP_ 가 아니며 ORGNO 가 있을 때만**. 그 밖에는 생략(리뷰 1회차 P1-3 — 빈 exchange 는 W `OrderRef` 가 `ValueError("incomplete order scope")` 로 거부한다) |
| `session` | `_get_current_market_session()` 값(regular/pre_close/closing/pre_market/next_market) |
| `source` | `KISBroker.order_source`. 기본값은 `Path(sys.argv[0]).name`(봇은 `run_trader.py`, CLI 는 자기 스크립트명 — 새 CLI 도 자동 구분). 대입으로 바꿀 수 있다 |

- `account_scope`: 원 계좌번호를 쓰지 않고 상수 `"primary"` 로 둔다. 봇은 단일 주문 계좌만 쓰고, 외부 계좌는 조회 전용이다.
- 거래소: regular/pre_close/closing 세션만 `"KRX"` 로 확정한다. NXT 세션(pre_market/next_market)은 공식 근거가 없어 추정 값을 쓰지 않는다 — `order_ref` 를 생략하고 원시 필드·`session` 으로 남긴다.
- 주문일: 브로커 세션 판정과 같은 로컬 `datetime.now().date()`.
- ODNO 가 `TEMP_` 이거나 ORGNO 가 비었으면 `order_ref` 를 **생략**한다(W 규칙상 `TEMP_` 는 신원이 아니고, W ACK 검증은 ORGNO 를 요구한다). 원시 필드·`session`·`source` 는 남긴다.
- 기존 필드(`order_id` 등)는 바꾸지 않는다. 감사 원장을 읽는 KR 제품 코드는 없다.

**하지 않는 것**
- 영속한 ODNO 로 재시작 때 브로커 `_pending_orders`·맵을 복원하지 않는다. `filled_quantity=0` 으로 복원되면 `check_fills` 가 누적 체결량 전체를 새 체결로 다시 내보내, 잔고 동기화가 이미 반영한 체결이 이중 계상된다.
- DB 스키마 변경, 정상 체결 경로의 `trade_events.kis_order_no` 채우기: 소비처가 attach 재개 뒤에야 생기므로 별도 과제다.
- W `lifecycle`/`AccountLease` 이식.

**한계**: 감사 원장은 fsync 가 없고 쓰기 실패를 삼킨다(`audit_log.py:66-68`). 응답 직후 크래시하면 행이 빠질 수 있다. 다만 그날 주문은 I2 로 소멸한다. 소비처가 생기면 fsync 를 검토한다.

## 3. 범위 밖 (한계로 기록)

- **장중 크래시 재시작 뒤 같은 종목 재매수 가능성**
  - 재시작하면 엔진 pending 이 비므로, 거래소에 살아 있는 봇 BUY 를 모른 채 같은 종목 BUY 를 다시 낼 수 있다.
  - 봇 장중 재시작은 규칙상 금지(P0 예외)이고, 크래시는 드물며, 현금 검사가 상한 역할을 한다.
  - 기동 시 거래소 미체결을 조회해 막는 안은 후속 후보로 둔다.
- US CLI(`cancel_immx_pending.py`)는 제외한다.

## 4. 시험 (시계·home 주입)

- **T1** `trader_lock` — 경로는 **명시 주입**(`hold_or_exit(tool, path=tmp)`, 기본값만 `lock_path()`). 시스템 HOME 변경에 기대지 않는다.
  - 다른 fd 가 락을 쥐고 있으면 안내 문구를 내고 exit 2.
  - 락이 비어 있으면 통과하고 fd 를 유지한다(같은 프로세스 새 fd 재시도 실패로 확인).
  - 부모 폴더가 없어도 exit 가 아니라 정상 획득.
- **T2** 두 CLI 는 `KISTokenManager`·`KISBroker` 를 만들기 전에 `hold_or_exit` 를 부른다 — **AST 로** 호출 순서를 단정(모듈 import 는 최상위 `load_env()` 를 실행하므로 하지 않는다). `liquidate_all` 은 dry-run 분기보다 앞에서 부른다.
- **T3** `release_singleton_lock` 이 락 파일을 지우지 않는다 — `run_trader` 의 `LOCK_FILE`·`PID_FILE` 모듈 상수를 tmp 경로로 monkeypatch 하고, PID kill 경로(acquire 1단계)는 실행하지 않는다.
- **T4** EV_ACCEPT 기록
  - 정규 세션: `order_ref` 7필드(W 규칙 검증: 빈 값 없음·TEMP_ 아님)·원시 필드·`session`·`source`.
  - `TEMP_`·ORGNO 공란·NXT 세션이면 `order_ref` 없음, 원시 필드는 있음.
  - `source` 기본값이 `sys.argv[0]` 이름, 대입하면 그 값.
  - 날짜·세션·HTTP·킬스위치·감사 기록 함수는 모두 고정/가짜.
  - 성공 경로의 반환·추적 dict·기타 필드는 변하지 않는다.
- **변이 확인**: 거부 분기 제거, unlink 복원, TEMP_·NXT 생략 조건 제거, CLI 의 호출 위치를 브로커 생성 뒤로 이동.
- **회귀**: 전체 suite 를 UTC·KST 각각.

## 5. 문서

- CHANGELOG
- `docs/operations/runbook.md`: 긴급 전량 매도, 싱글톤 락 충돌 절, (2)단계 불명 장부 절의 "후속 단계" 문구
- `docs/integrations/external-apis.md`: 감사 원장 필드
- CLAUDE.md: 주의사항 한 줄(새 주문 CLI 는 `hold_or_exit` 를 부른다)
- `docs/operations/monitoring-checkpoints.md`: 운영 수용 확인 — 봇 가동 중 `venv/bin/python -c 'from src.utils.trader_lock import hold_or_exit; hold_or_exit("check")'` → exit 2(KIS 호출 없음, 장중 무해). 봇과 CLI 의 `Path.home()` 이 같아야 가드가 성립한다(저장소 unit 은 `/home/user` — 운영 unit 확인).

## 6. 배포

- CLI 쪽 거부는 스크립트를 실행하는 시점에 적용되므로 재시작이 필요 없다.
- 봇 쪽 변경(unlink 삭제, EV_ACCEPT 필드)은 다음 장외 재시작 때 반영된다. 배포는 사용자 지시로 한다.

## 7. 리뷰 기록

### 설계 리뷰 1회차 (2026-09-29)

- Codex(교차 공급자): rollout 기준 실제 **gpt-6-astra/xhigh**, thread `01a0ec0c-75e9-7323-b9c8-447f80fa1015`. REQUEST_CHANGES — P0 0 · P1 3 · P2 3.
- 독립 Claude(운영 안전, Opus/high): REQUEST_CHANGES — P1 2(Codex P1-1·P1-2 와 같은 곳) · P2 9.
- 전부 코드·문서와 대조해 수용.

| # | 지적 | 처분(v2) |
|---|---|---|
| P1 (공통) | runbook `rm -f *.lock` 이 unlink 삭제로 닫은 경합을 사람이 다시 연다 | D2: 삭제, `fuser` 진단·"CLI 끝나기를 기다림"으로 교체 |
| P1 (공통) | 긴급 절차가 봇의 살아 있는 BUY(청산 뒤 체결)를 빠뜨림 — KILL_SWITCH·봇 종료는 기존 주문을 취소하지 않는다 | D2: 1순위 "KILL_SWITCH + MTS/HTS 일괄취소·매도", 2순위 CLI 절차에 HTS 미체결 취소·종료 뒤 잔고/미체결 확인 |
| P1 (Codex) | NXT 의 빈 exchange 는 W `OrderRef` 가 거부 | D3: 확정 세션에서만 `order_ref`, 원시 필드는 항상 |
| P2 | CLI 실행 중 배포·재기동 → 롤백·운영자 판단 없는 재기동 | D2 에 금지·설명 |
| P2 | `sell_specific` 폴백은 보유 전량 — "분할 가능" 설명 오류 | §1 정정, 코드 수정은 별도 과제 |
| P2 | 시험 격리(import 시점 상수, CLI 최상위 `load_env()`, 벽시계) | §4: 경로 명시 주입·AST·상수 monkeypatch·고정 |
| P2 (Claude) | 거부 메시지가 runbook 참조뿐 · 폴더 없음 트레이스백 · source 자동 구분 · 운영 수용 확인 · tmux | D1 메시지·mkdir, D3 `sys.argv[0]`, §5 점검 명령, D2 tmux |
| P2 (Claude) | `"a"` 보존 시험은 쓰는 곳 없는 성질 | 시험·변이에서 제외(`"a"` 는 유지) |

### 구현 리뷰 1회차 (2026-09-29, 대상 `cc15bf8`)

- Codex(교차 공급자, rollout 기준 gpt-6-astra/xhigh): REQUEST_CHANGES — P1 2 · P2 2.
- 독립 Claude: APPROVE — P2 3.
- coordinator 가 코드와 대조해 처분했다.

| # | 지적 | 처분 |
|---|---|---|
| P1 (공통, 돈 경로 회귀) | `Path(sys.argv[0])` 가 `audit_log.record` 호출 전에 평가돼, 빈 `sys.argv` 면 IndexError 가 posted 뒤 예외 경로로 흘러 성공 주문이 접수 불명(추적 dict 제거·그날 BUY 보류)이 된다 — 독립 리뷰 재현 | 신원 계산을 순수 함수 `kis_kr._accept_identity(odno, orgno, session, source)` 로 옮기고 `isinstance` 입력 검사만으로 예외가 없게 함(try/except 아님). source 는 `self.order_source or Path((sys.argv[:1] or [""])[0]).name or "unknown"`. 시험: `sys.argv=[]` BUY·SELL 성공 경로 유지·`_record_unknown` 미호출(스파이) |
| P2 (Codex) | `order_ref` 조건이 W 신원 규칙보다 느슨함(비문자열·공백·`local-`) | ODNO·ORGNO 가 비지 않은 `str`·앞뒤 공백 없음·ODNO 가 `TEMP_`/`local-` 아님·KRX 세션일 때만. `org_no` 는 None 이면 `""`(감사 None 제거 규칙에도 "항상 기록" 유지). 반환·추적 원본 값 무변경. 반례 시험 5건 |
| P1 (Codex) | 긴급 1순위: 킬스위치 직전 검사를 통과한 BUY 가 hashkey·rate-limit 대기 뒤 늦게 전송될 수 있다 | 코드 변경 없음. D2·runbook 1순위에 "30초 뒤 미체결 재확인·취소" 단계, 청산 완료 판정은 그 뒤. `trader_lock` 안내 1순위 줄에 "(30초 뒤 미체결 재확인)" |
| P2 (공통) | runbook·D2 의 CLI 명령이 상대경로 | `cd /home/ubuntu/projects/qwq-ai-trader && venv/bin/python scripts/liquidate_all.py --market kr` |
| P2 (Claude) | `LOCK_EX`→`LOCK_SH` 변이가 시험에 걸리지 않는다 | 같은 프로세스에서 첫 획득 fd 를 보관·`_held=None` 후 재호출 → exit 2 시험 추가 |

### 구현 리뷰 2회차 (2026-09-29, 대상 `0509545`, 한정 재리뷰)

- Codex(교차 공급자, rollout 기준 gpt-6-astra/xhigh): REQUEST_CHANGES — P1 1 · P2 1. 1회차의 나머지 처분은 종결.

| # | 지적 | 처분 |
|---|---|---|
| P1 | 지연 BUY 는 30초 재확인으로 닫히지 않는다 — 머리 킬스위치 검사를 지난 BUY 가 hashkey(15초 시한·최대 3회)·rate-limit(전체 시한 없음) 대기 뒤 운영자 재확인 이후에 전송될 수 있다 | 코드: `_api_post` 매수 TR 전송 직전 재확인에 `kill_switch.check("buy", market="KR")` 추가, 막히면 기존 `_blocked` 반환 → `submit_order` 의 기존 `_blocked` 처리(record_blocked + `(False, 사유)`) 재사용. 위치(매 시도·401 재전송 포함 rate-limit 직후) 유지, SELL 제외. 시험: 머리 허용·전송 직전 차단 → POST 0·blocked·`(False, 사유)`, 401 재전송 직전 차단, 대조군(킬스위치 없음) 성공 경로 그대로, SELL 은 검사 1회. 문서: runbook·D2 1순위에 "킬스위치 뒤(최대 2초 캐시) BUY 는 전송 직전에도 막힌다, 이미 전송 중이던 요청만 30초 재확인으로 닫는다" |
| P2 | `_accept_identity` "어떤 입력에도 예외 없음" 과장 | docstring 을 "JSON 기본형·내부 문자열 입력에서 예외 없음(str 하위 클래스 등 임의 객체는 보장 밖), `datetime.now()` 사용" 으로 정정. 코드 무변경 |

## 8. 구현 기록 (2026-09-29, 미배포)

- 브랜치 `fix/cli-refusal-orderref-20260929`, 기준 `de53941`(= main `4b8e146` + 이 문서). 작성 Claude Opus(요청 opus/high).
- 변경
  - 신규 `src/utils/trader_lock.py` — `lock_path()`, `hold_or_exit(tool, path=None)`(`"a"`·부모 mkdir·`LOCK_EX|LOCK_NB`·실패 시 D1 안내 stderr + exit 2·fd 모듈 전역 `_held` 유지·파일 미삭제).
  - `scripts/sell_specific.py`·`scripts/liquidate_all.py` — `args = …` 바로 다음 문장에서 호출(토큰·브로커·dry-run 분기 전, US 경로 포함 한 곳).
  - `scripts/run_trader.py` — `release_singleton_lock` 의 LOCK_FILE unlink 삭제, 락 획득 실패 로그에 `fuser -v {LOCK_FILE}` 한 줄. acquire 1단계 무변경.
  - `src/execution/broker/kis_kr.py` — EV_ACCEPT 에 `odno`·`org_no`·`order_date`·`account_scope`·`order_ref`(조건부)·`session`·`source`. 클래스 속성 `order_source = None`.
    `order_ref` 생략은 `None` 전달 → 감사 원장의 기존 None 제외 규칙으로 키 자체가 빠진다. `TEMP_` 판정은 `str(kis_ord_no)` — 성공 경로에 새 예외를 만들지 않기 위해.
- 시험 `tests/test_cli_refusal_orderref.py` — T1 4건·T2 3건·T3 1건·T4 9건(`test_kis_tr_switch` 의 `broker` 픽스처·`_order`, `test_t11_entry_plan._freeze_clock` 재사용).
  T4 는 W `OrderRef.__post_init__`(`daf8b3e:src/execution/safety/lifecycle.py:78-90`) 규칙을 시험 안에 옮겨 검증한다(W 코드는 main 에 없음).
- 변이 8종 전부 검출(sha256 복원 확인): 거부 분기 제거·exit 코드 제거·unlink 복원·NXT 조건 제거·TEMP_ 조건 제거·ORGNO 조건 제거·sell_specific/liquidate_all 호출을 브로커 생성 뒤로.
- 스모크: `async main()` 안의 `sys.exit(2)` 가 `asyncio.run` 을 거쳐 프로세스 종료코드 2 로 나온다(tmp 락 경로, KIS·`.env` 무관).
- 회귀: `TZ=UTC`·`TZ=Asia/Seoul` 각각 `pytest -q -p no:cacheprovider tests/` → **2386 passed / 2 xfailed**(기준 2369 + 신규 17), `[테스트 격리] … 0건`, 종료코드 0. toss 플레이크 미발생.
- 설계 이탈 없음.
- **구현 리뷰 1회차 반영**(§7 처분표):
  - `kis_kr._accept_identity`(순수 함수·예외 없음), 모듈 헬퍼 `_is_clean_id`. source 에 빈 argv 방어·`"unknown"`.
  - 시험 +8(총 25): `sys.argv=[]` BUY·SELL 성공 경로 2, W 신원 반례 5(ODNO int·`local-1`·앞 공백, ORGNO 공백·None), 같은 프로세스 두 번째 획득 거부 1.
  - 변이 5종 추가 검출(sha256 복원 확인): argv 방어 제거, `local-` 검사 제거, 공백 검사 제거, 둘 다 제거, `LOCK_EX`→`LOCK_SH`.
  - 회귀: `TZ=UTC`·`TZ=Asia/Seoul` 각각 **2394 passed / 2 xfailed**(2386 + 8), `[테스트 격리] … 0건`, 종료코드 0.
- **구현 리뷰 2회차 반영**(§7 처분표):
  - `kis_kr._api_post` 매수 TR 전송 직전 재확인에 킬스위치 재검사, `_api_post` docstring `_blocked` 설명·`_accept_identity` docstring 정정.
  - 시험 +4(총 29): 머리 허용·전송 직전 차단, 401 재전송 직전 차단, 대조군, SELL 미검사. 기존 `tests/test_review_fixes_2026_09.py::_post_broker` 가
    매수 TR 로 `_api_post` 를 직접 불러 운영 킬스위치 플래그를 stat 했으므로(격리 위반 4건) 그 헬퍼에 `kill_switch.check` 가짜 한 줄을 넣었다(허용 파일 밖 — 별도 커밋).
  - 변이 2종 검출(sha256 복원 확인): 재검사 제거, 재검사 결과 무시.
  - 회귀: `TZ=UTC`·`TZ=Asia/Seoul` 각각 **2398 passed / 2 xfailed**(2394 + 4), `[테스트 격리] … 0건`, 종료코드 0.
- 배포는 아직(사용자 지시 대기).
