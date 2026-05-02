"""매주 토요일 매도 후속 복기 시스템

매주 토요일 09:00 KST에 최근 30일간의 KR 매도 거래를 추적하여:
1. 각 종목 현재가를 KIS API로 조회
2. 매도 후 변동률 계산 (매도 시점 → 현재)
3. 전략 × exit_type 매트릭스 집계
4. GPT-5.4(STRATEGY_ANALYSIS)로 인과 추론 + 가설 생성
5. Wiki 페이지 + 텔레그램 리포트 생성

Wiki 페이지는 다음 weekly rebalance 시 LLM 컨텍스트로 자동 흡수됨.
"""

import json
import asyncio
from datetime import datetime, date, timedelta
from pathlib import Path
from typing import Dict, Any, List
from collections import defaultdict

from loguru import logger

from ..utils.llm import LLMTask, get_llm_manager


JOURNAL_DIR = Path.home() / ".cache" / "ai_trader" / "journal"
WIKI_DIR = Path.home() / ".cache" / "ai_trader" / "wiki"


class PostExitReviewer:
    """매도 후 추세 추적 + 전략 개선 인사이트 추출"""

    MISSED_GAIN_PCT = 3.0
    AVOIDED_LOSS_PCT = -3.0
    REVIEW_DAYS = 30
    MIN_SAMPLE_FOR_LLM = 5
    API_RATE_LIMIT_SLEEP = 0.2
    MAX_LLM_PROMPT_TRADES = 40

    def __init__(self, trade_journal=None, broker=None, llm_manager=None):
        self._tj = trade_journal
        self._broker = broker
        self._llm = llm_manager or get_llm_manager()

    async def run_weekly(self) -> Dict[str, Any]:
        """주간 매도 후속 복기 실행

        Returns:
            보고서 dict ({status, samples, missed, avoided, by_strategy, ...})
        """
        logger.info("[후속복기] 주간 매도 후속 복기 시작")
        try:
            trades = await self._fetch_recent_exits()
            if not trades:
                logger.info("[후속복기] 매도 거래 없음, 스킵")
                return {"status": "no_data", "samples": 0}

            tracked = await self._track_post_exit_prices(trades)
            if not tracked:
                logger.info("[후속복기] 추적 가능 종목 없음, 스킵")
                return {"status": "no_quotes", "samples": 0}

            classified = self._classify(tracked)
            aggregated = self._aggregate(tracked)

            llm_insights = ""
            if len(tracked) >= self.MIN_SAMPLE_FOR_LLM and self._llm:
                llm_insights = await self._generate_llm_insights(
                    tracked, classified, aggregated
                )

            report = {
                "status": "ok",
                "report_date": date.today().isoformat(),
                "review_window_days": self.REVIEW_DAYS,
                "samples": len(tracked),
                "missed_count": len(classified["missed"]),
                "avoided_count": len(classified["avoided"]),
                "neutral_count": len(classified["neutral"]),
                "missed": classified["missed"][:20],
                "avoided": classified["avoided"][:20],
                "by_strategy": aggregated["by_strategy"],
                "by_exit_type": aggregated["by_exit_type"],
                "llm_insights": llm_insights,
            }

            saved_path = self._save_report(report)
            report["saved_path"] = str(saved_path)

            try:
                wiki_path = self._write_wiki_page(report)
                report["wiki_path"] = str(wiki_path)
            except Exception as e:
                logger.warning(f"[후속복기] 위키 작성 실패 (무시): {e}")

            report["telegram_message"] = self._format_telegram(report)
            logger.info(
                f"[후속복기] 완료: 표본={len(tracked)}, "
                f"놓침={len(classified['missed'])}, "
                f"회피={len(classified['avoided'])}"
            )
            return report
        except Exception as e:
            logger.exception(f"[후속복기] 오류: {e}")
            return {"status": "error", "error": str(e)}

    async def _fetch_recent_exits(self) -> List[Dict[str, Any]]:
        if not self._tj or not getattr(self._tj, "pool", None):
            logger.warning("[후속복기] DB pool 없음")
            return []
        cutoff = datetime.now() - timedelta(days=self.REVIEW_DAYS)
        async with self._tj.pool.acquire() as conn:
            rows = await conn.fetch(
                """
                SELECT symbol, name, entry_strategy, entry_time, exit_time,
                       entry_price, exit_price, pnl_pct, exit_type, exit_reason,
                       holding_minutes
                FROM trades
                WHERE market = 'KR'
                  AND exit_time IS NOT NULL
                  AND exit_time >= $1
                ORDER BY exit_time DESC
                """,
                cutoff,
            )
        result = []
        for r in rows:
            result.append({
                "symbol": r["symbol"],
                "name": r["name"] or r["symbol"],
                "strategy": r["entry_strategy"] or "unknown",
                "entry_time": r["entry_time"].isoformat() if r["entry_time"] else None,
                "exit_time": r["exit_time"].isoformat() if r["exit_time"] else None,
                "entry_price": float(r["entry_price"]) if r["entry_price"] is not None else 0.0,
                "exit_price": float(r["exit_price"]) if r["exit_price"] is not None else 0.0,
                "pnl_pct": float(r["pnl_pct"]) if r["pnl_pct"] is not None else 0.0,
                "exit_type": r["exit_type"] or "unknown",
                "exit_reason": (r["exit_reason"] or "")[:200],
                "holding_minutes": r["holding_minutes"] or 0,
            })
        return result

    async def _track_post_exit_prices(
        self, trades: List[Dict[str, Any]]
    ) -> List[Dict[str, Any]]:
        if not self._broker:
            logger.warning("[후속복기] broker 없음")
            return []

        unique_symbols = list({t["symbol"] for t in trades})
        symbol_to_price: Dict[str, float] = {}
        failed = 0
        for sym in unique_symbols:
            try:
                quote = await self._broker.get_quote(sym)
                quote_data = quote if quote else {}
                raw_price = quote_data.get("price")
                price = float(raw_price) if raw_price is not None else 0.0
                if price > 0:
                    symbol_to_price[sym] = price
                else:
                    failed += 1
                    logger.debug(f"[후속복기] {sym} 현재가 0 (거래정지/상폐?)")
            except Exception as e:
                failed += 1
                logger.debug(f"[후속복기] {sym} 시세 조회 실패: {e}")
            await asyncio.sleep(self.API_RATE_LIMIT_SLEEP)

        if failed:
            logger.info(
                f"[후속복기] 시세 조회: 성공={len(symbol_to_price)}, "
                f"실패={failed}/{len(unique_symbols)}"
            )

        tracked = []
        now = datetime.now()
        for t in trades:
            cur = symbol_to_price.get(t["symbol"])
            if cur is None or cur <= 0:
                continue
            exit_px = t["exit_price"] or 0
            if exit_px <= 0:
                continue
            post_exit_pct = (cur - exit_px) / exit_px * 100
            days_since = 0
            if t["exit_time"]:
                try:
                    et = datetime.fromisoformat(t["exit_time"])
                    days_since = (now - et).days
                except Exception:
                    pass
            t2 = dict(t)
            t2["current_price"] = cur
            t2["post_exit_pct"] = round(post_exit_pct, 2)
            t2["days_since_exit"] = days_since
            tracked.append(t2)
        return tracked

    def _classify(self, tracked: List[Dict]) -> Dict[str, List]:
        missed, avoided, neutral = [], [], []
        for t in tracked:
            pe = t["post_exit_pct"]
            if pe >= self.MISSED_GAIN_PCT:
                missed.append(t)
            elif pe <= self.AVOIDED_LOSS_PCT:
                avoided.append(t)
            else:
                neutral.append(t)
        missed.sort(key=lambda x: x["post_exit_pct"], reverse=True)
        avoided.sort(key=lambda x: x["post_exit_pct"])
        return {"missed": missed, "avoided": avoided, "neutral": neutral}

    def _aggregate(self, tracked: List[Dict]) -> Dict[str, Dict]:
        def _new_bucket():
            return {
                "count": 0,
                "missed": 0,
                "avoided": 0,
                "neutral": 0,
                "avg_post_exit": 0.0,
                "avg_pnl_pct": 0.0,
                "_sum_pe": 0.0,
                "_sum_pnl": 0.0,
            }

        by_strategy = defaultdict(_new_bucket)
        by_exit_type = defaultdict(_new_bucket)

        for t in tracked:
            pe = t["post_exit_pct"]
            pnl = t["pnl_pct"]
            for bucket, key in [
                (by_strategy, t["strategy"]),
                (by_exit_type, t["exit_type"]),
            ]:
                b = bucket[key]
                b["count"] += 1
                b["_sum_pe"] += pe
                b["_sum_pnl"] += pnl
                if pe >= self.MISSED_GAIN_PCT:
                    b["missed"] += 1
                elif pe <= self.AVOIDED_LOSS_PCT:
                    b["avoided"] += 1
                else:
                    b["neutral"] += 1

        for bucket in (by_strategy, by_exit_type):
            for v in bucket.values():
                if v["count"] > 0:
                    v["avg_post_exit"] = round(v["_sum_pe"] / v["count"], 2)
                    v["avg_pnl_pct"] = round(v["_sum_pnl"] / v["count"], 2)
                v.pop("_sum_pe", None)
                v.pop("_sum_pnl", None)

        return {
            "by_strategy": dict(by_strategy),
            "by_exit_type": dict(by_exit_type),
        }

    async def _generate_llm_insights(
        self,
        tracked: List[Dict],
        classified: Dict,
        aggregated: Dict,
    ) -> str:
        prompt = self._build_prompt(tracked, classified, aggregated)
        system = (
            "당신은 한국 주식 자동매매 시스템의 수석 전략 분석가입니다. "
            "매도 후 추세를 비판적으로 분석하되, 항상 "
            "1) 표본 크기 한계, 2) 시장 체제(KOSPI/섹터) 영향, "
            "3) 검증 가능한 가설 형태(예: \"X 변경 시 Y 개선 예상, 5영업일 후 검증\") "
            "를 명시합니다. 절대 1주 표본만으로 단정 결론을 내리지 않습니다. "
            "출력은 한국어 마크다운으로 600단어 이내."
        )
        try:
            resp = await self._llm.complete(
                prompt=prompt,
                task=LLMTask.STRATEGY_ANALYSIS,
                system=system,
                max_tokens=1800,
            )
            if resp and getattr(resp, "success", False):
                return resp.content or ""
            err = getattr(resp, "error", "unknown")
            return f"[LLM 분석 실패: {err}]"
        except Exception as e:
            logger.warning(f"[후속복기] LLM 호출 예외: {e}")
            return f"[LLM 호출 예외: {e}]"

    def _build_prompt(
        self,
        tracked: List[Dict],
        classified: Dict,
        aggregated: Dict,
    ) -> str:
        lines = [
            "# QWQ AI Trader 주간 매도 후속 복기",
            f"\n분석 기간: 최근 {self.REVIEW_DAYS}일 KR 매도 거래",
            f"표본 크기: {len(tracked)}건 "
            f"(놓침 {len(classified['missed'])}, 회피 {len(classified['avoided'])}, 타당 {len(classified['neutral'])})",
            "",
            "## 1. 매도 후 상승 (놓친 수익) — 상위 10건",
            "| 종목 | 전략 | exit_type | 보유분 | pnl% | 매도후% | 경과일 |",
            "|---|---|---|---|---|---|---|",
        ]
        for t in classified["missed"][:10]:
            lines.append(
                f"| {t['name']}({t['symbol']}) | {t['strategy']} | {t['exit_type']} "
                f"| {t['holding_minutes']} | {t['pnl_pct']:+.2f}% "
                f"| **+{t['post_exit_pct']:.2f}%** | {t['days_since_exit']}일 |"
            )
        lines.extend([
            "",
            "## 2. 매도 후 하락 (회피 성공) — 상위 10건",
            "| 종목 | 전략 | exit_type | 보유분 | pnl% | 매도후% | 경과일 |",
            "|---|---|---|---|---|---|---|",
        ])
        for t in classified["avoided"][:10]:
            lines.append(
                f"| {t['name']}({t['symbol']}) | {t['strategy']} | {t['exit_type']} "
                f"| {t['holding_minutes']} | {t['pnl_pct']:+.2f}% "
                f"| **{t['post_exit_pct']:.2f}%** | {t['days_since_exit']}일 |"
            )

        lines.extend(["", "## 3. 전략별 집계", "| 전략 | n | 놓침 | 회피 | 평균 매도후% | 평균 실현 pnl% |", "|---|---|---|---|---|---|"])
        for k, v in sorted(aggregated["by_strategy"].items(), key=lambda x: -x[1]["count"]):
            lines.append(
                f"| {k} | {v['count']} | {v['missed']} | {v['avoided']} "
                f"| {v['avg_post_exit']:+.2f}% | {v['avg_pnl_pct']:+.2f}% |"
            )

        lines.extend(["", "## 4. exit_type별 집계", "| exit_type | n | 놓침 | 회피 | 평균 매도후% |", "|---|---|---|---|---|"])
        for k, v in sorted(aggregated["by_exit_type"].items(), key=lambda x: -x[1]["count"]):
            lines.append(
                f"| {k} | {v['count']} | {v['missed']} | {v['avoided']} "
                f"| {v['avg_post_exit']:+.2f}% |"
            )

        lines.extend([
            "",
            "## 분석 요청",
            "위 데이터를 바탕으로 다음을 답해 주세요:",
            "",
            "1. **통계적 신뢰도**: 표본 크기와 시장 체제 영향을 비판적으로 평가 (몇 % 설명력?)",
            "2. **가장 시급한 P0 파라미터 1건**: 구체 수치 + 근거 + 검증 가능 가설(예: \"5영업일 후 평균 실현 +X%p 개선\"). "
            "단, max_position_pct 28% × min_stop_pct 4.0% × 4건 = 4.48% < 5% daily_max 제약 유지.",
            "3. **전략별 분기 권고**: sepa_trend / rsi2_reversal / strategic_swing / theme_chasing 중 어느 전략의 exit 로직이 시장 체제에 둔감한가?",
            "4. **manual exit 패턴**: theme_chasing의 manual exit가 \"분류되지 않은 잔여\"이므로, 어떤 reason 패턴인지 추측해 차주 수정 우선순위를 매겨 주세요.",
            "5. **반증 가능성**: 위 결론을 부정할 수 있는 데이터/조건은? (예: \"강세장 종료 시 첫 익절을 늦추면 손실 확대\")",
            "",
            "출력은 표 + 짧은 단락. 진화 시스템 1건 변경 원칙 준수.",
        ])
        return "\n".join(lines)

    def _save_report(self, report: Dict) -> Path:
        JOURNAL_DIR.mkdir(parents=True, exist_ok=True)
        fn = JOURNAL_DIR / f"post_exit_review_{report['report_date'].replace('-','')}.json"
        with open(fn, "w", encoding="utf-8") as f:
            json.dump(report, f, ensure_ascii=False, indent=2)
        return fn

    def _write_wiki_page(self, report: Dict) -> Path:
        WIKI_DIR.mkdir(parents=True, exist_ok=True)
        iso_year, iso_week, _ = date.today().isocalendar()
        wiki_path = WIKI_DIR / f"weekly_post_exit_{iso_year}-W{iso_week:02d}.md"

        lines = [
            f"# 주간 매도 후속 복기 — {report['report_date']} (W{iso_week:02d})",
            "",
            f"- 분석 기간: 최근 {report['review_window_days']}일",
            f"- 표본: {report['samples']}건 "
            f"(놓침 {report['missed_count']} / 회피 {report['avoided_count']} / 타당 {report['neutral_count']})",
            "",
            "## 전략별 매도후 추세",
            "| 전략 | n | 놓침 | 회피 | 평균 매도후% | 평균 실현 pnl% |",
            "|---|---|---|---|---|---|",
        ]
        for k, v in sorted(report["by_strategy"].items(), key=lambda x: -x[1]["count"]):
            lines.append(
                f"| {k} | {v['count']} | {v['missed']} | {v['avoided']} "
                f"| {v['avg_post_exit']:+.2f}% | {v['avg_pnl_pct']:+.2f}% |"
            )
        lines.extend(["", "## exit_type별 매도후 추세", "| exit_type | n | 놓침 | 회피 | 평균 매도후% |", "|---|---|---|---|---|"])
        for k, v in sorted(report["by_exit_type"].items(), key=lambda x: -x[1]["count"]):
            lines.append(
                f"| {k} | {v['count']} | {v['missed']} | {v['avoided']} "
                f"| {v['avg_post_exit']:+.2f}% |"
            )

        if report.get("llm_insights"):
            lines.extend([
                "",
                "## LLM 분석 (GPT-5.4)",
                "",
                report["llm_insights"],
            ])

        lines.extend([
            "",
            "## Top 매도후 상승 (놓침)",
        ])
        for t in report.get("missed", [])[:10]:
            lines.append(
                f"- {t['name']}({t['symbol']}) [{t['strategy']}/{t['exit_type']}] "
                f"실현 {t['pnl_pct']:+.2f}% → 매도후 **+{t['post_exit_pct']:.2f}%** "
                f"({t['days_since_exit']}일 경과)"
            )
        lines.extend(["", "## Top 매도후 하락 (회피)"])
        for t in report.get("avoided", [])[:10]:
            lines.append(
                f"- {t['name']}({t['symbol']}) [{t['strategy']}/{t['exit_type']}] "
                f"실현 {t['pnl_pct']:+.2f}% → 매도후 **{t['post_exit_pct']:.2f}%** "
                f"({t['days_since_exit']}일 경과)"
            )

        with open(wiki_path, "w", encoding="utf-8") as f:
            f.write("\n".join(lines))
        return wiki_path

    def _format_telegram(self, report: Dict) -> str:
        lines = [
            "📋 <b>주간 매도 후속 복기</b>",
            f"<i>{report['report_date']} · 최근 {report['review_window_days']}일</i>",
            "",
            f"표본 <b>{report['samples']}</b>건 · "
            f"놓침 <b>{report['missed_count']}</b> / 회피 <b>{report['avoided_count']}</b> / 타당 {report['neutral_count']}",
            "",
            "<b>📈 매도후 상승 Top 5 (놓친 수익)</b>",
        ]
        for t in report.get("missed", [])[:5]:
            lines.append(
                f"  · {t['name']} [{_short_strat(t['strategy'])}/{t['exit_type']}] "
                f"+{t['post_exit_pct']:.1f}% ({t['days_since_exit']}일)"
            )
        if not report.get("missed"):
            lines.append("  (없음)")

        lines.append("")
        lines.append("<b>📉 매도후 하락 Top 5 (회피 성공)</b>")
        for t in report.get("avoided", [])[:5]:
            lines.append(
                f"  · {t['name']} [{_short_strat(t['strategy'])}/{t['exit_type']}] "
                f"{t['post_exit_pct']:.1f}% ({t['days_since_exit']}일)"
            )
        if not report.get("avoided"):
            lines.append("  (없음)")

        lines.append("")
        lines.append("<b>🧠 전략별 매도후 평균</b>")
        for k, v in sorted(
            report["by_strategy"].items(), key=lambda x: -x[1]["count"]
        )[:5]:
            lines.append(
                f"  · {_short_strat(k)} (n={v['count']}): {v['avg_post_exit']:+.1f}% "
                f"[놓침 {v['missed']}/회피 {v['avoided']}]"
            )

        if report.get("llm_insights"):
            insight_short = report["llm_insights"][:1200]
            lines.extend([
                "",
                "<b>🤖 GPT-5.4 분석</b>",
                f"<pre>{_escape_html(insight_short)}</pre>",
            ])
            if len(report["llm_insights"]) > 1200:
                lines.append(
                    f"<i>(전체는 위키: weekly_post_exit_{date.today().isocalendar()[0]}-W{date.today().isocalendar()[1]:02d}.md)</i>"
                )

        return "\n".join(lines)


_STRAT_SHORT = {
    "sepa_trend": "SEPA",
    "rsi2_reversal": "RSI2",
    "theme_chasing": "테마",
    "strategic_swing": "전략",
    "momentum_breakout": "모멘",
    "gap_and_go": "갭",
    "core_holding": "코어",
}


def _short_strat(s: str) -> str:
    return _STRAT_SHORT.get(s, s[:6])


def _escape_html(s: str) -> str:
    return s.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
