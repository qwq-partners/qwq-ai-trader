"""
QWQ AI Trader - 통합 대시보드 웹서버

KR(ai-trader-v2) + US(ai-trader-us) 대시보드를 단일 aiohttp 서버로 통합.
- KR API: /api/*
- US API: /api/us/*
- SSE 스트림: /api/stream
- 정적 파일: /static/*
"""

import asyncio
from pathlib import Path

from aiohttp import web
from loguru import logger

from .kr_api import setup_kr_api_routes
from .us_api import setup_us_api_routes
from .engine_api import setup_engine_api_routes
from .system_api import setup_system_api_routes
from .data_collector import DashboardDataCollector
from .sse import SSEManager


DASHBOARD_DIR = Path(__file__).parent
TEMPLATES_DIR = DASHBOARD_DIR / "templates"
STATIC_DIR = DASHBOARD_DIR / "static"


@web.middleware
async def cors_middleware(request, handler):
    """CORS 미들웨어 — 모바일 앱 웹 프리뷰 및 외부 클라이언트 허용"""
    if request.method == "OPTIONS":
        response = web.Response(status=204)
    else:
        response = await handler(request)

    response.headers["Access-Control-Allow-Origin"] = "*"
    response.headers["Access-Control-Allow-Methods"] = "GET, POST, OPTIONS"
    response.headers["Access-Control-Allow-Headers"] = "Content-Type"
    return response


@web.middleware
async def no_cache_middleware(request, handler):
    """정적 파일 캐시 방지 미들웨어"""
    response = await handler(request)
    if request.path.startswith("/static/"):
        response.headers["Cache-Control"] = "no-cache, no-store, must-revalidate"
        response.headers["Pragma"] = "no-cache"
        response.headers["Expires"] = "0"
    return response


