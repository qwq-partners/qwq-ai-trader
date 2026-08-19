"""
QWQ AI Trader - 거래 원칙 시스템

PRISM-INSIGHT Insights 페이지에서 영감을 받아 구현.
모든 매매에 적용되는 핵심 원칙과 반복 패턴에서 추출된 장기 인사이트를 관리합니다.

두 가지 유형:
1. 핵심 원칙 (Core Principles) — 수동 관리, 항상 활성.
   **불변이 아니다.** 전략이 바뀌면 함께 갱신한다 (CORE_PRINCIPLES 위 주석 참조).
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
# 핵심 원칙 — 모든 매매에 적용
#
# ⚠️ "불변"이 아니다. 전략이 바뀌면 원칙도 바뀌어야 한다.
#    원칙은 현재 시스템이 실제로 하는 일의 서술이지, 지켜야 할 이상이 아니다.
#    코드와 어긋난 원칙은 지침이 아니라 오해의 근원이 된다.
#
# 갱신 규칙:
#   - 전략을 폐지하면 그 전략 전용 원칙도 함께 삭제한다 (죽은 원칙을 남기지 않는다)
#   - 파라미터를 바꾸면 원칙의 수치와 `source`를 같이 고친다
#   - **삭제된 ID는 재사용하지 않는다** — 이력 추적을 위해 번호를 비워 둔다
#
# 2026-08-03 전면 개정 (Codex gpt-5.6-sol 교차 검증 반영):
#   삭제 CORE-005/009/017/019 — theme_chasing 폐지(2026-05-04)로 적용 대상 소멸
#   수정 CORE-001/002/004/006/007/008/010/012/013/014/015/018/021 —
#        수치 오류(본전보호 -1.5→-0.5, 감점 -10→-5, 재진입 ±3→±5%)와
#        과장된 서술("예외 없이", "전면 중단", "SEPA만", "필수") 정정
#   신규 CORE-022~028 — 킬스위치·exit_exempt·팀 심의·배분기·진화 게이트
#   신규 CORE-029~032 — 노출 한도(코드엔 있었으나 원칙에 빠져 있던 것)
#
# 이번 개정의 교훈: 원칙이 코드보다 **강하게** 쓰여 있으면(예외를 안 적으면)
# 그 자체가 오해를 만든다. fail-open 지점은 fail-open이라고 적어야 한다.
# ============================================================

CORE_PRINCIPLES = [
    # ============================================================
    # 리스크 관리 — risk/manager.py, engine.py 구현 기반
    # ============================================================
    {
        "id": "CORE-001",
        "rule": "손절가 도달 시 즉시 청산 — 예외는 exit_exempt 종목과 KILL_SWITCH_ALL뿐",
        "category": "risk",
        "priority": "high",
        "scope": "universal",
        "rationale": "작은 손실 10번이 큰 손실 1번보다 낫다. 두 예외는 사람이 명시적으로 지정한 경우이며, 그만큼 하락 노출은 수동 책임이다",
        "source": "exit_manager.py — ATR×2 동적 손절 (4.0~8.0%, evolved_overrides)",
    },
    {
        "id": "CORE-002",
        "rule": "일일 손실 -5% 초과 시 매수 차단 — 시장 회복세일 때만 방어 전략(core_holding·SEPA) 허용, -12.5% 이하 전면 중단",
        "category": "risk",
        "priority": "high",
        "scope": "KR",
        "rationale": "틸트 상태에서의 복구 매매는 손실을 키운다",
        "source": "risk/manager.py _is_sidecar_blocked — warn(-5%)~hard_stop(-12.5%) 구간은 trend['recovering']이 참일 때만 defensive_strategies 허용. 하락세이거나 추세 정보가 없으면 이 구간도 전면 차단이다. hard_stop 이하는 무조건 차단",
    },
    {
        "id": "CORE-003",
        "rule": "단일 종목 비중 28% 초과 금지 — 분산이 생존이다",
        "category": "risk",
        "priority": "high",
        "scope": "KR",
        "rationale": "한 종목 실패가 포트폴리오를 무너뜨리면 안 된다",
        "source": "engine.py — max_position_pct=28%, 15R very_strong 1.3배 제한",
    },
    {
        "id": "CORE-013",
        "rule": "포트폴리오 동기화 3회 연속 실패 시 매수 차단 — 단 10분 경과하면 경고와 함께 강제 해제",
        "category": "risk",
        "priority": "high",
        "scope": "universal",
        "rationale": "정확한 잔고를 모르는 상태에서 매수하면 과잉 노출된다. 다만 동기화가 영영 회복되지 않을 때 매매가 무한정 멈추는 것도 위험해 10분 상한을 뒀다",
        "source": "risk/manager.py — set_sync_status(), _sync_fail_threshold=3, 10분 후 강제 해제",
    },

    # ============================================================
    # 진입 원칙 — cross_validator.py, sepa_trend.py, kr_scheduler.py 구현 기반
    # ============================================================
    {
        "id": "CORE-004",
        "rule": "추격 매수 금지 — KR 자동진입은 ATR 1.2배 초과 급등 시 차단, 크로스검증은 1.5배 초과 시 -15점 감점(KR·US 공통)",
        "category": "entry",
        "priority": "high",
        "scope": "KR",
        "rationale": "이미 오른 종목을 쫓으면 고점에 물린다",
        "source": "kr_scheduler.py surge_ratio > 1.2 진입 차단(KR 전용) / cross_validator 규칙6은 1.5배 초과 시 감점(-15)이며 차단은 아니다(KR·US 공통). US에는 1.2배 하드 차단이 없다",
    },
    {
        "id": "CORE-006",
        "rule": "10:00 이전 +5% 초과 급등 후보는 진입 보류 — 별도로 09:00~09:29는 전면 차단",
        "category": "entry",
        "priority": "medium",
        "scope": "KR",
        "rationale": "장초반 과열은 10시 이후 눌림이 온다",
        "source": "kr_scheduler.py — 10시 이전 +5% 상한 필터 / cross_validator — 09:00~09:29 하드 차단, 09:30~10:30 -8점 (별개 정책)",
    },
    {
        "id": "CORE-007",
        "rule": "약세장(bear)에서는 gap_and_go를 차단한다 (차단 목록 방식)",
        "category": "entry",
        "priority": "high",
        "scope": "KR",
        "rationale": "약세장에서 공격적·역추세 전략은 손절 확률이 2배. ⚠️ 허용목록이 아니라 차단목록이라, 목록에 없는 전략(core_holding·strategic_swing·team)은 bear에서도 통과한다. 폐지된 rsi2_reversal·momentum_breakout·theme_chasing도 목록에 남아 있다",
        "source": "cross_validator.py 규칙3 — KR bear 차단 대상 {theme_chasing, gap_and_go, rsi2_reversal, momentum_breakout}",
    },
    {
        "id": "CORE-014",
        "rule": "비강세장에서 RSI(14) > 70인 SEPA 진입은 -5점 감점",
        "category": "entry",
        "priority": "medium",
        "scope": "sepa_trend",
        "rationale": "기술적 과매수 상태에서 추세 진입은 고점 물림 위험",
        "source": "cross_validator.py 규칙1 — bull에서는 감점 없음. kr_screener가 이미 RSI>75 -10 / >70 -5를 적용해 중복 방지로 -5로 축소",
    },
    {
        "id": "CORE-015",
        "rule": "MA200 하방 종목은 추세 전략 진입 시 -5점 감점",
        "category": "entry",
        "priority": "medium",
        "scope": "sepa_trend",
        "rationale": "장기 하락 추세 종목의 단기 반등은 지속력이 약하다",
        "source": "cross_validator.py 규칙7 (-5점). swing_screener에서 이미 반영돼 중복 방지로 축소. 과확장(+80%) 차단은 CORE-016 별건",
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

    # ============================================================
    # 청산 원칙 — exit_manager.py, kr_scheduler.py 구현 기반
    # ============================================================
    {
        "id": "CORE-008",
        "rule": "1차 익절(+10%) 시 10% 매도 → 잔량은 MA5/전일저가·본전보호·ATR 트레일링·보유기간 규칙으로 관리",
        "category": "exit",
        "priority": "medium",
        "scope": "sepa_trend",
        "rationale": "추세 종목은 기술적 지지선에서만 판단해야 수익을 극대화한다. 기존 +5%/30%는 평균 1.9일 만에 발동해 추세 초입을 잘라냈다(익절 +5.2% < 손절 -6.2%)",
        "source": "config exit_manager first_exit_pct=10.0/ratio=0.1 (2026-08-02 백테스트 검증). 2026-08-03 run_trader.py의 sepa_trend 오버라이드(+5%/20%)를 제거해 이 값이 실제로 적용된다. composite_trailing(MA5+전일저가)",
    },
    {
        "id": "CORE-018",
        "rule": "1차 익절 후 본전보호 -0.5% 적용 (코어 -2.0%) — 수익 확보 후 순손실 방지",
        "category": "exit",
        "priority": "medium",
        "scope": "universal",
        "rationale": "익절했는데 결국 손실로 마감하면 심리적 타격이 크다",
        "source": "exit_manager.py — FIRST/SECOND sell_fee_buffer=-0.5, is_core=-2.0, THIRD/TRAILING은 수수료 보호(KR 0.25)",
    },

    # ============================================================
    # 포트폴리오/시장 원칙 — cross_validator.py, market_regime.py 구현 기반
    # ============================================================
    {
        "id": "CORE-010",
        "rule": "동일 섹터 최대 2종목 — 3번째부터 차단, 섹터 급락 시 연쇄 손절 방지",
        "category": "portfolio",
        "priority": "high",
        "scope": "KR",
        "rationale": "섹터 집중은 분산의 적이다",
        "source": "cross_validator.py 규칙4 (max_sector_positions=2, 보유분 기준) + agents/allocator.py (동시 승인분까지 합산)",
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
        "rule": "당일 청산 종목은 30분 쿨다운 + 눌림(-5%~+5%)/재돌파(+5% 초과) 확인 후에만 재진입",
        "category": "entry",
        "priority": "medium",
        "scope": "KR",
        "rationale": "FOMO 재진입은 같은 실수를 반복한다",
        "source": "risk/manager.py — check_reentry_condition(). 2026-05-02 ±3%→±5% 완화 (후속복기: stop_loss 24건 중 17건 V자 반등)",
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
        "rule": "고점수(85+) 매수 시그널은 비강세장에서 LLM 2차 검증을 거친다 (장애·한도 소진 시 fail-open 통과)",
        "category": "entry",
        "priority": "medium",
        "scope": "universal",
        "rationale": "점수가 높아도 맥락이 나쁘면 진입하면 안 된다. 다만 LLM 장애로 매매가 멈추는 쪽이 더 해로워 fail-open이다 — 결정론적 게이트 11규칙이 앞단에 있으므로 검증 공백을 그쪽이 메운다",
        "source": "cross_validator.py — llm_second_check() → adversarial_validator (Bull=OpenAI/Bear=Gemini), 하루 10회 한도(_daily_llm_max)",
    },

    # ============================================================
    # 안전장치 — 2026-08 신설 (킬스위치·청산 면제)
    # ============================================================
    {
        "id": "CORE-022",
        "rule": "KILL_SWITCH는 신규 매수만 차단(청산 허용), KILL_SWITCH_ALL은 모든 주문 차단 — 봇 상태와 무관하게 최우선",
        "category": "risk",
        "priority": "high",
        "scope": "universal",
        "rationale": "엔진이 오작동 중일 때도 사람이 즉시 멈출 수단이 있어야 한다. "
                     "재시작을 기다리는 동안 주문이 나가면 이미 늦다",
        "source": "risk/kill_switch.py — 모든 주문이 통과하는 브로커 계층에서 검사 "
                  "(KILL_SWITCH=매수차단/청산허용, KILL_SWITCH_ALL=전면동결)",
    },
    {
        "id": "CORE-023",
        "rule": "exit_exempt 종목은 어떤 자동 청산도 하지 않는다 — 손절·트레일링·팀 판단 모두 무효",
        "category": "exit",
        "priority": "high",
        "scope": "universal",
        "rationale": "사용자가 명시적으로 지정한 장기 보유는 시스템 판단보다 우선한다. "
                     "다만 손절이 없다는 뜻이므로 하락 노출은 전적으로 수동 책임이다",
        "source": "config kr.no_auto_exit_symbols → exit_manager.add_exit_exempt(), "
                  "7개 청산 경로 + agents/portfolio_manager.py SELL 무효화",
    },

    # ============================================================
    # 팀 심의 — 2026-08 신설 (shadow 단계)
    # ============================================================
    {
        "id": "CORE-024",
        "rule": "근거가 낡았거나 부족하면 신규 매수를 하지 않는다 (fail-closed)",
        "category": "entry",
        "priority": "high",
        "scope": "team",
        "rationale": "가중평균은 상대 비중만 보므로 모든 근거가 함께 낡으면 감쇠가 상쇄된다 "
                     "(실측: 전부 신선 +62 / 전부 4시간 전 +62로 동일했다). "
                     "정보가 없다고 팔 이유는 없지만, 정보 없이 살 이유도 없다",
        "source": "agents/analysts.py — HARD_TTL_MIN(기술 45분/수급·뉴스 180분), "
                  "evidence_quality(유효 소스≥2, 가중치 합≥0.5)",
    },
    {
        "id": "CORE-025",
        "rule": "Bull/Bear 토론이 실패하면 신규 매수를 하지 않는다",
        "category": "entry",
        "priority": "medium",
        "scope": "team",
        "rationale": "토론은 반대 논거를 강제로 생성시키는 검증 계층이다. "
                     "검증이 성립하지 않았는데 매수를 허용하면 계층을 둔 의미가 없다",
        "source": "agents/trader.py — debate_ok 아니면 BUY→HOLD",
    },
    {
        "id": "CORE-026",
        "rule": "통계로 검증된 리스크 게이트를 LLM 합의만으로 뒤집지 않는다",
        "category": "risk",
        "priority": "high",
        "scope": "team",
        "rationale": "게이트 실측에서 차단 신호의 20영업일 수익률은 -3.7%~-13.2%였다. "
                     "반면 'LLM 만장일치가 그 성과를 역전한다'는 증거는 없다. "
                     "게다가 만장일치면 확신도 0.9가 자동 부여돼 임계값이 걸림돌 역할을 못 했다",
        "source": "config kr.trading_team.allow_pm_override=false (2026-08-03), "
                  "agents/portfolio_manager.py HARD_GATES",
    },
    {
        "id": "CORE-027",
        "rule": "종목별 승인과 별개로, 팀 심의 결과에 포트폴리오 제약을 한 번에 적용한다 (현재 shadow)",
        "category": "portfolio",
        "priority": "high",
        "scope": "team",
        "rationale": "종목을 하나씩 심의하면 서로를 보지 못한다. cross_validator의 섹터 규칙은 "
                     "'이미 보유 중'만 세므로 같은 배치에서 동시 승인된 후보들끼리는 걸러지지 않는다. "
                     "5개가 전부 같은 섹터여도 각각 통과할 수 있었다",
        "source": "agents/allocator.py — 확신도 순 배정, 배정마다 섹터·현금·슬롯 즉시 갱신. "
                  "배분기 거부는 오버라이드 불가",
    },

    # ============================================================
    # 자가 진화 — 2026-08 신설
    # ============================================================
    {
        "id": "CORE-028",
        "rule": "백테스트가 지원하고 게이트가 활성인 파라미터는 개선 확인 후에만 적용한다",
        "category": "evolution",
        "priority": "high",
        "scope": "universal",
        "rationale": "실거래 5영업일·10건 남짓의 표본으로는 실력과 잡음을 구분할 수 없다. "
                     "검증 못 한 변경을 적용하느니 하루 미루는 편이 안전하다 (fail-closed)",
        "source": "core/evolution/backtest_gate.py — A/B 백테스트(3개월/60종목), "
                  "수익률 개선 + MDD 악화≤1%p + 거래≥10건. 실행 실패 시 변경 보류(fail-closed). "
                  "⚠️ PARAM_MAP 미지원 파라미터·EVOLUTION_BACKTEST_GATE=0·게이트 초기화 실패 시에는 "
                  "검증 없이 통과한다",
    },

    # ============================================================
    # 노출 한도 — 2026-08-03 추가 (코드에는 있었으나 원칙에 누락돼 있었다)
    # ============================================================
    {
        "id": "CORE-029",
        "rule": "하루 신규 매수 5건, 비코어 8슬롯(잔여비율 가중) + 코어 3개 별도, 현금 5% 이상 유지",
        "category": "risk",
        "priority": "high",
        "scope": "KR",
        "rationale": "한 번에 얼마나 벌릴 수 있는지를 미리 못 박아 둬야 판단이 흔들려도 노출이 커지지 않는다. "
                     "현금 예비금은 급락 시 대응 여력이자 강제 청산 방지선이다",
        "source": "risk/manager.py can_open_position — 비코어는 잔여비율 가중합 < max_positions(8), "
                  "코어는 max_core_positions(3)로 별도 관리(가중 카운트 제외). "
                  "분할익절이 진행된 포지션은 슬롯을 일부만 차지하므로 실제 보유 종목 수는 11개를 넘을 수 있다. "
                  "engine.get_effective_max_positions()의 flex_extra_positions는 대시보드 표시 전용이며 주문 게이트가 아니다. "
                  "약세·횡보 체제에서는 신규매수 1~3건·현금 15%로 더 엄격해진다",
    },
    {
        "id": "CORE-030",
        "rule": "당일 손실 청산 3건 이상 + 당일 손익 -1% 미만이면 신규 매수 중단",
        "category": "risk",
        "priority": "medium",
        "scope": "KR",
        "rationale": "일일 손실 한도(-5%)에 닿기 전이라도 연속 손절은 시장과 전략이 어긋났다는 신호다. "
                     "한도까지 밀리기 전에 멈추는 편이 낫다",
        "source": "risk/manager.py — 손실 청산 카운트 + 당일 PnL 복합 조건",
    },
    {
        "id": "CORE-031",
        "rule": "SEPA는 14:30 이후 신규 진입하지 않는다",
        "category": "entry",
        "priority": "medium",
        "scope": "sepa_trend",
        "rationale": "추세 전략은 진입 후 관찰 시간이 필요한데, 마감 직전 진입은 "
                     "판단할 시간 없이 오버나이트 리스크만 떠안는다",
        "source": "strategies/kr/sepa_trend.py — 14:30 이후 신규 진입 차단",
    },
    {
        "id": "CORE-032",
        "rule": "gap_and_go 손실 포지션은 밤을 넘기지 않는다 (15:10 이후 장마감 전 청산)",
        "category": "exit",
        "priority": "medium",
        "scope": "gap_and_go",
        "rationale": "갭하락은 장중 스톱을 관통한다 — 손실 오버나잇이 사고의 원인이었고 "
                     "(2026-06 부검), 진입 시각 제한(구 규칙 3-4)은 흑자 구간을 막는 오진이었다",
        "source": "kr_scheduler.py — 갭EOD 손실 오버나잇 가드 (2026-08-19)",
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

        from ...utils.llm import LLMTask

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
