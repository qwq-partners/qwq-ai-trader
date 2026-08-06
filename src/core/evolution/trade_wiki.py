"""
QWQ AI Trader - Trade Wiki (Karpathy LLM Wiki 패턴)

거래 교훈을 종목/전략/섹터/시장체제별 마크다운 위키로 축적.
매매마다 관련 페이지 자동 업데이트, 주간 Lint 검증.

3가지 오퍼레이션:
- Ingest: 매도 체결 → 관련 위키 3~5개 페이지 업데이트
- Query: 크로스검증 시 관련 위키 컨텍스트 반환
- Lint: 주간 헬스체크 (모순, 고아 페이지, 누락)
"""

import re
import asyncio
from datetime import datetime, date
from pathlib import Path
from typing import Dict, Any, Optional, List

from loguru import logger


# 위키 기본 경로
WIKI_DIR = Path.home() / ".cache" / "ai_trader" / "wiki"

# 페이지 크기 제한
MAX_PAGE_LINES = 200
MAX_LOG_LINES = 500
MAX_TRADE_TABLE_ROWS = 30
# 종목 페이지 (2026-08-07 추가)
MAX_RESEARCH_ROWS = 30      # 리서치 노트 롤링 개수
MAX_SYMBOL_PAGES = 200      # 리서치가 새 페이지를 만들 수 있는 상한 (거래는 무제한)
MAX_LESSON_ROWS = 20


def _sanitize_filename(name: str) -> str:
    """파일명 안전 변환"""
    return re.sub(r'[/\\:*?"<>|]', '_', name).strip() or "unknown"


