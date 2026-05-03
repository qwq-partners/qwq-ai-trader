"""
AI Trading Bot v2 - 전략 진화기 (Strategy Evolver)

규칙 기반 자동 튜닝 + LLM 보조 분석.
한 번에 1개 파라미터만 변경, 5영업일+10건 평가, 즉시 롤백.
"""

import json
import os
from dataclasses import dataclass, field, asdict
from datetime import datetime, date, timedelta
from pathlib import Path
from typing import Dict, List, Optional, Any, Callable, Tuple
from loguru import logger

from .trade_journal import get_trade_journal
from .trade_reviewer import get_trade_reviewer, ReviewResult
from .config_persistence import get_evolved_config_manager


# ============================================================
# 데이터 클래스
# ============================================================

@dataclass
class ParameterChange:
    """파라미터 변경 기록"""
    timestamp: str  # ISO format
    strategy: str
    parameter: str
    old_value: Any
    new_value: Any
    reason: str
    source: str  # "rule" | "llm" | "manual" | "rollback"

    # 변경 전 지표 (비교 기준)
    win_rate_before: float = 0.0
    profit_factor_before: float = 0.0
    trades_before: int = 0

    # 변경 후 지표 (평가 시 채워짐)
    win_rate_after: float = 0.0
    profit_factor_after: float = 0.0
    trades_after: int = 0
    is_effective: Optional[bool] = None  # True=유지, False=롤백, None=평가중

    # 평가 메타
    applied_date: str = ""  # 적용 날짜 (YYYY-MM-DD)

    def to_dict(self) -> Dict:
        return asdict(self)


@dataclass
class EvolutionState:
    """진화 상태 (단순화)"""
    version: int = 1
    # 현재 활성 변경 (최대 1개만)
    active_change: Optional[ParameterChange] = None
    # 이력
    history: List[ParameterChange] = field(default_factory=list)
    # 통계
    total_applied: int = 0
    total_kept: int = 0
    total_rolled_back: int = 0

    def to_dict(self) -> Dict:
        return {
            "version": self.version,
            "active_change": self.active_change.to_dict() if self.active_change else None,
            "history": [h.to_dict() for h in self.history[-50:]],
            "total_applied": self.total_applied,
            "total_kept": self.total_kept,
            "total_rolled_back": self.total_rolled_back,
        }


@dataclass
class AutoTuningRule:
    """자동 튜닝 규칙"""
    name: str
    condition: Callable[[ReviewResult], bool]
    parameter: str  # 조정 대상 ("*.min_score" or "exit_manager.stop_loss_pct")
    adjustment: Callable[[Any], Any]  # current_value -> new_value
    reason_template: str  # 사유 템플릿 (f-string 변수: review)


# ============================================================
# 영업일 계산 헬퍼
# ============================================================

def _count_trading_days(start_date: date, end_date: date) -> int:
    """두 날짜 사이의 영업일 수 (주말 제외, 공휴일은 근사)"""
    days = 0
    current = start_date + timedelta(days=1)
    while current <= end_date:
        if current.weekday() < 5:  # 월~금
            days += 1
        current += timedelta(days=1)
    return days


# ============================================================
# 내장 규칙
# ============================================================

def _build_rules() -> List[AutoTuningRule]:
    """내장 자동 튜닝 규칙 목록"""
    return [
        # 승률 낮으면 → 진입 기준 강화
        AutoTuningRule(
            name="low_win_rate",
            condition=lambda r: r.win_rate < 40 and r.total_trades >= 5,
            parameter="*.min_score",
            adjustment=lambda v: min(v + 5, 90),
            reason_template="승률 {win_rate:.0f}% < 40% -> 진입 기준 +5",
        ),

        # 승률 높으면 → 진입 기준 완화
        AutoTuningRule(
            name="high_win_rate",
            condition=lambda r: r.win_rate > 65 and r.total_trades >= 10,
            parameter="*.min_score",
            adjustment=lambda v: max(v - 5, 40),
            reason_template="승률 {win_rate:.0f}% > 65% -> 진입 기준 -5",
        ),

        # 손익비 < 1.0 → 손절 축소
        AutoTuningRule(
            name="bad_profit_factor",
            condition=lambda r: r.profit_factor < 1.0 and r.total_trades >= 5,
            parameter="exit_manager.stop_loss_pct",
            adjustment=lambda v: max(v - 0.5, 3.0),
            reason_template="손익비 {profit_factor:.2f} < 1.0 -> 손절 -0.5%",
        ),

        # 거래 부족 → 진입 기준 완화
        AutoTuningRule(
            name="low_frequency",
            condition=lambda r: r.total_trades > 0 and r.total_trades < 5,
            parameter="*.min_score",
            adjustment=lambda v: max(v - 3, 40),
            reason_template="거래 부족 ({total_trades}건/7일) -> 기준 완화 -3",
        ),

        # 평균 손실 크면 → ATR 손절 상한 축소
        AutoTuningRule(
            name="large_avg_loss",
            condition=lambda r: r.avg_pnl_pct < -2.0 and r.total_trades >= 5,
            parameter="exit_manager.max_stop_pct",
            adjustment=lambda v: max(v - 1.0, 3.0),
            reason_template="평균 손익 {avg_pnl_pct:.1f}% < -2% -> ATR상한 -1%",
        ),
    ]


# ============================================================
# StrategyEvolver (재작성)
# ============================================================

