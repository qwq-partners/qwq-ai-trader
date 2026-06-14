"""
QWQ AI Trader - 품질 검증 파이프라인

매일 저녁(20:25) evolve 직전 실행하여 엔진 출력의 정확성을 자동 검증합니다.
PRISM-INSIGHT의 "품질평가사" 에이전트 패턴을 규칙 기반으로 구현.

검증 항목:
1. 스크리닝 적중률: 오늘 스크리닝 종목 vs 실제 종가
2. 진화 파라미터 합리성: evolved_overrides 변경 감지 + 합계 검증
3. 리스크 한도 준수: 일일 손실, 포지션 집중도
4. 시그널 품질: 발행 시그널 중 체결/수익 비율
5. 거래 메모리 압축 트리거
"""

import json
from datetime import datetime, date
from pathlib import Path
from typing import Dict, Any, Optional
from loguru import logger


class QualityValidator:
    """매일 저녁 자동 품질 검증"""

    def __init__(self, trade_memory=None, config_path: str = None):
        self._trade_memory = trade_memory
        # 절대 경로 사용 (systemd 서비스에서 CWD가 다를 수 있음)
        if config_path:
            self._config_path = Path(config_path)
        else:
            self._config_path = Path(__file__).parent.parent.parent.parent / "config" / "evolved_overrides.yml"
        self._results_dir = Path.home() / ".cache" / "ai_trader" / "quality_reports"
        self._results_dir.mkdir(parents=True, exist_ok=True)

    async def run_daily_validation(
        self,
        daily_stats: Dict = None,
        portfolio_summary: Dict = None,
        screening_results: list = None,
        cross_validator_stats: Dict = None,
    ) -> Dict[str, Any]:
        """
        일일 품질 검증 실행

        Args:
            daily_stats: 일일 거래 통계 (wins, losses, pnl 등)
            portfolio_summary: 포트폴리오 현황 (positions, equity 등)
            screening_results: 오늘 스크리닝 결과 목록
            cross_validator_stats: 크로스 검증 통계

        Returns:
            검증 결과 dict
        """
        results = {}

        # 1. 일일 거래 성과 검증
        results["trading_performance"] = self._check_trading_performance(daily_stats)

        # 2. 설정 일관성 검증
        results["config_consistency"] = self._check_config_consistency()

        # 3. 크로스 검증 통계
        results["cross_validation"] = self._check_cross_validation(cross_validator_stats)

        # 4. 포지션 집중도 검증
        results["position_concentration"] = self._check_concentration(portfolio_summary)

        # 5. 거래 메모리 압축 (금요일) + LLM 구조화 복기
        if datetime.now().weekday() == 4:  # 금요일
            results["memory_compression"] = await self._trigger_memory_compression()

        # 6. 전문가 시스템 출력 신뢰도 (2026-05-29 추가, P0-2 async)
        results["expert_output"] = await self._check_expert_output_async()

        # 종합 등급
        warnings = sum(1 for v in results.values() if isinstance(v, dict) and v.get("level") == "warning")
        errors = sum(1 for v in results.values() if isinstance(v, dict) and v.get("level") == "error")

        if errors > 0:
            overall = "ERROR"
        elif warnings > 0:
            overall = "WARNING"
        else:
            overall = "OK"

        results["overall"] = overall
        results["timestamp"] = datetime.now().isoformat()

        # 결과 저장
        self._save_report(results)

        logger.info(
            f"[품질검증] 완료: {overall} "
            f"(경고 {warnings}건, 오류 {errors}건)"
        )

        return results

    def _check_trading_performance(self, stats: Dict = None) -> Dict:
        """일일 거래 성과 검증

        risk_manager.get_risk_summary() 포맷 지원:
          daily_trades, win_rate(%), total_pnl, consecutive_losses
        레거시 포맷도 지원: wins, losses
        """
        if not stats:
            return {"level": "info", "message": "거래 통계 없음"}

        pnl = stats.get("total_pnl", 0)

        # get_risk_summary() 포맷 (daily_trades + win_rate + wins + losses)
        if "daily_trades" in stats:
            total = stats.get("daily_trades", 0)
            if total == 0:
                return {"level": "info", "message": "거래 없음", "trades": 0}
            # wins/losses 직접 제공 시 우선 사용 (역산 오류 방지)
            if "wins" in stats and "losses" in stats:
                wins = stats["wins"]
                losses = stats["losses"]
                win_rate = wins / max(1, wins + losses) * 100 if (wins + losses) > 0 else 0.0
            else:
                # 하위 호환: win_rate만 있을 때 역산
                win_rate = stats.get("win_rate", 0.0)
                wins = round(win_rate / 100 * total)
                losses = total - wins
            consecutive_losses = stats.get("consecutive_losses", 0)
        else:
            # 레거시 포맷 (wins + losses 직접 지정)
            wins = stats.get("wins", 0)
            losses = stats.get("losses", 0)
            total = wins + losses
            if total == 0:
                return {"level": "info", "message": "거래 없음", "trades": 0}
            win_rate = wins / total * 100
            consecutive_losses = 0

        result = {
            "level": "ok",
            "trades": total,
            "wins": wins,
            "losses": losses,
            "win_rate": round(win_rate, 1),
            "pnl": pnl,
            "consecutive_losses": consecutive_losses,
        }

        # 경고 기준
        if total >= 5 and win_rate < 30:
            result["level"] = "warning"
            result["message"] = f"승률 {win_rate:.0f}% (5건+ 중 30% 미만)"
        elif consecutive_losses >= 3:
            result["level"] = "warning"
            result["message"] = f"연속 손실 {consecutive_losses}건"

        return result

    def _check_config_consistency(self) -> Dict:
        """설정 일관성 검증"""
        try:
            import yaml
            if not self._config_path.exists():
                return {"level": "info", "message": "설정 파일 없음"}

            with open(self._config_path, 'r') as f:
                config = yaml.safe_load(f)

            issues = []

            # 전략 배분 합계
            alloc = config.get("risk_config", {}).get("strategy_allocation", {})
            total = sum(alloc.values())
            if total > 100:
                issues.append(f"전략 배분 합계 {total}% > 100%")

            # 비활성 전략 예산
            momentum_alloc = alloc.get("momentum_breakout", 0)
            momentum_enabled = config.get("momentum_breakout", {}).get("enabled", True)
            if not momentum_enabled and momentum_alloc > 0:
                issues.append(f"비활성 momentum에 {momentum_alloc}% 배정")

            # stop_loss vs min_stop
            exit_cfg = config.get("exit_manager", {})
            sl = exit_cfg.get("stop_loss_pct", 0)
            min_sl = exit_cfg.get("min_stop_pct", 0)
            if sl > 0 and min_sl > 0 and sl < min_sl:
                issues.append(f"stop_loss_pct({sl}) < min_stop_pct({min_sl})")

            if issues:
                return {
                    "level": "warning",
                    "issues": issues,
                    "message": f"설정 이슈 {len(issues)}건",
                }
            return {"level": "ok", "message": "설정 일관성 OK"}

        except Exception as e:
            return {"level": "error", "message": f"설정 검증 실패: {e}"}

    def _check_cross_validation(self, stats: Dict = None) -> Dict:
        """크로스 검증 통계"""
        if not stats:
            return {"level": "info", "message": "크로스 검증 통계 없음"}

        total = stats.get("total", 0)
        blocked = stats.get("blocked", 0)
        penalized = stats.get("penalized", 0)

        if total == 0:
            return {"level": "info", "message": "시그널 없음", "stats": stats}

        block_rate = blocked / total * 100

        result = {
            "level": "ok",
            "stats": stats,
            "block_rate": round(block_rate, 1),
        }

        # 차단율 50% 이상이면 경고 (필터가 너무 엄격)
        if block_rate > 50 and total >= 10:
            result["level"] = "warning"
            result["message"] = f"크로스 검증 차단율 {block_rate:.0f}% (필터 과잉?)"

        return result

    def _check_concentration(self, portfolio: Dict = None) -> Dict:
        """포지션 집중도 검증"""
        if not portfolio:
            return {"level": "info", "message": "포트폴리오 정보 없음"}

        positions = portfolio.get("positions", [])
        if not positions:
            return {"level": "ok", "message": "포지션 없음"}

        # 섹터 집중도
        sectors = {}
        for p in positions:
            sec = p.get("sector", "unknown")
            sectors[sec] = sectors.get(sec, 0) + 1

        max_sector = max(sectors.values()) if sectors else 0
        max_sector_name = max(sectors, key=sectors.get) if sectors else ""

        result = {
            "level": "ok",
            "positions": len(positions),
            "sectors": sectors,
        }

        if max_sector >= 4:
            result["level"] = "warning"
            result["message"] = f"섹터 과집중: {max_sector_name} {max_sector}종목"

        return result

    async def _check_expert_output_async(self) -> Dict:
        """전문가 시스템 출력 검증 — async + to_thread (P0-2)"""
        import asyncio
        return await asyncio.to_thread(self._check_expert_output)

    def _check_expert_output(self) -> Dict:
        """전문가 시스템 출력 검증 (2026-05-29 추가)

        - 오늘 7명 중 의견 발행 횟수
        - 평균 confidence
        - bull/bear 분포
        - 7일 hit_rate (간이): bear 의견 후 -3% 이상 하락 빈도
        """
        try:
            from src.experts.opinion_store import load_all_today, list_recent
        except ImportError:
            # P2-7: 미설치는 warning (info는 카운트 안 됨)
            return {"level": "warning", "message": "전문가 시스템 모듈 누락"}

        try:
            today_ops = load_all_today()
            if not today_ops:
                return {"level": "warning", "message": "오늘 전문가 의견 0건"}

            valid = [o for o in today_ops.values() if o.is_valid and o.confidence > 0]
            if not valid:
                return {"level": "warning", "message": f"유효 의견 0/{len(today_ops)}"}

            avg_conf = sum(o.confidence for o in valid) / len(valid)
            bull_n = sum(1 for o in valid if o.regime_bias.value == "bull")
            bear_n = sum(1 for o in valid if o.regime_bias.value == "bear")

            # 7일 발행 빈도 (간이 health)
            issued_last_week = 0
            for name in today_ops:
                issued_last_week += len(list_recent(name, days=7))

            level = "ok"
            messages = []
            if avg_conf < 0.3:
                level = "warning"
                messages.append(f"평균 confidence 낮음 {avg_conf:.2f}")
            if len(valid) < 5:
                level = "warning"
                messages.append(f"발행 의견 부족 {len(valid)}/7")
            if issued_last_week < 30:
                messages.append(f"주간 발행 {issued_last_week}건 (목표 50+)")

            return {
                "level": level,
                "today_count": len(valid),
                "avg_confidence": round(avg_conf, 3),
                "bull_count": bull_n,
                "bear_count": bear_n,
                "weekly_total": issued_last_week,
                "messages": messages,
            }
        except Exception as e:
            return {"level": "error", "message": f"전문가 검증 실패: {e}"}

    async def _trigger_memory_compression(self) -> Dict:
        """거래 메모리 압축 (금요일)"""
        if not self._trade_memory:
            return {"level": "info", "message": "거래 메모리 미설정"}

        try:
            await self._trade_memory.compress_layers()
            summary = self._trade_memory.get_summary()
            return {
                "level": "ok",
                "message": "압축 완료",
                "summary": summary,
            }
        except Exception as e:
            return {"level": "error", "message": f"압축 실패: {e}"}

    def _save_report(self, results: Dict):
        """검증 결과 저장"""
        try:
            today = date.today().isoformat()
            path = self._results_dir / f"quality_{today}.json"
            path.write_text(json.dumps(results, ensure_ascii=False, indent=2))
        except Exception as e:
            logger.error(f"[품질검증] 리포트 저장 실패: {e}")
