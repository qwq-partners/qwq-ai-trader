"""봇 실행 중 주문 CLI 거부 · EV_ACCEPT 주문 신원 — 2026-09-29 (절충안 3단계).

설계: docs/superpowers/specs/2026-09-29-cli-refusal-orderref-design.md §4
- T1 trader_lock: 쥔 쪽이 있으면 exit 2, 비어 있으면 획득·유지, 부모 폴더 없어도 획득 (경로 명시 주입)
- T2 두 CLI 는 토큰·브로커 생성 전에 hold_or_exit — AST 로만 본다(import 하면 최상위 load_env() 가 돈다)
- T3 봇 release_singleton_lock 이 락 파일을 지우지 않는다 (LOCK_FILE·PID_FILE monkeypatch, acquire 미실행)
- T4 EV_ACCEPT 원시 신원·order_ref·session·source (시계·세션·HTTP·킬스위치·감사 기록 전부 가짜)
"""
from __future__ import annotations

import ast
import asyncio
import fcntl
import sys
from datetime import date, datetime
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from src.core.types import OrderSide  # noqa: E402
from src.execution.broker import kis_kr  # noqa: E402
from src.utils import trader_lock  # noqa: E402

from test_kis_tr_switch import _order, broker  # noqa: E402,F401
from test_t11_entry_plan import _freeze_clock  # noqa: E402

NOW = datetime(2026, 9, 29, 10, 30, 0)


# ── T1 trader_lock ──────────────────────────────────────────────────────────

@pytest.fixture
def released(monkeypatch):
    """시험이 잡은 락 fd 를 끝에서 닫는다 — 모듈 전역을 시험마다 비운다."""
    monkeypatch.setattr(trader_lock, "_held", None)
    yield
    if trader_lock._held is not None:
        trader_lock._held.close()


def test_t1_refuses_with_next_steps_when_another_fd_holds_the_lock(tmp_path, capsys, released):
    path = tmp_path / "unified_trader.lock"
    with open(path, "w") as bot:
        fcntl.flock(bot.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        with pytest.raises(SystemExit) as exc:
            trader_lock.hold_or_exit("liquidate_all", path=path)
    assert exc.value.code == 2
    err = capsys.readouterr().err
    assert err.startswith("[liquidate_all] 봇 또는 다른 주문 CLI 가 실행 중 — 주문 CLI 거부")
    assert "fuser -v ~/.cache/ai_trader/unified_trader.lock" in err
    assert "touch ~/.cache/ai_trader/KILL_SWITCH" in err and "sudo systemctl stop qwq-ai-trader" in err
    assert "(30초 뒤 미체결 재확인)" in err
    assert trader_lock._held is None


def test_t1_acquires_and_keeps_holding_until_the_process_ends(tmp_path, released):
    path = tmp_path / "unified_trader.lock"
    trader_lock.hold_or_exit("sell_specific", path=path)
    assert trader_lock._held is not None
    with open(path, "a") as other:   # 같은 프로세스라도 새 fd(열린 파일 설명)는 못 잡는다 = 쥐고 있다
        with pytest.raises(BlockingIOError):
            fcntl.flock(other.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    assert path.exists()


def test_t1_second_holder_is_refused_even_in_the_same_process(tmp_path, released):
    """배타 락이어야 한다 — 공유 락이면 두 번째 CLI 도 통과한다(LOCK_EX→LOCK_SH 변이 검출)."""
    path = tmp_path / "unified_trader.lock"
    trader_lock.hold_or_exit("sell_specific", path=path)
    first, trader_lock._held = trader_lock._held, None
    try:
        with pytest.raises(SystemExit) as exc:
            trader_lock.hold_or_exit("liquidate_all", path=path)
        assert exc.value.code == 2 and trader_lock._held is None
    finally:
        first.close()


def test_t1_missing_parent_directory_is_created_not_a_traceback(tmp_path, released):
    path = tmp_path / "no" / "such" / "unified_trader.lock"
    trader_lock.hold_or_exit("sell_specific", path=path)
    assert path.exists() and trader_lock._held is not None


def test_t1_default_path_follows_home_at_call_time(tmp_path, monkeypatch):
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path))
    assert trader_lock.lock_path() == tmp_path / ".cache" / "ai_trader" / "unified_trader.lock"


