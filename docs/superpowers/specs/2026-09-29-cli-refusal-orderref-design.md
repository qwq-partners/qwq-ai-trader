# 봇 실행 중 주문 CLI 거부 · OrderRef 형식 영속 — 설계 (절충안 3단계, 2026-09-29)

> 상태: 설계 초안이며 아직 리뷰 전이다. 기준 main 은 `4b8e146` 이고, 운영 코드(15:33 배포 `974a71f`)와 같다.
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
| `scripts/sell_specific.py` | 인자로 받은 `symbol:qty` 를 매도한다. **분할 수량**도 가능하다. dry-run 이 없다 |

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
  - `flock(LOCK_EX|LOCK_NB)` 이 실패하면 아래 메시지를 출력하고 `sys.exit(2)` 한다.
    `"[{tool}] 봇 실행 중 — 주문 CLI 거부. 긴급 청산은 runbook '긴급 전량 매도' 순서(킬스위치 → systemctl stop → 이 CLI)"`
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
- 이유: 해제 순서가 LOCK_UN → close → unlink 다. 그 틈에 CLI 가 옛 inode 를 잡은 뒤 파일이 지워지면, 다음 봇이 새 inode 로 락을 잡아 CLI 와 봇이 동시에 돈다.
- 파일이 남아도 무해하다. 다음 기동의 `open(...,'w')`+flock 이 그대로 동작한다.

**한계**
- 나중에 추가되는 주문 스크립트는 자동으로 덮이지 않는다. "새 주문 CLI 는 `hold_or_exit` 를 부른다"는 규칙을 runbook·CLAUDE.md 에 한 줄 적는다.
- `sudo` 로 실행하면 home 이 달라져 검사가 통과해 버린다(추정). runbook 에 명시한다.

### D2. runbook "긴급 전량 매도" 절차를 바꾼다

```bash
touch ~/.cache/ai_trader/KILL_SWITCH                          # 1) 봇 신규 매수 차단 (KILL_SWITCH_ALL 금지 — CLI 매도까지 막힌다)
echo '<sudo>' | sudo -S -k systemctl stop qwq-ai-trader       # 2) 봇 정지 (최대 30초)
python scripts/liquidate_all.py --market kr                    # 3) 청산
```

- 봇을 정지해도 **거래소에 올라간 봇의 미체결 주문은 남는다**(I2 로 당일 소멸). CLI 는 그 주문을 취소할 수 없다. 봇의 살아 있는 SELL 이 매도가능수량을 잡고 있으면 CLI 전량 SELL 은 일부 거부된다(I1). 필요하면 HTS 에서 취소한다.
- `liquidate_all` 은 자동매도 금지 종목(087010)까지 판다. 운영자 도구의 기존 동작이며, 한 줄 경고로 명시한다.
- 청산이 끝난 뒤 봇을 다시 켤지는 운영자가 판단한다.
- "싱글톤 락 충돌" 절의 `rm -f *.lock` 은 봇을 멈춘 뒤에만 쓴다.

### D3. OrderRef 는 감사 원장 EV_ACCEPT 행에 필드로 영속한다(새 파일 없음)

`kis_kr.submit_order` 의 EV_ACCEPT 기록(`:650-654`) 한 곳에 필드를 더한다. 브로커 한 곳이므로 봇·수동 매수·CLI 가 모두 덮인다.

| 필드 | 값 |
|---|---|
| `order_ref` | `[account_scope, "KR", 주문일 ISO, exchange, ODNO, ORGNO, ""]` — W `OrderRef` 필드 순서 |
| `session` | `_get_current_market_session()` 값(regular/pre_close/closing/pre_market/next_market) |
| `source` | `KISBroker.order_source`. 기본값 `"bot"`, CLI 는 `"cli:<스크립트명>"` 을 대입 |

- `account_scope`: 원 계좌번호를 쓰지 않고 상수 `"primary"` 로 둔다. 봇은 단일 주문 계좌만 쓰고, 외부 계좌는 조회 전용이다.
- `exchange`: regular/pre_close/closing 세션이면 `"KRX"`, NXT 세션(pre_market/next_market)이면 `""` 다. NXT 거래소 구분값은 공식 근거가 없어 추정 값을 쓰지 않고, `session` 으로 구분할 수 있게 둔다.
- 주문일: 브로커 세션 판정과 같은 로컬 `datetime.now().date()`.
- ODNO 가 `TEMP_` 이거나 ORGNO 가 비었으면 `order_ref` 를 **생략**한다(W 규칙상 `TEMP_` 는 신원이 아니다). `session`·`source` 는 남긴다.
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

- **T1** `trader_lock`
  - 임시 HOME 에서 다른 fd 가 락을 쥐고 있으면 `hold_or_exit` 가 메시지를 내고 exit 2 로 끝난다.
  - 락이 비어 있으면 통과하고 fd 를 유지한다(같은 프로세스에서 새 fd 로 재시도하면 실패하는 것으로 확인).
  - `"a"` 모드라 파일의 기존 내용이 보존된다.
- **T2** 두 CLI 는 `KISTokenManager`·`KISBroker` 를 만들기 전에 `hold_or_exit` 를 부른다(AST 또는 monkeypatch 로 호출 순서 단정). `liquidate_all --dry-run` 도 거부된다.
- **T3** `release_singleton_lock` 이 락 파일을 지우지 않는다.
- **T4** EV_ACCEPT 기록
  - `order_ref` 7필드·`session`·`source` 가 기록된다.
  - `TEMP_` 이거나 ORGNO 가 비었으면 `order_ref` 가 없다.
  - NXT 세션이면 exchange 가 `""` 다.
  - CLI 가 `order_source` 를 대입하면 그 값이 반영된다.
  - 성공 경로의 반환·추적 dict·기타 필드는 변하지 않는다.
- **변이 확인**: 거부 분기 제거, `"a"`→`"w"`, unlink 복원, TEMP_ 생략 조건 제거.
- **회귀**: 전체 suite 를 UTC·KST 각각.

## 5. 문서

- CHANGELOG
- `docs/operations/runbook.md`: 긴급 전량 매도, 싱글톤 락 충돌 절, (2)단계 불명 장부 절의 "후속 단계" 문구
- `docs/integrations/external-apis.md`: 감사 원장 필드
- CLAUDE.md: 주의사항 한 줄(새 주문 CLI 는 `hold_or_exit` 를 부른다)
- `docs/operations/monitoring-checkpoints.md`

## 6. 배포

- CLI 쪽 거부는 스크립트를 실행하는 시점에 적용되므로 재시작이 필요 없다.
- 봇 쪽 변경(unlink 삭제, EV_ACCEPT 필드)은 다음 장외 재시작 때 반영된다. 배포는 사용자 지시로 한다.

## 7. 리뷰 기록

(리뷰 후 기입)
