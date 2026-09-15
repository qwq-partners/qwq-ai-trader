"""Counterfactual 추적기 (2026-08-08 — Codex 전략 리뷰 과제①/③)

AI 게이트가 차단·감지한 매수 후보의 "만약 거래했다면" 후속 수익률을 추적한다.
분기 말에 각 규칙/전문가가 실제로 얼마를 지켰는지(피한 손실) 또는 놓쳤는지
(놓친 수익) 증명하는 기반 데이터.

소스 (기록만 되고 후속 추적이 없던 shadow 로그):
  ~/.cache/ai_trader/rule11_shadow_log.jsonl  — 전문가 BEAR 합의 감지
  ~/.cache/ai_trader/rule12_shadow_log.jsonl  — 섹터 카운슬 약세 감지

산출:
  counterfactual_state.json — {key: {symbol, source, date, entry_px, r1, r5, r20}}
  · 가상 진입가 = 감지일 종가(해당 날짜 봉 필수), rN = N세션 후 종가 대비 수익률(%)
  · fill_evidence_unknown=True 행은 측정값을 보존하되 평가·가격 갱신에서 제외
  · 갱신은 저녁 품질검증 잡에서 1일 1회 (KIS get_daily_prices — 오래된 순 반환)

해석: 차단(rule11/rule12/team_hold) 후보의 rN이 음수(-) = 게이트가 손실을 막았다(정확한 차단).
반대로 team_buy_unfilled(팀이 승인한 매수, T11 E1 2026-09-15~)는 rN이 음수면 팀 판단이
틀렸다는 뜻이다 — 같은 부호라도 두 소스의 "적중"은 정반대 의미이므로 요약에서 섞어 읽지 않는다.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

from loguru import logger

from ..agents import team_ledger

_CACHE_DIR = Path.home() / ".cache" / "ai_trader"
_STATE_PATH = _CACHE_DIR / "counterfactual_state.json"
_SOURCES = {
    "rule11": _CACHE_DIR / "rule11_shadow_log.jsonl",
    "rule12": _CACHE_DIR / "rule12_shadow_log.jsonl",
}
# 팀 심의 판정 (2026-08-08 후속 — HOLD/거부 후보의 기회비용 추적)
_TEAM_VERDICT_DIR = _CACHE_DIR / "team_verdicts"
_HORIZONS = (("r1", 1), ("r5", 5), ("r20", 20))
# 콜백 미주입 시 기본 체결 증거원 — 실제 거래저널(src/core/evolution/trade_journal.py)과
# 같은 경로 규칙 <TRADE_JOURNAL_DIR 또는 ~/.cache/ai_trader/journal>/trades_YYYYMMDD.json.
# (배포 전 리뷰 2026-09-15: 이전 폴백 trade_journal_kr.json 은 아무도 쓰지 않는 파일이라
#  항상 '판정 불가'였고, 그걸 미체결로 읽어 실제 체결분까지 team_buy_unfilled 로 등록될 수 있었다.)
_TRADE_JOURNAL_DIR: Optional[Path] = None   # 테스트 격리용 override; None 이면 env/기본 경로


def _journal_dir() -> Path:
    if _TRADE_JOURNAL_DIR is not None:
        return Path(_TRADE_JOURNAL_DIR)
    return Path(os.getenv("TRADE_JOURNAL_DIR", str(_CACHE_DIR / "journal")))


def _close_price(bar: Dict[str, Any]) -> float:
    """봉의 종가 — close 우선, 없으면(None, 키 없음) KIS 필드(stck_clpr) 대체. 값 0 은 무효
    데이터로 보고 호출부에서 걸러진다('or 0' 금지 — 0.0 이 legit 값인지 결측 대체인지
    구분해서 명시적으로 처리한다)."""
    v = bar.get("close")
    if v is None:
        v = bar.get("stck_clpr")
    try:
        return float(v) if v is not None else 0.0
    except (TypeError, ValueError):
        return 0.0


def _read_trade_journal_buy_symbols(day: str, journal_dir: Path) -> Optional[set]:
    """당일(day, YYYY-MM-DD) 진입 기록 종목 집합.

    저널 디렉터리가 없으면 None(판정 불가 — 미체결로 오라벨하지 않는다), 날짜 파일이 없으면
    빈 집합(저널은 진입이 있는 날만 파일을 만든다 = 그날 진입 없음), 손상이면 None.
    """
    if not journal_dir.is_dir():
        return None
    path = journal_dir / f"trades_{day.replace('-', '')}.json"
    if not path.exists():
        return set()
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return None
    trades = data.get("trades") if isinstance(data, dict) else data
    if not isinstance(trades, list):
        return None
    symbols = set()
    for t in trades:
        if not isinstance(t, dict):
            continue
        entry_time = str(t.get("entry_time") or "")
        sym = t.get("symbol")
        if sym and entry_time[:10] == day:
            symbols.add(sym)
    return symbols


class CounterfactualTracker:
    """shadow 차단 후보의 가상 성과 추적"""

    def __init__(self, fill_evidence_check: Optional[Callable[[str, str], bool]] = None):
        """
        Args:
            fill_evidence_check: 승인 BUY 종목이 당일 실제로 체결됐는지 확인하는 콜백
                (symbol, day) -> bool. T11 E1 (2026-09-15) — 주입 전용, 기본 None 이면
                항상 "증거 없음"(미체결)으로 본다(보수적). 운영 배선은 이 트래커 밖에서 한다.
        """
        self._state: Dict[str, Dict[str, Any]] = self._load()
        self._fill_evidence_check = fill_evidence_check

    def _has_fill_evidence(self, symbol: str, day: str) -> Optional[bool]:
        """승인 BUY 의 실제 체결 증거 — True 체결 / False 미체결 / None 판정 불가.

        콜백이 주입되면 그것을 우선한다(콜백 실패 시엔 기존과 동일하게 보수적으로 '없음' —
        폴백으로 더 파고들지 않는다, 기존 계약 유지). 콜백이 아예 없을 때만 거래저널
        날짜 파일(`_journal_dir()/trades_YYYYMMDD.json`)을 기본 증거원으로 읽는다.
        저널 디렉터리 자체가 없거나 손상이면 None — 호출부는 등록을 보류한다(미체결로
        오라벨하지 않는다, 배포 전 리뷰 2026-09-15).
        """
        if self._fill_evidence_check is not None:
            try:
                return bool(self._fill_evidence_check(symbol, day))
            except Exception as e:
                logger.debug(f"[CF추적] 체결 증거 콜백 실패 ({symbol}/{day}, 미체결로 간주): {e}")
                return False
        try:
            symbols = _read_trade_journal_buy_symbols(day, _journal_dir())
        except Exception as e:
            logger.debug(f"[CF추적] 거래저널 폴백 조회 실패 ({symbol}/{day}, 판정 불가): {e}")
            return None
        if symbols is None:
            return None
        return symbol in symbols

    def _load(self) -> Dict[str, Dict[str, Any]]:
        try:
            if _STATE_PATH.exists():
                return json.loads(_STATE_PATH.read_text(encoding="utf-8"))
        except Exception as e:
            logger.warning(f"[CF추적] 상태 로드 실패: {e}")
        return {}

    def _save(self) -> None:
        try:
            from ..utils.atomic_io import atomic_write_json
            atomic_write_json(_STATE_PATH, self._state)
        except Exception as e:
            logger.warning(f"[CF추적] 상태 저장 실패: {e}")

    # ── 수집 ───────────────────────────────────────────────
    @staticmethod
    def _team_days():
        """불변 원장이 있는 날은 전체 심의 사용; 원장 없는 구버전 날짜만 latest 폴백.

        손상/빈 원장을 최신 HOLD로 대체하면 잃은 BUY를 다시 숨기므로 폴백하지 않는다.
        날짜별 원장의 경로는 생산자 team_ledger.LEDGER_DIR 한 곳을 참조한다.
        """
        ledger_days = {
            p.stem.removeprefix("deliberations_"): p
            for p in team_ledger.LEDGER_DIR.glob("deliberations_*.jsonl")
        }
        latest_days = {
            p.stem.removeprefix("verdicts_"): p
            for p in _TEAM_VERDICT_DIR.glob("verdicts_*.json")
        }
        for raw in sorted(ledger_days.keys() | latest_days.keys()):
            if len(raw) != 8 or not raw.isdigit():
                continue
            day = f"{raw[:4]}-{raw[4:6]}-{raw[6:]}"
            try:
                if raw in ledger_days:
                    rows = team_ledger.load_day(day)
                    if not rows:
                        logger.warning(f"[CF추적] {day} 불변 심의 원장 비어 있음/읽기 실패 — 최신 판정 폴백 보류")
                else:
                    rows = json.loads(latest_days[raw].read_text(encoding="utf-8"))
                if isinstance(rows, list):
                    yield day, [r for r in rows if isinstance(r, dict)]
            except (OSError, ValueError, TypeError, AttributeError) as e:
                logger.warning(f"[CF추적] {day} 심의 원장 읽기 실패 — 해당 날짜 보류: {e}")

    def _ingest_team_day(self, day: str, rows: List[Dict]) -> tuple[int, int]:
        """종목·일자당 1표본. 하루 중 승인 BUY가 하나라도 있으면 BUY 집단 우선.

        심의별 실시간 성과가 아니라 기존 종가 기준 CF다. 모든 심의 ID는 감사용으로
        보존하되, 같은 가격경로를 여러 표본으로 세지 않는다.
        """
        grouped: Dict[str, List[Dict]] = {}
        for row in rows:
            sym = row.get("symbol")
            if not sym or not isinstance(row.get("decision"), dict):
                continue
            grouped.setdefault(str(sym), []).append(row)
        added = undetermined = 0
        for sym, decisions in grouped.items():
            decisions.sort(key=lambda r: str(r.get("decided_at") or r.get("saved_at") or ""))
            buys = [r for r in decisions if r["decision"].get("approved")
                    and str(r["decision"].get("stance", "")).lower() == "buy"]
            hold_key = f"team_hold|{sym}|{day}"
            buy_key = f"team_buy_unfilled|{sym}|{day}"
            # 이전 수집의 BUY도 latest HOLD로 뒤집지 않는다(구버전 폴백 날짜 포함).
            had_buy = bool(buys) or buy_key in self._state
            source = "team_buy_unfilled" if had_buy else "team_hold"
            key = buy_key if had_buy else hold_key
            fill = False
            if had_buy:
                fill = self._has_fill_evidence(sym, day)
                if fill is True:
                    # 체결 확정만 미체결 표본에서 제거한다.
                    self._state.pop(hold_key, None)
                    self._state.pop(buy_key, None)
                    continue
                if fill is None:
                    undetermined += 1
                    # 새 표본은 등록 보류. 기존 측정은 평가 제외 상태로 보존한다.
                    if buy_key not in self._state and hold_key not in self._state:
                        continue
            prior_hold = self._state.pop(hold_key, None) if had_buy else None
            entry = self._state.get(key)
            selected = (buys or decisions)[0]
            if entry is None:
                # 재분류는 종가·rN 계산을 보존한다(같은 날짜·종목 가격경로).
                entry = prior_hold if prior_hold is not None else {
                    "symbol": sym, "date": day, "sector": None,
                    "entry_px": None, "r1": None, "r5": None, "r20": None,
                }
                entry["source"] = source
                entry["wiki_context_used"] = selected.get("wiki_context_used")
                self._state[key] = entry
                added += 1
            if fill is None:
                entry["fill_evidence_unknown"] = True
            else:
                entry.pop("fill_evidence_unknown", None)
            entry["deliberation_ids"] = sorted({
                *entry.get("deliberation_ids", []),
                *(str(r["deliberation_id"]) for r in decisions if r.get("deliberation_id")),
            })
        return added, undetermined

    def _ingest_sources(self) -> int:
        """shadow 로그에서 신규 감지 건을 상태에 등록 (일일 dedup 키)"""
        # 재분류·체결 대조만 바뀐 경우에도 update에서 저장해야 한다.
        before = json.dumps(self._state, sort_keys=True, ensure_ascii=False)
        added = 0
        for source, path in _SOURCES.items():
            if not path.exists():
                continue
            try:
                for line in path.read_text(encoding="utf-8").splitlines():
                    try:
                        r = json.loads(line)
                        day = str(r.get("timestamp", ""))[:10]
                        sym = r.get("symbol", "")
                        if not day or not sym:
                            continue
                        key = f"{source}|{sym}|{day}"
                        if key in self._state:
                            continue
                        self._state[key] = {
                            "symbol": sym,
                            "source": source,
                            "date": day,
                            "sector": r.get("sector"),
                            "entry_px": None,
                            "r1": None, "r5": None, "r20": None,
                        }
                        added += 1
                    except (json.JSONDecodeError, TypeError):
                        continue
            except Exception as e:
                logger.debug(f"[CF추적] {source} 읽기 실패: {e}")

        # 팀 원장 → 일일 CF. 최신 판정 파일은 과거 날짜 호환용으로만 사용한다.
        undetermined = 0
        try:
            for day, rows in self._team_days():
                new, unknown = self._ingest_team_day(day, rows)
                added += new
                undetermined += unknown
        except Exception as e:
            logger.debug(f"[CF추적] 팀 심의 읽기 실패: {e}")
        if undetermined:
            logger.warning(f"[CF추적] 승인 BUY {undetermined}건 체결 대조 판정 불가 — 신규 등록 보류·기존 측정 보존 및 평가 제외")
        self._ingest_changed = before != json.dumps(self._state, sort_keys=True, ensure_ascii=False)
        return added

    # ── 갱신 ───────────────────────────────────────────────
    def summary(self) -> str:
        """소스별 요약 + KODEX200 대비 초과수익(x5) 한 줄 — 판정은 초과수익 기준으로 읽을 것"""
        base = self._summary_base()
        try:
            groups: Dict[str, List[float]] = {}
            for v in self._state.values():
                if not v.get("fill_evidence_unknown") and v.get("x5") is not None:
                    groups.setdefault(str(v.get("source")), []).append(float(v["x5"]))
            if groups:
                # advisory(2026-09-15): team_buy_unfilled 는 음수=팀 판단이 틀림(다른 소스는 음수=적중) —
                # _summary_base 와 프레이밍이 반대이므로 이 줄에서도 명시한다.
                parts = [
                    f"{src} n={len(xs)} x5 {sum(xs) / len(xs):+.2f}% "
                    f"(음수 {sum(1 for x in xs if x < 0) / len(xs) * 100:.0f}%"
                    f"{'·팀 판단이 틀림' if src.split('|', 1)[0] == 'team_buy_unfilled' else ''})"
                    for src, xs in sorted(groups.items())
                ]
                base += "\n· KODEX200 대비 초과(r5): " + " | ".join(parts)
        except Exception:
            pass
        return base

    @staticmethod
    def _rows(prices) -> List[tuple]:
        """get_daily_prices 응답(오래된 순) → [(YYYY-MM-DD, 종가)]"""
        rows: List[tuple] = []
        for bar in prices or []:
            d = str(bar.get("date", "") or bar.get("stck_bsop_date", ""))
            c = _close_price(bar)
            if len(d) == 8 and c > 0:
                rows.append((f"{d[:4]}-{d[4:6]}-{d[6:]}", c))
        return rows

    @staticmethod
    def _pending_order(state: Dict[str, Dict[str, Any]]) -> List[tuple]:
        """미완성 항목 처리 순서 — 아직 가격도 없는 항목(entry_px None) 먼저, 그다음 오래된 순.

        2026-09-13 리뷰: 삽입순 상위 50개만 처리해 r20 대기 항목이 슬롯을 점유 → 08-24 이후
        신규 126건이 한 번도 가격 조회되지 않아 승격 지표가 8/2~8/24 코호트에 동결됐던 결함.
        """
        pending = [(k, v) for k, v in state.items()
                   if not v.get("fill_evidence_unknown") and v.get("r20") is None]
        pending.sort(key=lambda kv: (kv[1].get("entry_px") is not None, str(kv[1].get("date", ""))))
        return pending

    async def update(self, broker) -> Dict[str, int]:
        """미완성 항목의 가상 진입가·후속 수익률 채움 (일일 1회 호출)"""
        added = self._ingest_sources()
        # 완료(r20 채움) 후 180일 지난 항목 정리 — 무한 누적 방지 (리뷰 P2)
        try:
            from datetime import datetime as _dt, timedelta as _td
            _cut = (_dt.now() - _td(days=180)).strftime("%Y-%m-%d")
            _old = [k for k, v in self._state.items()
                    if not v.get("fill_evidence_unknown")
                    and v.get("r20") is not None and str(v.get("date", "")) < _cut]
            for k in _old:
                del self._state[k]
        except Exception:
            pass
        filled = 0
        pending = self._pending_order(self._state)
        # 벤치마크(KODEX200) 일봉 1회 — r5/r20에 대응하는 초과수익 x5/x20 (2026-09-13 리뷰:
        # 절대수익 판정이 시장 베타에 휘둘려 04-23·08-20 결정이 뒤집힌 문제)
        bench_rows: List[tuple] = []
        try:
            _b = await broker.get_daily_prices("069500", days=45)
            bench_rows = self._rows(_b or [])
        except Exception as _be:
            logger.debug(f"[CF추적] 벤치마크 조회 실패 (초과수익 생략): {_be}")
        for key, entry in pending[:150]:  # 호출당 상한 — 시세 TR(원장 무관), 공용 리미터 10/s 하에서 ~15초
            try:
                prices = await broker.get_daily_prices(entry["symbol"], days=45)
                if not prices or len(prices) < 2:
                    continue
                # get_daily_prices는 오래된 순 — (날짜, 종가) 리스트 구성
                rows = self._rows(prices)
                # 조회 창에서 감지일이 빠졌다면 다른 날 봉을 기준점으로 대체하지 않는다.
                idx0 = next(
                    (i for i, (d, _) in enumerate(rows) if d == entry["date"]), None
                )
                if idx0 is None:
                    continue
                if entry.get("entry_px") is None:
                    entry["entry_px"] = rows[idx0][1]
                base = entry["entry_px"]
                # 벤치마크 기준점 (같은 감지일)
                bidx0 = next((i for i, (d, _) in enumerate(bench_rows) if d == entry["date"]), None) if bench_rows else None
                for field, n in _HORIZONS:
                    if entry.get(field) is None and idx0 + n < len(rows):
                        entry[field] = round(
                            (rows[idx0 + n][1] - base) / base * 100, 2
                        )
                        filled += 1
                # KODEX200 대비 초과수익 (x1/x5/x20) — 판정은 이 값과 손절 클립을 우선 사용
                if bench_rows and bidx0 is not None:
                    for field, n in _HORIZONS:
                        xf = "x" + field[1:]
                        if entry.get(xf) is None and entry.get(field) is not None and bidx0 + n < len(bench_rows):
                            _bb = bench_rows[bidx0][1]
                            if _bb > 0:
                                _br = (bench_rows[bidx0 + n][1] - _bb) / _bb * 100
                                entry[xf] = round(float(entry[field]) - _br, 2)
            except Exception as e:
                logger.debug(f"[CF추적] {key} 갱신 실패: {e}")
        if added or filled or self._ingest_changed:
            self._save()
            logger.info(f"[CF추적] 신규 {added}건 등록, {filled}개 수익률 채움")
        return {"added": added, "filled": filled}

    # ── 요약 (주간 성적표) ──────────────────────────────────
    def _summary_base(self) -> str:
        """소스별 요약 — 차단 계열(rN<0="적중")과 승인 BUY 계열(rN<0="팀 판단이 틀림")을 분리해 표기한다.

        team_buy_unfilled 는 "게이트가 막은 후보"가 아니라 "팀이 승인한 매수"다. rN 이 음수라는
        같은 사실이 차단 계열에선 '정확한 차단'을, 승인 계열에선 '틀린 매수 판단'을 뜻하므로
        같은 "적중" 프레이밍으로 섞어 보고하면 오해를 낳는다(T11 담당D 리뷰 2026-09-15 반영).
        """
        groups: Dict[str, List[Dict[str, Any]]] = {}
        for v in self._state.values():
            if not v.get("fill_evidence_unknown") and v.get("r5") is not None:
                _g = v["source"]
                if v.get("wiki_context_used") is not None:  # 2026-09-13 위키 노출 유무로 분리
                    _g = f"{_g}|wiki={'Y' if v['wiki_context_used'] else 'N'}"
                groups.setdefault(_g, []).append(v)
        hold_note = ""
        unknown = sum(1 for v in self._state.values() if v.get("fill_evidence_unknown"))
        if unknown:
            hold_note = f"※ 체결 대조 판정 불가 {unknown}건 — 기존 측정 보존·평가 및 가격 갱신 제외"
        elif self._fill_evidence_check is None and not _journal_dir().is_dir():
            hold_note = ("※ 체결 대조 판정 불가(콜백 없음·거래저널 디렉터리 없음) — "
                         "승인 BUY 는 team_buy_unfilled 로 등록하지 않고 보류 중")
        if not groups:
            return "counterfactual 표본 없음 (r5 완성 건 0)" + (f"\n{hold_note}" if hold_note else "")
        lines = []
        for source, items in sorted(groups.items()):
            n = len(items)
            base_source = source.split("|", 1)[0]
            avg_r5 = sum(i["r5"] for i in items) / n  # groups 는 r5 not None 건만 모았다
            r20_items = [i for i in items if i.get("r20") is not None]
            avg_r20 = (
                sum(i["r20"] for i in r20_items) / len(r20_items)
                if r20_items else None
            )
            if base_source == "team_buy_unfilled":
                wrong = sum(1 for i in items if i["r5"] < 0)
                line = (
                    f"{source}: 승인 BUY 미체결 {n}건 | 5일 뒤 하락 {wrong}건 "
                    f"(팀 판단이 틀린 비율 {wrong/n*100:.0f}%) | 평균 r5 {avg_r5:+.1f}%"
                )
            else:
                avoided = sum(1 for i in items if i["r5"] < 0)
                line = (
                    f"{source}: {n}건 | 5일 뒤 하락 {avoided}건 ({avoided/n*100:.0f}% 적중) "
                    f"| 평균 r5 {avg_r5:+.1f}%"
                )
            if avg_r20 is not None:
                line += f" | 평균 r20 {avg_r20:+.1f}%"
            lines.append(line)
        if hold_note:
            lines.append(hold_note)
        return "\n".join(lines)


_tracker: Optional[CounterfactualTracker] = None


def get_counterfactual_tracker() -> CounterfactualTracker:
    global _tracker
    if _tracker is None:
        _tracker = CounterfactualTracker()
    return _tracker