# ── T2 두 CLI 의 호출 순서 (AST) ────────────────────────────────────────────

def _main_body(script: str) -> list:
    tree = ast.parse((ROOT / "scripts" / script).read_text(encoding="utf-8"))
    imports = {(n.module, a.name) for n in ast.walk(tree) if isinstance(n, ast.ImportFrom) for a in n.names}
    assert ("src.utils.trader_lock", "hold_or_exit") in imports
    main = next(n for n in tree.body if isinstance(n, ast.AsyncFunctionDef) and n.name == "main")
    return main.body


def _first_index(body: list, pred) -> int:
    return next(i for i, stmt in enumerate(body) if any(pred(n) for n in ast.walk(stmt)))


def _calls(name: str):
    return lambda n: isinstance(n, ast.Call) and isinstance(n.func, ast.Name) and n.func.id == name


@pytest.mark.parametrize("script,tool", [("sell_specific.py", "sell_specific"),
                                         ("liquidate_all.py", "liquidate_all")])
def test_t2_cli_holds_the_lock_right_after_parsing_before_any_kis_object(script, tool):
    body = _main_body(script)
    parse = next(i for i, s in enumerate(body)
                 if isinstance(s, ast.Assign) and any(isinstance(t, ast.Name) and t.id == "args" for t in s.targets))
    hold = body[parse + 1]   # 파싱 바로 다음 문장, 조건 없이 최상위에서 부른다
    assert isinstance(hold, ast.Expr) and _calls("hold_or_exit")(hold.value)
    assert [a.value for a in hold.value.args] == [tool]
    for name in ("KISTokenManager", "KISBroker"):
        assert parse + 1 < _first_index(body, _calls(name))


def test_t2_liquidate_all_holds_before_the_dry_run_branch():
    body = _main_body("liquidate_all.py")
    hold = _first_index(body, _calls("hold_or_exit"))
    dry_run = _first_index(body, lambda n: isinstance(n, ast.Attribute) and n.attr == "dry_run")
    assert hold < dry_run and hold < _first_index(body, _calls("KISUSBroker"))


# ── T3 봇 해제는 락 파일을 지우지 않는다 ────────────────────────────────────