class TradeWiki:
    """거래 지식 위키 — Karpathy LLM Wiki 패턴 적용"""

    def __init__(self, llm_manager=None, wiki_dir: Path = None):
        self._llm = llm_manager
        self._dir = wiki_dir or WIKI_DIR
        self._dir.mkdir(parents=True, exist_ok=True)
        (self._dir / "strategies").mkdir(exist_ok=True)
        (self._dir / "regimes").mkdir(exist_ok=True)
        (self._dir / "sectors").mkdir(exist_ok=True)
        (self._dir / "symbols").mkdir(exist_ok=True)   # 종목별 (2026-08-07)
        self._lock = asyncio.Lock()  # 동시 ingest 방지

    # ================================================================
    # Ingest: 매도 체결 후 관련 위키 페이지 업데이트
    # ================================================================

    async def ingest(self, trade: Dict[str, Any]):
        """매도 체결 데이터로 관련 위키 페이지 업데이트

        Args:
            trade: {symbol, name, strategy, sector, pnl_pct, exit_type,
                    holding_days, market_regime, entry_price, exit_price, ...}
        """
        async with self._lock:
            try:
                symbol = trade.get("symbol", "")
                strategy = trade.get("strategy", "") or "unknown"
                sector = trade.get("sector", "")
                regime = trade.get("market_regime", "neutral")
                pnl_pct = trade.get("pnl_pct", 0)
                name = trade.get("name", symbol)

                # 1. 전략 페이지 업데이트
                await self._update_strategy_page(strategy, trade)

                # 2. 섹터 페이지 업데이트 (섹터 정보 있을 때만)
                if sector:
                    await self._update_sector_page(sector, trade)

                # 3. 시장 체제 페이지 업데이트
                await self._update_regime_page(regime, trade)

                # 4. LLM 교훈 추출 (선택적)
                lesson = ""
                if self._llm:
                    lesson = await self._extract_lesson(trade)

                # 4.5 종목 페이지 업데이트 (2026-08-07 — 거래 이력 + 교훈)
                await self._update_symbol_page(trade, lesson)

                # 5. 로그 추가
                self._append_log(trade, lesson)

                # 6. 인덱스 재생성
                self._rebuild_index()

                logger.info(
                    f"[Wiki] Ingest: {symbol} {name} ({strategy}, "
                    f"{pnl_pct:+.1f}%) -> wiki updated"
                )
            except Exception as e:
                logger.debug(f"[Wiki] Ingest fail (ignored): {e}")

    async def _update_strategy_page(self, strategy: str, trade: Dict):
        """전략 위키 페이지 업데이트"""
        path = self._dir / "strategies" / f"{strategy}.md"
        page = self._read_or_create(path, self._strategy_template(strategy))

        # 프론트매터 업데이트
        fm = self._parse_frontmatter(page)
        fm["trade_count"] = fm.get("trade_count", 0) + 1
        wins = fm.get("wins", 0) + (1 if trade.get("pnl_pct", 0) > 0 else 0)
        fm["wins"] = wins
        fm["win_rate"] = round(wins / fm["trade_count"] * 100, 1)
        fm["last_updated"] = datetime.now().strftime("%Y-%m-%d %H:%M")

        # 거래 테이블에 행 추가
        row = (
            f"| {trade.get('symbol','')} | {trade.get('name','')} | "
            f"{trade.get('pnl_pct',0):+.1f}% | {trade.get('exit_type','')} | "
            f"{trade.get('holding_days', 0)}일 | "
            f"{trade.get('market_regime','?')} | "
            f"{datetime.now().strftime('%m/%d')} |"
        )
        page = self._append_to_section(page, "## 최근 거래", row, MAX_TRADE_TABLE_ROWS)

        # 프론트매터 교체 + 저장
        page = self._replace_frontmatter(page, fm)
        self._write_page(path, page)

    async def _update_sector_page(self, sector: str, trade: Dict):
        """섹터 위키 페이지 업데이트"""
        slug = _sanitize_filename(sector)
        path = self._dir / "sectors" / f"{slug}.md"
        page = self._read_or_create(path, self._sector_template(sector))

        fm = self._parse_frontmatter(page)
        fm["trade_count"] = fm.get("trade_count", 0) + 1
        wins = fm.get("wins", 0) + (1 if trade.get("pnl_pct", 0) > 0 else 0)
        fm["wins"] = wins
        fm["win_rate"] = round(wins / fm["trade_count"] * 100, 1) if fm["trade_count"] > 0 else 0
        fm["last_updated"] = datetime.now().strftime("%Y-%m-%d %H:%M")

        row = (
            f"| {trade.get('symbol','')} | {trade.get('strategy','')} | "
            f"{trade.get('pnl_pct',0):+.1f}% | {trade.get('exit_type','')} | "
            f"{datetime.now().strftime('%m/%d')} |"
        )
        page = self._append_to_section(page, "## 최근 거래", row, MAX_TRADE_TABLE_ROWS)
        page = self._replace_frontmatter(page, fm)
        self._write_page(path, page)

    async def _update_regime_page(self, regime: str, trade: Dict):
        """시장 체제 위키 페이지 업데이트"""
        path = self._dir / "regimes" / f"{regime}.md"
        page = self._read_or_create(path, self._regime_template(regime))

        fm = self._parse_frontmatter(page)
        fm["trade_count"] = fm.get("trade_count", 0) + 1
        wins = fm.get("wins", 0) + (1 if trade.get("pnl_pct", 0) > 0 else 0)
        fm["wins"] = wins
        fm["win_rate"] = round(wins / fm["trade_count"] * 100, 1) if fm["trade_count"] > 0 else 0
        fm["last_updated"] = datetime.now().strftime("%Y-%m-%d %H:%M")

        row = (
            f"| {trade.get('symbol','')} | {trade.get('strategy','')} | "
            f"{trade.get('pnl_pct',0):+.1f}% | {trade.get('exit_type','')} | "
            f"{datetime.now().strftime('%m/%d')} |"
        )
        page = self._append_to_section(page, "## 최근 거래", row, MAX_TRADE_TABLE_ROWS)
        page = self._replace_frontmatter(page, fm)
        self._write_page(path, page)

    async def _update_symbol_page(self, trade: Dict, lesson: str = ""):
        """종목 위키 페이지 업데이트 (2026-08-07 — 거래 이력 + 거래 교훈)

        파일명은 종목코드 고정 (이름은 frontmatter) — 이름 미상 시 이중 생성 방지.
        거래 ingest는 페이지 상한을 적용하지 않는다 (거래 교훈이 최우선 가치).
        """
        symbol = trade.get("symbol", "") or "unknown"
        name = trade.get("name", "") or symbol
        path = self._dir / "symbols" / f"{_sanitize_filename(symbol)}.md"
        page = self._read_or_create(path, self._symbol_template(symbol, name))

        fm = self._parse_frontmatter(page)
        fm["trade_count"] = fm.get("trade_count", 0) + 1
        wins = fm.get("wins", 0) + (1 if trade.get("pnl_pct", 0) > 0 else 0)
        fm["wins"] = wins
        fm["win_rate"] = round(wins / fm["trade_count"] * 100, 1)
        fm["name"] = name
        fm["last_updated"] = datetime.now().strftime("%Y-%m-%d %H:%M")

        row = (
            f"| {trade.get('strategy','')} | {trade.get('pnl_pct',0):+.1f}% | "
            f"{trade.get('exit_type','')} | {trade.get('holding_days',0)}일 | "
            f"{trade.get('market_regime','?')} | "
            f"{datetime.now().strftime('%m/%d')} |"
        )
        page = self._append_to_section(page, "## 거래 이력", row, MAX_TRADE_TABLE_ROWS)
        if lesson:
            page = self._append_to_section(
                page, "## 거래 교훈",
                f"- [{datetime.now().strftime('%m/%d')}] {lesson[:100]}",
                MAX_LESSON_ROWS,
            )
        page = self._replace_frontmatter(page, fm)
        self._write_page(path, page)

    async def note_research(self, symbol: str, name: str = "",
                            source: str = "", text: str = "") -> None:
        """전문가/뉴스 수집 시 종목 리서치 노트 기록 (fire-and-forget 안전)

        - 새 페이지 생성은 MAX_SYMBOL_PAGES 상한 내에서만 (거래 페이지는 무제한)
        - 같은 날 같은 source 중복 기록 스킵
        - 최근 MAX_RESEARCH_ROWS개 롤링
        """
        if not symbol or not text:
            return
        # 내부 개행이 마크다운 행 구조를 깨뜨림 — 한 줄로 정규화 (리뷰 P1-2)
        text = " ".join(text.split())
        async with self._lock:
            try:
                path = self._dir / "symbols" / f"{_sanitize_filename(symbol)}.md"
                if not path.exists():
                    # 상한은 리서치 전용 페이지(trade_count=0)만 계수 — 거래 페이지가
                    # 늘어나도 리서치 신규 생성이 막히지 않게 (리뷰 P1-1)
                    research_only = 0
                    for p in (self._dir / "symbols").glob("*.md"):
                        try:
                            fm_p = self._parse_frontmatter(
                                p.read_text(encoding="utf-8")
                            )
                            if fm_p.get("trade_count", 0) == 0:
                                research_only += 1
                        except OSError:
                            continue
                    if research_only >= MAX_SYMBOL_PAGES:
                        logger.debug(
                            f"[Wiki] 리서치 페이지 상한({MAX_SYMBOL_PAGES}) — {symbol} 생성 스킵"
                        )
                        return
                page = self._read_or_create(path, self._symbol_template(symbol, name))

                # 연도 포함 — 1년 뒤 같은 날짜 오탐 스킵 방지 (리뷰 P2-5)
                today = datetime.now().strftime("%Y-%m-%d")
                marker = f"[{today}] [{source}]"
                if marker in self._extract_section(page, "## 리서치 노트"):
                    return  # 같은 날 같은 출처 중복
                page = self._append_to_section(
                    page, "## 리서치 노트",
                    f"- {marker} {text[:100]}",
                    MAX_RESEARCH_ROWS,
                )
                fm = self._parse_frontmatter(page)
                fm["last_updated"] = datetime.now().strftime("%Y-%m-%d %H:%M")
                if name:
                    fm["name"] = name
                page = self._replace_frontmatter(page, fm)
                self._write_page(path, page)
            except Exception as e:
                logger.debug(f"[Wiki] 리서치 노트 실패 (ignored): {e}")

    def query_symbol(self, symbol: str) -> str:
        """종목 페이지 컨텍스트 (거래 통계 + 교훈 3개 + 최근 리서치 5개, 최대 600자)

        cross_validator LLM 2차 검증 프롬프트 주입용 — 파일 읽기만, LLM 불필요.
        """
        try:
            path = self._dir / "symbols" / f"{_sanitize_filename(symbol)}.md"
            if not path.exists():
                return ""
            content = path.read_text(encoding="utf-8")
            fm = self._parse_frontmatter(content)
            parts = []
            if fm.get("trade_count", 0):
                parts.append(
                    f"[이력] {fm.get('trade_count',0)}건 승률{fm.get('win_rate',0):.0f}%"
                )
            lessons = self._extract_section(content, "## 거래 교훈")
            if lessons:
                parts += [
                    l for l in lessons.split("\n") if l.strip().startswith("-")
                ][:3]
            notes = self._extract_section(content, "## 리서치 노트")
            if notes:
                parts += [
                    l for l in notes.split("\n") if l.strip().startswith("-")
                ][:5]
            return "\n".join(parts)[:600]
        except Exception:
            return ""

    async def _extract_lesson(self, trade: Dict) -> str:
        """LLM으로 교훈 1~2줄 추출"""
        try:
            from ...utils.llm import LLMTask

            # 기존 전략 교훈 발췌
            strat_path = self._dir / "strategies" / f"{trade.get('strategy','unknown')}.md"
            existing = ""
            if strat_path.exists():
                content = strat_path.read_text(encoding="utf-8")
                lessons_section = self._extract_section(content, "## 교훈")
                existing = lessons_section[:300] if lessons_section else ""

            prompt = (
                f"최근 거래를 분석하여 1~2줄 교훈을 추출하세요.\n\n"
                f"거래: {trade.get('symbol')} {trade.get('name','')}, "
                f"전략={trade.get('strategy')}, 섹터={trade.get('sector','?')}\n"
                f"결과: {trade.get('pnl_pct',0):+.1f}%, "
                f"{trade.get('holding_days',0)}일 보유, {trade.get('exit_type','')}\n"
                f"시장: {trade.get('market_regime','?')}\n\n"
                f"기존 교훈:\n{existing}\n\n"
                f"새 교훈을 1~2줄로 작성하세요. 기존과 중복되지 않는 새로운 관점만."
            )

            resp = await self._llm.complete(prompt, task=LLMTask.WIKI_INGEST, max_tokens=150)
            # 개행 정규화 — 마크다운 행 구조 보호 (리뷰 P1-2)
            lesson = " ".join((resp.content if resp.success else "").split())
            if lesson:
                # 전략 페이지 교훈 섹션에 추가
                page = self._read_or_create(strat_path, "")
                today = datetime.now().strftime("%m/%d")
                page = self._append_to_section(
                    page, "## 교훈",
                    f"- [{today}] {lesson}",
                    max_rows=20,
                )
                self._write_page(strat_path, page)
            return lesson
        except Exception as e:
            logger.debug(f"[Wiki] 교훈 추출 실패: {e}")
            return ""

    # ================================================================
    # Query: 크로스검증 시 관련 위키 컨텍스트 반환
    # ================================================================

    def query(self, strategy: str = "", sector: str = "",
              regime: str = "") -> str:
        """관련 위키 페이지에서 컨텍스트 추출 (LLM 불필요, 파일 읽기만)

        Returns:
            관련 교훈/통계 요약 문자열 (최대 500자)
        """
        try:
            parts = []

            # 전략 페이지
            strat_path = self._dir / "strategies" / f"{strategy}.md"
            if strat_path.exists():
                content = strat_path.read_text(encoding="utf-8")
                fm = self._parse_frontmatter(content)
                lessons = self._extract_section(content, "## 교훈")
                if fm.get("trade_count", 0) > 0:
                    parts.append(
                        f"[{strategy}] {fm.get('trade_count',0)}건 "
                        f"승률{fm.get('win_rate',0):.0f}%"
                    )
                if lessons:
                    # 최근 3개 교훈만 — 새 행이 섹션 상단에 삽입되므로 앞쪽이 최신
                    # (기존 [-3:]는 가장 오래된 3개를 집던 버그, 리뷰 P2-6)
                    recent = [l for l in lessons.split("\n") if l.strip().startswith("-")][:3]
                    parts.extend(recent)

            # 섹터 페이지
            if sector:
                slug = _sanitize_filename(sector)
                sect_path = self._dir / "sectors" / f"{slug}.md"
                if sect_path.exists():
                    content = sect_path.read_text(encoding="utf-8")
                    fm = self._parse_frontmatter(content)
                    if fm.get("trade_count", 0) > 0:
                        parts.append(
                            f"[{sector}] {fm.get('trade_count',0)}건 "
                            f"승률{fm.get('win_rate',0):.0f}%"
                        )

            # 체제 페이지
            regime_path = self._dir / "regimes" / f"{regime}.md"
            if regime_path.exists():
                content = regime_path.read_text(encoding="utf-8")
                fm = self._parse_frontmatter(content)
                if fm.get("trade_count", 0) > 0:
                    parts.append(
                        f"[{regime}장] {fm.get('trade_count',0)}건 "
                        f"승률{fm.get('win_rate',0):.0f}%"
                    )

            result = "\n".join(parts)
            return result[:500] if result else ""
        except Exception:
            return ""

    # ================================================================
    # Lint: 주간 위키 헬스체크
    # ================================================================

    async def lint(self) -> Dict[str, Any]:
        """주간 위키 헬스체크

        Returns:
            {"stale_pages": [...], "low_winrate": [...], "total_pages": int}
        """
        report = {"stale_pages": [], "low_winrate": [], "total_pages": 0}
        try:
            all_pages = list(self._dir.rglob("*.md"))
            report["total_pages"] = len(all_pages)

            for page_path in all_pages:
                if page_path.name in ("index.md", "log.md"):
                    continue
                if "archive" in page_path.parts:
                    continue  # 아카이브는 검사 제외 (2026-08-07)
                content = page_path.read_text(encoding="utf-8")
                fm = self._parse_frontmatter(content)

                # Stale: 30일 이상 미업데이트
                last_updated = fm.get("last_updated", "")
                if last_updated:
                    try:
                        last_dt = datetime.strptime(last_updated[:10], "%Y-%m-%d")
                        if (datetime.now() - last_dt).days > 30:
                            report["stale_pages"].append(page_path.name)
                    except ValueError:
                        pass

                # 저조: 5건+ 거래, 승률 30% 미만
                tc = fm.get("trade_count", 0)
                wr = fm.get("win_rate", 0)
                if tc >= 5 and wr < 30:
                    report["low_winrate"].append(f"{page_path.name} ({wr:.0f}%)")

            # 종목 페이지 아카이브 — 마지막 갱신 180일 초과 (2026-08-07)
            # ingest/note_research와 rename 경합 방지를 위해 락 보유 (리뷰 P2-1)
            report["archived"] = []
            async with self._lock:
                sym_dir = self._dir / "symbols"
                archive_dir = sym_dir / "archive"
                for p in sym_dir.glob("*.md"):
                    try:
                        fm = self._parse_frontmatter(p.read_text(encoding="utf-8"))
                        lu = str(fm.get("last_updated", ""))[:10]
                        if lu and (
                            datetime.now() - datetime.strptime(lu, "%Y-%m-%d")
                        ).days > 180:
                            archive_dir.mkdir(exist_ok=True)
                            p.rename(archive_dir / p.name)
                            report["archived"].append(p.name)
                    except (ValueError, OSError):
                        continue

            logger.info(
                f"[Wiki Lint] {report['total_pages']}페이지, "
                f"Stale={len(report['stale_pages'])}, "
                f"저조={len(report['low_winrate'])}, "
                f"아카이브={len(report['archived'])}"
            )
        except Exception as e:
            logger.debug(f"[Wiki Lint] 실패: {e}")
        return report

    # ================================================================
    # 내부 헬퍼
    # ================================================================

    def _read_or_create(self, path: Path, template: str) -> str:
        if path.exists():
            return path.read_text(encoding="utf-8")
        path.write_text(template, encoding="utf-8")
        return template

    def _write_page(self, path: Path, content: str):
        lines = content.split("\n")
        if len(lines) > MAX_PAGE_LINES:
            lines = lines[:MAX_PAGE_LINES]
        path.write_text("\n".join(lines), encoding="utf-8")

    def _append_log(self, trade: Dict, lesson: str):
        log_path = self._dir / "log.md"
        if not log_path.exists():
            log_path.write_text("# Wiki Log\n\n", encoding="utf-8")

        ts = datetime.now().strftime("%Y-%m-%d %H:%M")
        entry = (
            f"- [{ts}] **{trade.get('symbol','')}** {trade.get('name','')} | "
            f"{trade.get('strategy','')} | {trade.get('pnl_pct',0):+.1f}% | "
            f"{trade.get('exit_type','')}"
        )
        if lesson:
            entry += f" | 교훈: {lesson[:80]}"

        lines = log_path.read_text(encoding="utf-8").split("\n")
        # 헤더 뒤에 삽입
        insert_idx = 2 if len(lines) > 2 else len(lines)
        lines.insert(insert_idx, entry)
        if len(lines) > MAX_LOG_LINES:
            lines = lines[:MAX_LOG_LINES]
        log_path.write_text("\n".join(lines), encoding="utf-8")

    def _rebuild_index(self):
        """index.md 재생성"""
        lines = ["# Trade Wiki Index", "", f"*갱신: {datetime.now().strftime('%Y-%m-%d %H:%M')}*", ""]

        for category, subdir in [("전략", "strategies"), ("섹터", "sectors"), ("시장체제", "regimes"), ("종목", "symbols")]:
            cat_dir = self._dir / subdir
            pages = sorted(cat_dir.glob("*.md"))
            if pages:
                lines.append(f"## {category}")
                for p in pages:
                    content = p.read_text(encoding="utf-8")
                    fm = self._parse_frontmatter(content)
                    tc = fm.get("trade_count", 0)
                    wr = fm.get("win_rate", 0)
                    lines.append(f"- [{p.stem}]({subdir}/{p.name}) — {tc}건, 승률 {wr:.0f}%")
                lines.append("")

        (self._dir / "index.md").write_text("\n".join(lines), encoding="utf-8")

    def _parse_frontmatter(self, content: str) -> Dict[str, Any]:
        """YAML 프론트매터 파싱 (간이)"""
        if not content.startswith("---"):
            return {}
        end = content.find("---", 3)
        if end == -1:
            return {}
        fm_text = content[3:end].strip()
        result = {}
        for line in fm_text.split("\n"):
            if ":" in line:
                key, val = line.split(":", 1)
                key = key.strip()
                val = val.strip()
                # 숫자 변환
                try:
                    if "." in val:
                        result[key] = float(val)
                    else:
                        result[key] = int(val)
                except ValueError:
                    result[key] = val
        return result

    def _replace_frontmatter(self, content: str, fm: Dict) -> str:
        """프론트매터 교체"""
        fm_lines = ["---"]
        for k, v in fm.items():
            fm_lines.append(f"{k}: {v}")
        fm_lines.append("---")
        fm_str = "\n".join(fm_lines)

        if content.startswith("---"):
            end = content.find("---", 3)
            if end != -1:
                return fm_str + content[end + 3:]
        return fm_str + "\n" + content

    def _append_to_section(self, content: str, heading: str, row: str,
                           max_rows: int = 30) -> str:
        """특정 섹션 뒤에 행 추가 (FIFO)"""
        idx = content.find(heading)
        if idx == -1:
            content += f"\n\n{heading}\n{row}\n"
            return content

        # 섹션 시작 이후 줄 찾기
        lines = content.split("\n")
        section_start = -1
        for i, line in enumerate(lines):
            if line.strip() == heading.strip():
                section_start = i
                break
        if section_start == -1:
            return content + f"\n{row}\n"

        # 테이블 헤더+구분선 직후가 삽입 위치 (리뷰 P2-4: 미사용 insert_at 루프 제거)
        table_data_start = section_start + 1
        # 테이블 헤더+구분선 건너뛰기
        while table_data_start < len(lines) and lines[table_data_start].startswith("|"):
            if lines[table_data_start].startswith("|--") or lines[table_data_start].startswith("| ---"):
                table_data_start += 1
                break
            table_data_start += 1

        lines.insert(table_data_start, row)

        # max_rows 초과 시 오래된 행 삭제 (테이블 | 행 + 불릿 - 행 모두)
        data_rows = []
        for i in range(table_data_start, len(lines)):
            line_s = lines[i].strip()
            if line_s.startswith("|") and not line_s.startswith("|--"):
                data_rows.append(i)
            elif line_s.startswith("- "):
                data_rows.append(i)
            elif line_s.startswith("##") or (line_s == "" and i > table_data_start + 1):
                break
        while len(data_rows) > max_rows:
            lines.pop(data_rows[-1])
            data_rows.pop()

        return "\n".join(lines)

    def _extract_section(self, content: str, heading: str) -> str:
        """특정 섹션 내용 추출"""
        idx = content.find(heading)
        if idx == -1:
            return ""
        start = idx + len(heading)
        # 다음 ## 또는 끝까지
        next_heading = content.find("\n## ", start)
        if next_heading == -1:
            return content[start:].strip()
        return content[start:next_heading].strip()

    # ================================================================
    # 템플릿
    # ================================================================

    def _strategy_template(self, strategy: str) -> str:
        names = {
            "sepa_trend": "SEPA 추세", "rsi2_reversal": "RSI2 반전",
            "theme_chasing": "테마 추종", "gap_and_go": "갭상승",
            "strategic_swing": "전략적 스윙", "momentum_breakout": "모멘텀",
            "earnings_drift": "어닝스 드리프트", "core_holding": "코어홀딩",
        }
        display = names.get(strategy, strategy)
        return f"""---
strategy: {strategy}
display_name: {display}
trade_count: 0
wins: 0
win_rate: 0
last_updated: {datetime.now().strftime('%Y-%m-%d %H:%M')}
---

# {display} ({strategy})

## 교훈

## 최근 거래
| 종목 | 이름 | 수익률 | 청산유형 | 보유 | 체제 | 날짜 |
|------|------|--------|---------|------|------|------|
"""

    def _symbol_template(self, symbol: str, name: str = "") -> str:
        display = name or symbol
        return f"""---
symbol: {symbol}
name: {display}
trade_count: 0
wins: 0
win_rate: 0
last_updated: {datetime.now().strftime('%Y-%m-%d %H:%M')}
---

# {display} ({symbol})

## 거래 교훈

## 리서치 노트

## 거래 이력
| 전략 | 수익률 | 청산유형 | 보유 | 체제 | 날짜 |
|------|--------|---------|------|------|------|
"""

    def _sector_template(self, sector: str) -> str:
        return f"""---
sector: {sector}
trade_count: 0
wins: 0
win_rate: 0
last_updated: {datetime.now().strftime('%Y-%m-%d %H:%M')}
---

# 섹터: {sector}

## 최근 거래
| 종목 | 전략 | 수익률 | 청산유형 | 날짜 |
|------|------|--------|---------|------|
"""

    def _regime_template(self, regime: str) -> str:
        labels = {"bull": "강세장", "bear": "약세장", "sideways": "횡보장", "neutral": "중립"}
        label = labels.get(regime, regime)
        return f"""---
regime: {regime}
display_name: {label}
trade_count: 0
wins: 0
win_rate: 0
last_updated: {datetime.now().strftime('%Y-%m-%d %H:%M')}
---

# {label} ({regime})

## 최근 거래
| 종목 | 전략 | 수익률 | 청산유형 | 날짜 |
|------|------|--------|---------|------|
"""
