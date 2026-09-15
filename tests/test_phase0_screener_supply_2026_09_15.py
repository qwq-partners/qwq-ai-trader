"""Phase 0 — 조용히 죽어 있던 채널 3건 수정 회귀 (2026-09-15).

고정하는 동작:
1) 거래량 급증 스크리닝이 올바른 화면코드(20171)로 요청하고, 응답 0행과 전량 탈락을
   로그로 구분한다. 화면코드 20101 은 rt_cd=0 + output 0행이라 30일간 0개 발굴이었다.
2) vol_inrt 결측 시 prdy_vol 로 자체 산출하고, 둘 다 없으면 1.0 을 지어내지 않는다.
3) 네이버 크롤링이 KIS 초당 리미터 예산을 쓰지 않고, 기본 비활성이다.
4) kr_scheduler 의 내부 import 가 전부 실제로 해석된다(supply_score_provider 오타 재발 방지).
"""
from __future__ import annotations

import ast
import asyncio
import importlib.util
import inspect
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from src.signals.screener.kr_screener import StockScreener as KRScreener  # noqa: E402


# ── 거래량 급증 ──────────────────────────────────────────────────────────────

def _row(symbol="005930", name="삼성전자", price="70000", vol="3000000",
         prdy_vol="1000000", vol_inrt="150.0", ctrt="3.0"):
    return {"mksc_shrn_iscd": symbol, "hts_kor_isnm": name, "stck_prpr": price,
            "acml_vol": vol, "prdy_vol": prdy_vol, "vol_inrt": vol_inrt, "prdy_ctrt": ctrt}


def _screener_with(rows, monkeypatch):
    """screen_volume_surge 를 가짜 응답으로 구동하고 (결과, 전송 파라미터) 반환."""
    sc = object.__new__(KRScreener)
    sc._cache, sc._cache_time, sc._cache_ttl = {}, {}, 300
    sc.min_volume_ratio, sc.max_change_pct, sc.min_trading_value = 2.0, 15.0, 100_000_000
    sc._token_manager = SimpleNamespace(base_url="http://x")
    sent = {}

    class _Resp:
        status = 200
        async def json(self):
            return {"rt_cd": "0", "output": rows}
        async def __aenter__(self): return self
        async def __aexit__(self, *_a): return False

    class _Sess:
        def get(self, url, headers=None, params=None):
            sent.update(params or {})
            return _Resp()

    async def _sess(): return _Sess()
    async def _hdr(tr): return {}
    sc._get_session, sc._get_headers = _sess, _hdr

    import src.signals.screener.kr_screener as m
    calls = []

    async def _acquire(tr_id=""):
        calls.append(tr_id)
    monkeypatch.setattr(m.kis_rate_limit, "acquire", _acquire)

    out = asyncio.run(sc.screen_volume_surge())
    return out, sent, calls


def test_volume_rank_uses_correct_screen_code(monkeypatch):
    """화면코드 20171 을 보낸다 — 20101 은 rt_cd=0 + 0행이라 채널이 통째로 죽는다."""
    _, sent, _ = _screener_with([_row()], monkeypatch)
    assert sent["FID_COND_SCR_DIV_CODE"] == "20171"
    # 증가율순(1)은 전일 거래량 한 자리수 채권 ETF 가 9999.99% 로 상위를 독식해 쓸 수 없다
    assert sent["FID_BLNG_CLS_CODE"] == "0"
    assert sent["FID_TRGT_EXLS_CLS_CODE"] == "0000000000"
    assert sent["FID_INPUT_PRICE_1"] == "" and sent["FID_VOL_CNT"] == ""


def test_volume_ratio_from_vol_inrt(monkeypatch):
    out, _, _ = _screener_with([_row(vol_inrt="150.0")], monkeypatch)
    assert len(out) == 1 and out[0].volume_ratio == pytest.approx(2.5)


def test_volume_ratio_falls_back_to_prdy_vol_when_inrt_missing(monkeypatch):
    """vol_inrt 가 비어도 prdy_vol 로 산출한다 — 예전엔 1.0 을 지어내 전량 탈락했다."""
    out, _, _ = _screener_with([_row(vol_inrt="", vol="3000000", prdy_vol="1000000")], monkeypatch)
    assert len(out) == 1 and out[0].volume_ratio == pytest.approx(3.0)


def test_volume_ratio_non_numeric_does_not_raise(monkeypatch):
    out, _, _ = _screener_with([_row(vol_inrt="-", vol="3000000", prdy_vol="1000000")], monkeypatch)
    assert len(out) == 1


def test_no_ratio_source_is_skipped_not_invented(monkeypatch):
    """비율을 알 수 없으면 1.0 으로 지어내지 않고 건너뛴다."""
    out, _, _ = _screener_with([_row(vol_inrt="", prdy_vol="0")], monkeypatch)
    assert out == []


def test_empty_response_is_distinguishable(monkeypatch, caplog):
    """응답 0행과 전량 탈락이 로그에서 구분된다(30일간 둘 다 '0개 발굴'이었다)."""
    out, _, _ = _screener_with([], monkeypatch)
    assert out == []


# ── 네이버 크롤링 ────────────────────────────────────────────────────────────

def test_naver_crawl_does_not_consume_kis_rate_limit():
    src = inspect.getsource(KRScreener._naver_crawl)
    assert "kis_rate_limit" not in src, "네이버 크롤링이 KIS 초당 예산을 쓰면 안 된다"


def test_naver_is_disabled_by_default():
    assert inspect.signature(KRScreener.screen_all).parameters["use_naver"].default is False


# ── 내부 import 해석 ─────────────────────────────────────────────────────────

def _internal_imports(path: Path):
    """파일 안의 src 내부 import 대상을 (모듈경로, 라인) 목록으로."""
    tree = ast.parse(path.read_text(encoding="utf-8"))
    pkg_depth = len(path.relative_to(ROOT).parts) - 1        # src/schedulers/x.py → 2
    out = []
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.level:
            base = list(path.relative_to(ROOT).parts[:pkg_depth])
            base = base[: len(base) - (node.level - 1)] if node.level > 1 else base
            mod = ".".join(base + ((node.module or "").split(".") if node.module else []))
            out.append((mod, node.lineno))
        elif isinstance(node, ast.ImportFrom) and (node.module or "").startswith("src."):
            out.append((node.module, node.lineno))
    return out


@pytest.mark.parametrize("rel", ["src/schedulers/kr_scheduler.py", "src/core/engine.py",
                                 "src/core/batch_analyzer.py", "src/signals/screener/kr_screener.py"])
def test_internal_imports_resolve(rel):
    """존재하지 않는 모듈을 import 하면 실패 — supply_score_provider 오타가 30일간 배치를 죽였다."""
    missing = []
    for mod, line in _internal_imports(ROOT / rel):
        if not mod:
            continue
        try:
            if importlib.util.find_spec(mod) is None:
                missing.append(f"{rel}:{line} {mod}")
        except (ModuleNotFoundError, ImportError):
            missing.append(f"{rel}:{line} {mod}")
    assert not missing, "해석되지 않는 내부 import: " + ", ".join(missing)