def test_t3_release_keeps_the_lock_file_and_removes_only_the_pid_file(tmp_path, monkeypatch):
    from scripts import run_trader
    lock, pid = tmp_path / "unified_trader.lock", tmp_path / "unified_trader.pid"
    monkeypatch.setattr(run_trader, "LOCK_FILE", lock)
    monkeypatch.setattr(run_trader, "PID_FILE", pid)
    fd = open(lock, "w")
    fcntl.flock(fd.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    pid.write_text("1")
    monkeypatch.setattr(run_trader, "_lock_fd", fd)

    run_trader.release_singleton_lock()

    assert lock.exists() and not pid.exists()
    assert run_trader._lock_fd is None and fd.closed
    with open(lock, "a") as nxt:   # 풀렸다 — 같은 inode 를 다음 쪽이 잡는다
        fcntl.flock(nxt.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)


# ── T4 EV_ACCEPT 주문 신원 ──────────────────────────────────────────────────

_OLD = {"market": "KR", "symbol": "005930", "side": "buy", "qty": 10, "price": "70000"}


def _run(b, monkeypatch, output, session="regular"):
    """가짜 POST 응답 `output` 으로 submit_order 를 돌린다. 반환 (결과, EV_ACCEPT 필드 — None 은 감사 원장처럼 뺀다)."""
    _freeze_clock(monkeypatch, kis_kr, NOW)
    monkeypatch.setattr(kis_kr.KISBroker, "_get_current_market_session", lambda self: session)

    async def nxt(self):
        return ["005930"]
    monkeypatch.setattr(kis_kr.KISBroker, "get_nxt_symbols", nxt)
    rows = []
    monkeypatch.setattr(kis_kr.audit_log, "record", lambda event, **f: rows.append((event, f)))

    async def post(url, tr_id, json_data, extra_headers=None, retry=True):
        return {"rt_cd": "0", "output": output}
    b._api_post = post
    result = asyncio.run(b.submit_order(_order()))
    accept = [f for e, f in rows if e == kis_kr.audit_log.EV_ACCEPT]
    assert len(accept) == 1
    return result, {k: v for k, v in accept[0].items() if v is not None}


def _w_order_ref_ok(ref: list) -> bool:
    """W `OrderRef.__post_init__` 규칙 (daf8b3e:src/execution/safety/lifecycle.py:78-90)."""
    scope, market, order_date, exchange, order_no, org_no, parent = ref
    return (all(isinstance(v, str) and v and v == v.strip() for v in (scope, market, exchange, order_no))
            and date.fromisoformat(order_date).isoformat() == order_date
            and all(isinstance(v, str) and v == v.strip() for v in (org_no, parent))
            and not order_no.startswith(("TEMP_", "local-")))


@pytest.mark.parametrize("session", ["regular", "pre_close", "closing"])
def test_t4_krx_session_records_raw_identity_and_order_ref(broker, monkeypatch, session):  # noqa: F811
    monkeypatch.setattr(sys, "argv", ["/home/ubuntu/projects/qwq-ai-trader/scripts/run_trader.py", "--market", "kr"])
    result, f = _run(broker, monkeypatch, {"ODNO": "0001", "KRX_FWDG_ORD_ORGNO": "91252"}, session)
    assert result == (True, "0001")
    assert f == {**_OLD, "order_id": "0001",
                 "odno": "0001", "org_no": "91252", "order_date": "2026-09-29", "account_scope": "primary",
                 "order_ref": ["primary", "KR", "2026-09-29", "KRX", "0001", "91252", ""],
                 "session": session, "source": "run_trader.py"}
    assert _w_order_ref_ok(f["order_ref"])
    # 추적 dict 는 그대로
    assert broker._order_id_to_kis_no == {"o1": "0001"} and broker._order_id_to_orgno == {"o1": "91252"}
    assert list(broker._pending_orders) == ["o1"]


@pytest.mark.parametrize("session", ["pre_market", "next_market"])
def test_t4_nxt_session_omits_order_ref_but_keeps_raw_fields(broker, monkeypatch, session):  # noqa: F811
    result, f = _run(broker, monkeypatch, {"ODNO": "0001", "KRX_FWDG_ORD_ORGNO": "91252"}, session)
    assert result == (True, "0001")
    assert "order_ref" not in f
    assert (f["odno"], f["org_no"], f["order_date"], f["account_scope"], f["session"]) == \
        ("0001", "91252", "2026-09-29", "primary", session)


def test_t4_temp_odno_omits_order_ref_and_keeps_temp_visible(broker, monkeypatch):  # noqa: F811
    result, f = _run(broker, monkeypatch, {"KRX_FWDG_ORD_ORGNO": "91252"})
    temp = "TEMP_20260929103000000000"
    assert result == (True, temp)
    assert "order_ref" not in f
    assert f["odno"] == temp and f["order_id"] == temp and f["org_no"] == "91252" and f["session"] == "regular"


def test_t4_blank_orgno_omits_order_ref_and_records_the_blank(broker, monkeypatch):  # noqa: F811
    result, f = _run(broker, monkeypatch, {"ODNO": "0001"})
    assert result == (True, "0001")
    assert "order_ref" not in f
    assert f["odno"] == "0001" and f["org_no"] == "" and f["order_date"] == "2026-09-29"
    assert broker._order_id_to_orgno == {}


def test_t4_source_defaults_to_the_script_name_and_can_be_assigned(broker, monkeypatch):  # noqa: F811
    monkeypatch.setattr(sys, "argv", ["/home/ubuntu/projects/qwq-ai-trader/scripts/sell_specific.py", "005930:1"])
    assert kis_kr.KISBroker.order_source is None and "order_source" not in vars(broker)
    _, f = _run(broker, monkeypatch, {"ODNO": "0001", "KRX_FWDG_ORD_ORGNO": "91252"})
    assert f["source"] == "sell_specific.py"
    broker.order_source = "manual_buy"
    _, f = _run(broker, monkeypatch, {"ODNO": "0002", "KRX_FWDG_ORD_ORGNO": "91252"})
    assert f["source"] == "manual_buy"


def test_t4_sell_side_gets_the_same_identity(broker, monkeypatch):  # noqa: F811
    _freeze_clock(monkeypatch, kis_kr, NOW)
    rows = []
    monkeypatch.setattr(kis_kr.audit_log, "record", lambda event, **f: rows.append((event, f)))

    async def post(url, tr_id, json_data, extra_headers=None, retry=True):
        return {"rt_cd": "0", "output": {"ODNO": "0009", "KRX_FWDG_ORD_ORGNO": "91252"}}
    broker._api_post = post
    assert asyncio.run(broker.submit_order(_order(OrderSide.SELL))) == (True, "0009")
    accept = next(f for e, f in rows if e == kis_kr.audit_log.EV_ACCEPT)
    assert accept["side"] == "sell" and accept["order_ref"][4:6] == ["0009", "91252"]


@pytest.mark.parametrize("side", [OrderSide.BUY, OrderSide.SELL])
def test_t4_empty_argv_keeps_the_success_path_and_never_becomes_unknown(broker, monkeypatch, side):  # noqa: F811
    """빈 sys.argv(임베디드 인터프리터 등)에서도 성공 주문은 성공 — 신원 계산 예외가 접수 불명으로 새지 않는다."""
    monkeypatch.setattr(sys, "argv", [])
    _freeze_clock(monkeypatch, kis_kr, NOW)
    unknown = []
    monkeypatch.setattr(kis_kr.KISBroker, "_record_unknown", lambda self, *a, **k: unknown.append(a) or "x")
    rows = []
    monkeypatch.setattr(kis_kr.audit_log, "record", lambda event, **f: rows.append((event, f)))

    async def post(url, tr_id, json_data, extra_headers=None, retry=True):
        return {"rt_cd": "0", "output": {"ODNO": "0001", "KRX_FWDG_ORD_ORGNO": "91252"}}
    broker._api_post = post
    assert asyncio.run(broker.submit_order(_order(side))) == (True, "0001")
    assert unknown == []
    assert broker._order_id_to_kis_no == {"o1": "0001"} and broker._order_id_to_orgno == {"o1": "91252"}
    assert list(broker._pending_orders) == ["o1"]
    accept = [f for e, f in rows if e == kis_kr.audit_log.EV_ACCEPT]
    assert len(accept) == 1 and accept[0]["source"] == "unknown"
    assert accept[0]["order_ref"] == ["primary", "KR", "2026-09-29", "KRX", "0001", "91252", ""]


@pytest.mark.parametrize("output", [
    {"ODNO": 123, "KRX_FWDG_ORD_ORGNO": "91252"},        # 문자열 아닌 ODNO
    {"ODNO": "local-1", "KRX_FWDG_ORD_ORGNO": "91252"},  # W 합성 식별자
    {"ODNO": " 0001", "KRX_FWDG_ORD_ORGNO": "91252"},    # 앞뒤 공백
    {"ODNO": "0001", "KRX_FWDG_ORD_ORGNO": " "},         # 공백뿐인 ORGNO
    {"ODNO": "0001", "ORGNO": None},                     # ORGNO None
])
def test_t4_order_ref_follows_w_identity_rules_and_org_no_stays_a_string(broker, monkeypatch, output):  # noqa: F811
    result, f = _run(broker, monkeypatch, output)
    assert result == (True, output["ODNO"])   # 반환은 원본 값 그대로
    assert "order_ref" not in f
    assert f["odno"] == output["ODNO"] and isinstance(f["org_no"], str) and f["session"] == "regular"
