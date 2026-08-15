"""수확 shadow — Codex 이월 조건 #1·#3 테스트 (2026-08-16)

#1 parity: shadow의 증분 청산(`exit_step` 1봉씩)이 백테스트 `simulate_exit`
   (전 구간 일괄)과 동일한 청산가·손익을 내는가. 두 로직이 갈라지면 shadow
   관측치로 G4를 판정할 근거가 사라진다.
#3 실주문 불가: shadow가 도달 가능한 모듈 그래프에 브로커·엔진·주문 경로가
   임포트되지 않는가. 지금까지는 "그런 코드가 없다"는 구조적 근거뿐이었다.

실행: venv/bin/python -m pytest tests/test_harvest_shadow.py -q
     (또는 venv/bin/python tests/test_harvest_shadow.py)

프로덕션 상태 파일·네트워크 무접촉 — 봉 데이터는 전부 합성.
한계: 진입(D1 트리거·대금 게이트) parity는 미포함 — 조건 #1이 청산만 지목.
"""

import ast
import importlib.util
import random
import sys
from pathlib import Path

import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from src.strategies.harvest_shadow import exit_step  # noqa: E402


def _load_bt():
    """백테스트 모듈 — shadow와 동일한 방식(importlib)으로 로드"""
    spec = importlib.util.spec_from_file_location(
        "_bt_for_test", ROOT / "scripts" / "backtest_t1_gate.py"
    )
    mod = importlib.util.module_from_spec(spec)
    sys.modules["_bt_for_test"] = mod
    spec.loader.exec_module(mod)
    return mod


BT = _load_bt()


def _mkdf(bars):
    """(open, high, low, close) 리스트 → prep 적용 DataFrame"""
    df = pd.DataFrame(bars, columns=["Open", "High", "Low", "Close"])
    df["Volume"] = 1_000_000
    return BT.prep(df)


def _shadow_pnl(d, ei, entry):
    """shadow 경로: 진입 다음 봉부터 1봉씩 exit_step. 백테스트와 동일하게
    끝까지 청산 없으면 마지막 종가로 마감(백테스트 강제청산 대응)."""
    pos = {"entry": entry, "stop": entry * (1 - BT.RISK_PCT / 100), "runner": False}
    for j in range(ei + 1, len(d)):
        px, _reason = exit_step(pos, d.iloc[j])
        if px is not None:
            return (px - entry) / entry * 100 - BT.FEE_RT
    px = float(d["Close"].iloc[-1])
    return (px - entry) / entry * 100 - BT.FEE_RT


def _assert_parity(d, ei, entry, label):
    want = BT.simulate_exit(d, ei, entry)
    got = _shadow_pnl(d, ei, entry)
    assert abs(want - got) < 1e-9, f"{label}: 백테스트 {want:.6f}% vs shadow {got:.6f}%"


# ── 조건 #1: 청산 parity ────────────────────────────────────────────────

def _flat_base(n=40, px=10000.0):
    """저변동 워밍업 구간 — low10/low20 롤링을 채운다"""
    return [(px, px * 1.005, px * 0.995, px) for _ in range(n)]


def test_parity_stop_loss():
    """-4% 손절 (저가가 손절선 관통)"""
    bars = _flat_base() + [(10000, 10050, 9500, 9550)]
    _assert_parity(_mkdf(bars), 39, 10000.0, "손절")


def test_parity_gap_through_stop():
    """갭 관통 — 시가가 이미 손절선 아래면 시가 체결 (손절선 아님)"""
    bars = _flat_base() + [(9000, 9100, 8900, 8950)]
    _assert_parity(_mkdf(bars), 39, 10000.0, "갭관통")


def test_parity_channel_exit():
    """채널 이탈 — 종가가 10일 채널 하단 아래"""
    bars = _flat_base() + [
        (10000, 10800, 9990, 10700),   # 상승 (손절선 위 유지)
        (10700, 10900, 10600, 10800),
        (10800, 10850, 9960, 9970),    # low10 아래로 마감
    ]
    _assert_parity(_mkdf(bars), 39, 10000.0, "채널이탈")


