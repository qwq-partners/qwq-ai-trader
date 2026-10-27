"""
AI Trading Bot v2 - 일일 거래 리뷰어 (Daily Reviewer)

대시보드용 일일 거래 리포트와 LLM 종합 평가를 생성합니다.
- review_YYYYMMDD.json: 거래 통계 리포트 (17:00)
- llm_review_YYYYMMDD.json: LLM 종합 평가 (20:30)
"""

import asyncio
import html
import json
import math
import os
from collections import Counter
from datetime import datetime, date
from decimal import Decimal
from pathlib import Path
from typing import Dict, List, Optional, Any

from loguru import logger

from .trade_journal import TradeJournal, TradeRecord, get_trade_journal
from ...utils.llm import LLMManager, LLMTask, get_llm_manager
from ...utils.telegram import record_report


# LLM 시스템 프롬프트
_REVIEW_SYSTEM_PROMPT = """당신은 경험 많은 퀀트 트레이더이자 전략 분석가입니다.
오늘 하루의 거래를 상세히 복기하고, 각 거래의 성공/실패 원인을 분석해주세요.

## 분석 원칙
1. 각 거래별로 진입 판단의 적절성을 평가
2. 청산 타이밍과 방법의 효율성 분석
3. 지표 활용의 적합성 검토
4. 반복되는 실수 패턴 식별
5. 성공 패턴 강화 방안 제시

## 목표
- 일평균 수익률 1% 달성
- 승률 55% 이상 + 손익비 1.5 이상

## 중요 규칙
- parameter_suggestions는 "현재 설정" 섹션에 명시된 파라미터를 확인한 후,
  이미 적용된 개선은 재제안하지 마세요.
- 예: max_atr_pct가 이미 6.0%로 설정되어 있다면 같은 값을 다시 제안하지 않습니다.
- 제안은 현재 설정에서 변경이 필요한 경우에만 포함하세요.

## 응답 형식
반드시 유효한 JSON 형식으로만 응답하세요. 마크다운 코드 블록이나 설명 없이 JSON만 출력하세요."""

# LLM 응답 JSON 스키마 (프롬프트에 포함)
_RESPONSE_SCHEMA = """{
  "assessment": "good 또는 fair 또는 poor",
  "confidence": 0.0~1.0,
  "daily_return_pct": -0.52,
  "trade_reviews": [
    {
      "symbol": "005930",
      "name": "삼성전자",
      "pnl_pct": 2.1,
      "review": "상세 복기 코멘트",
      "lesson": "교훈"
    }
  ],
  "insights": ["인사이트1", "인사이트2"],
  "avoid_patterns": ["패턴1", "패턴2"],
  "focus_opportunities": ["기회1", "기회2"],
  "parameter_suggestions": [
    {
      "strategy": "momentum_breakout",
      "parameter": "min_score",
      "current_value": 65,
      "suggested_value": 70,
      "reason": "이유",
      "confidence": 0.8
    }
  ],
  "telegram_summary": "📊 <b>2/14 거래 리뷰</b>\\n\\n<b>■ 성과</b>\\n  승률 40% (2/5) | 손익 <b>-45,230원</b>\\n  거래률 -0.85% / 자본률 -0.18%\\n  PF 0.85\\n\\n<b>■ 인사이트</b>\\n  • 장초반 과열 진입 주의\\n  • SEPA 전략 유지"
}

## telegram_summary 작성 규칙 (필수)
- "거래률"은 요약 통계의 total_pnl_pct (각 거래의 진입 cost 기준 단순 합)
- "자본률"은 요약 통계의 capital_pnl_pct (전체 자본 기준)
- 두 값을 명확히 구분해 표시 — 단순 "수익률"이라 쓰면 사용자가 자본 수익률로 오해함
- 형식: "거래률 ±X.XX% / 자본률 ±Y.YYY%"
- 일평균 수익률 1% 목표는 **자본률 기준**임 (자본 25M의 1% = 25만원)"""


def _parse_date_str(date_str: Optional[str]) -> date:
    """날짜 문자열(YYYY-MM-DD)을 date 객체로 변환. None이면 오늘."""
    if date_str is None:
        return date.today()
    return datetime.strptime(date_str, "%Y-%m-%d").date()


def _date_to_file_suffix(d: date) -> str:
    """date 객체를 파일명용 YYYYMMDD 문자열로 변환."""
    return d.strftime("%Y%m%d")


def _finite_number(value: Any) -> Optional[float]:
    """유한한 숫자만 반환하고 결측·NaN·무한대는 구분한다."""
    if isinstance(value, bool) or value is None:
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) else None


def _nonnegative_count(value: Any) -> Optional[int]:
    number = _finite_number(value)
    if number is None or number < 0 or not number.is_integer():
        return None
    return int(number)


def _format_trade_for_prompt(trade: TradeRecord) -> Dict[str, Any]:
    """TradeRecord를 LLM 프롬프트용 딕셔너리로 변환."""
    return {
        "symbol": trade.symbol,
        "name": trade.name,
        "strategy": trade.entry_strategy,
        "entry_time": trade.entry_time.strftime("%H:%M") if trade.entry_time else "",
        "exit_time": trade.exit_time.strftime("%H:%M") if trade.exit_time else "",
        "entry_price": trade.entry_price,
        "exit_price": trade.exit_price,
        "quantity": trade.entry_quantity,
        "pnl": trade.pnl,
        "pnl_pct": round(trade.pnl_pct, 2),
        "holding_minutes": trade.holding_minutes,
        "entry_reason": trade.entry_reason,
        "exit_reason": trade.exit_reason,
        "exit_type": trade.exit_type,
        "indicators_at_entry": trade.indicators_at_entry,
    }


