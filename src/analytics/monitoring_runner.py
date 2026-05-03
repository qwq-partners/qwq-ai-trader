"""모니터링 체크포인트 자동 검증 시스템 (Phase 2)

매주 토요일 09:35 KST에 `docs/operations/monitoring-checkpoints.md`의
활성 체크포인트를 파싱하여:
1. SQL 코드블록 자동 실행 (DB 쿼리)
2. 결과를 캡처
3. wiki/monitoring/2026-WNN.md에 기록 (다음 weekly_rebalance LLM이 자동 흡수)

Phase 1(strategy_evolver._build_wiki_context)이 이 파일을 읽어
LLM 컨텍스트로 활용함.
"""

import re
import os
import asyncio
from datetime import date
from pathlib import Path
from typing import List, Dict, Any, Optional

import asyncpg
from loguru import logger


CHECKPOINTS_PATH = (
    Path(__file__).parent.parent.parent / "docs" / "operations" / "monitoring-checkpoints.md"
)
WIKI_MONITORING_DIR = Path.home() / ".cache" / "ai_trader" / "wiki" / "monitoring"


class MonitoringRunner:
    """체크포인트 SQL 자동 실행 + 결과 영속화"""

    def __init__(self, db_url: Optional[str] = None):
        self._db_url = db_url or os.getenv("DATABASE_URL")

    async def run_weekly(self) -> Dict[str, Any]:
        """주간 모니터링 실행

        Returns:
            {status, checkpoints_count, sql_executed, saved_path}
        """
        logger.info("[모니터링] 주간 체크포인트 자동 검증 시작")
        try:
            if not CHECKPOINTS_PATH.exists():
                logger.warning(f"[모니터링] 체크포인트 파일 없음: {CHECKPOINTS_PATH}")
                return {"status": "no_checkpoints"}

            content = CHECKPOINTS_PATH.read_text(encoding="utf-8")
            checkpoints = self._parse_active_checkpoints(content)
            if not checkpoints:
                logger.info("[모니터링] 활성 체크포인트 없음")
                return {"status": "empty", "checkpoints_count": 0}

            results = []
            for ckp in checkpoints:
                ckp_results = await self._execute_checkpoint(ckp)
                results.append({
                    "title": ckp["title"],
                    "commit": ckp.get("commit", ""),
                    "sql_results": ckp_results,
                })

            saved_path = self._write_wiki_page(results)
            logger.info(
                f"[모니터링] 완료: 체크포인트 {len(checkpoints)}건, "
                f"SQL {sum(len(r['sql_results']) for r in results)}건, "
                f"저장 {saved_path}"
            )
            return {
                "status": "ok",
                "checkpoints_count": len(checkpoints),
                "sql_executed": sum(len(r["sql_results"]) for r in results),
                "saved_path": str(saved_path),
            }
        except Exception as e:
            logger.exception(f"[모니터링] 오류: {e}")
            return {"status": "error", "error": str(e)}

    def _parse_active_checkpoints(self, content: str) -> List[Dict[str, Any]]:
        """활성 체크포인트 섹션 파싱

        구조 가정:
            ## 활성 체크포인트
            ### {title}
            - **커밋**: {hash}
            - **변경**: ...
            - **확인 항목**: ...
            - **검증 SQL**: ```sql ... ```
            ...
            ## 완료된 체크포인트
        """
        # 활성 섹션만 잘라내기
        if "## 활성 체크포인트" not in content:
            return []
        active_section = content.split("## 활성 체크포인트", 1)[1]
        if "## 완료된 체크포인트" in active_section:
            active_section = active_section.split("## 완료된 체크포인트", 1)[0]

        # ### 헤더로 체크포인트 분리
        checkpoints = []
        for chunk in re.split(r"\n### ", active_section)[1:]:
            title = chunk.split("\n", 1)[0].strip()
            ckp = {"title": title, "raw": chunk}
            # 커밋 해시 추출
            m = re.search(r"\*\*커밋\*\*:\s*([\w,+\s]+)", chunk)
            if m:
                ckp["commit"] = m.group(1).strip()
            # SQL 블록 추출
            sql_blocks = re.findall(r"```sql\s*\n(.*?)\n\s*```", chunk, re.DOTALL)
            ckp["sql"] = [s.strip() for s in sql_blocks if s.strip()]
            checkpoints.append(ckp)
        return checkpoints

    # SQL 화이트리스트 — read-only만 허용 (운영 DB 파괴 방지)
    _SAFE_SQL_RE = re.compile(r"^\s*(SELECT|WITH)\b", re.IGNORECASE)

    async def _execute_checkpoint(self, ckp: Dict[str, Any]) -> List[Dict[str, Any]]:
        """체크포인트의 SQL을 실행해 결과 반환

        SQL 안전성:
        - SELECT/WITH 시작만 허용 (DROP/DELETE/TRUNCATE/UPDATE 차단)
        - 단일 statement 가정 (multi-statement는 asyncpg가 거부)
        """
        sql_list = ckp.get("sql", [])
        if not sql_list:
            return []
        if not self._db_url:
            logger.warning("[모니터링] DATABASE_URL 미설정 — SQL 실행 스킵")
            return [{"sql": s[:200], "error": "no DB"} for s in sql_list]

        results = []
        conn = None
        try:
            conn = await asyncpg.connect(self._db_url)
            for sql in sql_list:
                if not self._SAFE_SQL_RE.match(sql):
                    logger.warning(
                        f"[모니터링] 비-read-only SQL 차단 (SELECT/WITH 강제): {sql[:80]}"
                    )
                    results.append({
                        "sql": sql[:200],
                        "error": "blocked: only SELECT/WITH allowed",
                    })
                    continue
                try:
                    rows = await conn.fetch(sql)
                    results.append({
                        "sql": sql[:200],
                        "rows": [dict(r) for r in rows],
                        "row_count": len(rows),
                    })
                except Exception as sql_err:
                    results.append({
                        "sql": sql[:200],
                        "error": str(sql_err),
                    })
        except asyncio.CancelledError:
            raise
        except Exception as conn_err:
            logger.error(f"[모니터링] DB 연결 실패: {conn_err}")
            return [{"sql": s[:200], "error": f"connection: {conn_err}"} for s in sql_list]
        finally:
            if conn is not None:
                try:
                    await conn.close()
                except Exception:
                    pass

        return results

    def _write_wiki_page(self, results: List[Dict[str, Any]]) -> Path:
        """결과를 wiki/monitoring/2026-WNN.md에 기록

        다음 weekly_rebalance가 _build_wiki_context를 통해 자동 흡수.
        """
        WIKI_MONITORING_DIR.mkdir(parents=True, exist_ok=True)
        iso_year, iso_week, _ = date.today().isocalendar()
        path = WIKI_MONITORING_DIR / f"{iso_year}-W{iso_week:02d}.md"

        lines = [
            f"# 주간 모니터링 검증 — {date.today().isoformat()} (W{iso_week:02d})",
            "",
            f"활성 체크포인트 {len(results)}건 자동 검증 결과.",
            "",
        ]
        for r in results:
            lines.append(f"## {r['title']}")
            if r.get("commit"):
                lines.append(f"- 커밋: `{r['commit']}`")
            sql_results = r.get("sql_results", [])
            if not sql_results:
                lines.append("- (실행할 SQL 없음)")
                lines.append("")
                continue
            for idx, sr in enumerate(sql_results, 1):
                lines.append(f"\n**SQL #{idx}:**")
                lines.append("```sql")
                lines.append(sr.get("sql", "")[:300])
                lines.append("```")
                if "error" in sr:
                    lines.append(f"[ERROR] {sr['error']}")
                    continue
                rows = sr.get("rows", [])
                lines.append(f"결과 {sr.get('row_count', 0)}건")
                if rows:
                    # 첫 5건만 마크다운 표로
                    keys = list(rows[0].keys())
                    lines.append("")
                    lines.append("| " + " | ".join(keys) + " |")
                    lines.append("|" + "|".join(["---"] * len(keys)) + "|")
                    for row in rows[:5]:
                        vals = [str(row.get(k, ""))[:40] for k in keys]
                        lines.append("| " + " | ".join(vals) + " |")
                    if len(rows) > 5:
                        lines.append(f"\n_... +{len(rows)-5}건 생략_")
            lines.append("")

        path.write_text("\n".join(lines), encoding="utf-8")
        return path

    def format_telegram(self, run_result: Dict[str, Any]) -> str:
        """텔레그램 요약"""
        if run_result.get("status") != "ok":
            return f"⚠️ 모니터링 검증 실패: {run_result.get('status')}"
        return (
            f"📋 <b>주간 모니터링 자동 검증</b>\n"
            f"• 체크포인트 {run_result['checkpoints_count']}건\n"
            f"• SQL 실행 {run_result['sql_executed']}건\n"
            f"• 위키 저장: <code>{run_result['saved_path']}</code>\n"
            f"\n다음 weekly_rebalance LLM이 자동 흡수 예정."
        )
