"""
QWQ AI Trader - 크로스 전략 검증 게이트

다중 전략 신호를 교차 검증하여 맹점을 보완합니다.
각 전략이 독립적으로 시그널을 발행하는 구조에서,
전략 간 모순, 수급-기술 불일치, 시장 체제 부적합 등을 감지합니다.

PRISM-INSIGHT의 "투자전략가" 패턴을 규칙 기반으로 구현.
"""

from datetime import datetime
from pathlib import Path
from typing import Dict, Optional, List, Tuple, Any
from loguru import logger
import json


class CrossStrategyValidator:
    """
    크로스 전략 검증 게이트

    시그널이 엔진(on_signal)에 도달하기 전에 교차 검증을 수행합니다.
    규칙 기반으로 동작하므로 LLM 호출 없이 실시간 성능을 유지합니다.
    """

    # 감점 후 최소 통과 점수 (이하면 차단)
    _MIN_PASS_SCORE: int = 50

    # 2026-04-23 추가: 튜닝 가능 상수 (매직넘버 정리)
    # 지표 결손 감점: 결손 지표 1개당 -N점, 최대 -M점
    _MISSING_IND_PENALTY_STEP: int = 2   # 지표 1개당 감점
    _MISSING_IND_PENALTY_CAP: int = 8    # 최대 감점 (지표 4개 이상 결손 시 적용)

    def __init__(self, portfolio=None, risk_manager=None, trade_memory=None,
                 llm_manager=None, market: str = "KR", trade_wiki=None,
                 max_sector_positions: int = 2,
                 # 2026-04-23 추가: YAML 토글 가능 튜닝 파라미터
                 min_pass_score: Optional[int] = None,
                 missing_indicator_penalty_step: Optional[int] = None,
                 missing_indicator_penalty_cap: Optional[int] = None,
                 llm_daily_max: Optional[int] = None,
                 expert_orchestrator=None):
        self._portfolio = portfolio
        self._risk_manager = risk_manager
        self._trade_memory = trade_memory
        self._llm_manager = llm_manager  # LLM 종합 판단 (선택적)
        self._market = market  # "KR" 또는 "US"
        self._trade_wiki = trade_wiki  # 거래 위키 (교훈 컨텍스트)
        self._max_sector_positions = max_sector_positions  # 동일 섹터 최대 포지션 수 (설정 참조)
        self._expert_orchestrator = expert_orchestrator  # 2026-05-29 추가

        # 적대적 교차 검증기 (2026-08-02 추가) — Bull/Bear 역할 분리 + 멀티 LLM 합의
        # 초기화 실패해도 단일 LLM 경로로 폴백되므로 매매에 영향 없음
        self._adversarial = None
        if llm_manager is not None:
            try:
                from .adversarial_validator import get_adversarial_validator
                self._adversarial = get_adversarial_validator(llm_manager)
            except Exception as e:
                logger.warning(f"[크로스검증] 적대검증기 초기화 실패 (단일 LLM으로 진행): {e}")
        # 2026-05-29: 규칙 #11 hit log 경로 (shadow mode 효과 측정용)
        self._rule11_log_path = (
            Path.home() / ".cache" / "ai_trader" / "rule11_shadow_log.jsonl"
        )
        # 2026-08-07: 규칙 #12 (섹터 카운슬 약세) shadow hit log
        self._rule12_log_path = (
            Path.home() / ".cache" / "ai_trader" / "rule12_shadow_log.jsonl"
        )
        self._rule12_logged_today: set = set()

        # YAML 토글 적용 (None이면 클래스 상수 사용)
        if min_pass_score is not None:
            self._MIN_PASS_SCORE = int(min_pass_score)
        if missing_indicator_penalty_step is not None:
            self._MISSING_IND_PENALTY_STEP = int(missing_indicator_penalty_step)
        if missing_indicator_penalty_cap is not None:
            self._MISSING_IND_PENALTY_CAP = int(missing_indicator_penalty_cap)

        # 오늘 검증 통계
        self._stats = {
            "total": 0,
            "passed": 0,
            "blocked": 0,
            "penalized": 0,
        }
        self._stats_date = None

        # 2026-06-14 추가: 규칙 #11 hit 중복 제거용 (date, symbol) 키셋
        # 5분 주기 스크리닝으로 동일 종목·동일 날짜가 다회 기록되는 문제 차단
        self._rule11_logged_today: set = set()
        self._rule11_dedup_date = None

        # 2026-06-14 추가: gap_and_go 장막판(14:30+) 일일 진입 카운터
        # 6/12 14:09~14:57 5건 집중 → 오버나잇 risk 풀 사고 재발 방지
        self._gap_late_count: int = 0
        self._gap_late_count_date = None

        # LLM 이중 검증 일일 한도 (비용 폭발 방지)
        self._daily_llm_count: int = 0
        self._daily_llm_count_date = None
        self._daily_llm_max: int = int(llm_daily_max) if llm_daily_max is not None else 10

        # 전문가 패널 캐시 (2026-05-03 통합 — 일요일 21:00 갱신)
        # _panel_loaded_at: 마지막 로드 시각, 6시간마다 재로드
        self._panel_outlook = None
        self._panel_loaded_at: Optional[datetime] = None
        self._panel_recommended: Dict[str, Any] = {}
        self._panel_risk_factors: List[str] = []
        self._panel_regime: str = ""

    def _load_panel_outlook(self) -> None:
        """전문가 패널 결과 로드 (성공 6h, 실패/None 30min 캐시)

        파일: ~/.cache/ai_trader/strategic/strategic_outlook.json
        주 1회 갱신(일요일 21:00).

        2026-05-04 코드리뷰 P1-1: 실패/None 시 30분 단축 lock — 일요 갱신 직후
        첫 호출 실패가 6시간 stale로 이어지는 회귀 방지.
        """
        now = datetime.now()
        # 성공 후 6시간 또는 실패 후 30분 lock
        if self._panel_loaded_at is not None:
            elapsed = (now - self._panel_loaded_at).total_seconds()
            success_lock = self._panel_outlook is not None and elapsed < 6 * 3600
            fail_lock = self._panel_outlook is None and elapsed < 30 * 60
            if success_lock or fail_lock:
                return
        try:
            from ..signals.strategic.expert_panel import ExpertPanel
            panel = ExpertPanel()
            outlook = panel.load_outlook()
            if outlook:
                self._panel_outlook = outlook
                self._panel_recommended = {
                    s.symbol: s for s in (outlook.recommended_stocks or [])
                }
                self._panel_risk_factors = list(outlook.risk_factors or [])
                self._panel_regime = (outlook.market_regime or "").lower()
                logger.info(
                    f"[크로스검증] 전문가 패널 로드: 추천 {len(self._panel_recommended)}종목, "
                    f"리스크 {len(self._panel_risk_factors)}건, 레짐={self._panel_regime}"
                )
            self._panel_loaded_at = now
        except Exception as e:
            logger.debug(f"[크로스검증] 전문가 패널 로드 실패 (무시): {e}")
            self._panel_loaded_at = now

    def _combine_regime(self, llm_regime: str) -> str:
        """LLM regime + 패널 regime 보수적 결합 (둘 중 약세 우선)

        예: llm=bull, panel=neutral → neutral (보수)
            llm=neutral, panel=bullish → neutral (LLM 우선, 단기 신호)
            둘 다 같으면 그대로
        """
        llm_regime = (llm_regime or "").lower()
        panel_regime = self._panel_regime
        if not panel_regime:
            return llm_regime
        # 매핑: bullish/강세 → bull, bearish/약세 → bear, 그 외 neutral
        def _norm(r: str) -> str:
            r = r.lower()
            if "bull" in r or "강세" in r:
                return "bull"
            if "bear" in r or "약세" in r:
                return "bear"
            return "neutral"
        l_norm = _norm(llm_regime)
        p_norm = _norm(panel_regime)
        # 보수적 결합: bear 있으면 bear, 둘 다 bull이면 bull, 그 외 neutral
        if l_norm == "bear" or p_norm == "bear":
            return "trending_bear" if "trending" in llm_regime else "bear"
        if l_norm == "bull" and p_norm == "bull":
            return llm_regime if "bull" in llm_regime else "trending_bull"
        return "neutral"

    def set_portfolio(self, portfolio):
        self._portfolio = portfolio

    def set_risk_manager(self, risk_manager):
        self._risk_manager = risk_manager

    def validate(
        self,
        symbol: str,
        side: str,
        strategy: str,
        score: float,
        metadata: dict,
        market_regime: str = "neutral",
        count_stats: bool = True,
    ) -> Tuple[bool, float, str]:
        """
        시그널 교차 검증

        Args:
            symbol: 종목 코드
            side: "buy" / "sell"
            strategy: 전략명 (sepa_trend, theme_chasing, ...)
            score: 전략 점수
            metadata: 시그널 메타데이터 (indicators, atr_pct, sector 등)
            market_regime: 시장 체제 ("bull", "bear", "sideways", "neutral")
            count_stats: False면 일일 통계(_stats) 미집계 (2026-08-05 P2 —
                팀 심의 shadow 게이트 조회가 실거래 통계를 오염시키던 문제)

        Returns:
            (통과 여부, 조정된 점수, 사유)
        """
        # 통계 집계 헬퍼 — shadow 호출(count_stats=False)은 카운트하지 않는다
        def _bump(key: str) -> None:
            if count_stats:
                self._stats[key] += 1
        # 일일 통계 리셋
        today = datetime.now().date()
        if self._stats_date != today:
            self._stats = {"total": 0, "passed": 0, "blocked": 0, "penalized": 0}
            self._stats_date = today

        # 2026-06-14 추가: 규칙 #11 dedup / gap 장막판 카운터 일일 리셋
        if self._rule11_dedup_date != today:
            self._rule11_logged_today = set()
            self._rule12_logged_today = set()   # 규칙#12도 동일 주기 리셋 (2026-08-07)
            self._rule11_dedup_date = today
        if self._gap_late_count_date != today:
            self._gap_late_count = 0
            self._gap_late_count_date = today

        # gap_and_go 장막판 진입 후보 (통과 시 카운터 증가)
        _late_gap_entry = False

        _bump("total")

        # 매도 시그널은 검증 없이 통과 (청산은 항상 허용)
        if side != "buy":
            _bump("passed")
            return True, score, ""

        indicators = metadata.get("indicators") or {}
        penalties = []
        adjusted_score = score

        # 2026-04-25 추가: 진입 시간대 가드 (KR 전용)
        # 30일 데이터: 09시 -440k(승률 26.7%) / 10시 +181k / 12시 +1.08M(승률 70%)
        # → 09:00~09:29 하드 차단, 09:30~10:30 -8점 페널티, 12:30~13:00 +5 보너스
        # 청산 시그널은 시간 가드 미적용 (sell은 통과)
        if self._market == "KR" and side == "buy":
            from datetime import datetime as _dt
            now = _dt.now()
            now_hm = now.hour * 100 + now.minute  # 930 = 09:30
            # 09:00~09:29 하드 차단 (장초반 30분 모든 매수 신호 차단)
            # 단, core_holding은 별도 배치(09:30 execute)로 처리되므로 영향 없음
            if 900 <= now_hm < 930 and strategy != "core_holding":
                _bump("blocked")
                logger.info(
                    f"[크로스검증] {symbol} 차단: 09:00~09:29 장초반 변동성 회피 "
                    f"({strategy}, KR 30일 -440k 패턴)"
                )
                return False, 0, "장초반 30분 진입 차단 (09:00~09:29)"
            # 09:30~10:30 -8점 페널티 (변동성 방향 미확정 구간)
            # core_holding/strategic_swing은 배치(T+1) 전략 — 09:30 시작이 정상 동작이므로 예외.
            # 주의: strategic_swing이 미래에 장중 진입 추가 시 이 면제 재검토 필수.
            # 2026-08-04 P1: 배치 실행 시그널(metadata batch_signal — sepa/rsi2/vcp 09:30
            # T+1 집행)도 동일 근거로 면제. 기존엔 -8 + 지표결손 감점 합산으로 55~64점대
            # 배치 시그널이 계통적으로 차단·유실됐다. 장중 자동진입 sepa는 마커가 없어
            # 페널티가 그대로 적용된다 (의도).
            _is_batch = bool((metadata or {}).get("batch_signal"))
            if (930 <= now_hm < 1030 and not _is_batch
                    and strategy not in ("core_holding", "strategic_swing")):
                adjusted_score -= 8
                penalties.append("장초반 변동성 -8 (09:30~10:30)")
            # 12:30~13:00 +5 보너스 (점심 batch 추세 확정 후 진입 sweet spot)
            if 1230 <= now_hm <= 1300:
                adjusted_score += 5
                penalties.append("점심 sweet spot +5 (12:30~13:00)")

        # 2026-04-25 추가: sepa_trend 고점수 추격매수 페널티
        # 90일 데이터 역설: 80~90점 승률 31.8% / -636k, 60~70점 승률 82.4% / +937k
        # 고점수가 오히려 추격매수 함정 → 90+ -10점 페널티
        if side == "buy" and strategy == "sepa_trend" and score >= 90:
            adjusted_score -= 10
            penalties.append("SEPA 90+ 추격매수 -10")

        # 2026-04-23 추가: 지표 결손 시 보수적 감점 (risk-auditor 감사 결과)
        # atr_pct 78.6%, PER/PBR 87.7%, 수급 78.6% 결손 → R6/R7/R8/R2 사실상 비활성.
        # 단기 임시 방편: 지표 None이면 "불확실 = 의심" 원칙으로 -2점 × 결손 지표 수.
        # 2026-04-23 수정: metadata 상위 키(atr_pct, sector 등)와 indicators dict 둘 다 체크.
        #   스크리너는 top-level metadata, 전략은 indicators dict로 공급하는 구조 혼재.
        _missing_key_indicators = []
        _has_atr = (
            indicators.get("atr_pct") is not None
            or indicators.get("atr_14") is not None
            or metadata.get("atr_pct") is not None
        )
        if not _has_atr:
            _missing_key_indicators.append("ATR")
        if indicators.get("per") is None and metadata.get("per") is None:
            _missing_key_indicators.append("PER")
        if indicators.get("pbr") is None and metadata.get("pbr") is None:
            _missing_key_indicators.append("PBR")
        # 수급은 KR 전용
        if self._market == "KR":
            _has_supply = (
                indicators.get("foreign_net_buy") is not None
                or indicators.get("inst_net_buy") is not None
                or metadata.get("foreign_net_buy") is not None
                or metadata.get("inst_net_buy") is not None
            )
            if not _has_supply:
                _missing_key_indicators.append("수급")
        if _missing_key_indicators:
            _missing_penalty = min(
                len(_missing_key_indicators) * self._MISSING_IND_PENALTY_STEP,
                self._MISSING_IND_PENALTY_CAP,
            )
            adjusted_score -= _missing_penalty
            penalties.append(f"지표결손({','.join(_missing_key_indicators)}) -{_missing_penalty}")

        # === 규칙 1: 기술적 과매수 상태에서 추세 전략 매수 ===
        # 중복 감점 주의: kr_screener._apply_momentum_filter가 이미 RSI>75 시 -10, >70 시 -5 적용.
        # 여기서는 크로스검증 단계(체제/수급 종합 판단) 보정이므로 감점 규모를 -5로 축소해
        # 이중 감점 폭을 -20 → -15로 완화. bull 체제는 RSI 70~80이 모멘텀의 sweet spot이므로
        # bull에서는 감점 생략.
        rsi_14 = indicators.get("rsi_14")
        if rsi_14 is None:
            rsi_14 = indicators.get("rsi")
        if (rsi_14 is not None and rsi_14 > 70
                and market_regime != "bull"
                and strategy in ("sepa_trend", "momentum_breakout")):
            adjusted_score -= 5
            penalties.append(f"RSI과매수({rsi_14:.0f}>70) -5")

        # === 규칙 2: 기관+외국인 동시 순매도 (KR 전용 — US는 수급 데이터 없음) ===
        if self._market == "KR":
            foreign_net = indicators.get("foreign_net_buy")
            inst_net = indicators.get("inst_net_buy")
            if foreign_net is not None and inst_net is not None:
                if foreign_net < 0 and inst_net < 0:
                    if strategy in ("theme_chasing", "momentum_breakout", "gap_and_go"):
                        _bump("blocked")
                        logger.info(
                            f"[크로스검증] {symbol} 차단: {strategy} 매수 + 기관/외국인 동시 순매도"
                        )
                        return False, 0, "수급 불일치: 기관+외국인 동시 순매도"
                    # sepa_trend: 배치(T+1) 특성상 완전 차단 대신 감점 처리
                    if strategy == "sepa_trend":
                        adjusted_score -= 10
                        penalties.append(f"[규칙2] 기관+외국인 동시 순매도 — SEPA 감점 -10")

        # === 규칙 3: 약세 체제에서 공격적/역추세 전략 차단 ===
        # KR: rsi2_reversal 추가 — Connors 원전 규칙(지수 약세 시 RSI(2) 진입 금지) 준수,
        #     "칼떨어지는 칼" 진입 방지. momentum_breakout도 약세장 추격 매수 위험으로 차단.
        # US: earnings_drift는 어닝 서프라이즈 기반 → bear에서도 허용
        _bear_block = ("theme_chasing", "gap_and_go", "rsi2_reversal", "momentum_breakout")
        if self._market == "US":
            _bear_block = ("momentum_breakout",)  # US bear: 모멘텀만 차단, SEPA/어닝은 허용
        if market_regime == "bear" and strategy in _bear_block:
            _bump("blocked")
            logger.info(
                f"[크로스검증] {symbol} 차단: 약세장 + {strategy}"
            )
            return False, 0, f"체제 부적합: 약세장에서 {strategy} 차단"

        # === 규칙 3-2 (2026-05-28 P1-A): caution에서도 gap_and_go 차단 ===
        # 5/28 사고: 09:53 caution 해제 직후 gap_and_go 3건 동시 진입 → 2건 손절(-357k)
        # 변동성 큰 시점에 단기 모멘텀 진입은 트랩 빈번 → caution에서도 차단
        if (self._market == "KR"
                and market_regime == "caution"
                and strategy == "gap_and_go"):
            _bump("blocked")
            logger.info(
                f"[크로스검증] {symbol} 차단: 주의장 + gap_and_go (변동성 회피)"
            )
            return False, 0, "체제 부적합: 주의장에서 gap_and_go 차단"

        # === 규칙 3-3 (2026-06-14): 횡보장에서 sepa_trend 차단 (KR) ===
        # 6/12 SK하이닉스 -272,868원 단일 최대 손실 사고:
        # ranging 레짐에서 SEPA 추세 추종 진입 → 신호 신뢰성 급감
        # SEPA는 명확한 추세 체제(bull)에서만 작동하도록 게이트
        if (self._market == "KR"
                and market_regime in ("sideways", "neutral")
                and strategy == "sepa_trend"):
            _bump("blocked")
            logger.info(
                f"[크로스검증] {symbol} 차단: 횡보장({market_regime}) + sepa_trend "
                f"(추세 부적합, 6/12 -272k 사고 재발 방지)"
            )
            return False, 0, f"체제 부적합: 횡보장({market_regime})에서 sepa_trend 차단"

        # === 규칙 3-4 (2026-06-14): gap_and_go 장막판(14:30+) 일일 진입 캡 (KR) ===
        # 6/12 14:09~14:57 5건 집중 진입 → 다음날 오버나잇 손실 -175k 사고
        # 장 막판 30분 진입은 추세 확정 부족 + 익일 갭하락 노출 구조
        # 일 2건으로 제한 → 추가 진입은 차단
        if (self._market == "KR"
                and side == "buy"
                and strategy == "gap_and_go"):
            _now_hm = datetime.now().hour * 100 + datetime.now().minute
            if _now_hm >= 1430:
                if self._gap_late_count >= 2:
                    _bump("blocked")
                    logger.info(
                        f"[크로스검증] {symbol} 차단: gap_and_go 장막판 진입 "
                        f"{self._gap_late_count}/2 도달 (14:30+ 오버나잇 risk 캡)"
                    )
                    return False, 0, "장막판 gap_and_go 일일 한도(2건) 도달"
                # 통과 후보 — 최종 패스 시 카운터 증가
                _late_gap_entry = True

        # === 규칙 4: 동일 섹터 과집중 ===
        sector = metadata.get("sector")
        if sector and self._portfolio:
            same_sector_count = sum(
                1 for p in self._portfolio.positions.values()
                if getattr(p, 'sector', None) == sector
            )
            if same_sector_count >= self._max_sector_positions:
                _bump("blocked")
                logger.info(
                    f"[크로스검증] {symbol} 차단: 섹터 과집중 ({sector}: {same_sector_count}종목)"
                )
                return False, 0, f"섹터 집중: {sector} {same_sector_count}종목 보유 중"

        # === 규칙 5: 당일 손절 종목과 동일 섹터 재진입 경고 ===
        # 손절 종목의 섹터 정보를 직접 비교 (섹터 미확인 시 스킵)
        if sector and self._risk_manager:
            exited = getattr(self._risk_manager, '_exited_today', {})
            for sl_symbol, sl_info in exited.items():
                sl_sector = sl_info.get("sector") if isinstance(sl_info, dict) else None
                if sl_sector and sl_sector == sector and sl_symbol != symbol:
                    adjusted_score -= 5
                    penalties.append(f"동일섹터({sector}) 손절종목 존재 -5")
                    break

        # === 규칙 5-2 (2026-04-25 추가): 외국인 5일 누적 매수 상위 섹터 보너스 ===
        # supply_score_provider 또는 supply_daily_*.json 5일 누적 합계 기준,
        # 외국인 매수 강한 섹터 종목에 +5 overlay (수급 모멘텀 추적)
        if sector and self._market == "KR":
            top_sectors = metadata.get("foreign_top_sectors") or []
            if isinstance(top_sectors, (list, tuple)) and sector in top_sectors:
                adjusted_score += 5
                penalties.append(f"외국인 매수 상위 섹터({sector}) +5")

        # === 규칙 6: ATR 대비 등락률 과다 (추격 매수 감지) ===
        atr_pct = metadata.get("atr_pct")
        if atr_pct is None:
            atr_pct = indicators.get("atr_14")
        rt_change = indicators.get("change_1d")
        if rt_change is None:
            rt_change = indicators.get("change_pct")
        if rt_change is None:
            rt_change = indicators.get("rt_change_pct")
        if atr_pct is not None and atr_pct > 0 and rt_change is not None and rt_change > 0:
            surge_ratio = rt_change / atr_pct
            if surge_ratio > 1.5:
                adjusted_score -= 15
                penalties.append(f"추격매수(등락/ATR={surge_ratio:.1f}x) -15")

        # === 규칙 7: MA200 하방에서 추세 추종 (SEPA/테마) ===
        # 중복 감점 주의: swing_screener._base_technical_score에서도 MA200 상방이 보너스 조건.
        # SEPA 후보가 스크리너에서 이미 감점된 상태로 올라오므로 여기서는 -5로 축소해
        # 중복 폭을 -20 → -15로 완화.
        ma200 = indicators.get("ma200")
        close = indicators.get("close")
        if ma200 is not None and close is not None and ma200 > 0:
            if close < ma200 and strategy in ("sepa_trend", "theme_chasing"):
                adjusted_score -= 5
                penalties.append(f"MA200하방(-{(1-close/ma200)*100:.1f}%) -5")

        # === 규칙 8: 펀더멘탈 밸류에이션 필터 (PRISM 차용) ===
        # PER 극단 고평가 또는 적자 + 고PBR → 추격 매수 위험
        per = indicators.get("per")
        pbr = indicators.get("pbr")
        if per is not None and pbr is not None:
            # 적자(PER<0) + 고PBR(>5) = 투기적 고평가
            if per < 0 and pbr > 5:
                adjusted_score -= 10
                penalties.append(f"적자+고PBR({pbr:.1f}) -10")
            # PER > 50 = 극단 고평가 (성장주 프리미엄 감안해도 과도)
            elif per > 50:
                adjusted_score -= 5
                penalties.append(f"극단PER({per:.0f}) -5")

        # === 규칙 9: 거래 메모리 기반 점수 보정 ===
        if self._trade_memory:
            memory_adj = self._trade_memory.get_score_adjustment(strategy, sector or "")
            if memory_adj != 0:
                adjusted_score += memory_adj
                penalties.append(f"메모리보정({memory_adj:+d})")

        # === 규칙 10: 전문가 패널 추천 보너스 (2026-05-03 P0 통합) ===
        # 일요일 21:00 갱신, 14일 이내 신선도 가중. 모든 전략에 일관 적용.
        # 2026-05-04 코드리뷰 P1 반영:
        # - side==BUY일 때만 적용 (매도 점수 부풀림 방지)
        # - 21일 초과 시 폐기 (보너스 0)
        # - freshness < 0.5 시 보너스 0 (stale 패널 영향 차단)
        is_buy_signal = str(side).upper().endswith("BUY")
        if is_buy_signal:
            self._load_panel_outlook()
            if symbol in self._panel_recommended:
                try:
                    pick = self._panel_recommended[symbol]
                    # 신선도 계산
                    freshness = 1.0
                    days_old = 0
                    if self._panel_outlook:
                        created = datetime.fromisoformat(self._panel_outlook.created_at)
                        days_old = (datetime.now() - created).days
                        freshness = max(0.3, 1.0 - days_old / 14.0)
                    # 21일 초과 패널은 폐기 (시장 상황 변화 가능)
                    if days_old > 21:
                        logger.debug(
                            f"[크로스검증] 패널 {days_old}일 경과 — 보너스 미적용 ({symbol})"
                        )
                    elif freshness < 0.5:
                        logger.debug(
                            f"[크로스검증] 패널 신선도 {freshness:.0%} < 50% — 보너스 미적용 ({symbol})"
                        )
                    else:
                        conv = float(getattr(pick, "conviction", 0.5))
                        bonus = max(2, int(conv * 10 * freshness))
                        adjusted_score += bonus
                        penalties.append(
                            f"전문가패널 추천(+{bonus} conv={conv:.0%}/신선도{freshness:.0%})"
                        )
                except Exception as _e:
                    logger.debug(f"[크로스검증] 패널 보너스 계산 실패 {symbol}: {_e}")

        # === 규칙 11: 전문가 BEAR 합의 게이트 (2026-05-29 추가) ===
        # Shadow mode: shadow_mode=true 일 때 차단하지 않고 로그만 기록.
        # 1주일 운영 후 hit rate 확인하고 false로 전환.
        if is_buy_signal and self._expert_orchestrator is not None:
            try:
                snap = self._expert_orchestrator.snapshot()
                valid_n = sum(1 for o in snap.values() if o.is_valid)
                _exp_cfg = getattr(self._expert_orchestrator, "config", None)
                _shadow = getattr(_exp_cfg, "shadow_mode", True)

                if valid_n >= 4 and self._expert_orchestrator.bear_consensus(
                    threshold_confidence=0.7, min_count=2, opinions=snap
                ):
                    # hit_log 영속화 (실제 후속 가격으로 hit rate 측정)
                    self._log_rule11_hit(symbol, snap, score)

                    if _shadow:
                        logger.info(
                            f"[크로스검증] {symbol} SHADOW: 전문가 BEAR 합의 감지 "
                            f"(차단 안 함, 유효 {valid_n}명)"
                        )
                        # 통과 — 점수만 정상 반환
                    else:
                        _bump("blocked")
                        logger.info(
                            f"[크로스검증] {symbol} 차단: 전문가 BEAR 합의 "
                            f"(규칙#11, 유효 의견 {valid_n}명)"
                        )
                        return False, adjusted_score, "전문가 BEAR 합의 — 신규매수 차단"
            except Exception as _e:
                _exp_cfg = getattr(self._expert_orchestrator, "config", None)
                _fail_open = getattr(_exp_cfg, "fail_open", True)
                logger.debug(
                    f"[크로스검증] 규칙#11 평가 실패 {symbol}: {_e} (fail_open={_fail_open})"
                )
                if not _fail_open:
                    _bump("blocked")
                    return False, adjusted_score, "전문가 시스템 평가 실패 (fail_closed)"

        # === 규칙 12: 섹터 카운슬 약세 게이트 (2026-08-07 추가, shadow 전용) ===
        # 종목 소속 섹터의 카운슬 점수가 강한 약세면 감지만 기록 (차단 없음).
        # 규칙#11과 동일 절차 — hit rate 관측 후 실차단 전환 여부 결정.
        if is_buy_signal and self._expert_orchestrator is not None:
            try:
                from src.experts.sector_council import SECTOR_BEAR_THRESHOLD
                _sc = self._expert_orchestrator.agents.get("sector_council")
                if _sc is not None:
                    # 규칙 4에서 이미 해석한 sector 재사용, 없을 때만 캐시 조회
                    _sector = sector or _sc.sector_of_cached(
                        symbol,
                        (metadata or {}).get("name", "")
                        or (metadata or {}).get("stock_name", ""),
                    )
                    _s_score = _sc.sector_score(_sector) if _sector else None
                    if _s_score is not None and _s_score <= SECTOR_BEAR_THRESHOLD:
                        self._log_rule12_hit(symbol, _sector, _s_score, score)
                        logger.info(
                            f"[크로스검증] {symbol} SHADOW: 섹터 약세 감지 "
                            f"(규칙#12, {_sector} {_s_score:+d}, 차단 안 함)"
                        )
            except Exception as _e:
                logger.debug(f"[크로스검증] 규칙#12 평가 실패 {symbol}: {_e}")

        # === 누적 감점 cap (2026-05-03 P0-1) ===
        # 시간대 -8 + 지표결손 -8 + MA200 -5 + 극단PER -5 = 최대 -26 누적 가능
        # 60-70점대 종목이 자동 차단되는 역설(이전 분석: 91.7% 승률 영역) 방지
        # 추격매수(-15)/RSI과매수(-5)/적자+고PBR(-10)는 hard block 의도라 캡 예외
        TOTAL_PENALTY_CAP = 15
        _HARD_BLOCK_TAGS = ("추격매수", "RSI과매수", "적자+고PBR")
        has_hard_block = any(any(tag in p for tag in _HARD_BLOCK_TAGS) for p in penalties)
        if not has_hard_block:
            total_penalty = score - adjusted_score
            if total_penalty > TOTAL_PENALTY_CAP:
                penalties.append(
                    f"누적감점캡({total_penalty:.0f}→{TOTAL_PENALTY_CAP})"
                )
                adjusted_score = score - TOTAL_PENALTY_CAP

        # 감점 적용 결과
        if penalties:
            _bump("penalized")
            penalty_str = ", ".join(penalties)
            logger.info(
                f"[크로스검증] {symbol} 감점: {score:.0f}→{adjusted_score:.0f} ({penalty_str})"
            )

        # 감점 후 최소 점수 미달이면 차단
        if adjusted_score < self._MIN_PASS_SCORE:
            _bump("blocked")
            logger.info(
                f"[크로스검증] {symbol} 차단: 감점 후 {adjusted_score:.0f} < {self._MIN_PASS_SCORE}"
            )
            return False, adjusted_score, f"크로스 감점 후 점수 부족 ({adjusted_score:.0f})"

        # 2026-06-14: 장막판 gap_and_go 통과 시 일일 카운터 증가 (규칙 3-4)
        if _late_gap_entry:
            self._gap_late_count += 1
            logger.debug(
                f"[크로스검증] gap_and_go 장막판 카운터: {self._gap_late_count}/2"
            )

        _bump("passed")
        return True, adjusted_score, ""

    def _log_rule11_hit(
        self,
        symbol: str,
        snap: Dict[str, Any],
        score: float,
    ) -> None:
        """규칙 #11 BEAR 합의 감지 시 hit_log에 기록

        Shadow mode 운영 중에는 차단하지 않으므로 실제 후속 가격으로
        효과를 측정해야 한다. 다음 정보를 jsonl로 영속화:
        - timestamp, symbol, strategy_score
        - 각 전문가의 bias/score/confidence
        - 후처리(주간 분석) 시 +N일 가격 변동으로 hit rate 계산
        """
        # 2026-06-14: (date, symbol) 중복 기록 방지
        # 5분 주기 스크리닝으로 동일 종목이 1일 다회 hit되어 통계 왜곡
        # → 같은 날 같은 종목은 첫 hit만 기록
        today = datetime.now().date()
        dedup_key = (today, symbol)
        if dedup_key in self._rule11_logged_today:
            return

        try:
            self._rule11_log_path.parent.mkdir(parents=True, exist_ok=True)
            record = {
                "timestamp": datetime.now().isoformat(),
                "symbol": symbol,
                "market": self._market,
                "score": float(score),
                "experts": {
                    name: {
                        "bias": op.regime_bias.value,
                        "score": op.score,
                        "confidence": round(op.confidence, 3),
                    }
                    for name, op in snap.items()
                    if op.is_valid
                },
            }
            with self._rule11_log_path.open("a", encoding="utf-8") as f:
                f.write(json.dumps(record, ensure_ascii=False) + "\n")
            # dedup 마킹 (성공 시에만)
            self._rule11_logged_today.add(dedup_key)
        except Exception as e:
            logger.debug(f"[크로스검증] hit_log 기록 실패: {e}")

    def _log_rule12_hit(
        self,
        symbol: str,
        sector: str,
        sector_score: int,
        score: float,
    ) -> None:
        """규칙 #12 섹터 약세 감지 시 hit_log 기록 (규칙#11과 동일 방식)

        shadow 운영 — 후속 가격 변동으로 hit rate를 측정해 실차단 전환을 판단한다.
        같은 날 같은 종목은 첫 hit만 기록.
        """
        dedup_key = (datetime.now().date(), symbol)
        if dedup_key in self._rule12_logged_today:
            return
        try:
            self._rule12_log_path.parent.mkdir(parents=True, exist_ok=True)
            record = {
                "timestamp": datetime.now().isoformat(),
                "symbol": symbol,
                "market": self._market,
                "sector": sector,
                "sector_score": int(sector_score),
                "score": float(score),
            }
            with self._rule12_log_path.open("a", encoding="utf-8") as f:
                f.write(json.dumps(record, ensure_ascii=False) + "\n")
            self._rule12_logged_today.add(dedup_key)
        except Exception as e:
            logger.debug(f"[크로스검증] 규칙#12 hit_log 기록 실패: {e}")

    async def llm_second_check(
        self,
        symbol: str,
        strategy: str,
        score: float,
        indicators: dict,
        market_regime: str,
        sector: str = "",
    ) -> bool:
        """
        LLM 종합 판단 — 고점수(85+) + 비강세장에서만 호출 (PRISM 차용)

        비용 최소화: 하루 최대 10회 (_daily_llm_max), 거부 시 주문 차단.
        실시간 성능: 타임아웃 10초, 실패 시 통과(fail-open).

        fail-open 정책 의도: LLM 장애나 한도 소진 시 매수 차단보다 기회 손실 방지를 우선.
        규칙1~9는 이미 결정론적 게이트로 작동하므로 LLM은 추가 안전장치 역할.
        엄격 모드가 필요하면 return True → return False 로 변경 (정책 결정 사안).
        """
        if not self._llm_manager:
            return True

        # 강세장이면 LLM 검증 생략 (속도 우선)
        if market_regime == "bull":
            return True

        # 고점수 시그널만 검증
        if score < 85:
            return True

        # 일일 한도 체크 (비용 제어)
        today = datetime.now().date()
        if self._daily_llm_count_date != today:
            self._daily_llm_count = 0
            self._daily_llm_count_date = today
        if self._daily_llm_count >= self._daily_llm_max:
            # 2026-04-23 수정: 한도 소진 시 경고 로그 승격 + 통과 유지
            # 기존엔 debug 로그라 LLM 보호 상실 인지 지연 → warning으로 즉시 알림
            # 상위 호출자(engine.py:1389)가 변동성 큰 날 본 로그를 보고 수동 조치 가능
            logger.warning(
                f"[크로스검증] LLM 이중검증 일일 한도 소진 "
                f"({self._daily_llm_max}회) → fail-open 통과. "
                f"고변동 날엔 수동 모니터링 필요."
            )
            return True
        self._daily_llm_count += 1
        logger.info(
            f"[크로스검증] LLM 이중검증 #{self._daily_llm_count}/{self._daily_llm_max}: "
            f"{symbol} 점수={score:.0f} 체제={market_regime}"
        )

        try:
            import asyncio
            # 거래 메모리 컨텍스트 (최근 유사 패턴)
            mem_context = ""
            if self._trade_memory and hasattr(self._trade_memory, 'get_context_for_signal'):
                mem_context = self._trade_memory.get_context_for_signal(strategy, sector)

            # 위키 컨텍스트 (전략/섹터/체제별 축적 교훈)
            wiki_context = ""
            if self._trade_wiki:
                wiki_context = self._trade_wiki.query(strategy, sector, market_regime)

            # 종목 노트 (2026-08-07 — 이 종목의 과거 거래 교훈 + 최근 리서치)
            symbol_wiki = ""
            if self._trade_wiki and hasattr(self._trade_wiki, "query_symbol"):
                symbol_wiki = self._trade_wiki.query_symbol(symbol)

            # 패널 risk_factors 컨텍스트 (2026-05-03 P1 통합, 5/4 토큰 cap 완화)
            self._load_panel_outlook()
            panel_risks = ""
            if self._panel_risk_factors:
                # 상위 5건, 각 250자 (이전 3×120 → 5×250 약 2배 확대)
                top_risks = self._panel_risk_factors[:5]
                panel_risks = " | ".join(r[:250] for r in top_risks)
            panel_combined_regime = self._combine_regime(market_regime)

            risk_guide = ""
            if panel_risks:
                risk_guide = "위 매크로 리스크가 해당 종목/전략에 직접 영향 가능 시 보수적으로 NO. "

            prompt = (
                f"종목 {symbol}, 전략 {strategy}, 점수 {score:.0f}.\n"
                f"시장 체제: {market_regime} (LLM+패널 결합={panel_combined_regime})."
                + (f" 섹터: {sector}." if sector else "") + "\n"
                f"지표: RSI={indicators.get('rsi_14', 'N/A')}, "
                f"ATR={indicators.get('atr_14', 'N/A')}%, "
                f"MA200거리={indicators.get('ma200_distance_pct', 'N/A')}%, "
                f"PER={indicators.get('per', 'N/A')}, "
                f"수급={'+' if (indicators.get('foreign_net_buy') if indicators.get('foreign_net_buy') is not None else 0) > 0 else '-'}.\n"
                + (f"최근 유사 거래 기억: {mem_context}\n" if mem_context else "")
                + (f"위키 교훈: {wiki_context}\n" if wiki_context else "")
                + (f"종목 노트 (과거 거래·최근 리서치):\n{symbol_wiki}\n" if symbol_wiki else "")
                + (f"주간 매크로 리스크 (전문가 패널): {panel_risks}\n" if panel_risks else "")
                + "\n이 매수 시그널을 승인하시겠습니까? "
                + risk_guide
                + "YES 또는 NO로 답하고, 한 줄 사유를 적어주세요."
            )
            # 적대적 교차 검증 (2026-08-02~)
            #   Bull(OpenAI) / Bear(Gemini)에게 서로 다른 역할을 주고 합의를 본다.
            #   Bear는 "문제 없음" 답변이 금지되어 반증 책임을 진다 → 확증 편향 완화.
            #   실패 시 아래 단일 LLM 경로로 폴백한다.
            if self._adversarial is not None:
                extra_ctx = "\n".join(filter(None, [
                    f"최근 유사 거래 기억: {mem_context}" if mem_context else "",
                    f"위키 교훈: {wiki_context}" if wiki_context else "",
                    f"주간 매크로 리스크: {panel_risks}" if panel_risks else "",
                ]))
                adv = await self._adversarial.validate(
                    symbol=symbol, strategy=strategy, score=score,
                    indicators=indicators, market_regime=market_regime,
                    sector=sector, extra_context=extra_ctx,
                )
                # 적대검증은 Bull+Bear로 LLM을 2회 호출한다.
                # 위에서 이미 1회를 세었으므로 1회를 더 반영해야 한도가 실제 호출 수와 맞는다.
                self._daily_llm_count += 1
                if not adv.failed:
                    if not adv.approved:
                        logger.info(f"[크로스검증] 적대검증 거부: {symbol} — {adv.reason}")
                    return adv.approved
                # adv.failed면 폴백 (아래 단일 LLM 경로)

            # 단일 LLM 폴백 — GPT-5.4 (STRATEGY_ANALYSIS)
            # 2026-08-08 P2: 적대검증 실패 후 폴백도 실호출 1회로 계수 (한도 정합).
            # 적대검증기 자체가 없으면(752행에서 이미 계수) 추가 계수 금지 —
            # 이중 계수로 한도가 절반이 되던 문제 (최종 리뷰 P2)
            if self._adversarial is not None:
                self._daily_llm_count += 1
            from ..utils.llm import LLMTask
            resp = await asyncio.wait_for(
                self._llm_manager.complete(prompt, task=LLMTask.STRATEGY_ANALYSIS, max_tokens=100),
                timeout=10.0,
            )
            if not resp.success:
                logger.debug(f"[크로스검증] LLM 응답 실패 (통과): {resp.error}")
                return True  # fail-open
            content = (resp.content or "").strip()
            if content and "NO" in content.upper()[:10]:
                logger.info(f"[크로스검증] LLM 거부: {symbol} — {content[:80]}")
                return False
            return True
        except Exception as e:
            logger.debug(f"[크로스검증] LLM 검증 실패 (통과): {e}")
            return True  # fail-open

    def get_stats(self) -> Dict:
        """오늘 검증 통계"""
        return dict(self._stats)