class DailyReviewer:
    """
    일일 거래 리뷰어

    두 종류의 리포트를 생성합니다:
    1. 거래 통계 리포트 (review_YYYYMMDD.json) -- 17:00
    2. LLM 종합 평가 (llm_review_YYYYMMDD.json) -- 20:30
    """

    def __init__(
        self,
        storage_dir: Optional[str] = None,
        llm_manager: Optional[LLMManager] = None,
    ):
        self.storage_dir = Path(storage_dir or os.getenv(
            "TRADE_JOURNAL_DIR",
            os.path.expanduser("~/.cache/ai_trader/journal")
        ))
        self.storage_dir.mkdir(parents=True, exist_ok=True)
        self.llm = llm_manager or get_llm_manager()

        logger.info(f"[거래리뷰] DailyReviewer 초기화: {self.storage_dir}")

    # --- 파일 경로 ---

    def _review_path(self, d: date) -> Path:
        """거래 리포트 파일 경로."""
        return self.storage_dir / f"review_{_date_to_file_suffix(d)}.json"

    def _llm_review_path(self, d: date) -> Path:
        """LLM 종합 평가 파일 경로."""
        return self.storage_dir / f"llm_review_{_date_to_file_suffix(d)}.json"

    # --- DB 거래 조회 ---

    @staticmethod
    async def _load_trades_from_db(
        trade_journal: TradeJournal, target_date: date
    ) -> Optional[List[TradeRecord]]:
        """DB에서 해당 날짜 거래를 TradeRecord로 비동기 로드.

        DB pool이 없으면 None을 반환하여 캐시 폴백합니다.
        """
        pool = getattr(trade_journal, 'pool', None)
        if not pool:
            return None

        try:
            async with pool.acquire() as conn:
                rows = await conn.fetch("""
                    SELECT id, symbol, name, entry_time, entry_price, entry_quantity,
                           exit_time, exit_price, exit_quantity,
                           entry_reason, entry_strategy, entry_signal_score,
                           exit_reason, exit_type, pnl, pnl_pct, holding_minutes,
                           indicators_at_entry, indicators_at_exit,
                           market_context, theme_info
                    FROM trades
                    WHERE entry_time::date = $1 OR exit_time::date = $1
                    ORDER BY entry_time
                """, target_date)

            result = []
            for r in rows:
                rec = TradeRecord(
                    id=r['id'], symbol=r['symbol'], name=r['name'] or '',
                    entry_time=r['entry_time'],
                    entry_price=Decimal(str(r['entry_price'])),
                    entry_quantity=r['entry_quantity'],
                    entry_reason=r['entry_reason'] or '',
                    entry_strategy=r['entry_strategy'] or '',
                    entry_signal_score=Decimal(str(r['entry_signal_score'] or 0)),
                )
                if r['exit_time']:
                    rec.exit_time = r['exit_time']
                    rec.exit_price = Decimal(str(r['exit_price'] or 0))
                    rec.exit_quantity = r['exit_quantity'] or 0
                    rec.exit_reason = r['exit_reason'] or ''
                    rec.exit_type = r['exit_type'] or ''
                    # TradeRecord 내부에서 float 연산에 사용되므로 Decimal 유지하되
                    # 하위 호출에서 float += Decimal 에러를 방지
                    rec.pnl = Decimal(str(float(r['pnl'] or 0)))
                    rec.pnl_pct = Decimal(str(float(r['pnl_pct'] or 0)))
                    rec.holding_minutes = r['holding_minutes'] or 0
                # JSONB 필드 (asyncpg는 자동 파싱하므로 dict일 수 있음)
                for field_name in ('indicators_at_entry', 'indicators_at_exit',
                                   'market_context', 'theme_info'):
                    val = r[field_name]
                    if val and isinstance(val, str):
                        val = json.loads(val)
                    setattr(rec, field_name, val or {})
                result.append(rec)

            if result:
                logger.info(f"[거래리뷰] DB에서 {target_date} 거래 {len(result)}건 로드")
            return result
        except Exception as e:
            logger.warning(f"[거래리뷰] DB 거래 조회 실패, 캐시 폴백: {e}")
            return None

    # --- 거래 리포트 생성 ---

    async def generate_trade_report(
        self,
        trade_journal: TradeJournal,
        date_str: Optional[str] = None,
    ) -> Dict[str, Any]:
        """
        일일 거래 통계 리포트를 생성하고 JSON 파일로 저장한다.

        Args:
            trade_journal: 거래 저널 인스턴스
            date_str: 대상 날짜 (YYYY-MM-DD). None이면 오늘.

        Returns:
            리포트 딕셔너리
        """
        target_date = _parse_date_str(date_str)

        # DB 우선 조회 (캐시가 봇 재시작으로 비었을 수 있음)
        db_trades = await self._load_trades_from_db(trade_journal, target_date)
        if db_trades is not None:
            trades = db_trades
        else:
            trades = trade_journal.get_trades_by_date(target_date)
        # 부분 청산도 포함 (exit_time이 있으면 매도 이력 있음)
        closed_trades = [t for t in trades if t.exit_time is not None]

        # 동기화/복구 포지션 분리 (전략 의사결정 없는 정합성 이벤트)
        sync_trades = [t for t in closed_trades if t.is_sync]
        closed_trades = [t for t in closed_trades if not t.is_sync]

        logger.info(
            f"[거래리뷰] 거래 리포트 생성: {target_date} "
            f"(전체 {len(trades)}건, 청산 {len(closed_trades)}건, 동기화 {len(sync_trades)}건)"
        )

        # 개별 거래 정보
        trade_details = []
        for t in closed_trades:
            trade_details.append({
                "symbol": t.symbol,
                "name": t.name,
                "strategy": t.entry_strategy,
                "entry_time": t.entry_time.isoformat() if t.entry_time else None,
                "exit_time": t.exit_time.isoformat() if t.exit_time else None,
                "entry_price": float(t.entry_price),
                "exit_price": float(t.exit_price),
                "quantity": t.entry_quantity,
                "pnl": round(float(t.pnl)),
                "pnl_pct": round(float(t.pnl_pct), 2),
                "holding_minutes": t.holding_minutes,
                "entry_reason": t.entry_reason,
                "exit_reason": t.exit_reason,
                "exit_type": t.exit_type,
                "indicators_at_entry": t.indicators_at_entry,
            })

        # 요약 통계
        summary = self._calculate_summary(closed_trades)

        # 전략별 성과
        strategy_performance = self._calculate_strategy_performance(closed_trades)

        # 동기화 이벤트 기록 (통계에서 제외되지만 이력은 보존)
        sync_details = []
        for t in sync_trades:
            sync_details.append({
                "symbol": t.symbol,
                "name": t.name,
                "entry_reason": t.entry_reason,
                "pnl": round(float(t.pnl)),
                "pnl_pct": round(float(t.pnl_pct), 2),
            })

        report = {
            "date": target_date.isoformat(),
            "generated_at": datetime.now().isoformat(),
            "trades": trade_details,
            "sync_events": sync_details,
            "summary": summary,
            "strategy_performance": strategy_performance,
        }

        # 파일 저장
        try:
            file_path = self._review_path(target_date)
            with open(file_path, "w", encoding="utf-8") as f:
                json.dump(report, f, ensure_ascii=False, indent=2)
            logger.info(f"[거래리뷰] 거래 리포트 저장: {file_path}")
        except Exception as e:
            logger.error(f"[거래리뷰] 거래 리포트 저장 실패: {e}")

        return report

    def _get_capital_basis_info(self) -> tuple[float, str]:
        """자본 기준값과 출처를 함께 반환한다.

        이 값은 계좌 수익률이나 TWR이 아니라 일일 실현 순손익을 나누는 정적
        참고 분모다. 출처를 함께 보존해 폴백값을 실계좌 잔고로 오해하지 않게 한다.
        """
        # 2026-05-27 폴백 순서 정정: 실제 운용 total_equity 우선, INITIAL_CAPITAL env는 보조
        # 사고: INITIAL_CAPITAL=500000(기본값)으로 자본률 171% 과장 표시
        # 정정: 봇 실제 운용 자본(daily_stats.total_equity 또는 dashboard API)을 1차 사용
        try:
            stats_path = Path(os.path.expanduser("~/.cache/ai_trader/daily_stats.json"))
            if stats_path.exists():
                data = json.loads(stats_path.read_text(encoding="utf-8"))
                # peak_equity (실제 운용 자본 추적) 또는 total_equity
                eq = float(data.get("peak_equity") or data.get("total_equity") or 0)
                if eq > 1_000_000:
                    return eq, "daily_stats_peak_or_total_equity"
        except Exception:
            pass
        # 보조: dashboard API (live engine 값)
        try:
            import urllib.request
            with urllib.request.urlopen("http://localhost:8080/api/portfolio", timeout=2) as resp:
                pf = json.loads(resp.read())
                eq = float(pf.get("initial_capital") or pf.get("total_equity") or 0)
                if eq > 1_000_000:
                    return eq, "local_portfolio_snapshot"
        except Exception:
            pass
        # 보조: env (단 100만원 이상 합리적인 값만 신뢰)
        try:
            cap = float(os.getenv("INITIAL_CAPITAL", "0") or 0)
            if cap > 1_000_000:
                return cap, "initial_capital_config"
        except (TypeError, ValueError):
            pass
        # 최후 폴백
        return 25_000_000.0, "static_fallback"

    def _get_capital_basis(self) -> float:
        """기존 호출자 호환용 자본 기준값."""
        return self._get_capital_basis_info()[0]

    def _calculate_summary(self, trades: List[TradeRecord]) -> Dict[str, Any]:
        """청산 거래 목록에서 요약 통계를 계산한다."""
        if not trades:
            capital_basis, capital_source = self._get_capital_basis_info()
            return {
                "total_trades": 0,
                "wins": 0,
                "losses": 0,
                "win_rate": 0.0,
                "total_pnl": 0.0,
                "total_pnl_pct": 0.0,
                "capital_pnl_pct": 0.0,
                "capital_basis": capital_basis,
                "capital_basis_source": capital_source,
                "pnl_basis": "net_of_commission_and_sell_tax",
                "return_basis": "sum_of_trade_net_pnl_pct",
                "profit_factor": 0.0,
                "best_trade": None,
                "worst_trade": None,
            }

        wins = [t for t in trades if t.is_win]
        losses = [t for t in trades if not t.is_win]

        total_profit = float(sum(float(t.pnl) for t in wins))
        total_loss = abs(float(sum(float(t.pnl) for t in losses)))

        # 손실 0원 시 profit_factor 상한 99.9 (왜곡 방지)
        if total_loss > 0:
            profit_factor = min(total_profit / total_loss, 99.9)
        elif total_profit > 0:
            profit_factor = 99.9
        else:
            profit_factor = 0.0

        total_pnl = float(sum(float(t.pnl) for t in trades))
        # 거래 단위 수익률 합 (각 거래의 진입 cost 기준)
        total_pnl_pct = float(sum(float(t.pnl_pct) for t in trades))
        # 2026-05-14 추가: 자본 단위 수익률 (전체 자산 대비)
        capital_basis, capital_source = self._get_capital_basis_info()
        capital_pnl_pct = (total_pnl / capital_basis * 100) if capital_basis > 0 else 0.0

        best = max(trades, key=lambda t: float(t.pnl_pct))
        worst = min(trades, key=lambda t: float(t.pnl_pct))

        return {
            "total_trades": len(trades),
            "wins": len(wins),
            "losses": len(losses),
            "win_rate": round(len(wins) / len(trades) * 100, 1),
            "total_pnl": round(total_pnl),
            "total_pnl_pct": round(total_pnl_pct, 2),
            "capital_pnl_pct": round(capital_pnl_pct, 3),
            "capital_basis": round(capital_basis),
            "capital_basis_source": capital_source,
            "pnl_basis": "net_of_commission_and_sell_tax",
            "return_basis": "sum_of_trade_net_pnl_pct",
            "profit_factor": round(profit_factor, 2),
            "best_trade": {
                "symbol": best.symbol,
                "name": best.name,
                "pnl_pct": round(float(best.pnl_pct), 2),
                "pnl": round(float(best.pnl)),
            },
            "worst_trade": {
                "symbol": worst.symbol,
                "name": worst.name,
                "pnl_pct": round(float(worst.pnl_pct), 2),
                "pnl": round(float(worst.pnl)),
            },
        }

    def _calculate_strategy_performance(
        self,
        trades: List[TradeRecord],
    ) -> Dict[str, Dict[str, Any]]:
        """전략별 성과를 계산한다."""
        stats: Dict[str, Dict[str, Any]] = {}

        for trade in trades:
            strategy = trade.entry_strategy or "unknown"
            if strategy not in stats:
                stats[strategy] = {
                    "trades": 0,
                    "wins": 0,
                    "losses": 0,
                    "total_pnl": 0.0,
                    "total_pnl_pct": 0.0,
                }

            stats[strategy]["trades"] += 1
            if trade.is_win:
                stats[strategy]["wins"] += 1
            else:
                stats[strategy]["losses"] += 1
            stats[strategy]["total_pnl"] += float(trade.pnl)
            stats[strategy]["total_pnl_pct"] += float(trade.pnl_pct)

        # 평균/승률 계산
        for s in stats.values():
            count = s["trades"]
            s["avg_pnl_pct"] = round(s["total_pnl_pct"] / count, 2) if count > 0 else 0.0
            s["win_rate"] = round(s["wins"] / count * 100, 1) if count > 0 else 0.0
            s["total_pnl"] = round(s["total_pnl"], 0)
            s["total_pnl_pct"] = round(s["total_pnl_pct"], 2)

        return stats

    # --- LLM 종합 평가 ---

    async def generate_llm_review(
        self,
        trade_journal: TradeJournal,
        date_str: Optional[str] = None,
    ) -> Dict[str, Any]:
        """
        LLM을 사용한 종합 거래 평가를 생성하고 저장한다.

        기존 거래 리포트가 없으면 먼저 생성한 후 LLM에 전달한다.

        Args:
            trade_journal: 거래 저널 인스턴스
            date_str: 대상 날짜 (YYYY-MM-DD). None이면 오늘.

        Returns:
            LLM 평가 딕셔너리
        """
        target_date = _parse_date_str(date_str)
        date_str_formatted = target_date.isoformat()

        logger.info(f"[거래리뷰] LLM 종합 평가 시작: {date_str_formatted}")

        # 거래 리포트 로드 (없으면 생성)
        report = self.load_report(date_str_formatted)
        if report is None:
            report = await self.generate_trade_report(trade_journal, date_str_formatted)

        raw_trades = report.get("trades")
        trades = raw_trades if isinstance(raw_trades, list) else None
        summary = report.get("summary", {})
        strategy_performance = report.get("strategy_performance", {})

        # 거래가 없거나 건수 자체가 기록되지 않았으면 추측하지 않고 평가를 보류한다.
        source_total = len(trades) if trades is not None else _nonnegative_count(
            summary.get("total_trades")
        )
        if source_total in (None, 0):
            llm_review = self._create_empty_review(target_date)
            llm_review["telegram_summary"] = self._build_authoritative_telegram_summary(
                target_date, summary, trades
            )
            self._save_llm_review(target_date, llm_review)
            try:
                await record_report("daily_review", llm_review["telegram_summary"])
            except Exception as e:
                logger.warning(f"[거래리뷰] 거래 없음 요약 입력 저장 실패: {e}")
            logger.info("[거래리뷰] 청산 거래 없음 -- 평가 보류 리뷰 저장")
            return llm_review

        # LLM 프롬프트 구성
        prompt = self._build_llm_prompt(
            target_date, trades or [], summary, strategy_performance
        )

        # LLM 호출
        try:
            llm_response = await self.llm.complete(
                prompt,
                budget_priority="research",
                task=LLMTask.TRADE_REVIEW,
                system=_REVIEW_SYSTEM_PROMPT,
            )

            if not llm_response.success or not llm_response.content:
                raise ValueError(llm_response.error or "LLM 응답 없음")

            # JSON 파싱
            llm_review = self._parse_llm_response(llm_response.content, target_date)
            logger.info(
                f"[거래리뷰] LLM 평가 완료: "
                f"assessment={llm_review.get('assessment')}, "
                f"trade_reviews={len(llm_review.get('trade_reviews', []))}건"
            )

        except Exception as e:
            logger.error(f"[거래리뷰] LLM 평가 실패, 폴백 생성: {e}")
            llm_review = self._create_fallback_review(
                target_date, summary, trades or []
            )

        # 사용자 알림의 수치와 분모는 결정론적 거래 리포트만 사용한다. 모델이 만든
        # telegram_summary는 기계 피드백에 쓰이지 않으므로 감사용으로만 별도 보존한다.
        model_summary = llm_review.get("telegram_summary")
        if model_summary:
            llm_review["model_telegram_summary"] = model_summary
        llm_review["telegram_summary"] = self._build_authoritative_telegram_summary(
            target_date, summary, trades
        )

        # 파일 저장
        self._save_llm_review(target_date, llm_review)

        # 텔레그램 알림
        telegram_summary = llm_review.get("telegram_summary", "")
        if telegram_summary:
            try:
                await record_report("daily_review", telegram_summary)
                logger.info("[거래리뷰] 텔레그램 리뷰 요약 입력 저장 완료")
            except Exception as e:
                logger.warning(f"[거래리뷰] 보고 입력 저장 실패: {e}")

        # daily_bias 피드백 루프: 내일 운영에 반영할 바이어스 저장
        try:
            await self._save_daily_bias(llm_review, summary)
        except Exception as e:
            logger.warning(f"[거래리뷰] daily_bias 저장 실패 (무시): {e}")

        return llm_review

    @staticmethod
    def _build_authoritative_telegram_summary(
        target_date: date,
        summary: Dict[str, Any],
        trades: Optional[List[Dict[str, Any]]],
    ) -> str:
        """결정론적 통계만으로 사용자용 일일 복기 본문을 만든다.

        LLM 서술 대신 저장된 거래 원자료와 결정론적 summary 필드만 사용한다.
        결측값과 비유한 수치는 0으로 바꾸지 않고 미기록/미산출로 표시한다.
        """
        total = len(trades) if trades is not None else _nonnegative_count(
            summary.get("total_trades")
        )
        if total is None:
            return (
                f"📊 <b>{target_date:%-m/%-d} 거래 복기</b>\n\n"
                "<b>■ 관측 범위</b>\n"
                "  거래저널 청산 거래 건수 미기록\n"
                "  성과 평가 보류 — 거래 건수와 원자료 확인이 필요합니다."
            )
        if total == 0:
            return (
                f"📊 <b>{target_date:%-m/%-d} 거래 복기</b>\n\n"
                "<b>■ 관측 범위</b>\n"
                "  거래저널 기준 청산 거래 0건\n"
                "  성과 평가 보류 — 비교할 실현 거래가 없습니다."
            )

        source_trades = trades or []
        positive = negative = flat = unknown_sign = 0
        loss_exit_types: Counter[str] = Counter()
        for trade in source_trades:
            if not isinstance(trade, dict):
                unknown_sign += 1
                continue
            pnl = _finite_number(trade.get("pnl"))
            if pnl is None:
                pnl = _finite_number(trade.get("pnl_pct"))
            if pnl is None:
                unknown_sign += 1
            elif pnl > 0:
                positive += 1
            elif pnl < 0:
                negative += 1
                exit_type = str(trade.get("exit_type") or "").strip()
                if exit_type:
                    loss_exit_types[exit_type] += 1
            else:
                flat += 1

        if trades is not None:
            count_parts = [f"수익 {positive}", f"손실 {negative}"]
            if flat:
                count_parts.append(f"보합 {flat}")
            if unknown_sign:
                count_parts.append(f"손익 부호 미기록 {unknown_sign}건")
            count_line = f"  청산 기록 {total}건 · " + " / ".join(count_parts)
            recorded_loss_count = negative
        else:
            wins = _nonnegative_count(summary.get("wins"))
            losses = _nonnegative_count(summary.get("losses"))
            recorded_loss_count = losses
            recorded = []
            if wins is not None:
                recorded.append(f"수익 {wins}")
            if losses is not None:
                recorded.append(f"손실 {losses}")
            count_line = f"  청산 기록 {total}건"
            if recorded:
                count_line += " · " + " / ".join(recorded)
            else:
                count_line += " · 수익/손실 건수 미기록"

        total_pnl = _finite_number(summary.get("total_pnl"))
        fee_basis_known = summary.get("pnl_basis") == "net_of_commission_and_sell_tax"
        pnl_label = "순손익" if fee_basis_known else "기록 손익"
        if total_pnl is None:
            pnl_line = f"  {pnl_label} 미산출"
        else:
            basis = "수수료·매도세 반영" if fee_basis_known else "비용 반영 여부 미기록"
            pnl_line = f"  {pnl_label} <b>{total_pnl:+,.0f}원</b> ({basis})"

        trade_return_sum = _finite_number(summary.get("total_pnl_pct"))
        return_label = "순손익률" if fee_basis_known else "수익률"
        return_line = (
            f"  개별 거래 {return_label} 합계 {trade_return_sum:+.2f}%"
            if trade_return_sum is not None
            else f"  개별 거래 {return_label} 합계 미산출"
        )

        capital_ratio = _finite_number(summary.get("capital_pnl_pct"))
        capital_basis = _finite_number(summary.get("capital_basis"))
        capital_source = summary.get("capital_basis_source")
        capital_labels = {
            "daily_stats_peak_or_total_equity": "일일 자산 통계",
            "local_portfolio_snapshot": "로컬 포트폴리오 스냅샷",
            "initial_capital_config": "초기자본 설정",
        }
        if (
            capital_source in capital_labels
            and capital_ratio is not None
            and capital_basis is not None
            and capital_basis > 0
        ):
            capital_line = (
                f"  참고 실현손익/기준자본 {capital_ratio:+.3f}% "
                f"(분모 {capital_basis:,.0f}원 · {capital_labels[capital_source]})\n"
                "  ※ 계좌수익률/TWR 아님 · 입출금·미실현손익 미반영"
            )
        else:
            capital_line = (
                "  계좌 기준 수익 미산출 "
                "(입출금·미실현손익·신뢰 가능한 기준잔고 필요)"
            )

        pf = _finite_number(summary.get("profit_factor"))
        if recorded_loss_count is None or (
            recorded_loss_count == 0 and trades is not None and unknown_sign > 0
        ):
            pf_line = "  PF 손실 표본 판정 불가 — 비율 미산출"
        elif recorded_loss_count == 0:
            pf_line = "  PF 손실 표본 없음 — 비율 미산출"
        else:
            pf_line = f"  PF {pf:.2f}" if pf is not None else "  PF 미산출"

        def _format_summary_trade(label: str, value: Any) -> str:
            if not isinstance(value, dict):
                return f"  {label}: 미산출"
            name = str(value.get("name") or "").strip()
            symbol = str(value.get("symbol") or "").strip()
            identity = name or symbol
            if name and symbol:
                identity = f"{name}({symbol})"
            identity = html.escape(identity) if identity else "종목 미기록"
            pnl = _finite_number(value.get("pnl"))
            pnl_pct = _finite_number(value.get("pnl_pct"))
            metrics = []
            if pnl is not None:
                metrics.append(f"{pnl:+,.0f}원")
            if pnl_pct is not None:
                metrics.append(f"{pnl_pct:+.2f}%")
            suffix = " · ".join(metrics) if metrics else "손익 미기록"
            return f"  {label}: {identity} · {suffix}"

        best_line = _format_summary_trade(
            "개별 수익률 상단", summary.get("best_trade")
        )
        worst_line = _format_summary_trade(
            "개별 수익률 하단", summary.get("worst_trade")
        )

        if trades is None:
            next_check = (
                "  청산유형 원자료 미기록 · 동일 진입 기준 비교 청산을 위해 원자료 확인 필요"
            )
        elif negative == 0 and unknown_sign > 0:
            next_check = (
                f"  손익 부호 미기록 {unknown_sign}건 · 손실 여부 판정을 보류하고 "
                "손익·청산유형 원자료를 먼저 확인"
            )
        elif negative == 0:
            next_check = (
                "  손실 거래 없음 · 다음 거래도 동일 진입 기준 실제 청산과 "
                "1·3·5영업일 비교 청산을 함께 기록"
            )
        elif loss_exit_types:
            exit_type, exit_count = loss_exit_types.most_common(1)[0]
            covered = sum(loss_exit_types.values())
            missing_types = negative - covered
            next_check = (
                f"  손실 {negative}건 중 청산유형 기록 {covered}건"
                f"(미기록 {missing_types}건) · "
                f"최다 {html.escape(exit_type)} {exit_count}건\n"
                "  동일 진입 기준 실제 청산과 1·3·5영업일 비교 청산의 비용 반영 손익을 기록"
            )
        else:
            next_check = (
                f"  손실 {negative}건의 청산유형 미기록 · 유형을 보완한 뒤 동일 진입 기준 "
                "실제/비교 청산 손익을 기록"
            )

        return (
            f"📊 <b>{target_date:%-m/%-d} 거래 복기</b>\n\n"
            "<b>■ 거래저널 실현 성과</b>\n"
            f"{count_line}\n{pnl_line}\n{return_line}\n{capital_line}\n{pf_line}\n\n"
            "<b>■ 개별 거래 수익률 범위</b>\n"
            f"{best_line}\n{worst_line}\n\n"
            "<b>■ 다음 확인</b>\n"
            f"{next_check}"
        )

    def _build_llm_prompt(
        self,
        target_date: date,
        trades: List[Dict[str, Any]],
        summary: Dict[str, Any],
        strategy_performance: Dict[str, Dict[str, Any]],
    ) -> str:
        """LLM에 전달할 분석 프롬프트를 구성한다."""
        date_display = target_date.strftime("%Y-%m-%d (%a)")

        lines = [
            f"# {date_display} 거래 리뷰 요청",
            "",
            "## 요약 통계",
            f"- 총 거래: {summary.get('total_trades', 0)}건",
            f"- 승리: {summary.get('wins', 0)}건 / 패배: {summary.get('losses', 0)}건",
            f"- 승률: {summary.get('win_rate', 0):.1f}%",
            f"- 총 손익: {summary.get('total_pnl', 0):+,.0f}원",
            f"- 거래률(개별 진입 cost 기준): {summary.get('total_pnl_pct', 0):+.2f}%",
            f"- 자본률(자본 {summary.get('capital_basis', 0):,.0f}원 기준): {summary.get('capital_pnl_pct', 0):+.3f}%",
            f"- Profit Factor: {summary.get('profit_factor', 0):.2f}",
        ]

        # 최고/최악 거래
        best = summary.get("best_trade")
        worst = summary.get("worst_trade")
        if best:
            lines.append(f"- 최고 거래: {best['name']}({best['symbol']}) {best['pnl_pct']:+.2f}%")
        if worst:
            lines.append(f"- 최악 거래: {worst['name']}({worst['symbol']}) {worst['pnl_pct']:+.2f}%")

        # 전략별 성과
        if strategy_performance:
            lines.extend(["", "## 전략별 성과"])
            for strategy, perf in strategy_performance.items():
                lines.append(
                    f"- {strategy}: {perf['trades']}건, "
                    f"승률 {perf['win_rate']:.1f}%, "
                    f"평균 {perf['avg_pnl_pct']:+.2f}%, "
                    f"총손익 {perf['total_pnl']:+,.0f}원"
                )

        # 개별 거래 상세
        lines.extend(["", "## 개별 거래 상세"])
        for i, t in enumerate(trades, 1):
            lines.extend([
                f"",
                f"### 거래 {i}: {t.get('name', '')} ({t.get('symbol', '')})",
                f"- 전략: {t.get('strategy', '')}",
                f"- 진입: {t.get('entry_time', '')} @ {t.get('entry_price', 0):,.0f}원",
                f"- 청산: {t.get('exit_time', '')} @ {t.get('exit_price', 0):,.0f}원",
                f"- 수량: {t.get('quantity', 0)}주",
                f"- 손익: {t.get('pnl', 0):+,.0f}원 ({t.get('pnl_pct', 0):+.2f}%)",
                f"- 보유시간: {t.get('holding_minutes', 0)}분",
                f"- 진입사유: {t.get('entry_reason', '')}",
                f"- 청산사유: {t.get('exit_reason', '')}",
                f"- 청산유형: {t.get('exit_type', '')}",
            ])

            indicators = t.get("indicators_at_entry", {})
            if indicators:
                indicator_parts = [f"{k}={v}" for k, v in indicators.items() if isinstance(v, (int, float))]
                if indicator_parts:
                    lines.append(f"- 진입지표: {', '.join(indicator_parts[:8])}")

        # 현재 적용 중인 전략 설정 (이미 적용된 개선 재제안 방지)
        lines.extend(["", "## 현재 설정 (이미 적용 중 — 재제안 금지)"])
        try:
            import yaml
            _ov_path = Path(__file__).parent.parent.parent.parent / "config" / "evolved_overrides.yml"
            _ov = yaml.safe_load(_ov_path.read_text(encoding="utf-8")) if _ov_path.exists() else {}

            # 핵심 파라미터만 추출
            _exit = _ov.get("exit_manager", {})
            if _exit:
                lines.append(f"- exit_manager: stop_loss={_exit.get('stop_loss_pct')}%, "
                             f"first_exit_pct={_exit.get('first_exit_pct')}%, "
                             f"first_exit_ratio={_exit.get('first_exit_ratio')}, "
                             f"trailing={_exit.get('trailing_stop_pct')}%")

            _risk = _ov.get("risk_config", {})
            _alloc = _risk.get("strategy_allocation", {})
            if _alloc:
                lines.append(f"- 전략배분: {', '.join(f'{k}={v}%' for k, v in _alloc.items())}")

            # 전략별 파라미터
            for _strat in ["sepa_trend", "rsi2_reversal", "theme_chasing", "gap_and_go"]:
                _s = _ov.get(_strat, {})
                if _s:
                    _parts = [f"{k}={v}" for k, v in _s.items() if k != "enabled"]
                    if _parts:
                        lines.append(f"- {_strat}: {', '.join(_parts)}")

            # 배치 설정
            _default_path = Path(__file__).parent.parent.parent.parent / "config" / "default.yml"
            if _default_path.exists():
                _default_cfg = yaml.safe_load(_default_path.read_text(encoding="utf-8")) or {}
                _batch_exec = (_default_cfg.get("kr", {}).get("batch", {}) or {}).get("execute_time", "09:30")
                lines.append(f"- 배치 실행 시간: {_batch_exec}")

        except Exception as _cfg_err:
            lines.append(f"- (설정 로드 실패: {_cfg_err})")

        # 전문가 의견 (2026-05-29 추가 — 7명 전문가 시스템)
        try:
            from src.experts.opinion_store import load_all_today
            ops = load_all_today()
            if ops:
                lines.extend(["", "## 오늘 전문가 의견 (참고)"])
                for name in (
                    "macro_economist", "kr_market_expert", "us_market_expert",
                    "kr_economy_expert", "global_micro_expert",
                    "news_curator", "earnings_expert",
                ):
                    op = ops.get(name)
                    if op is None or not op.is_valid:
                        continue
                    findings = "; ".join(op.key_findings[:3])
                    lines.append(
                        f"- **{name}**: {op.regime_bias.value}, "
                        f"score={op.score:+d}, conf={op.confidence:.2f} — {findings[:200]}"
                    )
                lines.append("")
                lines.append(
                    "→ 위 전문가 의견과 거래 결과를 대조하여, "
                    "전문가가 경고했는데도 진입한 거래/전문가 합의를 활용한 거래를 함께 평가하세요."
                )
        except Exception as _exp_err:
            lines.append(f"- (전문가 의견 로드 실패: {_exp_err})")

        # 응답 형식 안내
        lines.extend([
            "",
            "## 응답 형식",
            "다음 JSON 형식으로 응답해주세요:",
            _RESPONSE_SCHEMA,
        ])

        return "\n".join(lines)

    def _parse_llm_response(
        self,
        response_text: str,
        target_date: date,
    ) -> Dict[str, Any]:
        """LLM 응답에서 JSON을 추출하고 파싱한다."""
        # JSON 블록 추출
        json_start = response_text.find("{")
        json_end = response_text.rfind("}") + 1

        if json_start == -1 or json_end <= 0:
            raise ValueError("LLM 응답에서 JSON을 찾을 수 없음")

        json_str = response_text[json_start:json_end]
        data = json.loads(json_str)

        # 메타데이터 추가
        data["date"] = target_date.isoformat()
        data["generated_at"] = datetime.now().isoformat()
        data["source"] = "llm"

        return data

    def _create_empty_review(self, target_date: date) -> Dict[str, Any]:
        """거래가 없는 날의 빈 리뷰를 생성한다."""
        return {
            "date": target_date.isoformat(),
            "generated_at": datetime.now().isoformat(),
            "source": "empty",
            "assessment": "no_data",
            "confidence": 0.0,
            "daily_return_pct": 0.0,
            "trade_reviews": [],
            "insights": ["오늘은 거래가 없습니다."],
            "avoid_patterns": [],
            "focus_opportunities": [],
            "parameter_suggestions": [],
            "telegram_summary": "",
        }

    def _create_fallback_review(
        self,
        target_date: date,
        summary: Dict[str, Any],
        trades: List[Dict[str, Any]],
    ) -> Dict[str, Any]:
        """LLM 실패 시 기본 통계 기반 폴백 리뷰를 생성한다."""
        total_trades = summary.get("total_trades", 0)
        win_rate = summary.get("win_rate", 0)
        total_pnl = summary.get("total_pnl", 0)
        total_pnl_pct = summary.get("total_pnl_pct", 0)
        profit_factor = summary.get("profit_factor", 0)

        # 평가 결정
        if total_pnl_pct >= 1.0 and win_rate >= 50:
            assessment = "good"
        elif total_pnl_pct < 0 or win_rate < 40 or profit_factor < 1.0:
            assessment = "poor"
        else:
            assessment = "fair"

        # 인사이트 생성
        insights = []
        if win_rate < 40:
            insights.append(f"승률 {win_rate:.1f}%로 낮음 -- 진입 조건 강화 필요")
        if profit_factor < 1.0:
            insights.append(f"Profit Factor {profit_factor:.2f}로 1 미만 -- 손절 관리 필요")
        if total_pnl_pct < 0:
            insights.append(f"일 수익률 {total_pnl_pct:+.2f}% 손실 -- 원인 분석 필요")
        if not insights:
            insights.append(f"일 수익률 {total_pnl_pct:+.2f}%, 승률 {win_rate:.1f}%")

        # 간단한 거래별 리뷰
        trade_reviews = []
        for t in trades:
            pnl_pct = t.get("pnl_pct", 0)
            if pnl_pct > 0:
                review_comment = "수익 거래"
            else:
                review_comment = f"손실 거래 ({t.get('exit_type', '')})"

            trade_reviews.append({
                "symbol": t.get("symbol", ""),
                "name": t.get("name", ""),
                "pnl_pct": pnl_pct,
                "review": review_comment,
                "lesson": "LLM 분석 실패로 상세 복기 불가",
            })

        date_display = target_date.strftime("%-m/%-d")
        assess_emoji = {"good": "\U0001f7e2", "fair": "\U0001f7e1", "poor": "\U0001f534"}.get(assessment, "\u26aa")
        capital_pnl_pct = summary.get("capital_pnl_pct", 0.0)
        telegram_summary = (
            f"\U0001f4ca <b>{date_display} 거래 리뷰</b> (자동)\n\n"
            f"<b>\u25a0 성과</b>  {assess_emoji}\n"
            f"  승률 <b>{win_rate:.0f}%</b> ({summary.get('wins', 0)}/{total_trades})\n"
            f"  손익 <b>{total_pnl:+,.0f}원</b>\n"
            f"  거래률 {total_pnl_pct:+.2f}% / 자본률 {capital_pnl_pct:+.3f}%\n"
            f"  PF {profit_factor:.2f}"
        )

        return {
            "date": target_date.isoformat(),
            "generated_at": datetime.now().isoformat(),
            "source": "fallback",
            "assessment": assessment,
            "confidence": 0.3,
            "daily_return_pct": total_pnl_pct,
            "trade_reviews": trade_reviews,
            "insights": insights,
            "avoid_patterns": [],
            "focus_opportunities": [],
            "parameter_suggestions": [],
            "telegram_summary": telegram_summary,
        }

    def _save_llm_review(self, target_date: date, review: Dict[str, Any]) -> None:
        """LLM 리뷰를 파일에 저장한다."""
        try:
            file_path = self._llm_review_path(target_date)
            with open(file_path, "w", encoding="utf-8") as f:
                json.dump(review, f, ensure_ascii=False, indent=2)
            logger.info(f"[거래리뷰] LLM 리뷰 저장: {file_path}")
        except Exception as e:
            logger.error(f"[거래리뷰] LLM 리뷰 저장 실패: {e}")

    async def _save_daily_bias(self, llm_result: dict, summary: dict):
        """LLM 리뷰 결과에서 내일 운영 바이어스 추출 → daily_bias.json 저장"""
        import json
        from pathlib import Path
        from datetime import date, datetime

        bias_path = Path.home() / ".cache" / "ai_trader" / "daily_bias.json"

        # LLM이 제안한 parameter_suggestions에서 바이어스 추출
        param_suggestions = llm_result.get("parameter_suggestions", [])
        assessment = llm_result.get("assessment", "fair")

        sepa_boost = 0
        rsi2_boost = 0
        avoid_before = None
        regime_hint = "neutral"
        top_lesson = ""

        # 2026-09-13 WikiSkill 정렬: parameter_suggestions는 더 이상 런타임 점수 부스트로 게이트를
        # 우회하지 않는다 — StrategyEvolver._find_daily_review_trigger가 백테스트 게이트 경유 제안으로
        # 소비한다 (논문의 실패 모드: 검증 없는 지식이 런타임에 직접 주입). assessment 기반 보정만 유지.

        # 전체 평가 기반 기본 바이어스
        if assessment == "poor":
            sepa_boost = max(sepa_boost, 5)
        elif assessment == "good":
            sepa_boost = min(sepa_boost, 0)

        # 패턴 기반 시간대 제한
        avoid_patterns = llm_result.get("avoid_patterns", [])
        for pattern in avoid_patterns:
            if isinstance(pattern, str):
                if "오전" in pattern and ("10시" in pattern or "10:00" in pattern):
                    avoid_before = "10:00"
                elif "장초반" in pattern or "9시" in pattern:
                    avoid_before = "09:30"

        insights = llm_result.get("insights", [])
        if insights:
            top_lesson = insights[0] if isinstance(insights[0], str) else ""

        bias = {
            "date": str(date.today()),
            "assessment": assessment,
            "sepa_score_boost": sepa_boost,
            "rsi2_score_boost": rsi2_boost,
            "avoid_entry_before": avoid_before,
            "regime_hint": regime_hint,
            "top_lesson": top_lesson,
            "generated_at": datetime.now().isoformat(),
        }

        try:
            bias_path.parent.mkdir(parents=True, exist_ok=True)
            with open(bias_path, "w", encoding="utf-8") as f:
                json.dump(bias, f, ensure_ascii=False, indent=2)
            logger.info(
                f"[daily_bias] 저장 완료: sepa_boost={sepa_boost:+d}, "
                f"rsi2_boost={rsi2_boost:+d}, avoid_before={avoid_before}"
            )
        except Exception as e:
            logger.warning(f"[daily_bias] 저장 실패: {e}")

        return bias

    # --- 리포트 조회 ---

    def load_report(self, date_str: str) -> Optional[Dict[str, Any]]:
        """
        거래 리포트(review_YYYYMMDD.json)를 로드한다.

        Args:
            date_str: 날짜 (YYYY-MM-DD)

        Returns:
            리포트 딕셔너리 또는 None
        """
        target_date = _parse_date_str(date_str)
        file_path = self._review_path(target_date)

        if not file_path.exists():
            return None

        try:
            with open(file_path, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception as e:
            logger.error(f"[거래리뷰] 리포트 로드 실패 ({file_path}): {e}")
            return None

    def load_llm_review(self, date_str: str) -> Optional[Dict[str, Any]]:
        """
        LLM 종합 평가(llm_review_YYYYMMDD.json)를 로드한다.

        Args:
            date_str: 날짜 (YYYY-MM-DD)

        Returns:
            LLM 평가 딕셔너리 또는 None
        """
        target_date = _parse_date_str(date_str)
        file_path = self._llm_review_path(target_date)

        if not file_path.exists():
            return None

        try:
            with open(file_path, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception as e:
            logger.error(f"[거래리뷰] LLM 리뷰 로드 실패 ({file_path}): {e}")
            return None

    def list_available_dates(self) -> List[str]:
        """
        리뷰 파일이 존재하는 날짜 목록을 반환한다.

        review_YYYYMMDD.json 또는 llm_review_YYYYMMDD.json이 존재하는
        날짜를 YYYY-MM-DD 형식으로 정렬하여 반환한다.

        Returns:
            날짜 문자열 리스트 (오름차순)
        """
        dates = set()

        try:
            for file_path in self.storage_dir.iterdir():
                name = file_path.name

                # review_YYYYMMDD.json
                if name.startswith("review_") and name.endswith(".json"):
                    date_part = name[7:15]  # "review_" = 7글자
                    if len(date_part) == 8 and date_part.isdigit():
                        formatted = f"{date_part[:4]}-{date_part[4:6]}-{date_part[6:8]}"
                        dates.add(formatted)

                # llm_review_YYYYMMDD.json
                if name.startswith("llm_review_") and name.endswith(".json"):
                    date_part = name[11:19]  # "llm_review_" = 11글자
                    if len(date_part) == 8 and date_part.isdigit():
                        formatted = f"{date_part[:4]}-{date_part[4:6]}-{date_part[6:8]}"
                        dates.add(formatted)

        except Exception as e:
            logger.error(f"[거래리뷰] 날짜 목록 조회 실패: {e}")

        return sorted(dates)


# 싱글톤 인스턴스
_daily_reviewer: Optional[DailyReviewer] = None


def get_daily_reviewer() -> DailyReviewer:
    """DailyReviewer 싱글톤 인스턴스를 반환한다."""
    global _daily_reviewer
    if _daily_reviewer is None:
        _daily_reviewer = DailyReviewer()
    return _daily_reviewer
