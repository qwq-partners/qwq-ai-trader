"""
QWQ AI Trader - 거래 원칙 시스템

PRISM-INSIGHT Insights 페이지에서 영감을 받아 구현.
모든 매매에 적용되는 핵심 원칙과 반복 패턴에서 추출된 장기 인사이트를 관리합니다.

두 가지 유형:
1. 핵심 불변 원칙 (Core Principles) — 수동 설정, 항상 활성
2. 경험 기반 원칙 (Learned Principles) — TradeMemory Layer 3에서 자동 생성

매주 토요일 주간 원칙 리포트 생성 → 텔레그램 전송.
"""

import json
from dataclasses import dataclass, field, asdict
from datetime import datetime, date, timedelta
from pathlib import Path
from typing import Dict, List, Optional, Any
from loguru import logger


# ============================================================
# 핵심 불변 원칙 — 모든 매매에 적용, 수정 불가
# PRISM의 "Trading Principles" + 30년 트레이딩 경험 기반
# ============================================================

CORE_PRINCIPLES = [
    # ============================================================
    # 리스크 관리 — risk/manager.py, engine.py 구현 기반
    # ============================================================
    {
        "id": "CORE-001",
        "rule": "손절은 신성하다 — 손절가 도달 시 예외 없이 즉시 청산",
        "category": "risk",
        "priority": "high",
        "scope": "universal",
        "rationale": "작은 손실 10번이 큰 손실 1번보다 낫다",
        "source": "exit_manager.py — ATR×2 동적 손절 (3.5~6.0%)",
    },
    {
        "id": "CORE-002",
        "rule": "일일 손실 한도(-5%) 초과 시 당일 매수 전면 중단",
        "category": "risk",
        "priority": "high",
        "scope": "universal",
        "rationale": "틸트 상태에서의 복구 매매는 손실을 키운다",
        "source": "risk/manager.py — _is_daily_loss_limit_hit + 스마트 사이드카",
    },
    {
        "id": "CORE-003",
        "rule": "단일 종목 비중 28% 초과 금지 — 분산이 생존이다",
        "category": "risk",
        "priority": "high",
        "scope": "universal",
        "rationale": "한 종목 실패가 포트폴리오를 무너뜨리면 안 된다",
        "source": "engine.py — max_position_pct=28%, 15R very_strong 1.3배 제한",
    },
    {
        "id": "CORE-013",
        "rule": "포트폴리오 동기화 3회 연속 실패 시 매수 전면 차단",
        "category": "risk",
        "priority": "high",
        "scope": "universal",
        "rationale": "정확한 잔고를 모르는 상태에서 매수하면 과잉 노출된다",
        "source": "risk/manager.py — set_sync_status(), _sync_fail_threshold=3",
    },

    # ============================================================
    # 진입 원칙 — cross_validator.py, theme_chasing.py, kr_scheduler.py 구현 기반
    # ============================================================
    {
        "id": "CORE-004",
        "rule": "추격 매수 금지 — ATR의 1.2배 이상 급등한 종목은 진입하지 않는다",
        "category": "entry",
        "priority": "high",
        "scope": "universal",
        "rationale": "이미 오른 종목을 쫓으면 고점에 물린다",
        "source": "kr_scheduler.py — surge_ratio > 1.2 차단, cross_validator 규칙6 (1.5x -15점)",
    },
    {
        "id": "CORE-005",
        "rule": "수급 확인 없는 테마주 진입 금지 — 기관/외국인 동시 순매도 시 즉시 차단",
        "category": "entry",
        "priority": "high",
        "scope": "theme_chasing",
        "rationale": "개인만 매수하는 테마는 단기 급등 후 급락한다",
        "source": "cross_validator.py — 규칙2 즉시 차단, theme_chasing 수급 보너스/페널티",
    },
    {
        "id": "CORE-006",
        "rule": "장초반(09:05~10:00) 급등 종목은 +4%(테마)/+5%(장중) 초과 시 진입 보류",
        "category": "entry",
        "priority": "medium",
        "scope": "universal",
        "rationale": "장초반 과열은 10시 이후 눌림이 온다",
        "source": "theme_chasing.py — max_change_pct_morning=4.0, kr_scheduler 시간대 차등",
    },
    {
        "id": "CORE-007",
        "rule": "약세장(bear)에서는 SEPA/RSI2만 진입 — 테마/갭 전략 즉시 차단",
        "category": "entry",
        "priority": "high",
        "scope": "universal",
        "rationale": "약세장에서 공격적 전략은 손절 확률이 2배",
        "source": "cross_validator.py — 규칙3 체제 부적합 즉시 차단, market_regime.py",
    },
    {
        "id": "CORE-014",
        "rule": "RSI(14) > 70인 종목의 추세 전략(SEPA) 진입 시 -10점 감점",
        "category": "entry",
        "priority": "medium",
        "scope": "sepa_trend",
        "rationale": "기술적 과매수 상태에서 추세 진입은 고점 물림 위험",
        "source": "cross_validator.py — 규칙1, theme_chasing RSI>75 차단",
    },
    {
        "id": "CORE-015",
        "rule": "MA200 하방 종목은 추세 전략 진입 시 -10점 감점",
        "category": "entry",
        "priority": "medium",
        "scope": "sepa_trend",
        "rationale": "장기 하락 추세 종목의 단기 반등은 지속력이 약하다",
        "source": "cross_validator.py — 규칙7, sepa_trend MA200 과확장 차단(+80%)",
    },
    {
        "id": "CORE-016",
        "rule": "MA200 대비 +80% 이상 과확장 종목은 SEPA 진입 차단",
        "category": "entry",
        "priority": "high",
        "scope": "sepa_trend",
        "rationale": "60일 급등 후행 추격은 고점 물림의 전형",
        "source": "sepa_trend.py — ma200_distance_pct > 80 continue",
    },
    {
        "id": "CORE-017",
        "rule": "대형주(시총 상위 20)는 테마 추종 전략에서 제외",
        "category": "entry",
        "priority": "medium",
        "scope": "theme_chasing",
        "rationale": "대형주는 테마 모멘텀이 약해서 +2~3%가 한계",
        "source": "theme_chasing.py — exclude_large_cap_symbols, _large_caps 세트",
    },

    # ============================================================
    # 청산 원칙 — exit_manager.py, kr_scheduler.py 구현 기반
    # ============================================================
    {
        "id": "CORE-008",
        "rule": "1차 익절(+5%) 시 30% 매도 → 잔량은 MA5/전일 저가 이탈 시에만 청산",
        "category": "exit",
        "priority": "medium",
        "scope": "sepa_trend",
        "rationale": "추세 종목은 기술적 지지선에서만 판단해야 수익을 극대화한다",
        "source": "exit_manager.py — 3단계 분할 익절 + composite_trailing(MA5+전일저가)",
    },
    {
        "id": "CORE-009",
        "rule": "테마주는 장마감 전(15:10) 수익률 +1% 미만이면 당일 청산",
        "category": "exit",
        "priority": "medium",
        "scope": "theme_chasing",
        "rationale": "테마주 오버나이트 갭리스크는 수익보다 크다",
        "source": "kr_scheduler.py — _check_exit_signal 15:10 theme EOD 청산",
    },
    {
        "id": "CORE-018",
        "rule": "1차 익절 후 본전보호 -1.5% 적용 — 수익 확보 후 순손실 방지",
        "category": "exit",
        "priority": "medium",
        "scope": "universal",
        "rationale": "익절했는데 결국 손실로 마감하면 심리적 타격이 크다",
        "source": "exit_manager.py — FIRST stage sell_fee_buffer=-1.5%",
    },
    {
        "id": "CORE-019",
        "rule": "테마주 최대 보유기간 3영업일 — 단기 전략은 단기로 끝낸다",
        "category": "exit",
        "priority": "medium",
        "scope": "theme_chasing",
        "rationale": "테마 모멘텀은 3일 이후 급격히 소멸한다",
        "source": "run_trader.py — theme_chasing exit_params max_holding_days=3",
    },

    # ============================================================
    # 포트폴리오/시장 원칙 — cross_validator.py, market_regime.py 구현 기반
    # ============================================================
    {
        "id": "CORE-010",
        "rule": "동일 섹터 3종목 이상 보유 금지 — 섹터 급락 시 연쇄 손절 방지",
        "category": "portfolio",
        "priority": "high",
        "scope": "universal",
        "rationale": "섹터 집중은 분산의 적이다",
        "source": "cross_validator.py — 규칙4 동일 섹터 3종목+ 즉시 차단",
    },
    {
        "id": "CORE-011",
        "rule": "적자(PER<0) + 고PBR(>5) 종목은 투기적 — 진입 시 10점 감점",
        "category": "entry",
        "priority": "medium",
        "scope": "universal",
        "rationale": "펀더멘탈 없는 급등은 급락으로 끝난다",
        "source": "cross_validator.py — 규칙8 펀더멘탈 밸류에이션 필터 (PRISM 차용)",
    },
    {
        "id": "CORE-012",
        "rule": "당일 청산 종목은 30분 쿨다운 + 눌림(-3%~+3%)/재돌파(+3%) 확인 후에만 재진입",
        "category": "entry",
        "priority": "medium",
        "scope": "universal",
        "rationale": "FOMO 재진입은 같은 실수를 반복한다",
        "source": "risk/manager.py — check_reentry_condition(), _exited_today 영속화",
    },
    {
        "id": "CORE-020",
        "rule": "08:50 장전 LLM 시장 진단이 [방어]이면 bull→sideways로 체제 하향 조정",
        "category": "market",
        "priority": "medium",
        "scope": "universal",
        "rationale": "숫자가 강세여도 뉴스/매크로가 약세면 사전 방어가 맞다",
        "source": "market_regime.py — llm_morning_diagnosis(), Perplexity+넥스트장+뉴스 연동",
    },
    {
        "id": "CORE-021",
        "rule": "고점수(85+) 매수 시그널은 비강세장에서 LLM 2차 검증 통과 필수",
        "category": "entry",
        "priority": "medium",
        "scope": "universal",
        "rationale": "점수가 높아도 맥락이 나쁘면 진입하면 안 된다",
        "source": "cross_validator.py — llm_second_check(), GPT-5.4, 하루 5회 한도",
    },
]