class DashboardServer:
    """통합 대시보드 웹서버 (KR + US)"""

    def __init__(self, kr_bot=None, us_engine=None, host: str = "0.0.0.0", port: int = 8080):
        """
        Args:
            kr_bot: KR 트레이딩 봇 인스턴스 (None이면 KR API 비활성)
            us_engine: US LiveEngine 인스턴스 (None이면 US API 비활성)
            host: 바인딩 호스트
            port: 바인딩 포트
        """
        self.kr_bot = kr_bot
        self.us_engine = us_engine
        self.host = host
        self.port = port

        # KR 데이터 수집기 (kr_bot이 있을 때만 활성)
        self.data_collector = DashboardDataCollector(kr_bot) if kr_bot else None
        self.sse_manager = SSEManager(self.data_collector, us_engine=us_engine)

        self._app: web.Application = None
        self._runner: web.AppRunner = None
        self._site: web.TCPSite = None

    def _create_app(self) -> web.Application:
        """aiohttp 앱 생성"""
        app = web.Application(middlewares=[cors_middleware, no_cache_middleware])

        # KR REST API 라우트 (/api/*)
        if self.data_collector:
            setup_kr_api_routes(app, self.data_collector)

        # US REST API 라우트 (/api/us/*)
        if self.us_engine:
            setup_us_api_routes(app, self.us_engine)

        # 엔진 API 라우트 (/api/engine/*)
        setup_engine_api_routes(app)

        # 시스템 리소스 API (/api/system/*) — 인프라 다운사이징 검토용
        setup_system_api_routes(app)

        # 통합 SSE 스트림
        app.router.add_get("/api/stream", self.sse_manager.handle_stream)

        # 페이지 라우트
        app.router.add_get("/", self._serve_page("index.html"))
        app.router.add_get("/equity", lambda r: web.HTTPFound("/performance"))
        app.router.add_get("/trades", self._serve_page("trades.html"))
        app.router.add_get("/performance", self._serve_page("performance.html"))
        app.router.add_get("/themes", self._serve_page("themes.html"))
        app.router.add_get("/settings", self._serve_page("settings.html"))
        app.router.add_get("/evolution", self._serve_page("evolution.html"))
        app.router.add_get("/engine", self._serve_page("engine.html"))
        app.router.add_get("/principles", self._serve_page("principles.html"))
        app.router.add_get("/settlement", lambda r: web.HTTPFound("/trades"))

        # 정적 파일 서빙
        app.router.add_static("/static", STATIC_DIR, name="static")

        return app

    # 2026-04-22: US 엔진 비활성 시 US 관련 UI 전역 숨김 + JS 가드 주입
    # self.us_engine is None이면 자동으로 활성화 — 템플릿 수정 없이 정책 토글 가능.
    _US_DISABLE_SNIPPET = """<style id="us-disable-style">
/* US 엔진 비활성: 모든 US 관련 UI 숨김 */
#us-market-card, #us-positions-full, #us-signals-section,
#us-summary-section, #us-positions-summary,
#us-trades-section, #us-holdings-section,
#us-themes-section, #us-performance-section,
#us-screening-count, #us-themes-grid, #us-screening-body,
/* 2026-04-25 추가: KR/US 비교 섹션 (통합 필터 트리거) + 추가 US 자식 ID들 */
#combined-section,
#us-trade-date, #us-btn-today, #us-loading, #us-filter-tabs,
#us-s-realized, #us-s-unrealized, #us-s-buys, #us-s-sells, #us-s-winloss,
#us-trades-count, #us-trades-body, #us-positions-count, #us-holdings-tbody,
#us-perf-total, #us-perf-winrate, #us-perf-pnl, #us-perf-positions,
#btn-refresh-us-themes, #btn-refresh-us-screening,
.mf-btn[data-val="us"], .mf-btn[data-val="all"],
.nav-pill[data-page="us"],
[data-market="us"] {
    display: none !important;
}
/* 설정 페이지의 "US 오버나이트 시그널" 카드 숨김 */
.card:has(#cfg-us-market) { display: none !important; }
/* 2026-04-25: 호환성 — :has() 미지원 브라우저용 직접 ID 매칭 */
#cfg-us-market { display: none !important; }
/* US 카드가 사라진 상태에서 KR 카드를 전폭으로 확장 */
.markets-grid { grid-template-columns: 1fr !important; }
/* 2026-04-23 추가: 티커 스트립의 US 지수(S&P500/NASDAQ/DOW) 숨김 */
.nav-ti-us,
.nav-ti-us + .nav-tv,
.nav-ti-us + .nav-tv + .nav-ts { display: none !important; }
</style>
<script id="us-disable-script">
window.US_ENABLED = false;
// US 데이터 로딩 함수가 정의돼 있으면 noop으로 덮어쓰기 (fetch 낭비 방지)
document.addEventListener("DOMContentLoaded", function() {
    if (typeof loadUSData === "function") { window.loadUSData = function() {}; }
    // 마켓 필터가 us/all로 저장돼 있으면 kr로 강제
    try {
        var cur = localStorage.getItem("market_filter");
        if (cur === "us" || cur === "all") { localStorage.setItem("market_filter", "kr"); }
    } catch(e) {}
    // 2026-04-25 추가: 런타임 안전장치 — id="us-*" 또는 🇺🇸/미국 텍스트 포함 요소 숨김
    try {
        // ID 기반: us-* 시작
        document.querySelectorAll('[id^="us-"]').forEach(function(el) {
            el.style.setProperty('display', 'none', 'important');
        });
        // #cfg-us-market 부모 카드도 숨김 (settings.html)
        var cfgUs = document.getElementById('cfg-us-market');
        if (cfgUs) {
            var card = cfgUs.closest('.card, .card-inner') || cfgUs.parentElement;
            if (card) card.style.setProperty('display', 'none', 'important');
        }
        // KR/US 비교 섹션
        var combined = document.getElementById('combined-section');
        if (combined) combined.style.setProperty('display', 'none', 'important');
        // h2/h3 텍스트에 "🇺🇸" 포함된 카드 숨김 (auto-detection)
        document.querySelectorAll('h2, h3').forEach(function(h) {
            var t = h.textContent || '';
            if (t.indexOf('🇺🇸') >= 0 || t.indexOf('미국') >= 0) {
                var card = h.closest('.card, .card-inner, [id]') || h.parentElement;
                if (card) card.style.setProperty('display', 'none', 'important');
            }
        });
    } catch(e) {}
    // 티커 빌더가 US 지수를 내보내지 않도록 데이터 필터 래퍼 (common.js 실행 후 DOM mutation 방어)
    try {
        var _stripEl = document.getElementById('nav-ticker-inner');
        if (_stripEl) {
            var _observer = new MutationObserver(function() {
                _stripEl.querySelectorAll('.nav-ti-us').forEach(function(ti) {
                    // 함께 따라붙는 값(.nav-tv)과 구분자(.nav-ts) 제거
                    var nx1 = ti.nextElementSibling;
                    var nx2 = nx1 ? nx1.nextElementSibling : null;
                    ti.remove();
                    if (nx1 && nx1.classList.contains('nav-tv')) nx1.remove();
                    if (nx2 && nx2.classList.contains('nav-ts')) nx2.remove();
                });
            });
            _observer.observe(_stripEl, { childList: true, subtree: false });
        }
    } catch(e) {}
});
</script>
"""

    # 2026-04-23: Mobile-First v2 — 모든 페이지에 CSS/JS 자동 주입
    # 하단 fixed nav + 스티키 요약 + 홈 Quick KPI + 거래 카드 + 성과 탭
    _MOBILE_V2_SNIPPET = """
<meta name="viewport" content="width=device-width, initial-scale=1, viewport-fit=cover">
<meta name="theme-color" content="#0b0e18">
<link rel="stylesheet" href="/static/css/mobile-v2.css?v=7">
<script defer src="/static/js/mobile-v2.js?v=7"></script>
"""

    def _serve_page(self, template_name: str):
        """HTML 페이지 서빙 핸들러 팩토리"""
        async def handler(request: web.Request) -> web.Response:
            file_path = TEMPLATES_DIR / template_name
            if not file_path.exists():
                return web.Response(text="Page not found", status=404)

            content = file_path.read_text(encoding="utf-8")

            # US 엔진 비활성 시 전역 숨김 snippet 주입
            if not self.us_engine and "</head>" in content:
                content = content.replace(
                    "</head>", self._US_DISABLE_SNIPPET + "</head>", 1
                )

            # Mobile-First v2 주입 (모든 페이지 공통)
            if "</head>" in content:
                content = content.replace(
                    "</head>", self._MOBILE_V2_SNIPPET + "</head>", 1
                )

            return web.Response(text=content, content_type="text/html", charset="utf-8")

        return handler

    async def start(self):
        """서버 시작"""
        # 종목 마스터 사전 로드 (이벤트 루프 블로킹 방지)
        if self.data_collector:
            await DashboardDataCollector._load_stock_master()

        # SignalEventStorage SSE 콜백 연결
        try:
            from src.data.storage.signal_event_storage import SignalEventStorage
            SignalEventStorage.get().set_sse_callback(self.sse_manager.broadcast)
        except Exception as _e:
            logger.debug(f"[대시보드] SignalEventStorage SSE 연결 실패 (무시): {_e}")

        self._app = self._create_app()
        self._runner = web.AppRunner(self._app)
        await self._runner.setup()

        self._site = web.TCPSite(self._runner, self.host, self.port)
        await self._site.start()

        markets = []
        if self.kr_bot:
            markets.append("KR")
        if self.us_engine:
            markets.append("US")
        market_str = "+".join(markets) if markets else "NONE"

        logger.info(f"[대시보드] http://{self.host}:{self.port} 에서 실행 중 ({market_str})")

    async def stop(self):
        """서버 중지"""
        await self.sse_manager.stop()
        if self._site:
            await self._site.stop()
        if self._runner:
            await self._runner.cleanup()
        logger.info("[대시보드] 서버 종료")

    async def run(self):
        """서버 + SSE 브로드캐스트 실행 (태스크용)"""
        # 포트 바인딩 재시도 (재시작 시 이전 프로세스 포트 점유 대기)
        max_retries = 5
        for attempt in range(max_retries):
            try:
                await self.start()
                break
            except OSError as e:
                if "address already in use" in str(e) and attempt < max_retries - 1:
                    wait = 3 * (attempt + 1)
                    logger.warning(f"[대시보드] 포트 {self.port} 점유 중 — {wait}초 후 재시도 ({attempt + 1}/{max_retries})")
                    await asyncio.sleep(wait)
                else:
                    logger.error(f"[대시보드] 포트 바인딩 실패: {e}")
                    return

        try:
            # SSE 브로드캐스트 루프 실행
            await self.sse_manager.run_broadcast_loop()
        except asyncio.CancelledError:
            pass
        except Exception as e:
            logger.error(f"[대시보드] 서버 오류: {e}")
        finally:
            await self.stop()
