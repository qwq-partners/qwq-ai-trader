"""과거 오염 지표 감사 (2026-08-10 — 하네스 설계 Phase 0, 일회성·재실행 안전)

2026-08-08 이전의 승률/거래수 지표는 분할익절이 매도 이벤트마다 별도 승리로
집계돼 부풀려졌다 (v1_event). 이 스크립트는:

1. 진화 이력(evolution_state.json history)에서 v1 지표 기반 결정을 식별해
   불변 감사 파일(metric_contamination_audit.json)에 기록
   — 상태 파일 인라인 태깅은 하지 않는다 (로더가 미지 필드를 버리고
   저장 시 asdict로 재작성돼 태그가 소실됨)
2. Wiki 전략 페이지에 경고 배너 삽입 (idempotent, 마커 주석으로 중복 방지)
   — weekly rebalance LLM이 이 페이지를 컨텍스트로 소비하므로 가장 중요

사용: python scripts/audit_metric_contamination.py
"""

import json
import sys
from datetime import datetime
from pathlib import Path

CACHE = Path.home() / ".cache" / "ai_trader"
STATE_PATH = CACHE / "evolution" / "evolution_state.json"
AUDIT_PATH = CACHE / "evolution" / "metric_contamination_audit.json"
WIKI_STRATEGIES = CACHE / "wiki" / "strategies"

CUTOFF = "2026-08-08"  # 이 날짜 이전 지표 = v1_event (부풀림)
BANNER_MARKER = "<!-- metric-audit-v1 -->"
BANNER = (
    f"{BANNER_MARKER}\n"
    "> ⚠️ **지표 주의 (2026-08-10 감사)**: 이 페이지의 2026-08-08 이전 승률·거래수는\n"
    "> 분할익절이 매도 이벤트마다 별도 승리로 집계된 **이벤트 단위(v1_event) 지표**로\n"
    "> 부풀려져 있음. 정확한 포지션 단위 지표는 `position_ledger.jsonl` 기준\n"
    "> (2026-08-10 이후 축적). 이 페이지 수치로 전략 우열을 판단하지 말 것.\n"
)


def audit_evolution_history() -> list:
    """v1 지표 기반 진화 결정 식별"""
    if not STATE_PATH.exists():
        return []
    data = json.loads(STATE_PATH.read_text(encoding="utf-8"))
    contaminated = []
    for h in data.get("history", []):
        applied = h.get("applied_date") or (h.get("timestamp") or "")[:10]
        if applied and applied < CUTOFF:
            contaminated.append({
                "applied_date": applied,
                "parameter": f"{h.get('strategy', '?')}.{h.get('parameter', '?')}",
                "old_value": h.get("old_value"),
                "new_value": h.get("new_value"),
                "source": h.get("source"),
                "is_effective": h.get("is_effective"),
                "win_rate_before_v1": h.get("win_rate_before"),
                "win_rate_after_v1": h.get("win_rate_after"),
                "metric_version": "v1_event",
                "verdict": (
                    "지표 오염 — 승률 기반 판단은 재검토 대상. "
                    "단 롤백/유지 판정은 손익비 병용이라 부분 신뢰 가능"
                ),
            })
    return contaminated


def tag_wiki_pages() -> list:
    """전략 페이지에 경고 배너 삽입 (idempotent)"""
    tagged = []
    if not WIKI_STRATEGIES.exists():
        return tagged
    for page in sorted(WIKI_STRATEGIES.glob("*.md")):
        text = page.read_text(encoding="utf-8")
        if BANNER_MARKER in text:
            continue  # 이미 태깅됨
        lines = text.splitlines(keepends=True)
        # 제목 줄(# ...) 바로 뒤에 배너 삽입 — frontmatter는 건드리지 않음
        # (trade_wiki가 frontmatter를 파싱하므로 구조 변경 금지)
        inserted = False
        for i, line in enumerate(lines):
            if line.startswith("# "):
                lines.insert(i + 1, "\n" + BANNER + "\n")
                inserted = True
                break
        if not inserted:
            lines.append("\n" + BANNER + "\n")
        page.write_text("".join(lines), encoding="utf-8")
        tagged.append(page.name)
    return tagged


def main():
    contaminated = audit_evolution_history()
    tagged = tag_wiki_pages()

    audit = {
        "audited_at": datetime.now().isoformat(),
        "cutoff": CUTOFF,
        "reason": "분할익절 이벤트 단위 승률 집계 (2026-08-08 발견, position_ledger로 교정)",
        "contaminated_decisions": contaminated,
        "wiki_pages_tagged": tagged,
    }
    AUDIT_PATH.parent.mkdir(parents=True, exist_ok=True)
    AUDIT_PATH.write_text(
        json.dumps(audit, ensure_ascii=False, indent=2), encoding="utf-8"
    )

    print(f"감사 완료 → {AUDIT_PATH}")
    print(f"- 오염 진화 결정: {len(contaminated)}건")
    for c in contaminated:
        print(f"  · {c['applied_date']} {c['parameter']} "
              f"{c['old_value']}→{c['new_value']} (src={c['source']}, "
              f"wr_v1={c['win_rate_before_v1']:.1f}%)")
    print(f"- Wiki 배너 삽입: {len(tagged)}개 페이지 {tagged}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
