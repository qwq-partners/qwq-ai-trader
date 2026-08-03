"""
백테스트 사전 검증 게이트 — 진화가 파라미터를 바꾸기 전에 과거 데이터로 확인한다.

기존 진화는 실거래 5영업일 / 10건 남짓의 표본으로 파라미터를 조정했다.
그 표본 크기로는 실력과 잡음을 구분할 수 없어, 노이즈를 학습할 위험이 크다.

이 게이트는 변경안을 적용하기 **전에** 동일 기간을 두 번 백테스트한다:
  ① baseline  — 현재 파라미터
  ② candidate — 진화가 제안한 파라미터
개선이 확인되지 않으면 변경을 기각한다.

판정 기준 (모두 만족해야 통과):
  - 총수익률이 baseline 이상 (MIN_RETURN_GAIN 이상 개선)
  - MDD가 baseline 대비 MAX_MDD_WORSENING 이상 악화되지 않음
  - 후보의 거래 수가 MIN_TRADES 이상 (표본 부족한 결과는 신뢰하지 않음)
  - walk-forward: 기간을 3구간으로 나눠 2구간 이상에서 baseline을 이겨야 함
    (단일 구간 전체 수익률만 보면 특정 레짐에 우연히 맞은 파라미터가 통과한다.
     구간 다수결은 "여러 시장 국면에서 고르게 나은가"를 본다 — 2026-08-03 도입)

실행 실패(데이터 없음/예외/타임아웃) 시에는 **변경을 보류**한다.
파라미터 변경은 급할 이유가 없으므로, 검증 못 한 변경을 적용하는 것보다
하루 미루는 편이 안전하다 (fail-closed).
"""

from __future__ import annotations

import asyncio
import contextlib
import importlib.util
import io
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, Optional

from loguru import logger

# 백테스트 스크립트 경로 (scripts/ 는 패키지가 아니라 파일 경로로 로드한다)
_PROJECT_ROOT = Path(__file__).resolve().parents[3]
_BACKTEST_PATH = _PROJECT_ROOT / "scripts" / "backtest_strategies.py"

# ── 게이트 설정 ────────────────────────────────────────────────
# 매일 20:30 진화에서 2회 실행하므로 가볍게 유지한다 (40종목 2개월 ≈ 25초/회)
# walk-forward 도입으로 3→6개월 확장 (2개월×3구간). OHLCV는 캐시되므로
# 콜드 캐시 첫 실행만 느리다 — 타임아웃을 여유 있게 잡는다.
BT_MONTHS = 6
BT_UNIVERSE_SIZE = 60
BT_TIMEOUT_SEC = 900

MIN_RETURN_GAIN = 0.0      # 총수익률 개선 최소치 (%p). 동률은 통과시키지 않음
MAX_MDD_WORSENING = 1.0    # MDD 악화 허용 한도 (%p)
MIN_TRADES = 10            # 후보 백테스트 최소 거래 수

WF_SEGMENTS = 3            # walk-forward 구간 수 (6개월 → 2개월×3)
WF_MIN_WINS = 2            # 후보가 이겨야 하는 최소 구간 수
WF_MIN_POINTS = 30         # 구간 판정에 필요한 최소 거래일 수 (미만이면 WF 생략)


