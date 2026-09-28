"""안전자산 후보 검증 — 종목명은 종목 마스터에서 (2026-09-28).

KIS 현재가 응답엔 종목명이 없어 08-31 이후 검증이 한 번도 돌지 않았다. 마스터 이름으로 보면 현재
후보 코드 3개는 주석과 다른 종목이고(1개는 목록 없음) 전부 미매칭 → 루프가 '영구 비활성'으로 끝난다.
"""
import asyncio
from datetime import datetime
from types import SimpleNamespace

from src.schedulers.kr_scheduler import _pick_safe_asset

CANDIDATES = [
    ("458730", ["KOFR", "코퍼"]),
    ("357870", ["단기채", "단기 채권", "단기채권"]),
    ("152470", ["단기채", "단기 채권", "단기채권"]),
    ("273130", ["단기통안", "통안채", "통안"]),
]

# 2026-09-28 FDR ETF 목록 기준 실제 이름 (152470 은 목록에 없음)
REAL_NAMES = {
    "458730": "TIGER 미국배당다우존스",
    "357870": "TIGER CD금리투자KIS(합성)",
    "273130": "KODEX 종합채권(AA-이상)액티브",
}


class _Master:
    def __init__(self, names, fail=()):
        self.names, self.fail, self.calls = names, set(fail), []

    async def get_name(self, code):
        self.calls.append(code)
        if code in self.fail:
            raise RuntimeError("DB 끊김")
        return self.names.get(code)


def test_current_candidates_are_all_rejected_so_loop_disables():
    sym, name, fetched = asyncio.run(_pick_safe_asset(_Master(REAL_NAMES), CANDIDATES))
    assert sym is None and name == "" and fetched == 3  # 조회는 됐고 전부 미매칭 → 호출부 영구 비활성


def test_matching_name_is_picked():
    master = _Master({**REAL_NAMES, "458730": "KOSEF KOFR액티브"})
    assert asyncio.run(_pick_safe_asset(master, CANDIDATES)) == ("458730", "KOSEF KOFR액티브", 1)
    assert master.calls == ["458730"]  # 첫 매칭에서 멈춤


def test_no_master_or_all_lookup_failures_mean_retry():
    assert asyncio.run(_pick_safe_asset(None, CANDIDATES)) == (None, "", 0)
    failing = _Master(REAL_NAMES, fail=REAL_NAMES.keys())
    assert asyncio.run(_pick_safe_asset(failing, CANDIDATES)) == (None, "", 0)


def test_stock_master_without_pool_counts_as_lookup_failure():
    from src.data.storage.stock_master import StockMaster
    assert asyncio.run(_pick_safe_asset(StockMaster("postgresql://unused"), CANDIDATES)) == (None, "", 0)


# ── 실제 루프(제품 후보 목록) — 주문 전에 끝나는지 고정 (Codex P2) ─────────────

class _FixedDT(datetime):
    @classmethod
    def now(cls, tz=None):
        return cls(2026, 9, 28, 10, 0)  # 월요일 장중


def _run_loop(monkeypatch, tmp_path, stock_master, max_sleeps=3):
    from src.schedulers import kr_scheduler as ks
    submits, sleeps = [], []

    async def _submit(order):
        submits.append(order)
        return True, "X"

    async def _quote(symbol):
        return {"price": 10000.0, "name": ""}  # KIS 현재가엔 종목명이 없다

    bot = SimpleNamespace(running=True, stock_master=stock_master, engine=None, batch_analyzer=None,
                          risk_manager=None, exit_manager=None,
                          broker=SimpleNamespace(submit_order=_submit, get_quote=_quote))

    async def _sleep(sec):
        sleeps.append(sec)
        if len(sleeps) > max_sleeps:
            bot.running = False

    monkeypatch.setattr(ks.asyncio, "sleep", _sleep)
    monkeypatch.setattr(ks, "datetime", _FixedDT)
    monkeypatch.setattr(ks, "is_kr_market_holiday", lambda d: False)
    monkeypatch.setattr(ks.Path, "home", classmethod(lambda cls: tmp_path))
    sched = object.__new__(ks.KRScheduler)
    sched.bot = bot
    asyncio.run(sched.run_safe_asset_loop())
    return submits, sleeps


def test_loop_disables_itself_before_any_order_when_all_candidates_mismatch(monkeypatch, tmp_path):
    master = _Master(REAL_NAMES)
    submits, sleeps = _run_loop(monkeypatch, tmp_path, master)
    assert submits == [] and sleeps == [300]  # 첫 주기에 '영구 비활성' 으로 return
    assert master.calls == ["458730", "357870", "152470", "273130"]


def test_loop_retries_without_orders_when_names_unavailable(monkeypatch, tmp_path):
    submits, sleeps = _run_loop(monkeypatch, tmp_path, None)
    assert submits == [] and len(sleeps) == 4  # 매 주기 재검증, bot.running=False 로만 끝남


def test_loop_validates_once_then_keeps_candidate(monkeypatch, tmp_path):
    master = _Master({"458730": "KOSEF KOFR액티브"})
    submits, sleeps = _run_loop(monkeypatch, tmp_path, master)  # engine=None → 포트폴리오 없음 → 주문 전 continue
    assert submits == [] and len(sleeps) == 4
    assert master.calls == ["458730"]  # 통과 뒤 재검증 없음
