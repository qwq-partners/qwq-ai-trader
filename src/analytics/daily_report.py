"""
AI Trading Bot v2 - 일일 투자 레포트 시스템

매일 아침 8시: 오늘의 추천 종목 레포트
매일 오후 5시: 추천 종목 결과 레포트
"""

import asyncio
import dataclasses
import hashlib
import json
import os
import re
from dataclasses import dataclass, field
from datetime import datetime, date, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Dict, List, Optional, Any, Tuple
from loguru import logger

# 추천 종목 캐시 경로
_REC_CACHE_DIR = Path.home() / ".cache" / "ai_trader"

# 모닝브리프 캐시·평가 원장 경로 (2026-09-14 T9 요청 3·5)
MORNING_BRIEF_PATH = _REC_CACHE_DIR / "llm_morning_brief.json"
MORNING_BRIEF_LEDGER_PATH = _REC_CACHE_DIR / "morning_brief_eval.jsonl"
# 날짜·버전별 원문/발송문 아카이브 (2026-09-15 T10 F22) — 최신 캐시(위 두 경로)는
# 조회 편의용이고, 이 아카이브가 감사 가능한 원본이다. <kr_date>.json 1개 파일에
# generated[](07:00 생성본, 재생성해도 누적) / dispatch[](07:30 발송 attempt, 누적).

def _today() -> date:
    """테스트 주입용 단일 시계 진입점 — 브리프 kr_date·평가 기준일·과거 일자 가드가 같은 날짜를 본다"""
    return date.today()

# 브리프 주장 범위 (T9 요청 3)
SCOPE_US_ONLY = "us_close_only"
SCOPE_WITH_KR = "with_kr_inputs"
NO_KR_INPUT_NOTE = "국내 자료 없음 — 개장 방향 판단 불가."

# 브리프 테마 → 평가 대상 KIS 업종지수명 (2026-09-15 T10 F20)
# extract_brief_claims 의 테마명(US_KOREA_SECTOR_MAP 키)과 _collect_brief_actuals 의
# 업종명(KIS hts_kor_isnm)이 서로 달라 exact match 가 항상 실패하던 문제 — 근거가
# 분명한 항목만 올린다(kr_theme_detector.THEME_SECTOR_MAP 은 스코어 보정용 광범위
# 매핑이라 그대로 재사용하지 않는다). 매핑에 없는 테마는 "평가 대상 미합의"로 미평가.
# 집계 규칙은 mean 고정(다대다 매핑 대비, 현재는 전부 1:1).
BRIEF_THEME_EVAL_TARGETS: Dict[str, Dict] = {
    "AI/반도체": {
        "sectors": ["전기전자"],
        "agg": "mean",
        "basis": "KRX 업종분류상 삼성전자·SK하이닉스 등 반도체 대형주가 전기전자 업종",
    },
    "바이오": {
        "sectors": ["의약품"],
        "agg": "mean",
        "basis": "KRX 업종분류상 바이오/제약 종목이 의약품 업종",
    },
}

# 국내 개장 방향·대응 전략 단정 표현 — 국내 자료 없이 쓰이면 제거한다
_OPEN_CLAIM_PATTERNS = (
    "갭 출발", "갭출발", "갭상승", "갭 상승", "갭하락", "갭 하락",
    "상승 출발", "하락 출발", "보합 출발", "강세 출발", "약세 출발",
    "상승 개장", "하락 개장", "갭업", "갭다운",
    "시가 매수", "시초가 매수", "매수 대응", "매도 대응",
)
# 국내 장을 주어로 삼는 토큰 — 하나라도 있으면 미국 근거가 섞여 있어도 국내 단정으로 본다
_KR_SUBJECT_TOKENS = (
    "KOSPI", "코스피", "KOSDAQ", "코스닥", "국내", "한국", "국장",
    "오늘 장", "오늘 개장", "우리 증시", "원화",
)
# KR 주어가 없고 미국 시장을 가리키는 문장이면 제거하지 않는다
_US_MARKET_TOKENS = (
    "미국", "美", "S&P", "SP500", "나스닥", "NASDAQ", "다우", "DOW",
    "SOX", "VIX", "뉴욕", "빅테크", "필라델피아", "뉴욕증시",
)
_BULL_TONE_TOKENS = ("강세", "상승", "호조", "반등", "랠리", "훈풍", "급등")
_BEAR_TONE_TOKENS = ("약세", "하락", "부진", "조정", "급락", "위축", "충격")
# 브리프 톤 판정 임계 (강세/약세 토큰 수 차이) — 한쪽으로 2회 이상 기울 때만 방향으로 본다
_TONE_MARGIN = 2

# 마침표 뒤에 공백·문장끝이 와야 문장 경계로 본다 — "+0.97%", "S&P500 +0.8%" 가 쪼개지면
# KR 주어와 단정이 서로 다른 조각으로 갈려 검사·본문이 모두 훼손된다 (2026-09-14 리뷰)
_SENTENCE_SPLIT_RE = re.compile(r"([.!?]+(?:\s+|$|(?=<))|\n)")   # 마침표 직후 태그(.<b>)도 경계 (2026-09-14 재리뷰)
# 구분자에서 문장부호만 떼고 공백·줄바꿈은 남기기 위한 패턴
_SEP_WHITESPACE_RE = re.compile(r"[.!?]+")

# 프로젝트 내 모듈
from ..utils.telegram import get_telegram_notifier, TelegramNotifier
from ..signals.screener.kr_screener import get_screener, ScreenedStock
from ..signals.sentiment.kr_theme_detector import get_theme_detector, NewsCollector
from ..data.storage.news_storage import get_news_storage


@dataclass
class RecommendedStock:
    """추천 종목"""
    rank: int
    symbol: str
    name: str

    # 투자 포인트
    investment_thesis: str        # 왜 이 종목인가? (1줄 요약)
    catalyst: str                 # 촉매 (상승 이유)

    # 가격 정보
    prev_close: float = 0        # 전일 종가
    target_entry: float = 0      # 목표 진입가
    target_exit: float = 0       # 목표 청산가 (익절)
    stop_loss: float = 0         # 손절가

    # 점수
    news_score: float = 0        # 뉴스 기반 점수 (0~100)
    tech_score: float = 0        # 기술적 점수 (0~100)
    theme_score: float = 0       # 테마 점수 (0~100)
    total_score: float = 0       # 종합 점수

    # 리스크
    risk_level: str = "중"       # 낮음/중/높음
    risk_factors: List[str] = field(default_factory=list)

    # 관련 정보
    related_theme: str = ""      # 관련 테마
    key_news: str = ""           # 핵심 뉴스 요약

    # 결과 (오후 리포트용)
    result_price: Optional[float] = None
    result_pct: Optional[float] = None


def _split_sentences(text: str) -> List[Tuple[str, str]]:
    """(문장, 구분자) 목록 — 그대로 이어붙이면 원문이 복원된다"""
    tokens = _SENTENCE_SPLIT_RE.split(text)
    pairs: List[Tuple[str, str]] = []
    for i in range(0, len(tokens), 2):
        body = tokens[i]
        sep = tokens[i + 1] if i + 1 < len(tokens) else ""
        if body or sep:
            pairs.append((body, sep))
    return pairs


def _has_kr_subject(sentence: str) -> bool:
    """국내 장을 주어로 삼는 문장인가"""
    return any(tok in sentence for tok in _KR_SUBJECT_TOKENS)


def _is_kr_open_claim(sentence: str) -> bool:
    """국내 개장 방향·대응 전략 단정 문장인가

    KR 주어가 있으면 미국 근거가 같은 문장에 섞여 있어도 국내 단정으로 본다
    ("미국 반도체 랠리를 반영해 오늘 KOSPI는 갭상승 출발이 예상된다" 형태 차단).
    KR 주어가 없을 때만 미국 시장 문장으로 보고 면제한다.
    """
    if not any(pat in sentence for pat in _OPEN_CLAIM_PATTERNS):
        return False
    if _has_kr_subject(sentence):
        return True
    return not any(tok in sentence for tok in _US_MARKET_TOKENS)


def sanitize_brief_claims(text: str, scope: str) -> Tuple[str, List[str]]:
    """국내 자료 없이 개장 방향을 단정한 문장을 제거한다.

    Returns: (정제된 본문, 제거된 문장 목록)
    scope 가 with_kr_inputs 이면 원문을 그대로 둔다 (근거가 있는 주장).
    """
    if scope != SCOPE_US_ONLY or not text:
        return text, []

    out: List[str] = []
    removed: List[str] = []
    note_emitted_for_run = False
    for body, sep in _split_sentences(text):
        sentence = (body + sep).strip()
        if body.strip() and _is_kr_open_claim(sentence):
            removed.append(sentence)
            if not note_emitted_for_run:
                out.append(NO_KR_INPUT_NOTE)
                note_emitted_for_run = True
            # 문장은 지우되 줄바꿈·공백은 보존한다 (문단 구조 유지)
            out.append(_SEP_WHITESPACE_RE.sub("", sep))
            continue
        note_emitted_for_run = False
        out.append(body + sep)
    # 문장을 지우며 남은 줄 끝 공백 정리
    return re.sub(r"[ \t]+\n", "\n", "".join(out)), removed


def brief_tone(text: str) -> str:
    """브리프 톤 — bull / bear / neutral (근거 없는 확신을 만들지 않도록 여유 폭 적용)"""
    bull = sum(text.count(tok) for tok in _BULL_TONE_TOKENS)
    bear = sum(text.count(tok) for tok in _BEAR_TONE_TOKENS)
    if bull - bear >= _TONE_MARGIN:
        return "bull"
    if bear - bull >= _TONE_MARGIN:
        return "bear"
    return "neutral"


def _expert_label(score: int) -> str:
    """전문가 종합점수 라벨 (07:30 브리핑과 동일 기준)"""
    if score >= 15:
        return "강세 우위"
    if score >= 5:
        return "약상승"
    if score <= -15:
        return "약세 우위"
    if score <= -5:
        return "약하락"
    return "중립"


