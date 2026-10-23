"""매주 토요일 매도 후속 복기 시스템

매주 토요일 09:30~09:44 KST에 최근 30일간의 KR 매도 거래를 추적하여:
1. 각 종목 현재가를 KIS API로 조회
2. 매도 후 변동률 계산 (매도 시점 → 현재)
3. 전략 × exit_type 매트릭스 집계
4. LLM으로 관측 한계와 후속 검증 가설 생성
5. Wiki 페이지 + 텔레그램 리포트 생성

Wiki 페이지는 다음 weekly rebalance 시 LLM 컨텍스트로 자동 흡수됨.
"""

import json
import asyncio
import math
from statistics import mean
from datetime import datetime, date, timedelta
from pathlib import Path
from typing import Dict, Any, List
from collections import defaultdict

from loguru import logger

from ..utils.llm import get_llm_manager
from ..utils.llm_roles import LLMRole


JOURNAL_DIR = Path.home() / ".cache" / "ai_trader" / "journal"
WIKI_DIR = Path.home() / ".cache" / "ai_trader" / "wiki"


def _finite_number(value: Any) -> float | None:
    """불리언을 수치로 인정하지 않고 유한한 값만 반환한다."""
    if isinstance(value, bool):
        return None
    try:
        number = float(value)
    except (TypeError, ValueError, OverflowError):
        return None
    return number if math.isfinite(number) else None