def test_parity_runner_promotion():
    """+30% 도달 시 10일→20일 채널로 승격 — 승격 여부가 청산가를 바꾼다"""
    bars = _flat_base() + [
        (10000, 13500, 9990, 13400),   # +30% 돌파 → runner
        (13400, 13600, 13000, 13100),
        (13100, 13200, 12000, 12050),
        (12000, 12100, 9900, 9950),
    ]
    d = _mkdf(bars)
    _assert_parity(d, 39, 10000.0, "runner 승격")

    pos = {"entry": 10000.0, "stop": 9600.0, "runner": False}
    exit_step(pos, d.iloc[40])
    assert pos["runner"] is True, "고가 +30% 도달했는데 runner 미승격"


def test_parity_no_exit_holds():
    """청산 조건 미달이면 shadow는 포지션을 계속 보유 (stop만 승급)"""
    d = _mkdf(_flat_base(45))
    pos = {"entry": 10000.0, "stop": 9600.0, "runner": False}
    px, reason = exit_step(pos, d.iloc[44])
    assert px is None and reason == "", f"청산되면 안 됨: {px} {reason}"
    assert pos["stop"] >= 9600.0, "stop이 역행(하향)했다"


def test_parity_random_walk():
    """무작위 경로 200개 — 손절·채널·runner가 뒤섞인 실제 형태에서 대조"""
    rng = random.Random(20260816)
    for seed in range(200):
        px, bars = 10000.0, []
        for _ in range(60):
            px = max(1000.0, px * (1 + rng.gauss(0, 0.035)))
            hi = px * (1 + abs(rng.gauss(0, 0.02)))
            lo = px * (1 - abs(rng.gauss(0, 0.02)))
            op = rng.uniform(lo, hi)
            bars.append((op, hi, lo, px))
        d = _mkdf(bars)
        _assert_parity(d, 39, float(d["Close"].iloc[39]), f"random#{seed}")


# ── 조건 #3: 실주문 호출 불가 ───────────────────────────────────────────

# shadow가 런타임에 도달하는 파일 전부 (importlib로 로드하는 백테스트 포함)
_REACHABLE = [
    ROOT / "src" / "strategies" / "harvest_shadow.py",
    ROOT / "scripts" / "backtest_t1_gate.py",
]

# 주문·실전 상태를 건드릴 수 있는 경로. 임포트만 돼도 위반으로 본다.
_FORBIDDEN_IMPORT = (
    "broker", "execution", "exit_manager", "risk", "engine",
    "position_ledger", "portfolio", "order", "kis",
)
_FORBIDDEN_CALL = (
    "place_order", "send_order", "buy", "sell", "submit_order",
    "create_order", "order_cash", "execute_order",
)


def _imported_names(tree):
    """함수 내부 지역 임포트까지 포함해 전부 수집"""
    out = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            out += [a.name for a in node.names]
        elif isinstance(node, ast.ImportFrom):
            base = "." * node.level + (node.module or "")
            out += [f"{base}.{a.name}" for a in node.names]
    return out


def test_no_order_path_imported():
    for path in _REACHABLE:
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for name in _imported_names(tree):
            low = name.lower()
            hit = [w for w in _FORBIDDEN_IMPORT if w in low]
            assert not hit, f"{path.name}: 금지 임포트 '{name}' (매치: {hit})"


def test_no_order_call_sites():
    for path in _REACHABLE:
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            fn = node.func
            name = fn.attr if isinstance(fn, ast.Attribute) else getattr(fn, "id", "")
            assert name not in _FORBIDDEN_CALL, (
                f"{path.name}:{node.lineno} 주문 호출 '{name}'"
            )


def test_ledger_marks_shadow_mode():
    """원장 기록에 execution_mode=shadow가 남아야 실원장과 구분된다 (조건 2)"""
    src = (ROOT / "src" / "strategies" / "harvest_shadow.py").read_text(encoding="utf-8")
    assert '"execution_mode": "shadow"' in src, "원장에 shadow 표식이 없다"


if __name__ == "__main__":
    fns = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    for fn in fns:
        fn()
        print(f"  ok  {fn.__name__}")
    print(f"\n{len(fns)}건 통과")