def build_expert_conflict_note(
    tone_or_text: Optional[str], expert_consensus: Optional[Dict],
) -> Optional[str]:
    """브리프 톤과 전문가 종합판단이 상충하면 표시 문구를 만든다 (없으면 None).

    07:30 결합부(kr_scheduler)가 그대로 호출할 수 있는 순수 함수다.

    Args:
        tone_or_text: 브리프 레코드의 `tone`("bull"/"bear"/"neutral") 또는 본문 `text`
                      (톤 값이 아니면 본문으로 보고 brief_tone() 으로 판정한다)
        expert_consensus: {"score": int} 또는 {"bias": "bull"|"bear"|"neutral"}
    Returns:
        표시 문구, 또는 상충이 없거나 전문가 판단이 없으면 None
    """
    if not expert_consensus or not tone_or_text:
        return None
    tone = tone_or_text if tone_or_text in ("bull", "bear", "neutral") else brief_tone(tone_or_text)
    score = expert_consensus.get("score")
    if score is not None:
        label = _expert_label(score)
        expert_dir = "bull" if score >= 5 else ("bear" if score <= -5 else "neutral")
        score_txt = f"{score:+d} {label}"
    else:
        expert_dir = expert_consensus.get("bias")
        if expert_dir not in ("bull", "bear", "neutral"):
            return None
        score_txt = {"bull": "강세 우위", "bear": "약세 우위", "neutral": "중립"}[expert_dir]
    if tone == "neutral" or tone == expert_dir:
        return None
    tone_kr = "강세" if tone == "bull" else "약세"
    return f"⚠️ 전문가 종합판단({score_txt})과 상충 — 브리프 톤은 {tone_kr}"


def valid_kr_inputs(kr_inputs: Optional[List[Dict]]) -> List[Dict]:
    """as_of 가 있는 국내 자료만 유효로 인정한다 (신선도 미상 값은 근거로 쓰지 않는다)"""
    return [item for item in (kr_inputs or []) if item and item.get("as_of")]


def brief_scope(kr_inputs: Optional[List[Dict]]) -> str:
    """국내 자료 유무로 장전 발송 범위를 판정하는 단일 지점 (2026-09-15 T10 F21).

    build_morning_brief(LLM 프롬프트 제약) 와 generate_us_market_report 의 고정
    문구(market_msg)가 이 함수 하나를 공유해야 판정이 어긋나지 않는다.
    """
    return SCOPE_WITH_KR if valid_kr_inputs(kr_inputs) else SCOPE_US_ONLY


def extract_brief_claims(
    text: str, sector_signals: Optional[Dict], scope: str, basis: List[str],
) -> Dict:
    """브리프 주장을 구조화한다 — 근거 없는 축은 None (기본값으로 채우지 않는다)

    claims["sectors"] 는 2026-09-15(T10 F20)부터 테마명 문자열이 아니라
    {"theme", "eval_targets", "agg", "supported", "reason"} 딕셔너리 목록이다.
    평가 대상(KIS 업종지수명)을 생성 시점에 고정해, 저녁 평가가 결과를 본 뒤
    대상을 고르지 못하게 한다. BRIEF_THEME_EVAL_TARGETS 에 없는 테마는
    supported=False + "평가 대상 미합의"로 남기고 미평가 처리한다.
    """
    open_direction = None
    close_direction = None
    if scope == SCOPE_WITH_KR:
        # 미국 마감 서술("S&P500은 상승 마감했다")을 KOSPI 주장으로 원장에 남기지 않는다
        kr_text = "\n".join(
            body + sep for body, sep in _split_sentences(text)
            if _has_kr_subject(body + sep)
        )
        if any(p in kr_text for p in ("상승 출발", "갭상승", "갭 상승", "상승 갭", "상승 개장", "갭업", "강세 출발")):
            open_direction = "up"
        elif any(p in kr_text for p in ("하락 출발", "갭하락", "갭 하락", "하락 갭", "하락 개장", "갭다운", "약세 출발")):
            open_direction = "down"
        elif "보합 출발" in kr_text:
            open_direction = "flat"

        if any(p in kr_text for p in ("상승 마감", "반등 마감", "상승 전환 마감")):
            close_direction = "up"
        elif any(p in kr_text for p in ("하락 마감", "약세 마감")):
            close_direction = "down"

    sectors: List[Dict] = []
    for name in (sector_signals or {}):
        if not name or name not in text:
            continue
        target = BRIEF_THEME_EVAL_TARGETS.get(name)
        if target:
            sectors.append({
                "theme": name, "eval_targets": list(target["sectors"]),
                "agg": target["agg"], "supported": True,
            })
        else:
            sectors.append({
                "theme": name, "eval_targets": [], "agg": None,
                "supported": False, "reason": "평가 대상 미합의",
            })
    return {
        "open_direction": open_direction,
        "close_direction": close_direction,
        "sectors": sectors,
        "basis": list(basis),
    }


def _brief_archive_path(kr_date: str, archive_dir=None) -> Path:
    """archive_dir 미지정 시 최신 캐시(MORNING_BRIEF_PATH) 옆 morning_brief/ —
    save_morning_brief/evaluate_morning_brief 가 brief_path 에서 유도하는 규칙과
    같은 값이라(운영 기본 경로 = ~/.cache/ai_trader/morning_brief) 세 진입점의 기본
    아카이브가 한 곳으로 모인다. 호출 시점의 모듈 전역을 읽으므로 테스트가
    MORNING_BRIEF_PATH 를 바꾸면 record_morning_brief_dispatch 도 따라간다
    (2026-09-15 T10 통합 — D 재현 F19: 두 규칙이 갈라져 발송 기록이 유실됐다)."""
    base = Path(archive_dir) if archive_dir is not None else (MORNING_BRIEF_PATH.parent / "morning_brief")
    return base / f"{kr_date}.json"


def _load_brief_archive(kr_date: str, archive_dir=None) -> Dict:
    path = _brief_archive_path(kr_date, archive_dir)
    if not path.exists():
        return {"kr_date": kr_date, "generated": [], "dispatch": []}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except Exception as e:
        # 손상본을 덮어써 지우지 않는다 — 옆에 보존하고 빈 아카이브로 새로 시작 (R-C advisory)
        aside = path.with_name(f"{path.name}.corrupt-{datetime.now():%Y%m%dT%H%M%S}")
        try:
            os.replace(path, aside)
            logger.warning(f"[모닝브리프] 아카이브 파싱 실패 ({path}) — {aside.name} 로 보존 후 새 아카이브: {e}")
        except OSError as mv_err:
            logger.warning(f"[모닝브리프] 아카이브 파싱 실패 ({path}), 보존도 실패({mv_err}) — 빈 아카이브로 취급: {e}")
        return {"kr_date": kr_date, "generated": [], "dispatch": []}
    data.setdefault("generated", [])
    data.setdefault("dispatch", [])
    return data


def _save_brief_archive(kr_date: str, data: Dict, archive_dir=None) -> Path:
    path = _brief_archive_path(kr_date, archive_dir)
    path.parent.mkdir(parents=True, exist_ok=True)
    # 원자적 교체 — 쓰는 도중 프로세스가 죽어도 감사 원본이 반쪽으로 남지 않는다
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    os.replace(tmp, path)
    return path


def save_morning_brief(record: Dict, path=None, archive_dir=None) -> Path:
    """모닝브리프 레코드를 고정 스키마로 저장한다 (07:30 브리핑이 text 키를 읽는다)

    최신 캐시(`path`, 기본 llm_morning_brief.json)는 조회 편의용 단일 파일이라
    재생성 시 덮어써진다. 감사 원본은 날짜별 아카이브의 generated[] 에 매번
    추가한다 — 같은 날 여러 번 생성해도 이전 버전이 사라지지 않는다
    (2026-09-15 T10 F22).
    """
    path = Path(path) if path is not None else MORNING_BRIEF_PATH
    path.parent.mkdir(parents=True, exist_ok=True)
    # 최신 캐시는 조회 편의용 — raw_text/body_full(절단 없는 원문·전체 본문)은
    # 아카이브 전용이라 캐시엔 담지 않는다(2026-09-15 T10 F22 blocking).
    cache_record = {k: v for k, v in record.items() if k not in ("raw_text", "body_full")}
    path.write_text(json.dumps(cache_record, ensure_ascii=False, indent=2), encoding="utf-8")

    kr_date = record.get("kr_date")
    if kr_date:
        # archive_dir 미지정 시 캐시 파일과 같은 디렉터리 밑 (테스트가 tmp_path 로
        # path 를 넘기면 아카이브도 그 밑으로 격리된다 — 운영 캐시 오염 방지)
        _archive_dir = Path(archive_dir) if archive_dir is not None else (path.parent / "morning_brief")
        try:
            archive = _load_brief_archive(kr_date, _archive_dir)
            text = record.get("text") or ""
            entry = dict(record)
            entry["version"] = len(archive["generated"]) + 1
            entry["text_sha256"] = hashlib.sha256(text.encode("utf-8")).hexdigest()
            entry["archived_at"] = datetime.now().isoformat()
            archive["generated"].append(entry)
            _save_brief_archive(kr_date, archive, _archive_dir)
        except Exception as e:
            logger.warning(f"[모닝브리프] 아카이브 저장 실패 (최신 캐시는 저장됨): {e}")
    return path


def record_morning_brief_dispatch(
    *,
    kr_date: str,
    sent_text: str,
    expert_consensus: Optional[Dict],
    status: str,
    conflict_note: Optional[str] = None,
    archive_dir=None,
) -> Path:
    """07:30 발송 결과를 날짜별 아카이브에 기록한다 (2026-09-15 T10 F19 C→A 배선 계약).

    kr_scheduler._send_expert_briefing_telegram 의 `ok = await notifier.send_report(msg)`
    직후 호출된다. 같은 날 여러 attempt(재시도 포함)를 dispatch[] 에 덧붙이며,
    generated[] 의 생성 원본은 절대 덮어쓰지 않는다. evaluate_morning_brief 가
    이 스냅샷을 발송 판단의 근거로 우선 사용한다.

    Args:
        status: "sent" 또는 "failed"만 허용.
        conflict_note: build_expert_conflict_note() 결과(있으면).
    """
    if status not in ("sent", "failed"):
        raise ValueError(f"[모닝브리프] 알 수 없는 발송 status: {status!r} (sent/failed만 허용)")

    archive = _load_brief_archive(kr_date, archive_dir)
    generated = archive.get("generated") or []
    brief_ref = None
    if generated:
        last = generated[-1]
        brief_ref = {
            "version": last.get("version"),
            "text_sha256": last.get("text_sha256"),
            "generated_at": last.get("generated_at"),
            "model": last.get("model"),
            # archive_dir 를 운영 기본값 밖으로 주입해도 원장 레코드만으로 원본
            # 아카이브 파일을 특정할 수 있게 경로를 남긴다 (advisory F22)
            "archive_path": str(_brief_archive_path(kr_date, archive_dir)),
        }

    dispatch_entry = {
        "attempt": len(archive.get("dispatch") or []) + 1,
        "sent_at": datetime.now().isoformat(),
        "sent_text": sent_text,
        "expert_consensus": expert_consensus,
        "status": status,
        "conflict_note": conflict_note,
        "brief_ref": brief_ref,
    }
    archive.setdefault("dispatch", []).append(dispatch_entry)
    return _save_brief_archive(kr_date, archive, archive_dir)