# 진화 파라미터 -> BacktestConfig 필드 매핑
#   진화는 "strategy.parameter" 형태로 대상을 지목한다 (예: sepa.min_score).
#   백테스트 config는 평평한 필드라서 변환이 필요하다.
#   여기에 없는 파라미터는 백테스트로 검증할 수 없으므로 게이트를 건너뛴다.
PARAM_MAP: Dict[str, str] = {
    # 전략별 진입 기준
    "sepa.min_score": "sepa_min_score",
    "rsi2.min_score": "rsi2_min_score",
    "core.min_score": "core_min_score",
    "core_holding.min_score": "core_min_score",
    # 손절
    "sepa.stop_loss_pct": "sepa_stop_loss_pct",
    "rsi2.stop_loss_pct": "rsi2_stop_loss_pct",
    "core.stop_loss_pct": "core_stop_loss_pct",
    "exit_manager.max_stop_pct": "max_stop_pct",
    "exit_manager.min_stop_pct": "min_stop_pct",
    "exit_manager.atr_multiplier": "atr_multiplier",
    # 분할 익절
    "exit_manager.first_exit_pct": "first_exit_pct",
    "exit_manager.first_exit_ratio": "first_exit_ratio",
    "exit_manager.second_exit_pct": "second_exit_pct",
    "exit_manager.second_exit_ratio": "second_exit_ratio",
    "exit_manager.third_exit_pct": "third_exit_pct",
    "exit_manager.third_exit_ratio": "third_exit_ratio",
    # 트레일링
    "exit_manager.trailing_stop_pct": "trailing_stop_pct",
    "exit_manager.trailing_activate_pct": "trailing_activate_pct",
    # 보유기간/정체
    "sepa.max_holding_days": "sepa_max_holding_days",
    "rsi2.max_holding_days": "rsi2_max_holding_days",
    "exit_manager.stale_exit_days": "stale_exit_days",
    "exit_manager.stale_exit_pnl_pct": "stale_exit_pnl_pct",
}

# exit_manager.stop_loss_pct 는 전략별 손절 3개에 동시 반영해야 한다
_FANOUT: Dict[str, tuple] = {
    "exit_manager.stop_loss_pct": (
        "sepa_stop_loss_pct", "rsi2_stop_loss_pct",
    ),
}


@dataclass
class GateResult:
    """게이트 판정 결과"""
    passed: bool
    reason: str
    skipped: bool = False           # 검증 대상 아님 (매핑 없음 등) → 게이트 미적용
    # 백테스트를 돌리지 못해 보류한 경우 (타임아웃/예외/데이터 없음).
    # "성능이 나빠서 기각"과 구분해야 한다 — 전자는 장애이므로 사람이 알아야 하고,
    # 후자는 게이트가 제 역할을 한 정상 동작이다.
    errored: bool = False
    baseline: Dict[str, Any] = field(default_factory=dict)
    candidate: Dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> Dict[str, Any]:
        keep = ("total_return_pct", "mdd_pct", "win_rate",
                "profit_factor", "total_trades", "sharpe")
        return {
            "passed": self.passed,
            "skipped": self.skipped,
            "errored": self.errored,
            "reason": self.reason,
            "baseline": {k: self.baseline.get(k) for k in keep if k in self.baseline},
            "candidate": {k: self.candidate.get(k) for k in keep if k in self.candidate},
        }