class TradingPrinciplesManager:
    """
    거래 원칙 관리자

    핵심 원칙(CORE) + 경험 원칙(LEARNED) 통합 관리.
    매주 토요일 주간 원칙 리포트 생성.
    """

    def __init__(self, trade_memory=None, llm_manager=None):
        self._trade_memory = trade_memory
        self._llm_manager = llm_manager
        self._cache_dir = Path.home() / ".cache" / "ai_trader" / "principles"
        self._cache_dir.mkdir(parents=True, exist_ok=True)

    def get_all_principles(self) -> Dict[str, list]:
        """핵심 원칙 + 경험 원칙 통합 반환"""
        learned = []
        if self._trade_memory:
            summary = self._trade_memory.get_summary()
            learned = summary.get("principles", [])

        return {
            "core": CORE_PRINCIPLES,
            "learned": learned,
            "total": len(CORE_PRINCIPLES) + len(learned),
        }

    async def generate_weekly_report(self) -> str:
        """
        매주 토요일 주간 원칙 리포트 생성

        1. 이번 주 거래 요약 (승패, 전략별 성과)
        2. 경험 원칙 현황 (활성, 신규, 비활성화)
        3. LLM 인사이트 (반복 패턴, 개선점)
        4. 다음 주 권고 (시장 체제 + 전략 방향)

        Returns:
            텔레그램 전송용 HTML 메시지
        """
        lines = [
            "📊 <b>주간 거래 원칙 리포트</b>",
            f"📅 {date.today().isoformat()}",
            "",
        ]

        # 1. 경험 원칙 현황
        if self._trade_memory:
            summary = self._trade_memory.get_summary()
            lines.append(f"<b>■ 거래 메모리</b>")
            lines.append(f"  L1(원시): {summary.get('layer1_count', 0)}건")
            lines.append(f"  L2(요약): {summary.get('layer2_count', 0)}건")
            lines.append(f"  L3(원칙): {summary.get('layer3_active', 0)}개 활성 / {summary.get('layer3_total', 0)}개 전체")
            lines.append("")

            # 활성 원칙 목록
            principles = summary.get("principles", [])
            if principles:
                lines.append("<b>■ 활성 경험 원칙</b>")
                for p in principles[:5]:
                    delta = p.get("delta", 0)
                    conf = p.get("confidence", 0)
                    sign = "📈" if delta > 0 else "📉" if delta < 0 else "➡️"
                    lines.append(f"  {sign} {p.get('rule', '')[:50]}")
                    lines.append(f"     신뢰도={conf:.0%}, 보정={delta:+d}점")
                lines.append("")

        # 2. LLM 주간 인사이트 (선택적)
        if self._llm_manager and self._trade_memory:
            try:
                insight = await self._generate_llm_weekly_insight()
                if insight:
                    lines.append("<b>■ AI 주간 인사이트</b>")
                    lines.append(f"  {insight}")
                    lines.append("")
            except Exception as e:
                logger.debug(f"[원칙] LLM 주간 인사이트 실패: {e}")

        # 3. 핵심 원칙 리마인더 (2개 랜덤)
        import random
        reminders = random.sample(CORE_PRINCIPLES, min(2, len(CORE_PRINCIPLES)))
        lines.append("<b>■ 이번 주 핵심 원칙 리마인더</b>")
        for r in reminders:
            lines.append(f"  💡 {r['rule']}")
        lines.append("")

        lines.append(f"<i>핵심 원칙 {len(CORE_PRINCIPLES)}개 + 경험 원칙 {summary.get('layer3_active', 0) if self._trade_memory else 0}개 운영 중</i>")

        return "\n".join(lines)

    async def _generate_llm_weekly_insight(self) -> str:
        """LLM으로 주간 인사이트 생성"""
        if not self._llm_manager or not self._trade_memory:
            return ""

        from ..utils.llm import LLMTask

        # Layer 1 + Layer 2에서 최근 데이터 수집
        recent = []
        if hasattr(self._trade_memory, '_layer1'):
            for o in self._trade_memory._layer1[-15:]:
                emoji = "✅" if o.pnl_pct > 0 else "❌"
                recent.append(
                    f"{emoji} {o.symbol} {o.strategy} {o.pnl_pct:+.1f}% "
                    f"({o.exit_type}, {o.holding_days}일, {o.market_regime})"
                )

        if len(recent) < 3:
            return ""

        prompt = (
            f"이번 주 거래 {len(recent)}건을 분석하세요:\n"
            + "\n".join(recent)
            + "\n\n다음 주에 집중해야 할 핵심 인사이트를 2줄로 작성하세요."
        )

        resp = await self._llm_manager.complete(
            prompt, task=LLMTask.TRADE_REVIEW, max_tokens=150,
        )
        if resp.success and resp.content:
            return resp.content.strip()[:200]
        return ""

    def save_report(self, report: str):
        """리포트 파일 저장"""
        try:
            path = self._cache_dir / f"weekly_{date.today().isoformat()}.txt"
            path.write_text(report, encoding="utf-8")
        except Exception as e:
            logger.error(f"[원칙] 리포트 저장 실패: {e}")