class DailyReportGenerator:
    """
    일일 투자 레포트 생성기

    투자자 관점에서 실제로 도움이 되는 레포트를 생성합니다.

    핵심 원칙:
    1. 간결하고 명확하게 - 한눈에 파악 가능
    2. 액션 가이드 제공 - 무엇을 언제 얼마에 살지
    3. 리스크 경고 - 어떤 위험이 있는지
    4. 근거 제시 - 왜 이 종목인지
    """

    def __init__(self, kis_market_data=None):
        self.telegram = get_telegram_notifier()
        self.screener = get_screener()
        self.theme_detector = get_theme_detector()
        self.news_collector = NewsCollector()
        self._kis_market_data = kis_market_data
        self._us_market_data = None

        # NewsStorage 추가 (종목별 뉴스 조회용)
        self._news_storage = None

        # 오늘의 추천 종목 저장 (오후 결과 리포트용)
        self._today_recommendations: List[RecommendedStock] = []
        self._recommendation_date: Optional[date] = None
        self._today_news: List[Dict] = []  # 당일 핵심 뉴스

    async def generate_morning_report(
        self,
        llm_manager=None,
        max_stocks: int = 10,
        send_telegram: bool = True,
    ) -> str:
        """
        아침 8시 추천 종목 레포트 생성

        Args:
            llm_manager: LLM 매니저 (뉴스 분석용)
            max_stocks: 추천 종목 수 (최소 10개)
            send_telegram: 텔레그램 발송 여부

        Returns:
            레포트 메시지
        """
        logger.info("[레포트] 아침 추천 종목 레포트 생성 시작")

        today = date.today()
        max_stocks = max(max_stocks, 10)  # 최소 10개 보장

        # LLM 매니저 자동 연결 (미전달 시)
        if llm_manager is None:
            try:
                from ..utils.llm import get_llm_manager
                llm_manager = get_llm_manager()
            except Exception as e:
                logger.warning(f"LLM 매니저 자동 연결 실패: {e}")

        # 2. 테마 탐지 (스크리닝 전에 먼저)
        hot_themes = []
        if self.theme_detector:
            try:
                themes = await self.theme_detector.detect_themes()
                hot_themes = [t for t in themes if t.score >= 60][:5]
            except Exception as e:
                logger.warning(f"테마 탐지 실패: {e}")

        # 1. 배치 스캔 결과(pending_signals.json) 우선 사용
        #    프리장(08:25)에 screen_all()을 직접 실행하면 KIS 데이터 없어 0건 반환됨
        #    → 08:20 배치 스캔이 이미 완료된 경우 그 결과를 재활용
        recommendations = self._load_from_pending_signals(today, max_stocks, hot_themes)
        if recommendations:
            logger.info(f"[레포트] 배치 스캔 결과 재활용: {len(recommendations)}종목 (pending_signals.json)")
        else:
            # 폴백: 직접 스크리닝 (배치 결과 없을 때)
            logger.info("[레포트] pending_signals 없음 → 직접 스크리닝 실행")
            screened = await self.screener.screen_all(
                llm_manager=llm_manager,
                min_price=5000,
                theme_detector=self.theme_detector,
            )
            # 3. 종목 점수 재계산 및 순위 결정
            recommendations = await self._rank_stocks(screened, hot_themes, max_stocks)

        # 4. 종목별 대표뉴스 수집
        await self._collect_per_stock_news(recommendations)

        # 5. 추천 종목 저장 (오후 리포트용) + 파일 영속화
        self._today_recommendations = recommendations
        self._recommendation_date = today
        self._save_recommendations(today)

        # 5-1. 업종 동향 데이터 조회
        sector_lines = await self._fetch_sector_summary()

        # 5-2. US 시장 오버나이트 데이터 조회
        us_lines = await self._fetch_us_market_summary()

        # 6. 레포트 생성
        report = self._format_morning_report(recommendations, hot_themes, today, sector_lines, us_lines)

        # 7. 텔레그램 레포트 채널로 발송
        if send_telegram:
            success = await self.telegram.send_report(report)
            if success:
                logger.info(f"[레포트] 아침 레포트 발송 완료 ({len(recommendations)}종목)")
            else:
                logger.error("[레포트] 아침 레포트 발송 실패")

        return report

    async def generate_evening_report(
        self,
        send_telegram: bool = True,
    ) -> str:
        """
        오후 5시 결과 레포트 생성

        아침에 추천한 종목들의 당일 성과 + 실제 거래 결과를 보고합니다.
        """
        logger.info("[레포트] 오후 결과 레포트 생성 시작")

        today = date.today()

        # 메모리에 없으면 파일에서 복원 시도 (봇 재시작 대응)
        if not self._today_recommendations or self._recommendation_date != today:
            loaded = self._load_recommendations(today)
            if loaded:
                self._today_recommendations = loaded
                self._recommendation_date = today
                logger.info(f"[레포트] 추천 종목 파일 복원: {len(loaded)}종목")
            else:
                logger.warning("[레포트] 오늘 추천 종목이 없습니다 (메모리 + 파일 모두 없음)")
                return ""

        # 현재가 조회 및 결과 계산
        await self._update_results()

        # 레포트 생성 (추천 종목 결과만 — 봇 실거래 결과는 별도 채널)
        report = self._format_evening_report(self._today_recommendations, today)

        # 텔레그램 레포트 채널로 발송
        if send_telegram:
            success = await self.telegram.send_report(report)
            if success:
                logger.info("[레포트] 오후 결과 레포트 발송 완료")
            else:
                logger.error("[레포트] 오후 결과 레포트 발송 실패")

        return report

    async def _get_trade_summary(self) -> str:
        """DB에서 당일 실거래 결과 조회 (봇 재시작 대응)"""
        try:
            from ..data.storage.trade_storage import get_trade_storage
            storage = get_trade_storage()
            if storage is None:
                return ""

            # DB에서 오늘 청산된 거래 조회
            stats = await storage.get_statistics_from_db(days=1)
            # 오늘 오픈 포지션은 엔진에서 직접 가져옴
            open_positions = []
            try:
                from ..core.evolution.trade_journal import get_trade_journal
                journal = get_trade_journal()
                for t in journal.get_today_trades():
                    if not t.get("exit_price"):
                        open_positions.append(t)
            except Exception:
                pass

            if not stats and not open_positions:
                return ""

            total_trades = stats.get("total_trades", 0)
            wins = stats.get("wins", 0)
            losses = stats.get("losses", 0)
            total_pnl = stats.get("total_pnl", 0.0)
            win_rate = stats.get("win_rate", 0.0)
            avg_pnl_pct = stats.get("avg_pnl_pct", 0.0)
            best = stats.get("best_trade")
            worst = stats.get("worst_trade")

            lines = [
                "─" * 20,
                "<b>📊 봇 실거래 결과</b>",
                "",
            ]

            if total_trades > 0:
                lines.extend([
                    f"• 총 거래: {total_trades}건 (승 {wins} / 패 {losses})",
                    f"• 승률: {win_rate:.1f}% / 평균 손익률: {avg_pnl_pct:+.2f}%",
                    f"• 실현 손익: <b>{total_pnl:+,.0f}원</b>",
                ])
                if best and worst and total_trades >= 2:
                    lines.append(
                        f"• 최고: {best['name']} {best['pnl_pct']:+.1f}% / "
                        f"최저: {worst['name']} {worst['pnl_pct']:+.1f}%"
                    )

            if open_positions:
                lines.append(f"• 보유 중: {len(open_positions)}종목")

            return "\n".join(lines)

        except Exception as e:
            logger.warning(f"실거래 결과 조회 실패: {e}")
            return ""

    async def _rank_stocks(
        self,
        screened: List[ScreenedStock],
        hot_themes: List,
        max_stocks: int,
    ) -> List[RecommendedStock]:
        """종목 순위 결정 및 추천 종목 생성"""

        # 테마 관련 종목 맵
        theme_map = {}
        for theme in hot_themes:
            for symbol in getattr(theme, 'related_stocks', []):
                theme_map[symbol] = theme.name

        recommendations = []

        for i, stock in enumerate(screened[:max_stocks * 3]):  # 후보군 넉넉하게
            # ETF/ETN 방어적 필터 (스크리너 미경유 시 대비)
            if self.screener._is_etf_etn(stock.name):
                continue

            # 기본 점수
            news_score = min(stock.score, 100)
            tech_score = self._calculate_tech_score(stock)
            theme_score = 80 if stock.symbol in theme_map else 0

            # 종합 점수
            total = (news_score * 0.4) + (tech_score * 0.3) + (theme_score * 0.3)

            # 가격 계산
            entry = stock.price
            target = entry * 1.03  # +3% 익절
            stop = entry * 0.98   # -2% 손절

            # 리스크 평가
            risk_level, risk_factors = self._assess_risk(stock)

            # 상세 투자 포인트 생성
            thesis = self._generate_detailed_thesis(stock, theme_map.get(stock.symbol, ""))
            catalyst = self._generate_catalyst(stock, theme_map.get(stock.symbol, ""))

            rec = RecommendedStock(
                rank=len(recommendations) + 1,
                symbol=stock.symbol,
                name=stock.name,
                investment_thesis=thesis,
                catalyst=catalyst,
                prev_close=stock.price,
                target_entry=entry,
                target_exit=target,
                stop_loss=stop,
                news_score=news_score,
                tech_score=tech_score,
                theme_score=theme_score,
                total_score=total,
                risk_level=risk_level,
                risk_factors=risk_factors,
                related_theme=theme_map.get(stock.symbol, ""),
                key_news="",  # 이후 종목별 뉴스에서 채움
            )
            recommendations.append(rec)

            if len(recommendations) >= max_stocks:
                break

        # 최소 10개가 안 되면 점수 낮은 것도 포함
        if len(recommendations) < 10 and len(screened) > len(recommendations):
            for stock in screened[len(recommendations):]:
                if len(recommendations) >= max_stocks:
                    break
                if stock.symbol in [r.symbol for r in recommendations]:
                    continue
                if self.screener._is_etf_etn(stock.name):
                    continue

                entry = stock.price
                thesis = self._generate_detailed_thesis(stock, theme_map.get(stock.symbol, ""))
                catalyst = self._generate_catalyst(stock, theme_map.get(stock.symbol, ""))
                risk_level, risk_factors = self._assess_risk(stock)

                rec = RecommendedStock(
                    rank=len(recommendations) + 1,
                    symbol=stock.symbol,
                    name=stock.name,
                    investment_thesis=thesis,
                    catalyst=catalyst,
                    prev_close=entry,
                    target_entry=entry,
                    target_exit=entry * 1.03,
                    stop_loss=entry * 0.98,
                    news_score=min(stock.score, 100),
                    tech_score=self._calculate_tech_score(stock),
                    theme_score=80 if stock.symbol in theme_map else 0,
                    total_score=stock.score,
                    risk_level=risk_level,
                    risk_factors=risk_factors,
                    related_theme=theme_map.get(stock.symbol, ""),
                    key_news="",
                )
                recommendations.append(rec)

        return recommendations

    def _calculate_tech_score(self, stock: ScreenedStock) -> float:
        """기술적 점수 계산 (수급 중심, 모멘텀 편향 제거)"""
        score = 40  # 기본점수
        reasons_str = " ".join(stock.reasons)

        # 수급 신호 (최우선, 신뢰도 높음)
        if stock.has_foreign_buying:
            score += 25
        if stock.has_inst_buying:
            score += 20

        # 기술적 지표
        if "SPDI↑" in reasons_str:      score += 10  # 수급선행 지표
        if "지속상승" in reasons_str:    score += 8
        if "MA20+" in reasons_str:       score += 5
        if "RSI" in reasons_str:         score += 5
        if "저PER" in reasons_str:       score += 5
        if "저PBR" in reasons_str:       score += 3
        if "고ROE" in reasons_str:       score += 4

        # 거래량 (있으면 가산, 단독으론 약한 신호)
        if stock.volume_ratio >= 3.0:    score += 10
        elif stock.volume_ratio >= 2.0:  score += 6
        elif "거래량" in reasons_str:    score += 3

        # 등락률 — 보조 신호만 (최대 8점)
        if stock.change_pct > 5:         score += 8
        elif stock.change_pct > 2:       score += 5
        elif stock.change_pct > 0:       score += 2

        return min(score, 100)

    def _assess_risk(self, stock: ScreenedStock) -> Tuple[str, List[str]]:
        """리스크 평가"""
        factors = []

        # 과열 체크
        if stock.change_pct > 10:
            factors.append("과열 주의 (10%+ 급등)")

        # 저가주 체크
        if stock.price < 2000:
            factors.append("저가주 변동성")

        # 레버리지 ETF 체크
        if "레버리지" in stock.name or "인버스" in stock.name:
            factors.append("레버리지/인버스 상품")

        # 리스크 레벨
        if len(factors) >= 2:
            level = "높음"
        elif len(factors) >= 1:
            level = "중"
        else:
            level = "낮음"

        return level, factors

    def _generate_detailed_thesis(self, stock: ScreenedStock, theme: str) -> str:
        """스크리너 reasons 기반 상세 투자 포인트 생성 (수급 → 테마 → 기술 순)"""
        parts = []
        reasons_str = " ".join(stock.reasons)

        # 1. 수급 신호 (가장 신뢰도 높음 — 최우선)
        for r in stock.reasons:
            if "외국인 순매수" in r and r not in parts:
                parts.append(r)
                break
        for r in stock.reasons:
            if "기관 순매수" in r and r not in parts:
                parts.append(r)
                break

        # 2. 테마 멤버십
        if theme:
            parts.append(f"{theme} 테마")

        # 3. 기술적 신호 — 스크리너 reasons 직접 사용
        TECH_KEYWORDS = ["SPDI↑", "지속상승", "MA20+", "RSI", "저PER", "저PBR", "고ROE", "흑자"]
        for kw in TECH_KEYWORDS:
            for r in stock.reasons:
                if kw in r and r not in parts:
                    parts.append(r)
                    break
            if len(parts) >= 4:
                break

        # 4. 거래량 (있으면 추가)
        for r in stock.reasons:
            if "거래량" in r and r not in parts:
                parts.append(r)
                break

        # 5. 아무것도 없으면 등락률로 보완
        if not parts:
            if abs(stock.change_pct) > 0.5:
                parts.append(f"전일 {stock.change_pct:+.1f}%")
            else:
                parts.append("기술적 돌파 신호")

        return " / ".join(parts[:4])

    def _generate_catalyst(self, stock: ScreenedStock, theme: str) -> str:
        """상승 촉매 생성"""
        catalysts = []

        if theme:
            catalysts.append(f"{theme} 테마 강세")

        reasons_str = " ".join(stock.reasons)
        if "거래량" in reasons_str:
            catalysts.append("거래량 폭발")
        if "신고가" in reasons_str:
            catalysts.append("신고가 돌파")
        if "상승률" in reasons_str:
            catalysts.append("강한 상승 모멘텀")

        if stock.change_pct > 5:
            catalysts.append(f"전일 {stock.change_pct:+.1f}% 급등")

        if not catalysts:
            catalysts.append("기술적 반등 신호")

        return ", ".join(catalysts[:2])

    async def _collect_per_stock_news(self, recommendations: List[RecommendedStock]):
        """
        종목별 대표뉴스 수집

        DB에 저장된 뉴스 중 해당 종목이 언급된 뉴스를 조회합니다.
        (네이버 API가 아닌 자체 DB 사용)
        """
        # NewsStorage 초기화
        if self._news_storage is None:
            self._news_storage = await get_news_storage()

        for rec in recommendations:
            try:
                # DB에서 종목명으로 뉴스 검색 (최근 3일, 최대 5건)
                articles = await self._news_storage.search_news(
                    keyword=rec.name,
                    days=3,
                    limit=5
                )

                if articles:
                    # 가장 최근 뉴스의 제목 사용
                    rec.key_news = articles[0].title
                    logger.debug(f"[레포트] {rec.name} 대표뉴스: {rec.key_news[:30]}...")
                else:
                    rec.key_news = ""
                    logger.debug(f"[레포트] {rec.name} 관련 뉴스 없음")

            except Exception as e:
                logger.warning(f"종목 뉴스 검색 실패 ({rec.name}): {e}")
                rec.key_news = ""

    def _load_from_pending_signals(
        self,
        today: date,
        max_stocks: int,
        hot_themes: list,
    ) -> List["RecommendedStock"]:
        """pending_signals.json(배치 스캔 결과)에서 RecommendedStock 변환

        배치 스캔(08:20)이 완료된 경우 해당 결과를 레포트에 재활용.
        프리장(08:25)에 screen_all()을 직접 실행하면 KIS 데이터 없어 0건 반환되는 문제 방지.
        """
        try:
            signals_path = _REC_CACHE_DIR / "pending_signals.json"
            if not signals_path.exists():
                return []

            import json as _json
            from datetime import datetime as _dt
            data = _json.loads(signals_path.read_text(encoding="utf-8"))
            if not data:
                return []

            # 오늘 날짜 시그널만 (만료된 것 제외)
            now_str = _dt.now().isoformat()
            valid = [
                d for d in data
                if d.get("date", d.get("created_at", ""))[:10] == today.isoformat()
                and d.get("expires_at", "9999") >= now_str
            ]
            if not valid:
                return []

            # 테마 이름 맵 (hot_themes)
            theme_map: dict = {}
            if hot_themes:
                for ht in hot_themes:
                    theme_map[getattr(ht, "name", str(ht))] = getattr(ht, "score", 0)

            results: List[RecommendedStock] = []
            strategy_label = {
                "sepa_trend": "SEPA 성장주",
                "rsi2_reversal": "RSI2 단기반등",
                "strategic_swing": "전략적 스윙",
            }
            for i, d in enumerate(sorted(valid, key=lambda x: x.get("score", 0), reverse=True)[:max_stocks], 1):
                strategy = d.get("strategy", "")
                reason = d.get("reason", "")
                entry = float(d.get("entry_price", 0))
                stop = float(d.get("stop_price", 0))
                target = float(d.get("target_price", 0))
                score = float(d.get("score", 0))

                rec = RecommendedStock(
                    rank=i,
                    symbol=d.get("symbol", ""),
                    name=d.get("name", ""),
                    investment_thesis=reason[:80] if reason else strategy_label.get(strategy, strategy),
                    catalyst=reason if reason else "배치 스캔 신호",
                    prev_close=entry,
                    target_entry=entry,
                    target_exit=target,
                    stop_loss=stop,
                    tech_score=score,
                    total_score=score,
                    related_theme=strategy_label.get(strategy, strategy),
                    risk_level="중",
                )
                results.append(rec)

            logger.info(f"[레포트] pending_signals → RecommendedStock 변환: {len(results)}개")
            return results

        except Exception as e:
            logger.warning(f"[레포트] pending_signals 로드 실패 (폴백 스크리닝): {e}")
            return []

    def _save_recommendations(self, report_date: date) -> None:
        """추천 종목을 파일에 영속화 (봇 재시작 대응)"""
        try:
            _REC_CACHE_DIR.mkdir(parents=True, exist_ok=True)
            path = _REC_CACHE_DIR / f"morning_recs_{report_date.isoformat()}.json"
            data = {
                "date": report_date.isoformat(),
                "stocks": [dataclasses.asdict(r) for r in self._today_recommendations],
            }
            path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
            logger.debug(f"[레포트] 추천 종목 저장: {path} ({len(self._today_recommendations)}종목)")
        except Exception as e:
            logger.warning(f"[레포트] 추천 종목 저장 실패: {e}")

    def _load_recommendations(self, report_date: date) -> List["RecommendedStock"]:
        """파일에서 추천 종목 복원"""
        try:
            path = _REC_CACHE_DIR / f"morning_recs_{report_date.isoformat()}.json"
            if not path.exists():
                return []
            raw = json.loads(path.read_text(encoding="utf-8"))
            if raw.get("date") != report_date.isoformat():
                return []
            stocks = []
            for d in raw.get("stocks", []):
                # risk_factors 필드 타입 보정
                d["risk_factors"] = d.get("risk_factors") or []
                stocks.append(RecommendedStock(**d))
            return stocks
        except Exception as e:
            logger.warning(f"[레포트] 추천 종목 로드 실패: {e}")
            return []

    async def _update_results(self):
        """추천 종목 결과 업데이트 (KIS API → pykrx 종가 → 네이버 금융 순)"""
        import aiohttp

        today_str = date.today().strftime("%Y%m%d")

        for rec in self._today_recommendations:
            try:
                price = None

                # 1차: KIS API (실시간, 정확)
                if hasattr(self, '_bot') and hasattr(self._bot, 'broker') and self._bot.broker:
                    try:
                        quote = await self._bot.broker.get_quote(rec.symbol)
                        if quote and quote.get("price", 0) > 0:
                            price = quote["price"]
                    except Exception:
                        pass

                # 2차: pykrx 종가 (장 마감 후 안정적)
                if price is None:
                    try:
                        def _fetch_pykrx(sym: str, date_str: str) -> Optional[float]:
                            from pykrx import stock as pykrx_stock
                            df = pykrx_stock.get_market_ohlcv(date_str, date_str, sym)
                            if df is not None and not df.empty:
                                return float(df["종가"].iloc[-1])
                            return None

                        price = await asyncio.to_thread(_fetch_pykrx, rec.symbol, today_str)
                        if price:
                            logger.debug(f"[결과] {rec.symbol} pykrx 종가: {price:,.0f}원")
                    except Exception as e:
                        logger.debug(f"[결과] {rec.symbol} pykrx 조회 실패: {e}")

                # 3차: 네이버 금융 HTML 파싱 (최후 폴백)
                if price is None:
                    try:
                        async with aiohttp.ClientSession() as session:
                            url = f"https://finance.naver.com/item/main.nhn?code={rec.symbol}"
                            async with session.get(url, timeout=aiohttp.ClientTimeout(total=10)) as resp:
                                if resp.status == 200:
                                    html = await resp.text()
                                    price_match = re.search(r'<span class="blind">현재가</span>([0-9,]+)', html)
                                    if not price_match:
                                        price_match = re.search(r'class="no_today"[^>]*>.*?<span[^>]*>([0-9,]+)', html, re.DOTALL)
                                    if price_match:
                                        price = float(price_match.group(1).replace(",", ""))
                    except Exception as e:
                        logger.debug(f"[결과] {rec.symbol} 네이버 조회 실패: {e}")

                if price is not None and price > 0:
                    rec.result_price = price
                    if rec.prev_close > 0:
                        rec.result_pct = (rec.result_price - rec.prev_close) / rec.prev_close * 100
                    else:
                        rec.result_pct = 0.0
                    logger.debug(f"[결과] {rec.symbol}: {rec.result_price:,.0f}원 ({rec.result_pct:+.1f}%)")
                else:
                    logger.warning(f"[결과] {rec.symbol}: 종가 조회 실패 (3개 소스 모두 실패)")

            except Exception as e:
                logger.warning(f"현재가 조회 실패 ({rec.symbol}): {e}")
                rec.result_price = None
                rec.result_pct = None

    async def _fetch_sector_summary(self) -> List[str]:
        """업종지수 상승/하락 TOP 5 요약"""
        kmd = self._kis_market_data
        if not kmd:
            try:
                from ..data.providers.kis_market_data import get_kis_market_data
                kmd = get_kis_market_data()
            except Exception:
                return []

        try:
            sectors = await kmd.fetch_sector_indices()
            if not sectors:
                return []

            # 등락률 파싱
            parsed = []
            for s in sectors:
                name = s.get("name", "")
                change_pct = s.get("change_pct", 0.0)
                if name:
                    parsed.append((name, change_pct))

            if not parsed:
                return []

            parsed.sort(key=lambda x: x[1], reverse=True)

            lines = ["📈 <b>업종 동향 (전일 기준)</b>"]

            # 상승 TOP 5
            top = [f"{n}({p:+.1f}%)" for n, p in parsed[:5] if p > 0]
            if top:
                lines.append(f"  ▲ 상승: {' / '.join(top)}")

            # 하락 TOP 5
            bottom = [f"{n}({p:+.1f}%)" for n, p in parsed[-5:] if p < 0]
            if bottom:
                bottom.reverse()
                lines.append(f"  ▼ 하락: {' / '.join(bottom)}")

            lines.append("")
            return lines

        except Exception as e:
            logger.warning(f"[레포트] 업종 동향 조회 실패: {e}")
            return []

    async def _fetch_us_market_summary(self) -> List[str]:
        """US 시장 오버나이트 요약 (텔레그램 HTML)"""
        umd = self._us_market_data
        if not umd:
            try:
                from ..data.providers.us_market_data import get_us_market_data
                umd = get_us_market_data()
            except Exception:
                return []

        try:
            signal = await umd.get_overnight_signal()
            if not signal or not signal.get("indices"):
                return []

            sentiment = signal.get("sentiment", "neutral")
            indices = signal.get("indices", {})
            sector_signals = signal.get("sector_signals", {})

            # 심리 이모지
            sentiment_emoji = {
                "bullish": "📈", "bearish": "📉", "neutral": "➡️"
            }.get(sentiment, "➡️")
            sentiment_kr = {
                "bullish": "강세", "bearish": "약세", "neutral": "보합"
            }.get(sentiment, "보합")

            lines = [f"{sentiment_emoji} <b>US 시장 마감 ({sentiment_kr})</b>"]

            # 지수 등락률
            idx_parts = []
            for name, info in indices.items():
                pct = info["change_pct"]
                arrow = "▲" if pct > 0 else ("▼" if pct < 0 else "─")
                idx_parts.append(f"{name} {arrow}{abs(pct):.1f}%")
            if idx_parts:
                lines.append(f"  {' / '.join(idx_parts)}")

            # 한국 테마 연동 (부스트가 있는 테마만)
            if sector_signals:
                boost_parts = []
                for theme, sig in sector_signals.items():
                    boost = sig["boost"]
                    if boost > 0:
                        boost_parts.append(f"{theme}(+{boost})")
                    elif boost < 0:
                        boost_parts.append(f"{theme}({boost})")
                if boost_parts:
                    lines.append(f"  → 한국 테마 영향: {', '.join(boost_parts)}")

            lines.append("")
            return lines

        except Exception as e:
            logger.warning(f"[레포트] US 시장 요약 조회 실패: {e}")
            return []

    async def generate_us_market_report(
        self, send_telegram: bool = True, kr_inputs: Optional[List[Dict]] = None,
    ) -> str:
        """
        미국증시 마감 레포트 생성 (매일 07:00)

        Yahoo Finance 데이터 기반으로 지수, 섹터 ETF, 개별 종목 등락을
        한눈에 보기 좋게 정리하여 텔레그램 발송.

        Args:
            kr_inputs: 국내 장전 자료(as_of 포함) — build_morning_brief 와 동일한
                형식. 없으면(현재 기본 경로) 고정 문구(market_msg)도 한국 개장
                방향을 단정하지 않는다 (2026-09-15 T10 F21 — scope 판정을
                brief_scope() 한 곳으로 통일, LLM 브리프와 반드시 같은 값을 쓴다).
        """
        from ..data.providers.us_market_data import (
            get_us_market_data, US_KOREA_SECTOR_MAP, INDEX_SYMBOLS, INDEX_NAMES,
        )

        umd = self._us_market_data or get_us_market_data()
        # 07:00 레포트는 캐시를 무시하고 최신 US 마감 데이터 강제 조회
        quotes = await umd.fetch_us_market_summary(force_refresh=True)

        if not quotes:
            msg = "⚠️ 미국증시 데이터 조회 실패"
            if send_telegram:
                await self.telegram.send_report(msg)
            return msg

        now = datetime.now()

        # US 마감일 계산: KST 전날 기준, 주말이면 금요일로 조정
        # (US 공휴일은 별도 처리 생략 — 해당일 Yahoo Finance가 직전 영업일 반환)
        us_close_date = (now - timedelta(days=1)).date()
        if us_close_date.weekday() == 5:   # 토 → 금
            us_close_date -= timedelta(days=1)
        elif us_close_date.weekday() == 6:  # 일 → 금
            us_close_date -= timedelta(days=2)
        us_date_str = us_close_date.strftime("%Y.%m.%d")   # US 마감일 (현지)
        kst_date_str = now.strftime("%m/%d")                # KST 오늘 날짜 (보고서 발송일)

        # ── 지수 ──
        idx_lines = []
        idx_pcts = []
        for sym in INDEX_SYMBOLS:
            q = quotes.get(sym)
            if not q:
                continue
            name = INDEX_NAMES.get(sym, sym)
            pct = q["change_pct"]
            price = q["price"]
            idx_pcts.append(pct)
            if pct > 0:
                idx_lines.append(f"  🔼 {name}  <b>+{pct:.2f}%</b>  ({price:,.1f})")
            elif pct < 0:
                idx_lines.append(f"  🔽 {name}  <b>{pct:.2f}%</b>  ({price:,.1f})")
            else:
                idx_lines.append(f"  ▪️ {name}  0.00%  ({price:,.1f})")

        # 지수 4종이 전부 결측이면(개별 종목만 살아남는 부분 응답) avg_pct=0 으로 떨어져
        # "+0.00% 보합권 마감" 이라는 없는 사실이 세 발송 경로로 나갔다 — 결측을 0·보합으로
        # 포장하지 않는다 (2026-09-15 T10 통합 리뷰 INT-2 blocking). avg_pct 는 하위
        # 호출부 타입 호환을 위해 0.0 을 유지하되 문구는 idx_missing 으로만 결정한다.
        idx_missing = not idx_pcts
        avg_pct = sum(idx_pcts) / len(idx_pcts) if idx_pcts else 0.0
        if idx_missing:
            mood = "⚠️ 지수 시세 미수집"
        elif avg_pct >= 1.0:
            mood = "📈 강세 마감"
        elif avg_pct <= -1.0:
            mood = "📉 약세 마감"
        else:
            mood = "➡️ 보합 마감"

        # brief_scope() 와 동일 판정 — market_msg 뿐 아니라 섹터 매핑 헤더도
        # 이 값을 써야 대상 시장·시간범위 표기가 market_msg 와 어긋나지 않는다
        # (2026-09-15 T10 F21 advisory — 헤더는 방향 단정은 아니었지만 대상
        # 시장·시간범위가 없어 F21 이 요구한 문장 단위 명시 기준에서 비어 있었다)
        scope = brief_scope(kr_inputs)
        sector_header = (
            "<b>■ 미국 섹터 → 국내 테마 매핑 (전일 미국 세션 기준, 국내 개장 반영 아님)</b>"
            if scope == SCOPE_US_ONLY else "<b>■ 한국 시장 영향</b>"
        )

        lines = [
            f"🇺🇸 <b>미국증시 마감 리포트</b>",
            f"<i>{us_date_str} NY 마감 (KST {kst_date_str} 07:00 수신)</i>",
            "",
            f"<b>■ 주요 지수  {mood}</b>",
        ]
        lines.extend(idx_lines)
        lines.append("")

        # ── 빅테크 ──
        bigtech = ["NVDA", "AAPL", "MSFT", "GOOG", "META", "AMZN", "TSLA"]
        bt_lines = []
        for sym in bigtech:
            q = quotes.get(sym)
            if not q:
                continue
            pct = q["change_pct"]
            name = q.get("name", sym)
            # 이름이 너무 길면 축약
            if len(name) > 12:
                name = name[:12]
            if pct > 0:
                bt_lines.append(f"{sym} <b>+{pct:.1f}%</b>")
            elif pct < 0:
                bt_lines.append(f"{sym} {pct:.1f}%")
            else:
                bt_lines.append(f"{sym} 0.0%")

        if bt_lines:
            lines.append(f"<b>■ 빅테크</b>")
            # 4개씩 줄바꿈
            for i in range(0, len(bt_lines), 4):
                lines.append("  " + "  ".join(bt_lines[i:i + 4]))
            lines.append("")

        # ── 섹터 ETF + 개별종목 (테마 매핑) ──
        sector_signals = await umd.get_sector_signals()
        if sector_signals:
            lines.append(sector_header)
            for theme, sig in sorted(
                sector_signals.items(),
                key=lambda x: abs(x[1]["boost"]),
                reverse=True,
            ):
                boost = sig["boost"]
                avg = sig["us_avg_pct"]
                movers = sig.get("top_movers", [])
                icon = "🔺" if boost > 0 else "🔻"
                movers_str = ", ".join(movers[:3])
                lines.append(
                    f"  {icon} <b>{theme}</b> (부스트 {boost:+d}점)"
                )
                lines.append(f"      평균 {avg:+.1f}%  {movers_str}")
            lines.append("")

        # ── 공포/탐욕 지표 대용: VIX ──
        # VIX는 US_SYMBOLS에 없으므로 별도 조회 불필요, 지수 평균으로 대체
        #
        # 2026-09-15 T10 F21: 이 고정 문구가 미국 지수 평균만으로 "한국 관련
        # 테마주 갭업 가능성" 같은 국내 개장 방향을 단정해 왔다(정상/차트동반/
        # 폴백 세 경로 모두 이 market_msg 를 그대로 발송). 국내 자료(kr_inputs)
        # 없이는 문장 대상을 "미국 세션"으로 한정하고 한국 판단은 보류한다.
        # brief_scope() 는 build_morning_brief 의 LLM 스코프 판정과 동일한
        # 함수라 두 판정이 어긋나지 않는다 (scope 는 위에서 sector_header 와
        # 함께 이미 계산했다).
        if idx_missing:
            # 지수 시세가 한 건도 없으면 미국 마감 방향도 단정하지 않는다 (0 포장 금지)
            market_msg = "⚠️ 미국 지수 시세 미수집 — 마감 방향 판단 불가 (한국 개장 방향 판단 보류)"
        elif scope == SCOPE_WITH_KR:
            if avg_pct >= 1.5:
                market_msg = "💡 강한 상승 — 한국 관련 테마주 갭업 가능성"
            elif avg_pct >= 0.5:
                market_msg = "💡 소폭 상승 — 반도체·IT 섹터 긍정적"
            elif avg_pct <= -1.5:
                market_msg = "⚠️ 강한 하락 — 한국 시장 하방 압력 주의"
            elif avg_pct <= -0.5:
                market_msg = "⚠️ 소폭 하락 — 보수적 접근 권장"
            else:
                market_msg = "💡 변동 미미 — 국내 자체 재료에 주목"
        else:
            if avg_pct >= 1.5:
                mkt_fact = f"💡 미국 지수 평균 {avg_pct:+.2f}% 강한 상승 마감 (전일 미국 세션)"
            elif avg_pct >= 0.5:
                mkt_fact = f"💡 미국 지수 평균 {avg_pct:+.2f}% 소폭 상승 마감 (전일 미국 세션)"
            elif avg_pct <= -1.5:
                mkt_fact = f"⚠️ 미국 지수 평균 {avg_pct:+.2f}% 강한 하락 마감 (전일 미국 세션)"
            elif avg_pct <= -0.5:
                mkt_fact = f"⚠️ 미국 지수 평균 {avg_pct:+.2f}% 소폭 하락 마감 (전일 미국 세션)"
            else:
                mkt_fact = f"➡️ 미국 지수 평균 {avg_pct:+.2f}% 보합권 마감 (전일 미국 세션)"
            market_msg = f"{mkt_fact} — 국내 자료 없음, 한국 개장 방향 판단 보류"

        lines.append(f"<b>■ 오늘의 포인트</b>")
        lines.append(f"  {market_msg}")

        report = "\n".join(lines)

        if send_telegram:
            # ── 1) 통합 차트 이미지 생성 (섹터 히트맵 + S&P500 맵 합본) ──
            chart_sent = False
            try:
                from .us_market_chart import generate_combined_chart
                from ..utils.telegram import send_photo as tg_send_photo

                # S&P500 개별 종목 시세 조회
                try:
                    stock_quotes = await umd.fetch_sp500_stocks()
                except Exception as sp_err:
                    logger.warning(f"[레포트] S&P500 시세 조회 실패 (빈 맵으로 진행): {sp_err}")
                    stock_quotes = {}

                # 통합 차트: 지수 카드 + 섹터 ETF 히트맵 + S&P500 맵
                combined_buf = generate_combined_chart(
                    quotes=quotes,
                    stock_quotes=stock_quotes,
                    date_str=us_date_str,
                    avg_pct=avg_pct,
                )
                if combined_buf:
                    caption_lines = [
                        f"🇺🇸 <b>미국증시 마감</b>  {us_date_str}  {mood}",
                        "",
                    ]
                    caption_lines.extend(idx_lines)
                    caption = "\n".join(caption_lines)[:1024]

                    _report_cid = self.telegram.report_chat_id
                    chart_sent = await tg_send_photo(
                        combined_buf, caption=caption, parse_mode="HTML",
                        chat_id=_report_cid,
                    )
                    if chart_sent:
                        logger.info(f"[레포트] 통합 차트 이미지 발송 완료 → {_report_cid}")
                    else:
                        logger.warning("[레포트] 통합 차트 이미지 발송 실패")

            except Exception as chart_err:
                logger.error(f"[레포트] 차트 생성/전송 오류: {chart_err}", exc_info=True)

            # ── 2) 텍스트 리포트 전송 ─────────────────────────────────────
            # 차트 전송 성공 시 → 섹터 영향 + 포인트만 (중복 지수 생략)
            # 차트 전송 실패 시 → 전체 report 텍스트 전송
            if chart_sent:
                detail_lines = []
                if bt_lines:
                    detail_lines.append("<b>■ 빅테크</b>")
                    for i in range(0, len(bt_lines), 4):
                        detail_lines.append("  " + "  ".join(bt_lines[i:i + 4]))
                    detail_lines.append("")
                if sector_signals:
                    detail_lines.append(sector_header)
                    for theme, sig in sorted(
                        sector_signals.items(),
                        key=lambda x: abs(x[1]["boost"]),
                        reverse=True,
                    ):
                        boost = sig["boost"]
                        avg_s = sig["us_avg_pct"]
                        movers = sig.get("top_movers", [])
                        icon = "🔺" if boost > 0 else "🔻"
                        movers_str = ", ".join(movers[:3])
                        detail_lines.append(
                            f"  {icon} <b>{theme}</b> (부스트 {boost:+d}점)"
                        )
                        detail_lines.append(
                            f"      평균 {avg_s:+.1f}%  {movers_str}"
                        )
                    detail_lines.append("")
                detail_lines.append("<b>■ 오늘의 포인트</b>")
                detail_lines.append(f"  {market_msg}")
                if detail_lines:
                    await self.telegram.send_report("\n".join(detail_lines))
            else:
                # 차트 없이 텍스트 전체 전송 (fallback)
                success = await self.telegram.send_report(report)
                if success:
                    logger.info("[레포트] 미국증시 텍스트 레포트 발송 완료")
                else:
                    logger.error("[레포트] 미국증시 레포트 발송 실패")

            # ── 3) LLM 증권사 모닝브리프 (2026-05-13 추가, 2026-06-05 캐시화) ─
            # 2026-06-05: 별도 발송 중단 → 캐시 파일에 저장.
            # 07:30 전문가 브리핑(_send_expert_briefing_telegram)이 캐시를 읽어
            # 하나의 통합 메시지로 발송한다.
            try:
                brief_record = await self.build_morning_brief(
                    quotes=quotes,
                    sector_signals=sector_signals,
                    avg_pct=avg_pct,
                    us_date_str=us_date_str,
                    mood=mood,
                    kr_inputs=kr_inputs,
                )
                if brief_record:
                    cache_path = save_morning_brief(brief_record)
                    logger.info(
                        f"[레포트] LLM 모닝브리프 캐시 저장 → {cache_path} "
                        f"(scope={brief_record['scope']}, "
                        f"제거된 단정 {len(brief_record['removed_claims'])}건)"
                    )
            except Exception as brief_err:
                logger.error(f"[레포트] LLM 모닝브리프 생성/캐시 실패: {brief_err}", exc_info=True)

        return report

    async def build_morning_brief(
        self,
        *,
        quotes: Dict,
        sector_signals: Dict,
        avg_pct: float,
        us_date_str: str,
        mood: str,
        kr_inputs: Optional[List[Dict]] = None,
        expert_consensus: Optional[Dict] = None,
    ) -> Optional[Dict]:
        """LLM 모닝브리프 생성 (2026-05-13 신규, 2026-09-14 주장 범위 제한)

        입력이 미국 마감 자료뿐이면 제목·본문을 "미국시장 마감 요약"으로 제한하고
        한국장 개장 방향·갭·대응 전략 단정을 금지한다 (프롬프트 규칙 + 응답 후 검사).
        기준시각(as_of)이 있는 국내 자료가 주어질 때만 "개장 관찰 포인트"를 허용하되
        반대 근거를 함께 요구한다.

        Returns:
            고정 스키마 레코드 (us_date/text/generated_at/model/scope/inputs/claims/…)
            또는 실패 시 None
        """
        try:
            from ..utils.llm import get_llm_manager, LLMTask
        except Exception as e:
            logger.warning(f"[모닝브리프] LLM 모듈 로드 실패: {e}")
            return None

        # ── 입력 정리 (사용 자료와 기준시각을 레코드에 고정 저장) ──
        index_list = ["^GSPC", "^IXIC", "^DJI", "^SOX", "^VIX"]
        index_names_map = {
            "^GSPC": "S&P500", "^IXIC": "NASDAQ", "^DJI": "DOW",
            "^SOX": "필라델피아 반도체(SOX)", "^VIX": "VIX(공포지수)",
        }
        idx_lines = []
        for sym in index_list:
            q = quotes.get(sym)
            if not q:
                continue
            name = index_names_map.get(sym, sym)
            pct = q.get("change_pct", 0.0)
            price = q.get("price", 0.0)
            idx_lines.append(f"  - {name}: {pct:+.2f}% ({price:,.1f})")

        bigtech = ["NVDA", "AAPL", "MSFT", "GOOG", "META", "AMZN", "TSLA"]
        bt_lines = []
        for sym in bigtech:
            q = quotes.get(sym)
            if q:
                bt_lines.append(f"{sym} {q.get('change_pct', 0):+.1f}%")

        sector_lines = []
        if sector_signals:
            for theme, sig in sorted(
                sector_signals.items(),
                key=lambda x: abs(x[1].get("boost", 0)),
                reverse=True,
            )[:5]:
                boost = sig.get("boost", 0)
                avg_s = sig.get("us_avg_pct", 0)
                movers = sig.get("top_movers", [])[:3]
                sector_lines.append(
                    f"  - {theme}: 부스트 {boost:+d}점, 평균 {avg_s:+.1f}%, {', '.join(movers)}"
                )

        inputs: List[Dict] = [
            {"name": "미국 지수", "kind": "us", "as_of": us_date_str,
             "source": "us_market_data", "valid": bool(idx_lines),
             "detail": f"{len(idx_lines)}종"},
            {"name": "빅테크", "kind": "us", "as_of": us_date_str,
             "source": "us_market_data", "valid": bool(bt_lines),
             "detail": f"{len(bt_lines)}종"},
            {"name": "US섹터→KR테마 매핑", "kind": "us", "as_of": us_date_str,
             "source": "us_market_data", "valid": bool(sector_lines),
             "detail": f"{len(sector_lines)}건"},
        ]

        # 국내 자료는 as_of 가 있을 때만 유효로 인정한다 (기준시각 없는 값은 신선도 미상)
        valid_kr: List[Dict] = []
        for item in (kr_inputs or []):
            entry = {
                "name": item.get("name"), "kind": "kr",
                "as_of": item.get("as_of"), "source": item.get("source"),
                "value": item.get("value"),
                "valid": bool(item.get("as_of")),
            }
            if not entry["valid"]:
                entry["reason"] = "as_of 없음 — 유효 국내 자료로 인정하지 않음"
            else:
                valid_kr.append(entry)
            inputs.append(entry)

        # brief_scope() 와 같은 as_of 판정을 쓴다 (F21 — 고정 문구와 단일 지점 공유)
        scope = brief_scope(kr_inputs)

        if scope == SCOPE_WITH_KR:
            kr_block = "\n".join(
                f"  - {i['name']}: {i.get('value')} (기준시각 {i['as_of']}, 출처 {i.get('source')})"
                for i in valid_kr
            )
            scope_rules = (
                "5. <b>■ 개장 관찰 포인트</b>: 위 국내 자료(기준시각 명시)로만 관찰 포인트 2건.\n"
                "   각 포인트마다 <b>반대 근거</b>를 반드시 함께 쓸 것. 단정 대신 조건부로 서술."
            )
            title = "🧠 <b>LLM 모닝브리프</b> — 미국시장 마감 + 국내 장전 자료"
        else:
            kr_block = ""
            scope_rules = (
                "5. <b>■ 판단 보류</b>: 국내 수급·선물·환율·장전 뉴스가 입력에 없다.\n"
                "   따라서 한국장 <b>개장 방향·갭·시가 대응 전략을 쓰지 말 것</b>.\n"
                f"   대신 '{NO_KR_INPUT_NOTE}' 라고만 쓸 것."
            )
            title = "🧠 <b>LLM 모닝브리프</b> — 미국시장 마감 요약"

        prompt = f"""{'한국 장전 자료를 포함한 미국증시 마감 브리프' if scope == SCOPE_WITH_KR else '미국시장 마감 요약'} 작성.

[기준일] {us_date_str} 미국시장 마감

[주요 지수]
{chr(10).join(idx_lines) if idx_lines else '  (데이터 없음)'}
지수 평균 등락: {avg_pct:+.2f}%
시장 분위기: {mood}

[빅테크]
  {' / '.join(bt_lines) if bt_lines else '(데이터 없음)'}

[US 섹터 → KR 테마 매핑]
{chr(10).join(sector_lines) if sector_lines else '  (영향 미미)'}
{f'{chr(10)}[국내 장전 자료]{chr(10)}{kr_block}' if kr_block else ''}

★ 자료 범위 규칙 (반드시 준수):
- 위에 주어진 자료로 확인되는 것만 쓴다. 없는 자료를 추정해 단정하지 않는다.
- 국내 수급·공매도·최신 선물·환율·장전 뉴스는 {'일부만 주어졌다' if scope == SCOPE_WITH_KR else '주어지지 않았다'}.
- 근거가 없으면 "추가 확인 필요"라고 쓴다. 중립·확신으로 포장하지 않는다.

출력 형식 (이 순서):
1. <b>■ 미국시장 종합 평가</b>: 2~3문장 (분위기, 변동성, 동력)
2. <b>■ 핵심 이슈 분석</b>: 자료에서 추론 가능한 이슈 2~3건 (단서 없으면 "추가 확인 필요")
3. <b>■ 미국 섹터 흐름</b>: 강세 2~3개 + 약세 2~3개 (이유 포함)
4. <b>■ 한국 테마 연결</b>: 위 매핑에 나온 테마만 언급. {'개장 방향은 단정하지 않는다.' if scope == SCOPE_US_ONLY else '조건부로만 서술한다.'}
{scope_rules}

500~800자 권장. 마지막에 "<i>※ 본 분석은 LLM 자동 생성. 투자 판단 본인 책임.</i>" 추가.
"""

        llm = get_llm_manager()
        try:
            result = await asyncio.wait_for(
                llm.complete(
                    prompt=prompt,
                    system=(
                        "당신은 한국 증권사 리서치센터 미국시장 담당 애널리스트. "
                        "주어진 자료 범위 밖을 단정하지 않는다."
                    ),
                    task=LLMTask.MARKET_ANALYSIS,
                    max_tokens=1200,
                ),
                timeout=45.0,
            )
        except asyncio.TimeoutError:
            logger.warning("[모닝브리프] LLM 타임아웃 (45s)")
            return None
        except Exception as e:
            logger.warning(f"[모닝브리프] LLM 호출 실패: {e}")
            return None

        if not result:
            return None

        # 성공 여부 확인 (LLMResponse 우선)
        if hasattr(result, "success") and not result.success:
            err = getattr(result, "error", "unknown")
            logger.warning(f"[모닝브리프] LLM 호출 실패: {err}")
            return None

        text = getattr(result, "content", None)
        if text is None:
            if isinstance(result, dict):
                text = result.get("content") or result.get("text") or ""
            else:
                text = str(result)
        raw_text = (text or "").strip()
        if not raw_text:
            logger.warning("[모닝브리프] LLM 응답 빈값")
            return None

        # ── 응답 후 검사: 국내 자료 없이 나온 개장 방향 단정 제거 ──
        body, removed = sanitize_brief_claims(raw_text, scope)
        if removed:
            logger.warning(
                f"[모닝브리프] 국내 자료 없는 개장 단정 {len(removed)}건 제거: "
                f"{removed[0][:60]}"
            )

        tone = brief_tone(raw_text)
        conflict = build_expert_conflict_note(tone, expert_consensus)
        if conflict:
            body = body.rstrip() + "\n\n" + conflict

        claims = extract_brief_claims(
            body, sector_signals, scope,
            basis=[i["name"] for i in inputs if i.get("valid")],
        )

        header = f"{title}\n<i>{us_date_str} 미국 마감 기반</i>\n\n"
        # Telegram 메시지 길이 제한 (~4096자) 안전 마진 — 발송/캐시용 "text" 는
        # 잘라내되(save_morning_brief 가 최신 캐시엔 이 절단본만 쓴다), 원문·
        # 정제 전체 본문은 raw_text/body_full 에 그대로 담아 아카이브에서
        # 사후 검증할 수 있게 한다 (2026-09-15 T10 F22 blocking — 절단 이상으로
        # sanitize 가 무엇을 지웠는지도 원문 대조 없이는 알 수 없었다).
        return {
            "us_date": us_date_str,
            # 평가 대상 KR 거래일 — 저녁 평가가 전날 브리프를 오늘 실측과 대조하지 않도록
            "kr_date": _today().isoformat(),
            "title": title,
            "text": header + body[:3800],
            "raw_text": raw_text,
            "body_full": body,
            "generated_at": datetime.now().isoformat(),
            "model": getattr(result, "model", None) or "unknown",
            "scope": scope,
            "inputs": inputs,
            "claims": claims,
            "removed_claims": removed,
            "tone": tone,
            "expert_consensus": expert_consensus,
            "expert_conflict": conflict,
        }

    def _resolve_brief_for_eval(self, kr_date: str, brief_path: Path, archive_dir=None):
        """평가에 쓸 브리프를 고른다 — 발송 스냅샷 우선 (2026-09-15 T10 F19/F22).

        우선순위: 1) 07:30 발송 성공(status=sent) 스냅샷의 expert_consensus로
        평가 2) 발송이 실패만 했으면 생성본은 쓰되 전문가 축은 평가하지 않음
        3) 발송 기록이 아예 없으면 생성본만 쓰고 전문가 축은 "발송 기록 없음"
        4) 아카이브 자체가 없으면(구버전 호환) 단일 캐시 파일로 폴백.

        Returns:
            (brief, dispatched, brief_ref, dispatch_reason)
        """
        archive = _load_brief_archive(kr_date, archive_dir)
        generated = archive.get("generated") or []
        dispatches = archive.get("dispatch") or []

        def _find_generated(ref):
            if not ref:
                return None
            for g in generated:
                if g.get("version") == ref.get("version"):
                    return g
            return None

        sent = next((d for d in reversed(dispatches) if d.get("status") == "sent"), None)
        if sent is not None:
            base = _find_generated(sent.get("brief_ref")) or (generated[-1] if generated else {})
            brief = dict(base)
            brief["kr_date"] = kr_date
            brief["expert_consensus"] = sent.get("expert_consensus")
            return brief, True, sent.get("brief_ref"), None

        failed = next((d for d in reversed(dispatches) if d.get("status") == "failed"), None)
        dispatch_reason = "07:30 발송 실패 — 전문가 판단 미평가" if failed else "07:30 발송 기록 없음"
        if generated:
            brief = dict(generated[-1])
            brief["kr_date"] = kr_date
            brief["expert_consensus"] = None
            return brief, False, None, dispatch_reason

        if failed is not None:
            # 발송 시도는 있었지만 생성본이 없다 (LLM 실패 등) — 평가할 본문 자체가 없다
            return None, False, None, dispatch_reason

        # 아카이브 없음 (구버전 단일 캐시 호환)
        if brief_path.exists():
            try:
                brief = json.loads(brief_path.read_text(encoding="utf-8"))
            except Exception as e:
                logger.warning(f"[브리프평가] 브리프 캐시 파싱 실패 → 스킵: {e}")
                return None, False, None, None
            return brief, False, None, dispatch_reason

        return None, False, None, None

    async def evaluate_morning_brief(
        self,
        report_date: Optional[date] = None,
        *,
        brief_path=None,
        ledger_path=None,
        archive_dir=None,
    ) -> Optional[Dict]:
        """장전 브리프의 장후 평가 (2026-09-14 T9 요청 5, 2026-09-15 T10 F19/F22 갱신).

        날짜별 아카이브(발송 스냅샷 우선)에서 브리프를 읽어 실제 발송 판단을
        평가하고, KOSPI/KOSDAQ 시가·종가 + 업종 수익률을 모아
        morning_brief_eval.evaluate() 로 판정한 뒤 JSONL 원장에 누적한다.
        같은 날짜 레코드가 이미 원장에 있으면 재기록하지 않는다 (평가 분모
        중복 증가 방지 — 재실행/재시도 대비).

        Args:
            report_date: 평가 대상일 (기본 오늘). 오늘이 아니면 실측을 새로
                수집하지 않는다 (현재가 API라 과거 시점 재현 불가).
            brief_path: 구버전 단일 캐시 폴백 경로 (기본 llm_morning_brief.json)
            ledger_path: 평가 원장 경로 (기본 morning_brief_eval.jsonl)
            archive_dir: 날짜별 아카이브 디렉터리 (기본 brief_path 옆의 morning_brief/
                — record_morning_brief_dispatch 의 기본(MORNING_BRIEF_PATH 옆)과
                같은 규칙, 단일 출처)

        Returns:
            원장에 기록한(또는 이미 있던) 평가 레코드. 브리프가 없으면 None.
        """
        from . import morning_brief_eval

        report_date = report_date or _today()
        kr_date = report_date.isoformat()
        brief_path = Path(brief_path) if brief_path is not None else MORNING_BRIEF_PATH
        ledger_path = (
            Path(ledger_path) if ledger_path is not None else MORNING_BRIEF_LEDGER_PATH
        )
        # archive_dir 미지정 시 brief_path 와 같은 디렉터리 밑 — save_morning_brief 의
        # 기본 규칙과 동일해서 운영 기본 경로끼리는 자동으로 일치하고, 테스트가
        # tmp_path 로 brief_path 를 넘기면 아카이브 조회도 같이 격리된다.
        archive_dir = Path(archive_dir) if archive_dir is not None else (brief_path.parent / "morning_brief")

        existing = morning_brief_eval.find_ledger_record(ledger_path, kr_date)
        if existing is not None:
            logger.info(f"[브리프평가] {kr_date} 이미 평가됨 → 재기록 스킵")
            return existing

        brief, dispatched, brief_ref, dispatch_reason = self._resolve_brief_for_eval(
            kr_date, brief_path, archive_dir,
        )
        if brief is None:
            logger.info(f"[브리프평가] 브리프 없음 → 스킵 ({kr_date})")
            return None

        actual = {"date": kr_date}
        try:
            actual.update(await self._collect_brief_actuals(report_date))
        except Exception as e:
            logger.warning(f"[브리프평가] 실측 수집 실패: {e}")

        record = morning_brief_eval.evaluate(
            brief, actual,
            dispatched=dispatched, brief_ref=brief_ref, dispatch_reason=dispatch_reason,
        )
        morning_brief_eval.append_ledger(ledger_path, record)
        logger.info(
            f"[브리프평가] {record['date']} evaluated={record['evaluated']} "
            f"개장={record['open_direction']['hit']} 종가={record['close_direction']['hit']} "
            f"dispatched={dispatched}"
        )
        return record

    async def _collect_brief_actuals(self, report_date: Optional[date] = None) -> Dict:
        """평가용 실측 — KOSPI/KOSDAQ 시가·종가 방향 + 업종 수익률

        결측은 키를 만들지 않는다 (0 으로 채우면 미평가가 적중으로 둔갑한다).
        현재가 조회 API 라 report_date 가 오늘이 아니면 실측을 수집하지 않는다
        (2026-09-15 T10 F22 — 과거 날짜 재평가 시 오늘 실측을 그 날짜로
        재라벨링하면 안 된다).
        """
        if report_date is not None and report_date != _today():
            logger.info(
                f"[브리프평가] {report_date} 과거 날짜 재평가 — 현재가 API라 실측 미수집"
            )
            return {"note": "과거 날짜 재평가 — 현재가 API라 당시 실측을 재수집할 수 없음"}

        kmd = self._kis_market_data
        if not kmd:
            from ..data.providers.kis_market_data import get_kis_market_data
            kmd = get_kis_market_data()

        out: Dict = {}
        for code, key in (("0001", "kospi"), ("1001", "kosdaq")):
            try:
                data = await kmd.fetch_index_price(code)
            except Exception as e:
                logger.debug(f"[브리프평가] 지수 조회 실패 {key}: {e}")
                continue
            if not data:
                continue
            price = data.get("price")
            change = data.get("change")
            open_price = data.get("open")
            close_pct = data.get("change_pct")
            index_actual: Dict = {}
            if close_pct is not None:
                index_actual["close_change_pct"] = close_pct
            # 시가 방향 = (시가 - 전일종가) / 전일종가. 전일종가 = 현재가 - 전일대비
            if price is not None and change is not None and open_price is not None:
                prev_close = price - change
                if prev_close > 0 and open_price > 0:
                    index_actual["open_change_pct"] = round(
                        (open_price - prev_close) / prev_close * 100, 4
                    )
            if index_actual:
                out[key] = index_actual

        try:
            sectors = await kmd.fetch_sector_indices()
        except Exception as e:
            logger.debug(f"[브리프평가] 업종 조회 실패: {e}")
            sectors = None
        if sectors:
            out["sectors"] = {
                s.get("name"): s.get("change_pct")
                for s in sectors
                if s.get("name") and s.get("change_pct") is not None
            }
        return out

    def _format_morning_report(
        self,
        recommendations: List[RecommendedStock],
        hot_themes: List,
        report_date: date,
        sector_lines: Optional[List[str]] = None,
        us_lines: Optional[List[str]] = None,
    ) -> str:
        """아침 레포트 포맷팅"""

        date_str = report_date.strftime("%Y년 %m월 %d일")

        lines = [
            f"📊 <b>오늘의 추천 종목 ({len(recommendations)}개)</b>",
            f"<i>{date_str} 08:00 기준</i>",
            "",
        ]

        # 핫 테마
        if hot_themes:
            theme_strs = [f"{t.name}({t.score:.0f})" for t in hot_themes[:5]]
            lines.append(f"🔥 <b>핫 테마:</b> {' / '.join(theme_strs)}")
            lines.append("")

        # US 시장 오버나이트
        if us_lines:
            lines.extend(us_lines)

        # 업종 동향
        if sector_lines:
            lines.extend(sector_lines)

        # 추천 종목
        for rec in recommendations:
            risk_emoji = {"낮음": "🟢", "중": "🟡", "높음": "🔴"}.get(rec.risk_level, "⚪")

            lines.append(f"<b>{rec.rank}. {rec.name}</b> <code>{rec.symbol}</code> {risk_emoji}{rec.total_score:.0f}점")
            lines.append(f"   📌 {rec.investment_thesis}")

            if rec.key_news:
                news_title = rec.key_news[:55] + "..." if len(rec.key_news) > 55 else rec.key_news
                lines.append(f"   📰 {news_title}")

            if rec.risk_factors:
                lines.append(f"   ⚠️ {', '.join(rec.risk_factors)}")

            lines.append("")

        # 투자 주의사항
        lines.extend([
            "─" * 20,
            "<i>본 정보는 투자 참고용이며, 투자 판단과 책임은 본인에게 있습니다.</i>",
        ])

        return "\n".join(lines)

    def _format_evening_report(
        self,
        recommendations: List[RecommendedStock],
        report_date: date,
    ) -> str:
        """오후 결과 레포트 포맷팅 (HTML, Telegram)"""

        date_str = report_date.strftime("%Y.%m.%d")
        SEP = "─" * 18

        lines = [
            f"📋 <b>추천종목 결과</b>  <i>{date_str} 장마감</i>",
            SEP,
            "",
        ]

        wins = 0
        total_pct = 0.0
        evaluated = []

        for rec in recommendations:
            if rec.result_pct is not None:
                # 등급 판정
                if rec.result_pct >= 3:
                    grade = "🎯"
                    wins += 1
                elif rec.result_pct >= 0:
                    grade = "✅"
                    wins += 1
                elif rec.result_pct >= -2:
                    grade = "➖"
                else:
                    grade = "❌"

                # 목표/손절 도달 태그
                tag = ""
                if rec.target_exit > 0 and rec.result_price and rec.result_price >= rec.target_exit:
                    tag = "  <b>🏆목표</b>"
                elif rec.stop_loss > 0 and rec.result_price and rec.result_price <= rec.stop_loss:
                    tag = "  <b>🛑손절</b>"

                total_pct += rec.result_pct
                evaluated.append(rec)

                # 종목 헤더: 순위 + 이름 + 심볼
                lines.append(
                    f"{grade} <b>{rec.rank}. {rec.name}</b> "
                    f"<code>{rec.symbol}</code>"
                )
                # 결과 수치: 종가 + 등락률 + 태그
                lines.append(
                    f"   <code>{rec.result_price:>10,.0f}원</code>  "
                    f"<b>{rec.result_pct:+.1f}%</b>{tag}"
                )
                lines.append("")
            else:
                lines.append(
                    f"⏳ <b>{rec.rank}. {rec.name}</b> "
                    f"<code>{rec.symbol}</code>"
                )
                lines.append("   <i>종가 데이터 없음</i>")
                lines.append("")

        # ── 성과 요약 ──
        n = len(evaluated)
        lines.append(SEP)
        lines.append("<b>📊 성과 요약</b>")
        lines.append("")

        if n > 0:
            avg_pct = total_pct / n
            hit_rate = wins / n * 100

            sorted_recs = sorted(evaluated, key=lambda r: r.result_pct or 0, reverse=True)
            best  = sorted_recs[0]
            worst = sorted_recs[-1]

            lines.extend([
                f"적중률  <b>{wins} / {n}</b>  ({hit_rate:.0f}%)",
                f"평균    <b>{avg_pct:+.2f}%</b>",
                "",
                f"🥇  {best.name}  <b>{best.result_pct:+.1f}%</b>",
                f"🔻  {worst.name}  <b>{worst.result_pct:+.1f}%</b>",
            ])
        else:
            lines.append("<i>결과 데이터 없음</i>")

        return "\n".join(lines)


# 싱글톤 인스턴스
_report_generator: Optional[DailyReportGenerator] = None


def get_report_generator() -> DailyReportGenerator:
    """레포트 생성기 인스턴스 반환"""
    global _report_generator
    if _report_generator is None:
        _report_generator = DailyReportGenerator()
    return _report_generator