class StrategyEvolver:
    """
    전략 진화기 (단순화 재설계)

    원칙:
    1. 규칙 기반 우선 (LLM 없이 독립 작동)
    2. 한 번에 1개만 변경
    3. 5영업일 + 10건 최소 거래로 평가
    4. 악화 시 즉시 롤백

    충돌 방지 (daily_bias / llm_regime_today):
    - daily_bias.json: 매일 20:30 LLM 리뷰 후 생성 → 익일 배치 스캔에서 score boost 적용
    - llm_regime_today.json: 매일 08:10 생성 → 배치 스캔에서 min_score 오버라이드
    - 진화 로직(여기): 주 1회 또는 일 1회 파라미터 변경 → evolved_overrides.yml에 영속화
    - 우선순위: daily_bias/regime은 일시적 보정(당일 한정), 진화는 영속적 변경 → 충돌 없음
    - daily_bias는 score에 가감만 하고 evolved_overrides의 min_score 자체를 변경하지 않음
    """

    def __init__(
        self,
        storage_dir: str = None,
    ):
        self.reviewer = get_trade_reviewer()
        self.journal = get_trade_journal()

        # LLM 전략가 (선택적, 초기화 실패해도 무방)
        self.strategist = None
        try:
            from .llm_strategist import get_llm_strategist
            self.strategist = get_llm_strategist()
        except Exception:
            logger.info("[진화] LLM 전략가 미사용 (규칙 기반만 작동)")

        # 저장소
        self.storage_dir = Path(storage_dir or os.getenv(
            "EVOLUTION_DIR",
            os.path.expanduser("~/.cache/ai_trader/evolution")
        ))
        self.storage_dir.mkdir(parents=True, exist_ok=True)

        # 상태
        self.state = self._load_state()

        # 규칙
        self._rules = _build_rules()

        # 전략/컴포넌트 참조 (외부에서 설정)
        self._strategies: Dict[str, Any] = {}
        self._components: Dict[str, Any] = {}
        self._component_config_attrs: Dict[str, str] = {}

        # 진화 잠금 파라미터 — 수동 분석 후에만 조정 (evolved_overrides 덮어쓰기 금지)
        self._locked_params: set = {
            "base_position_pct",       # 포지션 크기: 25% 고정
            "trailing_stop_pct",       # 트레일링 스탑: 3.0% 고정
            "trailing_activate_pct",   # 트레일링 활성화: 5.0% 고정
            "first_exit_pct",          # 1차 익절: 5.0% 고정
            "stop_loss_pct",           # 손절 비율: 수동 조정만 허용
        }

        # 파라미터 범위 (locked 파라미터는 진화 대상에서 자동 제외)
        self._param_bounds: Dict[str, Tuple[Any, Any]] = {
            "min_score": (40, 85),
            "stop_loss_pct": (1.0, 8.0),
            "take_profit_pct": (2.0, 20.0),
            "max_stop_pct": (3.0, 10.0),
            "min_stop_pct": (1.0, 5.0),
            "daily_max_loss_pct": (2.0, 8.0),
            "max_atr_pct": (3.0, 8.0),
            "min_change_pct": (1.0, 5.0),
            "min_volume_ratio": (1.0, 5.0),
        }

        logger.info(f"StrategyEvolver 초기화: 규칙 {len(self._rules)}개, 저장소 {self.storage_dir}")

    # ============================================================
    # 상태 로드/저장
    # ============================================================

    def _load_state(self) -> EvolutionState:
        """진화 상태 로드"""
        state_file = self.storage_dir / "evolution_state.json"
        if not state_file.exists():
            return EvolutionState()

        try:
            with open(state_file, "r", encoding="utf-8") as f:
                data = json.load(f)

            active = None
            if data.get("active_change"):
                ac = data["active_change"]
                active = ParameterChange(**{
                    k: ac.get(k)
                    for k in ParameterChange.__dataclass_fields__
                    if k in ac
                })

            history = []
            for h in data.get("history", []):
                try:
                    history.append(ParameterChange(**{
                        k: h.get(k)
                        for k in ParameterChange.__dataclass_fields__
                        if k in h
                    }))
                except Exception:
                    pass

            state = EvolutionState(
                version=data.get("version", 1),
                active_change=active,
                history=history,
                total_applied=data.get("total_applied", 0),
                total_kept=data.get("total_kept", 0),
                total_rolled_back=data.get("total_rolled_back", 0),
            )

            logger.info(
                f"진화 상태 로드: v{state.version}, "
                f"적용={state.total_applied}, 유지={state.total_kept}, 롤백={state.total_rolled_back}, "
                f"활성={'있음' if state.active_change else '없음'}"
            )
            return state

        except Exception as e:
            logger.warning(f"진화 상태 로드 실패: {e}")
            return EvolutionState()

    def _save_state(self):
        """진화 상태 저장"""
        try:
            state_file = self.storage_dir / "evolution_state.json"
            with open(state_file, "w", encoding="utf-8") as f:
                json.dump(self.state.to_dict(), f, ensure_ascii=False, indent=2)
        except Exception as e:
            logger.error(f"진화 상태 저장 실패: {e}")

    # ============================================================
    # 전략/컴포넌트 등록
    # ============================================================

    def register_strategy(self, name: str, strategy: Any, param_setters: Dict[str, Callable] = None):
        """전략 등록"""
        self._strategies[name] = strategy

        # LLM 전략가에 현재 파라미터 전달
        if self.strategist and hasattr(strategy, 'config'):
            config = strategy.config
            params = {
                k: getattr(config, k)
                for k in dir(config)
                if not k.startswith('_') and not callable(getattr(config, k))
            }
            self.strategist.set_current_params(name, params)

        logger.info(f"전략 등록: {name}")

    def register_component(self, name: str, component: Any, config_attr: str = "config"):
        """컴포넌트 등록 (ExitManager, RiskManager 등)"""
        self._components[name] = component
        self._component_config_attrs[name] = config_attr

        config_obj = getattr(component, config_attr, None)
        if config_obj is None:
            config_obj = component
            self._component_config_attrs[name] = "__self__"

        # LLM 전략가에 현재 파라미터 전달
        if self.strategist:
            params = {
                k: getattr(config_obj, k)
                for k in dir(config_obj)
                if not k.startswith('_') and not callable(getattr(config_obj, k))
            }
            self.strategist.set_current_params(name, params)

        logger.info(f"컴포넌트 등록: {name}")

    # ============================================================
    # 진화 실행 (핵심)
    # ============================================================

    async def evolve(self, days: int = 7, dry_run: bool = False) -> Dict[str, Any]:
        """
        전략 진화 실행

        Returns:
            {"status": "applied|skipped|waiting|no_change|rollback|keep",
             "change": {...} or None, "reason": str}
        """
        logger.info(f"[진화] 최근 {days}일 분석 시작 (dry_run={dry_run})")

        # 1. 복기
        review = self.reviewer.review_period(days)
        if review.total_trades < 3:
            logger.info(f"[진화] 거래 부족 ({review.total_trades}건 < 3건), 스킵")
            return {"status": "skipped", "reason": f"거래 부족 ({review.total_trades}건)"}

        # 2. 활성 변경이 있으면 먼저 평가
        if self.state.active_change:
            eval_result = self._evaluate_active_change(review)
            if eval_result == "rollback":
                self._rollback_active_change()
                self._save_state()
                return {"status": "rollback", "change": self.state.history[-1].to_dict() if self.state.history else None}
            elif eval_result == "keep":
                self._finalize_active_change(review)
                self._save_state()
                return {"status": "keep", "change": self.state.history[-1].to_dict() if self.state.history else None}
            else:  # "wait"
                return {
                    "status": "waiting",
                    "reason": "활성 변경 평가 대기 중",
                    "active_change": self.state.active_change.to_dict(),
                }

        # 3. 규칙 기반 트리거 확인 (한 번에 1개만)
        triggered = self._find_triggered_rule(review)

        # 4. 규칙 없으면 LLM 보조 분석 (선택적)
        if not triggered and self.strategist and not dry_run:
            triggered = await self._get_llm_suggestion(review, days)

        if triggered and not dry_run:
            self._apply_change(triggered, review)
            self._save_state()
            return {"status": "applied", "change": triggered}

        if triggered and dry_run:
            return {"status": "dry_run", "change": triggered}

        return {"status": "no_change", "reason": "트리거 규칙 없음"}

    # ============================================================
    # 평가 로직
    # ============================================================

    def _evaluate_active_change(self, current_review: ReviewResult) -> str:
        """활성 변경 평가: 'keep' | 'rollback' | 'wait'"""
        change = self.state.active_change
        if not change:
            return "wait"

        # 적용 날짜 파싱
        try:
            applied = date.fromisoformat(change.applied_date)
        except (ValueError, TypeError):
            return "wait"

        today = date.today()
        trading_days = _count_trading_days(applied, today)

        # 최소 5영업일 경과 필요
        if trading_days < 5:
            logger.debug(f"[진화 평가] 경과 {trading_days}영업일 < 5일, 대기")
            return "wait"

        # 최소 10건 거래 필요 (변경 적용일 이후 거래만 필터링)
        recent = self.journal.get_closed_trades(days=trading_days + 2)  # 약간 여유
        recent = [t for t in recent
                  if t.exit_time and t.exit_time.date() >= applied
                  and t.entry_time and t.entry_time.date() >= applied
                  and not t.is_sync]  # 동기화 포지션 제외, 변경 이전 진입 거래 제외
        if len(recent) < 10:
            if trading_days > 10:  # 10영업일 넘었는데도 10건 미달 → 검증 불가, 보수적 롤백
                logger.info(f"[진화 평가] {trading_days}영업일 경과, {len(recent)}건 < 10건 → 데이터 부족으로 롤백 (보수적)")
                return "rollback"
            logger.debug(f"[진화 평가] {len(recent)}건 < 10건, 대기")
            return "wait"

        # 비교 지표
        before_wr = change.win_rate_before
        after_wr = sum(1 for t in recent if t.is_win) / len(recent) * 100

        before_pf = change.profit_factor_before
        total_profit = sum(t.pnl for t in recent if t.is_win) or 0
        total_loss = abs(sum(t.pnl for t in recent if not t.is_win))
        after_pf = min(total_profit / total_loss, 99.9) if total_loss > 0 else (99.9 if total_profit > 0 else 0)

        logger.info(
            f"[진화 평가] {change.strategy}.{change.parameter}: "
            f"승률 {before_wr:.1f}% -> {after_wr:.1f}%, "
            f"PF {before_pf:.2f} -> {after_pf:.2f}"
        )

        # 판정: 승률 5%p 이상 하락 OR 손익비 0.3 이상 하락 → 롤백
        if after_wr < before_wr - 5 or after_pf < before_pf - 0.3:
            logger.warning(
                f"[진화 평가] 악화 감지 → 롤백 "
                f"(승률 차이: {after_wr - before_wr:+.1f}%p, PF 차이: {after_pf - before_pf:+.2f})"
            )
            return "rollback"

        # 그 외 → 유지
        return "keep"

    def _rollback_active_change(self):
        """활성 변경 롤백"""
        change = self.state.active_change
        if not change:
            return

        # 원래 값으로 복원
        self._set_param_value(change.strategy, change.parameter, change.old_value)

        # 영속화에서 제거
        try:
            config_mgr = get_evolved_config_manager()
            config_mgr.remove_override(change.strategy, change.parameter)
        except Exception as e:
            logger.warning(f"[진화] 영속화 롤백 실패: {e}")

        # 이력 기록
        rollback_record = ParameterChange(
            timestamp=datetime.now().isoformat(),
            strategy=change.strategy,
            parameter=change.parameter,
            old_value=change.new_value,
            new_value=change.old_value,
            reason=f"자동 롤백 (원래 변경: {change.reason})",
            source="rollback",
            applied_date=date.today().isoformat(),
        )
        self.state.history.append(rollback_record)
        self.state.active_change = None
        self.state.total_rolled_back += 1

        logger.warning(
            f"[진화] 롤백: {change.strategy}.{change.parameter} "
            f"= {change.new_value} -> {change.old_value}"
        )

    def _finalize_active_change(self, review: ReviewResult):
        """활성 변경 확정 (유지)"""
        change = self.state.active_change
        if not change:
            return

        change.is_effective = True
        change.win_rate_after = review.win_rate
        change.profit_factor_after = review.profit_factor
        change.trades_after = review.total_trades

        self.state.history.append(change)
        self.state.active_change = None
        self.state.total_kept += 1

        logger.info(
            f"[진화] 확정 유지: {change.strategy}.{change.parameter} "
            f"= {change.new_value}"
        )

    # ============================================================
    # 규칙 기반 트리거
    # ============================================================

    def _find_triggered_rule(self, review: ReviewResult) -> Optional[Dict]:
        """트리거된 규칙 찾기 (최초 1개만)"""
        for rule in self._rules:
            try:
                if not rule.condition(review):
                    continue

                # 파라미터 대상 결정
                targets = self._resolve_param_targets(rule.parameter)
                if not targets:
                    continue

                # low_frequency 규칙: 전략별 성과에서 가장 거래가 적은 전략만 타겟팅
                if rule.name == "low_frequency" and review.strategy_performance:
                    targets = self._narrow_targets_by_lowest_trades(
                        targets, review.strategy_performance
                    )
                    if not targets:
                        continue

                # 첫 번째 대상만 사용
                strategy_name, param_name = targets[0]

                # 잠금 파라미터 건너뛰기
                if param_name in self._locked_params:
                    continue

                current_value = self._get_param_value(strategy_name, param_name)
                if current_value is None:
                    continue

                new_value = rule.adjustment(current_value)

                # bounds 적용
                new_value = self._clamp_value(param_name, new_value, current_value)

                # 변경 없으면 스킵
                if new_value == current_value:
                    continue

                # 사유 생성
                reason = rule.reason_template.format(
                    win_rate=review.win_rate,
                    profit_factor=review.profit_factor,
                    total_trades=review.total_trades,
                    avg_pnl_pct=review.avg_pnl_pct,
                )

                logger.info(f"[진화] 규칙 트리거: {rule.name} -> {strategy_name}.{param_name}")

                return {
                    "strategy": strategy_name,
                    "parameter": param_name,
                    "old_value": current_value,
                    "new_value": new_value,
                    "reason": reason,
                    "source": "rule",
                    "rule_name": rule.name,
                }

            except Exception as e:
                logger.warning(f"[진화] 규칙 체크 오류 ({rule.name}): {e}")

        return None

    def _resolve_param_targets(self, param_pattern: str) -> List[Tuple[str, str]]:
        """파라미터 패턴 해석: "*.min_score" -> [(strategy1, min_score), ...]"""
        if "." in param_pattern:
            prefix, param = param_pattern.split(".", 1)
        else:
            return []

        targets = []
        if prefix == "*":
            # 모든 전략에서 찾기
            for name, strategy in self._strategies.items():
                if hasattr(strategy, 'config') and hasattr(strategy.config, param):
                    targets.append((name, param))
            # 컴포넌트에서도 찾기
            for name in self._components:
                config_obj = self._get_component_config(name)
                if config_obj and hasattr(config_obj, param):
                    targets.append((name, param))
        else:
            # 특정 컴포넌트/전략
            if prefix in self._strategies:
                strategy = self._strategies[prefix]
                if hasattr(strategy, 'config') and hasattr(strategy.config, param):
                    targets.append((prefix, param))
            elif prefix in self._components:
                config_obj = self._get_component_config(prefix)
                if config_obj and hasattr(config_obj, param):
                    targets.append((prefix, param))

        return targets

    def _narrow_targets_by_lowest_trades(
        self,
        targets: List[Tuple[str, str]],
        strategy_performance: Dict[str, Dict],
    ) -> List[Tuple[str, str]]:
        """와일드카드 타겟 중 거래가 가장 적은 전략만 반환 (low_frequency 전용)

        strategy_performance에 등록된 전략만 비교하고, 등록되지 않은 전략은
        거래 0건으로 간주합니다.
        """
        if not targets:
            return targets

        # 각 타겟 전략의 거래 수 확인
        target_trades = []
        for strategy_name, param_name in targets:
            perf = strategy_performance.get(strategy_name, {})
            trades_count = perf.get("trades", 0)
            target_trades.append((strategy_name, param_name, trades_count))

        # 거래가 가장 적은 전략 선택
        target_trades.sort(key=lambda x: x[2])
        lowest = target_trades[0]
        logger.info(
            f"[진화] low_frequency 타겟 좁히기: {lowest[0]}.{lowest[1]} "
            f"(거래 {lowest[2]}건, 전체 {len(targets)}개 전략 중)"
        )
        return [(lowest[0], lowest[1])]

    # ============================================================
    # LLM 보조 분석 (선택적)
    # ============================================================

    async def _get_llm_suggestion(self, review: ReviewResult, days: int) -> Optional[Dict]:
        """LLM에게 파라미터 조정 제안 받기 (실패해도 무방)"""
        if not self.strategist:
            return None

        try:
            advice = await self.strategist.analyze_and_advise(days)
            if not advice or not advice.parameter_adjustments:
                return None

            # 신뢰도 높은 첫 번째 제안만 사용
            for adj in advice.parameter_adjustments:
                if adj.confidence < 0.6:
                    continue

                # 잠금 파라미터 건너뛰기
                raw_param = adj.parameter.split(".")[-1] if "." in adj.parameter else adj.parameter
                if raw_param in self._locked_params:
                    logger.info(f"[진화] 잠금 파라미터 스킵: {adj.parameter}")
                    continue

                # 파라미터 키 찾기
                param_key = adj.parameter
                if "." in param_key:
                    strategy_name, param_name = param_key.split(".", 1)
                else:
                    # 전체 검색
                    found = False
                    for name in list(self._strategies.keys()) + list(self._components.keys()):
                        targets = self._resolve_param_targets(f"{name}.{param_key}")
                        if targets:
                            strategy_name, param_name = targets[0]
                            found = True
                            break
                    if not found:
                        continue

                current = self._get_param_value(strategy_name, param_name)
                if current is None:
                    continue

                new_value = self._clamp_value(param_name, adj.suggested_value, current)

                if new_value == current:
                    continue

                return {
                    "strategy": strategy_name,
                    "parameter": param_name,
                    "old_value": current,
                    "new_value": new_value,
                    "reason": adj.reason,
                    "source": "llm",
                }

        except Exception as e:
            logger.warning(f"[진화] LLM 분석 실패 (무시): {e}")

        return None

    # ============================================================
    # 파라미터 적용
    # ============================================================

    def _apply_change(self, change_dict: Dict, review: ReviewResult):
        """변경 적용 (영속화 먼저 → 성공 시 런타임 적용)"""
        strategy_name = change_dict["strategy"]
        param_name = change_dict["parameter"]
        new_value = change_dict["new_value"]

        # 영속화 먼저 (실패 시 런타임 변경도 취소)
        try:
            config_mgr = get_evolved_config_manager()
            config_mgr.save_override(strategy_name, param_name, new_value, source=change_dict.get("source", "rule"))
        except Exception as e:
            logger.error(f"[진화] 영속화 실패, 변경 취소: {e}")
            return

        # 영속화 성공 후 런타임 적용
        self._set_param_value(strategy_name, param_name, new_value)

        # 활성 변경으로 등록
        change = ParameterChange(
            timestamp=datetime.now().isoformat(),
            strategy=strategy_name,
            parameter=param_name,
            old_value=change_dict["old_value"],
            new_value=new_value,
            reason=change_dict["reason"],
            source=change_dict.get("source", "rule"),
            win_rate_before=review.win_rate,
            profit_factor_before=review.profit_factor,
            trades_before=review.total_trades,
            applied_date=date.today().isoformat(),
        )

        self.state.active_change = change
        self.state.total_applied += 1
        self.state.version += 1

        logger.info(
            f"[진화] 변경 적용: {strategy_name}.{param_name} "
            f"= {change_dict['old_value']} -> {new_value} "
            f"(사유: {change_dict['reason']})"
        )

    # ============================================================
    # 파라미터 값 읽기/쓰기
    # ============================================================

    def _get_param_value(self, strategy_name: str, param_name: str) -> Optional[Any]:
        """파라미터 현재 값 조회"""
        if strategy_name in self._strategies:
            strategy = self._strategies[strategy_name]
            if hasattr(strategy, 'config') and hasattr(strategy.config, param_name):
                return getattr(strategy.config, param_name)

        if strategy_name in self._components:
            config_obj = self._get_component_config(strategy_name)
            if config_obj and hasattr(config_obj, param_name):
                return getattr(config_obj, param_name)

        return None

    def _set_param_value(self, strategy_name: str, param_name: str, value: Any) -> bool:
        """파라미터 값 설정"""
        if strategy_name in self._strategies:
            strategy = self._strategies[strategy_name]
            if hasattr(strategy, 'config') and hasattr(strategy.config, param_name):
                setattr(strategy.config, param_name, value)
                return True

        if strategy_name in self._components:
            config_obj = self._get_component_config(strategy_name)
            if config_obj and hasattr(config_obj, param_name):
                setattr(config_obj, param_name, value)
                return True

        return False

    def _get_component_config(self, comp_name: str) -> Any:
        """컴포넌트의 config 객체 반환"""
        component = self._components.get(comp_name)
        if component is None:
            return None
        config_attr = self._component_config_attrs.get(comp_name, "config")
        if config_attr == "__self__":
            return component
        return getattr(component, config_attr, None)

    def _clamp_value(self, param_name: str, new_value: Any, current_value: Any) -> Any:
        """파라미터 범위 제한"""
        if param_name not in self._param_bounds:
            return new_value
        min_val, max_val = self._param_bounds[param_name]
        try:
            clamped_float = max(min_val, min(max_val, float(new_value)))
            # float 파라미터는 float 유지 (int 캐스팅 시 소수점 손실 방지)
            clamped = clamped_float if isinstance(current_value, float) else type(current_value)(clamped_float)
            return clamped
        except (ValueError, TypeError):
            return current_value

    # ============================================================
    # 외부 인터페이스 (하위 호환)
    # ============================================================

    def get_evolution_summary(self) -> Dict:
        """진화 요약"""
        total_decided = self.state.total_kept + self.state.total_rolled_back
        return {
            "version": self.state.version,
            "total_evolutions": self.state.total_applied,
            "last_evolution": self.state.active_change.timestamp if self.state.active_change else (
                self.state.history[-1].timestamp if self.state.history else None
            ),
            "active_changes": 1 if self.state.active_change else 0,
            "successful_changes": self.state.total_kept,
            "rolled_back_changes": self.state.total_rolled_back,
            "success_rate": (
                self.state.total_kept / total_decided * 100
                if total_decided > 0 else 0
            ),
        }

    def get_evolution_state(self) -> Optional['EvolutionState']:
        """현재 진화 상태 반환 (대시보드 호환)"""
        return self.state

    async def evaluate_changes(self) -> Dict:
        """변경 효과 평가 (스케줄러 호환)"""
        if not self.state.active_change:
            return {}
        review = self.reviewer.review_period(7)
        result = self._evaluate_active_change(review)
        if result == "rollback":
            self._rollback_active_change()
            self._save_state()
            return {"effectiveness": "poor", "should_rollback": True}
        elif result == "keep":
            self._finalize_active_change(review)
            self._save_state()
            return {"effectiveness": "good", "should_rollback": False}
        return {"effectiveness": "pending", "should_rollback": False}

    async def rollback_last_change(self) -> bool:
        """마지막 변경 롤백"""
        if not self.state.active_change:
            logger.warning("[진화] 롤백할 활성 변경 없음")
            return False
        self._rollback_active_change()
        self._save_state()
        return True

    async def manual_adjust(
        self,
        strategy: str,
        parameter: str,
        new_value: Any,
        reason: str = "수동 조정",
    ) -> bool:
        """수동 파라미터 조정"""
        current = self._get_param_value(strategy, parameter)
        review = self.reviewer.review_period(7)

        change_dict = {
            "strategy": strategy,
            "parameter": parameter,
            "old_value": current,
            "new_value": new_value,
            "reason": reason,
            "source": "manual",
        }
        self._apply_change(change_dict, review)
        self._save_state()
        return True

    # ============================================================
    # 주간 전략 예산 리밸런싱
    # ============================================================
    _VALID_STRATEGIES = {
        "momentum_breakout", "sepa_trend", "rsi2_reversal",
        "theme_chasing", "gap_and_go", "strategic_swing",
    }
    _ALLOC_MIN_PCT = 5.0       # 최소 5% (테스트 기회 보장, 활성 전략만)
    _ALLOC_MAX_PCT = 60.0      # 최대 60%
    _ALLOC_MAX_CHANGE = 10.0   # 주당 ±10%p (과격 변동 방지)
    _ALLOC_MAX_TOTAL = 100.0   # 합계 상한 (100% 초과 금지)

    async def rebalance_strategy_allocation(self) -> Dict[str, Any]:
        """
        주간 전략 예산 리밸런싱

        Returns:
            {"status": "applied|skipped|error", "before": {...}, "after": {...},
             "reasoning": str}
        """
        logger.info("[리밸런싱] 주간 전략 예산 리밸런싱 시작")

        # DB에서 거래 기록 보강 (JSON 누락분 복구)
        # 2026-05-03: 90일 시계열 review 추가 → DB sync 범위도 90일로 확장
        try:
            await self.journal.sync_from_db(days=90)
        except Exception as e:
            logger.warning(f"[리밸런싱] DB 동기화 실패 (JSON 폴백): {e}")

        # 1. 현재 배분 조회
        config_mgr = get_evolved_config_manager()
        overrides = config_mgr.get_overrides()
        risk_alloc = (overrides.get("risk_config", {})
                      .get("strategy_allocation", None))
        if risk_alloc is None:
            # 기본값 사용
            from ..types import RiskConfig
            risk_alloc = dict(RiskConfig().strategy_allocation)
        current = {k: float(v) for k, v in risk_alloc.items()}
        logger.info(f"[리밸런싱] 현재 배분: {current}")

        # 2. 다중 시계열 전략별 성과 (1주 + 30일 + 90일 누적)
        # 2026-04-25: 1주 표본이 작으면 노이즈 위험 — 누적(30일) 함께 분석.
        # 2026-05-03: 90일 누적 추가 — 단일 시점 표본 과적합 방지 (rsi2 4월 7건 vs 누적 10건/60% 모순 사례)
        review_7d = self.reviewer.review_period(7)
        review_30d = self.reviewer.review_period(30)
        review_90d = self.reviewer.review_period(90)
        if review_7d.total_trades < 3 and review_30d.total_trades < 5:
            logger.info(
                f"[리밸런싱] 거래 부족 (1주 {review_7d.total_trades}건 < 3 AND "
                f"30일 {review_30d.total_trades}건 < 5), 스킵"
            )
            return {
                "status": "skipped",
                "reason": f"거래 부족 (1주 {review_7d.total_trades}건, 30일 {review_30d.total_trades}건)",
            }
        # primary review (LLM 신뢰도/메트릭 노출용)
        review = review_7d if review_7d.total_trades >= 3 else review_30d

        # 3. LLM 호출
        try:
            from ...utils.llm import get_llm_manager, LLMTask

            llm = get_llm_manager()
            perf_summary_7d = self._build_perf_summary(review_7d)
            perf_summary_30d = self._build_perf_summary(review_30d)
            perf_summary_90d = self._build_perf_summary(review_90d)

            system_prompt = (
                "당신은 한국 주식 단기매매 봇의 자본 배분 전략가입니다.\n"
                "세 시계열(지난 1주, 누적 30일, 누적 90일) 전략별 성과를 함께 보고, "
                "각 전략의 총예산 비중(%)을 조정하세요.\n\n"
                "분석 원칙 (시계열 우선순위):\n"
                "- **누적 90일이 가장 신뢰**: 표본이 가장 크고 시장 체제 변동을 흡수\n"
                "- **30일은 최근 추세 단서**: 90일 대비 큰 차이 시 체제 전환 의심\n"
                "- **1주는 노이즈 가능성**: 단독 판단 자제, 추세 단서로만 활용\n"
                "- 시계열 간 상충 시: **표본이 큰 쪽(90일 > 30일 > 1주) 우선**\n"
                "- 30일 vs 90일 부호 일치 + 1주 부진 → 일시적 변동 가능성, 급감액 자제\n"
                "- 30일 vs 90일 부호 불일치 → 체제 전환 가능성, 보수적 조정\n"
                "- 90일 부진하나 30일 호전 → 회복 단서지만 신뢰도 낮음, 완만 증액\n\n"
                "지표 우선순위:\n"
                "- 수익성: 승률 × 평균수익률이 높은 전략에 더 많은 자본 배분\n"
                "- 안정성: 연속 손실이 적은 전략 선호\n"
                "- 거래빈도: 거래 기회가 충분한 전략에 배분\n"
                "- 점진적 변화: 급격한 변경은 위험, 주당 ±10%p 이내\n\n"
                "제약:\n"
                "- 각 전략: 최소 5%, 최대 60%\n"
                "- 합계: ≤ 100% (레버리지 금지)\n"
                "- 주당 변경: 각 전략 ±10%p 이내\n"
                "- 비활성 전략(momentum_breakout): 반드시 0%\n\n"
                "누적 교훈 활용 (Trade Wiki + 매도후 복기):\n"
                "- 통계만 보지 말고 **왜 이 전략이 부진/양호한지** 누적 교훈 컨텍스트 함께 판단\n"
                "- 직전 주 매도후 복기의 LLM 분석은 **단기 신호 보조**로만 활용 (90일 누적이 우선)\n"
                "- Wiki 교훈에 명시된 패턴(예: '특정 시간대 손실 다발')이 있으면 reasoning에 인용\n\n"
                "JSON 형식으로 응답:\n"
                '{ "allocations": {"momentum_breakout": 60, ...}, '
                '"reasoning": "분석 사유 (1주/30일/90일 시계열 비교 + 누적 교훈 인용 명시)", '
                '"confidence": 0.7 }'
            )

            user_prompt = (
                f"현재 배분: {json.dumps(current, ensure_ascii=False)}\n\n"
                f"=== 지난 1주 성과 ===\n{perf_summary_7d}\n"
                f"  요약: 총 {review_7d.total_trades}건, "
                f"승률 {review_7d.win_rate:.1f}%, "
                f"손익비 {review_7d.profit_factor:.2f}, "
                f"총손익 {review_7d.total_pnl:,.0f}원\n\n"
                f"=== 누적 30일 성과 ===\n{perf_summary_30d}\n"
                f"  요약: 총 {review_30d.total_trades}건, "
                f"승률 {review_30d.win_rate:.1f}%, "
                f"손익비 {review_30d.profit_factor:.2f}, "
                f"총손익 {review_30d.total_pnl:,.0f}원\n\n"
                f"=== 누적 90일 성과 (가장 신뢰) ===\n{perf_summary_90d}\n"
                f"  요약: 총 {review_90d.total_trades}건, "
                f"승률 {review_90d.win_rate:.1f}%, "
                f"손익비 {review_90d.profit_factor:.2f}, "
                f"총손익 {review_90d.total_pnl:,.0f}원"
            )

            # Phase 1: Wiki 축적 교훈 컨텍스트 주입 (2026-05-03)
            wiki_ctx = self._build_wiki_context(list(current.keys()))
            if wiki_ctx:
                user_prompt += f"\n\n=== 📚 누적 교훈 (Trade Wiki + 직전 주 매도후 복기) ===\n{wiki_ctx}"

            result = await llm.complete_json(
                prompt=user_prompt,
                task=LLMTask.STRATEGY_ANALYSIS,
                system=system_prompt,
            )
        except Exception as e:
            logger.error(f"[리밸런싱] LLM 호출 실패: {e}")
            return {"status": "error", "reason": str(e)}

        # 4. LLM 결과 파싱
        proposed = result.get("allocations")
        reasoning = result.get("reasoning", "")
        confidence = result.get("confidence", 0.0)

        if not proposed or not isinstance(proposed, dict):
            logger.warning(f"[리밸런싱] LLM 응답 형식 오류: {result}")
            return {"status": "error", "reason": "LLM 응답 형식 오류"}

        if confidence < 0.4:
            logger.info(f"[리밸런싱] 신뢰도 낮음 ({confidence:.2f} < 0.4), 스킵")
            return {"status": "skipped", "reason": f"신뢰도 낮음 ({confidence:.2f})"}

        # 5. 가드레일 적용
        adjusted = self._apply_allocation_guardrails(current, proposed)
        logger.info(f"[리밸런싱] 조정 결과: {adjusted} (사유: {reasoning})")

        # 변경이 없으면 스킵
        if all(abs(adjusted.get(k, 0) - current.get(k, 0)) < 0.5
               for k in set(list(adjusted.keys()) + list(current.keys()))):
            logger.info("[리밸런싱] 유의미한 변경 없음, 스킵")
            return {"status": "skipped", "reason": "유의미한 변경 없음"}

        # 6. 영속화
        try:
            config_mgr.save_override(
                "risk_config", "strategy_allocation", adjusted, "weekly_rebalance"
            )
        except Exception as e:
            logger.error(f"[리밸런싱] 영속화 실패: {e}")

        # 7. 런타임 반영
        risk_config = self._get_component_config("risk_config")
        if risk_config and hasattr(risk_config, "strategy_allocation"):
            risk_config.strategy_allocation = adjusted

        # 8. 이력 저장
        self._save_rebalance_history(current, adjusted, reasoning)

        logger.info("[리밸런싱] 전략 예산 리밸런싱 완료")
        return {
            "status": "applied",
            "before": current,
            "after": adjusted,
            "reasoning": reasoning,
            "confidence": confidence,
        }

    def _build_wiki_context(self, strategies: List[str]) -> str:
        """Trade Wiki 축적 교훈 + 직전 주 매도후 복기 추출 (2026-05-03 Phase 1)

        파일 직접 읽기 (LLM/instance 의존 없음, 안전).

        Args:
            strategies: 활성 전략명 리스트

        Returns:
            마크다운 컨텍스트 (~3-5KB), 빈 문자열이면 wiki 미사용
        """
        from pathlib import Path
        wiki_dir = Path.home() / ".cache" / "ai_trader" / "wiki"
        if not wiki_dir.exists():
            return ""

        parts: List[str] = []

        # 1. 전략별 위키 페이지 핵심 추출 (각 ~300자)
        strat_dir = wiki_dir / "strategies"
        if strat_dir.exists():
            parts.append("**전략별 누적 교훈:**")
            for strat in strategies:
                if not strat or strat == "momentum_breakout":
                    continue
                page = strat_dir / f"{strat}.md"
                if not page.exists():
                    continue
                try:
                    content = page.read_text(encoding="utf-8")
                    # 교훈 섹션 추출 (## 교훈 ~ 다음 ## 까지)
                    # 2026-05-04 토큰 cap 완화: 600 → 1200자
                    lessons_section = ""
                    if "## 교훈" in content:
                        after = content.split("## 교훈", 1)[1]
                        next_h2 = after.find("\n## ")
                        lessons_section = after[:next_h2] if next_h2 > 0 else after
                        lessons_section = lessons_section.strip()[:1200]
                    if lessons_section:
                        parts.append(f"\n[{strat}]")
                        parts.append(lessons_section)
                except Exception as _e:
                    logger.debug(f"[리밸런싱] wiki {strat}.md 읽기 실패: {_e}")

        # 2-1. 직전 주 모니터링 자동 검증 결과 (Phase 2 통합, 2026-05-03)
        # 파일명 기반 정렬 (YYYY-WNN.md) — mtime은 백업 복원 시 흐트러짐
        monitoring_dir = wiki_dir / "monitoring"
        if monitoring_dir.exists():
            mon_files = sorted(
                monitoring_dir.glob("*.md"),
                key=lambda p: p.name,
                reverse=True,
            )
            if mon_files:
                try:
                    latest_mon = mon_files[0]
                    # 2026-05-04 토큰 cap 완화: 1200 → 2500자
                    mon_content = latest_mon.read_text(encoding="utf-8")[:2500]
                    parts.append(f"\n**직전 주 모니터링 검증 ({latest_mon.name}):**")
                    parts.append(mon_content)
                except Exception as _e:
                    logger.debug(f"[리밸런싱] monitoring 읽기 실패: {_e}")

        # 2-2. 직전 주 매도후 복기 (LLM 분석 섹션)
        post_exit_files = sorted(
            wiki_dir.glob("weekly_post_exit_*.md"),
            key=lambda p: p.stat().st_mtime,
            reverse=True,
        )
        if post_exit_files:
            try:
                latest = post_exit_files[0]
                content = latest.read_text(encoding="utf-8")
                # LLM 분석 섹션 추출 (## LLM 분석 ~ 다음 ## 까지)
                llm_section = ""
                for marker in ("## LLM 분석", "## LLM"):
                    if marker in content:
                        after = content.split(marker, 1)[1]
                        next_h2 = after.find("\n## ")
                        llm_section = after[:next_h2] if next_h2 > 0 else after
                        break
                # LLM 섹션이 없으면 전체 3000자
                # 2026-05-04 토큰 cap 완화: 1500 → 3000자
                if not llm_section:
                    llm_section = content[:3000]
                else:
                    llm_section = llm_section.strip()[:3000]
                if llm_section:
                    parts.append(f"\n**직전 주 매도후 복기 ({latest.name}):**")
                    parts.append(llm_section)
            except Exception as _e:
                logger.debug(f"[리밸런싱] weekly_post_exit 읽기 실패: {_e}")

        result = "\n".join(parts)
        # 토큰 예산: 최대 12KB (2026-05-04 사용자 요청 2~3배 완화)
        return result[:12000] if result else ""

    def _build_perf_summary(self, review: ReviewResult) -> str:
        """전략별 성과 요약 텍스트 생성"""
        lines = []
        for strat, perf in review.strategy_performance.items():
            trades = perf.get("trades", 0)
            wr = perf.get("win_rate", 0)
            pnl = perf.get("total_pnl", 0)
            avg = perf.get("avg_pnl_pct", 0)
            lines.append(
                f"- {strat}: {trades}건, 승률 {wr:.1f}%, "
                f"평균수익률 {avg:.2f}%, 총손익 {pnl:,.0f}원"
            )
        return "\n".join(lines) if lines else "전략별 성과 데이터 없음"

    def _apply_allocation_guardrails(
        self, current: Dict[str, float], proposed: Dict[str, float]
    ) -> Dict[str, float]:
        """가드레일 적용: min/max/change/total 제한

        비활성 전략(enabled=false)은 0% 강제 — 예산 낭비 방지.
        합계 재검증 루프로 100% 초과 방지.
        """
        adjusted: Dict[str, float] = {}

        # 비활성 전략 탐지: evolved_overrides에서 enabled=false인 전략
        _disabled = set()
        if hasattr(self, '_config') and self._config:
            for strat in self._VALID_STRATEGIES:
                strat_cfg = self._config.get(strat) or {}
                if isinstance(strat_cfg, dict) and strat_cfg.get("enabled") is False:
                    _disabled.add(strat)
        # 폴백: engine.py에서 비활성 하드코딩된 전략
        _disabled.add("momentum_breakout")  # 03-04 대참사 이후 영구 비활성

        # 현재 키 + 제안 키 합집합 (유효 전략만)
        all_keys = (set(current.keys()) | set(proposed.keys())) & self._VALID_STRATEGIES

        for key in all_keys:
            # 비활성 전략: 예산 0% 강제
            if key in _disabled:
                adjusted[key] = 0.0
                continue

            old = current.get(key, 0.0)
            new = float(proposed.get(key, old))

            # min/max 클램프
            new = max(self._ALLOC_MIN_PCT, min(self._ALLOC_MAX_PCT, new))

            # 주당 변경 제한
            delta = new - old
            if abs(delta) > self._ALLOC_MAX_CHANGE:
                new = old + (self._ALLOC_MAX_CHANGE if delta > 0 else -self._ALLOC_MAX_CHANGE)

            adjusted[key] = round(new, 1)

        # 진화 비대상 전략 보존 (core_holding 등 — 수동 관리 전략)
        for key in current:
            if key not in self._VALID_STRATEGIES and key not in adjusted:
                adjusted[key] = current[key]

        # 합계 상한 체크 + 재검증 루프 (최대 3회)
        non_valid_total = sum(v for k, v in adjusted.items() if k not in self._VALID_STRATEGIES)
        valid_cap = self._ALLOC_MAX_TOTAL - non_valid_total
        for _ in range(3):
            valid_total = sum(v for k, v in adjusted.items() if k in self._VALID_STRATEGIES)
            if valid_cap > 0 and valid_total > valid_cap:
                ratio = valid_cap / valid_total
                for k in list(adjusted.keys()):
                    if k in self._VALID_STRATEGIES and k not in _disabled:
                        adjusted[k] = round(adjusted[k] * ratio, 1)
                # 축소 후 최소값 보장 (비활성 제외)
                for k in adjusted:
                    if k in self._VALID_STRATEGIES and k not in _disabled:
                        adjusted[k] = max(self._ALLOC_MIN_PCT, adjusted[k])
            else:
                break

        return adjusted

    def _save_rebalance_history(
        self, before: Dict[str, float], after: Dict[str, float], reasoning: str
    ):
        """리밸런싱 이력 저장 (최근 52주 보관)"""
        history_path = Path(os.path.expanduser(
            "~/.cache/ai_trader/evolution/rebalance_history.json"
        ))
        history_path.parent.mkdir(parents=True, exist_ok=True)

        entries = []
        if history_path.exists():
            try:
                entries = json.loads(history_path.read_text(encoding="utf-8"))
            except Exception:
                entries = []

        entries.append({
            "timestamp": datetime.now().isoformat(),
            "before": before,
            "after": after,
            "reasoning": reasoning,
        })

        # 최근 52주만 보관
        entries = entries[-52:]

        try:
            history_path.write_text(
                json.dumps(entries, ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
        except Exception as e:
            logger.warning(f"[리밸런싱] 이력 저장 실패: {e}")


# 싱글톤
_strategy_evolver: Optional[StrategyEvolver] = None


def get_strategy_evolver() -> StrategyEvolver:
    """StrategyEvolver 인스턴스 반환"""
    global _strategy_evolver
    if _strategy_evolver is None:
        _strategy_evolver = StrategyEvolver()
    return _strategy_evolver