def _format_signed_pct(value: Any, digits: int = 2) -> str:
    number = _finite_number(value)
    return f"{number:+.{digits}f}%" if number is not None else "미기록"


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
            coverage = {
                "eligible": len(trades),
                "tracked": len(tracked),
                "missing": len(trades) - len(tracked),
                "quote_observed_at": getattr(self, "_last_quote_observed_at", None),
            }
            if not tracked:
                report = {
                    "status": "no_quotes",
                    "report_date": date.today().isoformat(),
                    "review_window_days": self.REVIEW_DAYS,
                    "samples": 0,
                    "eligible_samples": coverage["eligible"],
                    "quote_missing_samples": coverage["missing"],
                    "quote_observed_at": coverage["quote_observed_at"],
                    "observation_horizon_min_days": None,
                    "observation_horizon_max_days": None,
                    "measurement_basis": "gross_price_change_from_exit_to_current_quote",
                    "costs_included": False,
                    "missed_count": 0,
                    "avoided_count": 0,
                    "neutral_count": 0,
                    "missed": [],
                    "avoided": [],
                    "by_strategy": {},
                    "by_exit_type": {},
                    "llm_insights": "",
                }
                saved_path = self._save_report(report)
                report["saved_path"] = str(saved_path)
                report["telegram_message"] = self._format_telegram(report)
                logger.info(
                    "[후속복기] 가격 관측 0건: "
                    f"대상={coverage['eligible']}, 미관측={coverage['missing']}"
                )
                return report

            classified = self._classify(tracked)
            aggregated = self._aggregate(tracked)
            llm_insights = ""
            if len(tracked) >= self.MIN_SAMPLE_FOR_LLM and self._llm:
                llm_insights = await self._generate_llm_insights(
                    tracked, classified, aggregated, coverage=coverage
                )

            horizons = [int(t.get("days_since_exit", 0)) for t in tracked]

            report = {
                "status": "ok",
                "report_date": date.today().isoformat(),
                "review_window_days": self.REVIEW_DAYS,
                "samples": len(tracked),
                "eligible_samples": len(trades),
                "quote_missing_samples": len(trades) - len(tracked),
                "quote_observed_at": coverage["quote_observed_at"],
                "observation_horizon_min_days": min(horizons) if horizons else None,
                "observation_horizon_max_days": max(horizons) if horizons else None,
                "measurement_basis": "gross_price_change_from_exit_to_current_quote",
                "costs_included": False,
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
                "entry_price": _finite_number(r["entry_price"]),
                "exit_price": _finite_number(r["exit_price"]),
                "pnl_pct": _finite_number(r["pnl_pct"]),
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
                price = _finite_number(raw_price)
                if price is not None and price > 0:
                    symbol_to_price[sym] = price
                else:
                    failed += 1
                    logger.debug(f"[후속복기] {sym} 현재가 무효/0 (거래정지/상폐?)")
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
        self._last_quote_observed_at = now.isoformat()
        for t in trades:
            cur = symbol_to_price.get(t["symbol"])
            if cur is None or cur <= 0:
                continue
            exit_px = _finite_number(t.get("exit_price"))
            if exit_px is None or exit_px <= 0:
                continue
            post_exit_pct = (cur - exit_px) / exit_px * 100
            if not math.isfinite(post_exit_pct):
                logger.debug(f"[후속복기] {t['symbol']} 매도후 변동률 무효")
                continue
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
            t2["pnl_pct"] = _finite_number(t.get("pnl_pct"))
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
                "_post_exit_values": [],
                "_pnl_values": [],
            }

        by_strategy = defaultdict(_new_bucket)
        by_exit_type = defaultdict(_new_bucket)

        for t in tracked:
            pe = t["post_exit_pct"]
            pnl = _finite_number(t.get("pnl_pct"))
            for bucket, key in [
                (by_strategy, t["strategy"]),
                (by_exit_type, t["exit_type"]),
            ]:
                b = bucket[key]
                b["count"] += 1
                b["_post_exit_values"].append(pe)
                if pnl is not None:
                    b["_pnl_values"].append(pnl)
                if pe >= self.MISSED_GAIN_PCT:
                    b["missed"] += 1
                elif pe <= self.AVOIDED_LOSS_PCT:
                    b["avoided"] += 1
                else:
                    b["neutral"] += 1

        for bucket in (by_strategy, by_exit_type):
            for v in bucket.values():
                pe_values = v.pop("_post_exit_values")
                pnl_values = v.pop("_pnl_values")
                if v["count"] > 0:
                    # 표준 평균 계산은 유한한 개별 값의 중간 합산 오버플로를 피한다.
                    v["avg_post_exit"] = round(mean(pe_values), 2)
                    if len(pnl_values) == v["count"]:
                        v["avg_pnl_pct"] = round(mean(pnl_values), 2)
                    else:
                        v["avg_pnl_pct"] = None

        return {
            "by_strategy": dict(by_strategy),
            "by_exit_type": dict(by_exit_type),
        }

    async def _generate_llm_insights(
        self,
        tracked: List[Dict],
        classified: Dict,
        aggregated: Dict,
        coverage: Dict[str, Any] | None = None,
    ) -> str:
        prompt = self._build_prompt(tracked, classified, aggregated, coverage=coverage)
        system = (
            "당신은 한국 주식 자동매매 시스템의 수석 전략 분석가입니다. "
            "매도 후 가격 관측을 비판적으로 분석하되, 항상 "
            "1) 표본 크기 한계, 2) 시장 체제(KODEX200/섹터) 영향, "
            "3) 검증 가능한 다음 측정 조건을 명시합니다. 인과·우월성·실현 가능 수익을 "
            "단정하거나 파라미터 행동 지시를 하지 않습니다. "
            "출력은 한국어 마크다운으로 600단어 이내."
        )
        try:
            resp = await self._llm.complete_role(
                prompt=prompt,
                role=LLMRole.POST_EXIT_RESEARCH,
                system=system,
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
        coverage: Dict[str, Any] | None = None,
    ) -> str:
        coverage = coverage or {
            "eligible": len(tracked), "tracked": len(tracked), "missing": 0,
            "quote_observed_at": None,
        }
        horizons = [int(t.get("days_since_exit", 0)) for t in tracked]
        horizon_text = f"{min(horizons)}~{max(horizons)}일" if horizons else "산출 불가"
        lines = [
            "# QWQ AI Trader 주간 매도 후 가격 관측",
            f"\n분석 기간: 최근 {self.REVIEW_DAYS}일 KR 매도 거래",
            f"커버리지: 관측 {coverage['tracked']}/{coverage['eligible']}건 "
            f"(미관측 {coverage['missing']}건)",
            f"관측 시차: {horizon_text}; 조회 시각: {coverage.get('quote_observed_at') or '미기록'}",
            "측정 한계: 매도 가격→현재 조회 가격의 총 변동률이며 수수료·세금·스프레드·"
            "체결 가능성·KODEX200/섹터 대조를 반영하지 않음. 서로 다른 관측 시차를 성과처럼 비교하지 않음.",
            f"관측 구간: +3% 이상 {len(classified['missed'])}, -3% 이하 {len(classified['avoided'])}, "
            f"그 사이 {len(classified['neutral'])}",
            "",
            "## 1. 매도 후 +3% 이상 가격 관측 — 상위 10건",
            "| 종목 | 전략 | exit_type | 보유분 | pnl% | 매도후% | 경과일 |",
            "|---|---|---|---|---|---|---|",
        ]
        for t in classified["missed"][:10]:
            lines.append(
                f"| {t['name']}({t['symbol']}) | {t['strategy']} | {t['exit_type']} "
                f"| {t.get('holding_minutes', '미기록')} | {_format_signed_pct(t.get('pnl_pct'))} "
                f"| **+{t['post_exit_pct']:.2f}%** | {t['days_since_exit']}일 |"
            )
        lines.extend([
            "",
            "## 2. 매도 후 -3% 이하 가격 관측 — 상위 10건",
            "| 종목 | 전략 | exit_type | 보유분 | pnl% | 매도후% | 경과일 |",
            "|---|---|---|---|---|---|---|",
        ])
        for t in classified["avoided"][:10]:
            lines.append(
                f"| {t['name']}({t['symbol']}) | {t['strategy']} | {t['exit_type']} "
                f"| {t.get('holding_minutes', '미기록')} | {_format_signed_pct(t.get('pnl_pct'))} "
                f"| **{t['post_exit_pct']:.2f}%** | {t['days_since_exit']}일 |"
            )

        lines.extend(["", "## 3. 전략별 집계", "| 전략 | n | +3% 이상 | -3% 이하 | 평균 매도후% | 평균 실현 pnl% |", "|---|---|---|---|---|---|"])
        for k, v in sorted(aggregated["by_strategy"].items(), key=lambda x: -x[1]["count"]):
            lines.append(
                f"| {k} | {v['count']} | {v['missed']} | {v['avoided']} "
                f"| {_format_signed_pct(v['avg_post_exit'])} | {_format_signed_pct(v['avg_pnl_pct'])} |"
            )

        lines.extend(["", "## 4. exit_type별 집계", "| exit_type | n | +3% 이상 | -3% 이하 | 평균 매도후% |", "|---|---|---|---|---|"])
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
            "1. **관측 품질**: 표본 수, 미관측 분모, 가변 시차, 시장 대조 결측을 적으세요.",
            "2. **가설**: 관측된 연관성만 기술하고 원인·수익·우월한 파라미터로 단정하지 마세요.",
            "3. **다음 확인**: 매도 후 5영업일 고정 시점, KODEX200/섹터 대비, 비용과 "
            "체결 가능성을 포함한 동일 코호트 재측정 절차를 한 건 제시하세요.",
            "4. **반증 조건**: 가설을 기각할 관측 조건을 적으세요.",
            "",
            "출력은 표 + 짧은 단락. 행동 지시가 아니라 검증 전 가설로 표시하세요.",
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
            f"# 주간 매도 후 가격 관측 — {report['report_date']} (W{iso_week:02d})",
            "",
            f"- 분석 기간: 최근 {report['review_window_days']}일",
            f"- 커버리지: 관측 {report['samples']}/{report.get('eligible_samples', report['samples'])}건 "
            f"(미관측 {report.get('quote_missing_samples', 0)}건)",
            f"- 관측 시차: {report.get('observation_horizon_min_days')}~"
            f"{report.get('observation_horizon_max_days')}일",
            f"- 조회 시각: {report.get('quote_observed_at') or '미기록'}",
            "- 한계: 매도 가격→조회 가격의 총 변동률. 비용·세금·스프레드·체결 가능성·시장 대조 미반영.",
            "- 다음 확인: 같은 코호트를 매도 후 5영업일 고정 시점·KODEX200 대비·비용 포함 기준으로 재측정.",
            "",
            "## 전략별 매도후 추세",
            "| 전략 | n | +3% 이상 | -3% 이하 | 평균 매도후% | 평균 실현 pnl% |",
            "|---|---|---|---|---|---|",
        ]
        for k, v in sorted(report["by_strategy"].items(), key=lambda x: -x[1]["count"]):
            lines.append(
                f"| {k} | {v['count']} | {v['missed']} | {v['avoided']} "
                f"| {_format_signed_pct(v['avg_post_exit'])} | {_format_signed_pct(v['avg_pnl_pct'])} |"
            )
        lines.extend(["", "## exit_type별 매도후 추세", "| exit_type | n | +3% 이상 | -3% 이하 | 평균 매도후% |", "|---|---|---|---|---|"])
        for k, v in sorted(report["by_exit_type"].items(), key=lambda x: -x[1]["count"]):
            lines.append(
                f"| {k} | {v['count']} | {v['missed']} | {v['avoided']} "
                f"| {v['avg_post_exit']:+.2f}% |"
            )

        if report.get("llm_insights"):
            lines.extend([
                "",
                "## LLM 검증 전 가설",
                "",
                report["llm_insights"],
            ])

        lines.extend([
            "",
            "## 매도 후 +3% 이상 가격 관측",
        ])
        for t in report.get("missed", [])[:10]:
            lines.append(
                f"- {t['name']}({t['symbol']}) [{t['strategy']}/{t['exit_type']}] "
                f"실현 {_format_signed_pct(t.get('pnl_pct'))} → 매도후 **+{t['post_exit_pct']:.2f}%** "
                f"({t['days_since_exit']}일 경과)"
            )
        lines.extend(["", "## 매도 후 -3% 이하 가격 관측"])
        for t in report.get("avoided", [])[:10]:
            lines.append(
                f"- {t['name']}({t['symbol']}) [{t['strategy']}/{t['exit_type']}] "
                f"실현 {_format_signed_pct(t.get('pnl_pct'))} → 매도후 **{t['post_exit_pct']:.2f}%** "
                f"({t['days_since_exit']}일 경과)"
            )

        with open(wiki_path, "w", encoding="utf-8") as f:
            f.write("\n".join(lines))
        return wiki_path

    def _format_telegram(self, report: Dict) -> str:
        if report["samples"] == 0:
            return "\n".join([
                "📋 <b>주간 매도 후 가격 관측</b>",
                f"<i>{report['report_date']} · 최근 {report['review_window_days']}일</i>",
                "",
                f"관측 0/{report.get('eligible_samples', 0)}건 · "
                f"미관측 {report.get('quote_missing_samples', 0)}건",
                f"조회 {report.get('quote_observed_at') or '시각 미기록'}",
                "유효한 매도·조회 가격 쌍이 없어 가격 변동·성과 판단 보류",
                "※ 매도 가격→조회 가격 총 변동률 · 비용·세금·체결 가능성 미반영",
            ])
        lines = [
            "📋 <b>주간 매도 후 가격 관측</b>",
            f"<i>{report['report_date']} · 최근 {report['review_window_days']}일</i>",
            "",
            f"관측 {report['samples']}/{report.get('eligible_samples', report['samples'])}건 · "
            f"미관측 {report.get('quote_missing_samples', 0)}건",
            f"관측 시차 {report.get('observation_horizon_min_days')}~"
            f"{report.get('observation_horizon_max_days')}일 · 조회 {report.get('quote_observed_at') or '시각 미기록'}",
            "※ 매도 가격→조회 가격 총 변동률 · 비용·세금·체결 가능성 미반영",
            "",
            "<b>📈 매도 후 +3% 이상 가격 관측</b>",
        ]
        for t in report.get("missed", [])[:5]:
            lines.append(
                f"  · {t['name']} [{_short_strat(t['strategy'])}/{t['exit_type']}] "
                f"+{t['post_exit_pct']:.1f}% ({t['days_since_exit']}일)"
            )
        if not report.get("missed"):
            lines.append("  (없음)")

        lines.append("")
        lines.append("<b>📉 매도 후 -3% 이하 가격 관측</b>")
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
                f"[+3% 이상 {v['missed']}/-3% 이하 {v['avoided']}]"
            )

        lines.extend([
            "",
            "<b>다음 확인</b>: 같은 코호트를 매도 후 5영업일 고정 시점·KODEX200 대비·"
            "비용 포함 기준으로 재측정",
        ])
        if report.get("llm_insights"):
            lines.append("<i>LLM 검증 전 가설은 위키에 저장됨 · 행동 지시로 사용하지 않음</i>")

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
