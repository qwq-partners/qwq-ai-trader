"""안전자산 후보 검증 — 종목명은 종목 마스터에서 (2026-09-28).

KIS 현재가 응답엔 종목명이 없어 08-31 이후 검증이 한 번도 돌지 않았다. 마스터 이름으로 보면 현재
후보 코드 3개는 주석과 다른 종목이고(1개는 목록 없음) 전부 미매칭 → 루프가 '영구 비활성'으로 끝난다.
"""
import asyncio

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


def test_safe_asset_loop_reads_names_from_stock_master():
    # 호출부 고정: KIS get_quote 의 빈 이름으로 되돌리는 변이를 잡는다
    import inspect
    from src.schedulers import kr_scheduler
    src = inspect.getsource(kr_scheduler.KRScheduler.run_safe_asset_loop)
    assert '_pick_safe_asset(\n                        getattr(bot, "stock_master", None), SAFE_CANDIDATES' in src
    assert "hts_kor_isnm" not in src