class BacktestGate:
    """진화 변경안을 백테스트로 사전 검증"""

    def __init__(self, enabled: bool = True):
        self.enabled = enabled
        self._module = None

    # ── 백테스트 모듈 로딩 ──────────────────────────────────
    def _load_module(self):
        """scripts/backtest_strategies.py 를 모듈로 로드 (지연 로딩)"""
        if self._module is not None:
            return self._module

        if not _BACKTEST_PATH.exists():
            raise FileNotFoundError(f"백테스트 스크립트 없음: {_BACKTEST_PATH}")

        spec = importlib.util.spec_from_file_location(
            "_bt_strategies_for_gate", _BACKTEST_PATH
        )
        if spec is None or spec.loader is None:
            raise ImportError(f"백테스트 모듈 로드 실패: {_BACKTEST_PATH}")

        module = importlib.util.module_from_spec(spec)
        # 스크립트가 프로젝트 루트 기준 import를 하므로 경로를 보장한다
        root = str(_PROJECT_ROOT)
        if root not in sys.path:
            sys.path.insert(0, root)
        sys.modules["_bt_strategies_for_gate"] = module
        spec.loader.exec_module(module)

        self._module = module
        return module

    # ── 설정 구성 ──────────────────────────────────────────
    def _build_config(self, module, overrides: Dict[str, Any]):
        """BacktestConfig 생성 + 파라미터 오버라이드 적용"""
        cfg = module.BacktestConfig(
            months=BT_MONTHS,
            universe_size=BT_UNIVERSE_SIZE,
            use_cache=True,
        )
        for field_name, value in overrides.items():
            if not hasattr(cfg, field_name):
                logger.warning(f"[백테게이트] 알 수 없는 config 필드 무시: {field_name}")
                continue
            setattr(cfg, field_name, value)
        return cfg

    def _resolve_fields(self, strategy: str, parameter: str) -> list:
        """진화 파라미터를 BacktestConfig 필드명 목록으로 변환"""
        key = f"{strategy}.{parameter}"
        if key in _FANOUT:
            return list(_FANOUT[key])
        if key in PARAM_MAP:
            return [PARAM_MAP[key]]

        # "*.min_score" 처럼 전략이 와일드카드로 넘어오는 경우
        if strategy in ("*", "all"):
            return [v for k, v in PARAM_MAP.items() if k.endswith(f".{parameter}")]
        return []

    # ── 실행 ───────────────────────────────────────────────
    def _run_once(self, module, overrides: Dict[str, Any]) -> Dict[str, Any]:
        """백테스트 1회 실행 (동기, stdout 억제)"""
        cfg = self._build_config(module, overrides)
        engine = module.BacktestEngine(cfg)
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            metrics = engine.run(save_results=False)
        metrics = metrics or {}
        if metrics:
            # walk-forward 구간 판정용 자산 곡선 (to_dict의 keep 목록에 없어 외부 노출 안 됨)
            metrics["_equity_curve"] = list(engine.equity_curve)
        return metrics

    async def verify(self, change: Dict[str, Any]) -> GateResult:
        """
        변경안을 검증한다.

        Args:
            change: {"strategy": str, "parameter": str,
                     "old_value": Any, "new_value": Any, ...}

        Returns:
            GateResult — passed=False면 변경을 적용하지 않아야 한다.
                         skipped=True면 검증 대상이 아니므로 기존 흐름대로 진행한다.
        """
        if not self.enabled:
            return GateResult(True, "게이트 비활성", skipped=True)

        strategy = str(change.get("strategy", ""))
        parameter = str(change.get("parameter", ""))
        old_value = change.get("old_value")
        new_value = change.get("new_value")

        fields = self._resolve_fields(strategy, parameter)
        if not fields:
            # 백테스트가 모사하지 못하는 파라미터 (예: 배분 비율, 알림 설정)
            return GateResult(
                True, f"백테스트 미지원 파라미터 ({strategy}.{parameter}) — 게이트 생략",
                skipped=True,
            )

        if old_value is None or new_value is None:
            return GateResult(True, "old/new 값 없음 — 게이트 생략", skipped=True)

        try:
            module = await asyncio.to_thread(self._load_module)

            base_overrides = {f: old_value for f in fields}
            cand_overrides = {f: new_value for f in fields}

            logger.info(
                f"[백테게이트] 검증 시작: {strategy}.{parameter} "
                f"{old_value} -> {new_value} (필드 {fields}, "
                f"{BT_MONTHS}개월/{BT_UNIVERSE_SIZE}종목)"
            )

            baseline = await asyncio.wait_for(
                asyncio.to_thread(self._run_once, module, base_overrides),
                timeout=BT_TIMEOUT_SEC,
            )
            candidate = await asyncio.wait_for(
                asyncio.to_thread(self._run_once, module, cand_overrides),
                timeout=BT_TIMEOUT_SEC,
            )
        except asyncio.TimeoutError:
            logger.error(f"[백테게이트] 타임아웃 ({BT_TIMEOUT_SEC}s) — 변경 보류")
            return GateResult(False, f"백테스트 타임아웃 ({BT_TIMEOUT_SEC}s) — 변경 보류",
                              errored=True)
        except Exception as e:
            logger.exception(f"[백테게이트] 실행 실패 — 변경 보류: {e}")
            return GateResult(False, f"백테스트 실행 실패 ({e}) — 변경 보류", errored=True)

        if not baseline or not candidate:
            return GateResult(False, "백테스트 결과 없음 (데이터 부족) — 변경 보류",
                              errored=True, baseline=baseline, candidate=candidate)

        return self._judge(baseline, candidate, strategy, parameter,
                           old_value, new_value)

    # ── walk-forward 구간 수익률 ───────────────────────────
    @staticmethod
    def _segment_returns(curve, n_segments: int):
        """자산 곡선을 n등분해 구간별 수익률(%) 리스트를 반환.

        baseline/candidate는 동일 유니버스·동일 거래일로 시뮬레이션되므로
        인덱스 등분만으로 두 곡선의 구간이 날짜 단위로 정렬된다.
        """
        if not curve or len(curve) < WF_MIN_POINTS:
            return None
        size = len(curve) // n_segments
        if size < 2:
            return None
        rets = []
        for i in range(n_segments):
            lo = i * size
            hi = (i + 1) * size if i < n_segments - 1 else len(curve)
            start_eq = curve[lo][1]
            end_eq = curve[hi - 1][1]
            rets.append((end_eq - start_eq) / start_eq * 100 if start_eq > 0 else 0.0)
        return rets

    # ── 판정 ───────────────────────────────────────────────
    def _judge(self, baseline: Dict, candidate: Dict, strategy: str,
               parameter: str, old_value: Any, new_value: Any) -> GateResult:
        b_ret = float(baseline.get("total_return_pct", 0.0))
        c_ret = float(candidate.get("total_return_pct", 0.0))
        b_mdd = abs(float(baseline.get("mdd_pct", 0.0)))
        c_mdd = abs(float(candidate.get("mdd_pct", 0.0)))
        c_trades = int(candidate.get("total_trades", 0))

        gain = c_ret - b_ret
        mdd_delta = c_mdd - b_mdd

        summary = (
            f"{strategy}.{parameter} {old_value}->{new_value} | "
            f"수익률 {b_ret:+.2f}% -> {c_ret:+.2f}% ({gain:+.2f}%p), "
            f"MDD {b_mdd:.2f}% -> {c_mdd:.2f}% ({mdd_delta:+.2f}%p), "
            f"거래 {c_trades}건"
        )

        if c_trades < MIN_TRADES:
            reason = f"표본 부족 (거래 {c_trades}건 < {MIN_TRADES}건) — 변경 보류 | {summary}"
            logger.warning(f"[백테게이트] 기각: {reason}")
            return GateResult(False, reason, baseline=baseline, candidate=candidate)

        if gain <= MIN_RETURN_GAIN:
            reason = f"수익률 개선 없음 ({gain:+.2f}%p) — 변경 기각 | {summary}"
            logger.info(f"[백테게이트] 기각: {reason}")
            return GateResult(False, reason, baseline=baseline, candidate=candidate)

        # walk-forward: 전체 수익률이 좋아도 특정 구간에 몰빵된 개선이면 기각
        b_seg = self._segment_returns(baseline.get("_equity_curve"), WF_SEGMENTS)
        c_seg = self._segment_returns(candidate.get("_equity_curve"), WF_SEGMENTS)
        if b_seg is not None and c_seg is not None:
            wins = sum(1 for b, c in zip(b_seg, c_seg) if c > b)
            seg_txt = (
                f"구간승 {wins}/{WF_SEGMENTS} "
                f"(base {['%.1f' % r for r in b_seg]} vs "
                f"cand {['%.1f' % r for r in c_seg]})"
            )
            summary += f" | {seg_txt}"
            if wins < WF_MIN_WINS:
                reason = (f"walk-forward 미달 ({seg_txt}, 최소 {WF_MIN_WINS}구간) "
                          f"— 변경 기각 | {summary}")
                logger.info(f"[백테게이트] 기각: {reason}")
                return GateResult(False, reason, baseline=baseline, candidate=candidate)
        else:
            # 자산 곡선이 짧으면(거래일 부족) WF는 생략하고 기존 기준만 적용
            logger.warning("[백테게이트] 자산 곡선 부족 — walk-forward 판정 생략")

        if mdd_delta > MAX_MDD_WORSENING:
            reason = (f"MDD 악화 ({mdd_delta:+.2f}%p > {MAX_MDD_WORSENING}%p) "
                      f"— 변경 기각 | {summary}")
            logger.info(f"[백테게이트] 기각: {reason}")
            return GateResult(False, reason, baseline=baseline, candidate=candidate)

        reason = f"검증 통과 | {summary}"
        logger.info(f"[백테게이트] 통과: {reason}")
        return GateResult(True, reason, baseline=baseline, candidate=candidate)


_gate: Optional[BacktestGate] = None


def get_backtest_gate(enabled: bool = True) -> BacktestGate:
    """싱글턴 게이트"""
    global _gate
    if _gate is None:
        _gate = BacktestGate(enabled=enabled)
    return _gate
