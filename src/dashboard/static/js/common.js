/**
 * AI Trader v2 - 공통 유틸리티
 * SSE 연결, API 호출, 포맷팅
 */

// ============================================================
// SSE 연결 관리
// ============================================================
class SSEClient {
    constructor() {
        this.eventSource = null;
        this.handlers = {};
        this.reconnectDelay = 1000;
        this.maxReconnectDelay = 30000;
        this.connected = false;
    }

    connect() {
        if (this.eventSource) {
            this.eventSource.close();
        }

        this.eventSource = new EventSource('/api/stream');

        this.eventSource.onopen = () => {
            this.connected = true;
            this.reconnectDelay = 1000;
            console.log('[SSE] 연결됨');
            this._updateConnectionStatus(true);
        };

        this.eventSource.onerror = () => {
            this.connected = false;
            this._updateConnectionStatus(false);
            this.eventSource.close();

            // 재연결
            setTimeout(() => this.connect(), this.reconnectDelay);
            this.reconnectDelay = Math.min(this.reconnectDelay * 2, this.maxReconnectDelay);
        };

        // 이벤트 타입별 리스너
        const eventTypes = ['status', 'portfolio', 'positions', 'risk', 'events', 'pending_orders',
                            'us_status', 'us_portfolio', 'us_positions', 'us_risk',
                            'market_indices', 'core_holdings'];
        eventTypes.forEach(type => {
            this.eventSource.addEventListener(type, (e) => {
                try {
                    const data = JSON.parse(e.data);
                    this._dispatch(type, data);
                } catch (err) {
                    console.error(`[SSE] ${type} 파싱 오류:`, err);
                }
            });
        });
    }

    on(eventType, handler) {
        if (!this.handlers[eventType]) {
            this.handlers[eventType] = [];
        }
        this.handlers[eventType].push(handler);
    }

    _dispatch(eventType, data) {
        const handlers = this.handlers[eventType] || [];
        handlers.forEach(h => {
            try { h(data); } catch(e) { console.error(e); }
        });
    }

    _updateConnectionStatus(connected) {
        const el = document.getElementById('sb-status');
        if (!el) return;
        if (connected) {
            el.innerHTML = '<span class="status-dot green"></span> 연결됨';
        } else {
            el.innerHTML = '<span class="status-dot red"></span> 연결 끊김';
        }
    }
}

// 전역 SSE 클라이언트
const sse = new SSEClient();

// ============================================================
// API 호출
// ============================================================
async function api(path) {
    const resp = await fetch(path);
    if (!resp.ok) throw new Error(`API error: ${resp.status}`);
    return resp.json();
}

// ============================================================
// 포맷팅
// ============================================================
function formatNumber(n, decimals = 0) {
    if (n === null || n === undefined || isNaN(n)) return '--';
    return Number(n).toLocaleString('ko-KR', {
        minimumFractionDigits: decimals,
        maximumFractionDigits: decimals,
    });
}

function formatCurrency(n) {
    if (n === null || n === undefined || isNaN(n)) return '--';
    return formatNumber(n, 0);
}

function formatUSD(n, decimals = 2) {
    if (n === null || n === undefined || isNaN(n)) return '--';
    return '$' + Number(n).toLocaleString('en-US', {
        minimumFractionDigits: decimals,
        maximumFractionDigits: decimals,
    });
}

function formatPct(n, decimals = 2) {
    if (n === null || n === undefined || isNaN(n)) return '--';
    const prefix = n > 0 ? '+' : '';
    return prefix + Number(n).toFixed(decimals) + '%';
}

function formatPnl(n) {
    if (n === null || n === undefined || isNaN(n)) return '--';
    const prefix = n > 0 ? '+' : '';
    return prefix + formatNumber(n, 0);
}

function pnlClass(n) {
    if (n > 0) return 'text-profit';
    if (n < 0) return 'text-loss';
    return '';
}

function formatTime(isoString) {
    if (!isoString) return '--';
    const d = new Date(isoString);
    return d.toLocaleTimeString('ko-KR', { hour: '2-digit', minute: '2-digit', second: '2-digit' });
}

function formatDate(isoString) {
    if (!isoString) return '--';
    const d = new Date(isoString);
    return d.toLocaleDateString('ko-KR', { year: 'numeric', month: '2-digit', day: '2-digit' });
}

function formatDuration(seconds) {
    if (!seconds && seconds !== 0) return '--';
    const h = Math.floor(seconds / 3600);
    const m = Math.floor((seconds % 3600) / 60);
    const s = Math.floor(seconds % 60);
    return `${String(h).padStart(2,'0')}:${String(m).padStart(2,'0')}:${String(s).padStart(2,'0')}`;
}

function esc(str) {
    // HTML 텍스트 노드 + 속성값 양쪽에 안전 — &, <, >, ", ', / 모두 escape
    // (createElement+textContent는 "와 ' 미처리 → title="..."에 삽입 시 속성 폭주 위험)
    if (str == null) return '';
    return String(str)
        .replace(/&/g, '&amp;')
        .replace(/</g, '&lt;')
        .replace(/>/g, '&gt;')
        .replace(/"/g, '&quot;')
        .replace(/'/g, '&#39;')
        .replace(/\//g, '&#x2F;');
}

/** URL 검증 — javascript: / data: 스킴 차단 */
function safeUrl(url) {
    if (!url) return '';
    const s = String(url).trim().toLowerCase();
    if (s.startsWith('javascript:') || s.startsWith('data:') || s.startsWith('vbscript:')) {
        return '#';
    }
    return url;
}

function sessionLabel(session) {
    const map = {
        'pre_market': '프리장',
        'regular': '정규장',
        'next': '넥스트장',
        'after_hours': '시간외',
        'closed': '마감',
    };
    return map[session] || session;
}

// ============================================================
// 마켓 필터 유틸리티 (KR / US / ALL)
// ============================================================
const MarketFilter = {
    STORAGE_KEY: "market_filter",
    DEFAULT: "all",

    get() {
        return localStorage.getItem(this.STORAGE_KEY) || this.DEFAULT;
    },

    set(val) {
        localStorage.setItem(this.STORAGE_KEY, val);
        document.dispatchEvent(new CustomEvent("market_filter_change", { detail: { filter: val } }));
    },

    /** 필터 바 HTML 생성 및 containerEl에 삽입 후 이벤트 바인딩 */
    render(containerEl, onChange) {
        const current = this.get();
        containerEl.innerHTML = `
            <div class="mf-bar" style="display:flex;gap:6px;align-items:center;">
                <span style="font-size:0.75rem;color:var(--text-muted);margin-right:4px;">마켓</span>
                <button class="mf-btn ${current==="all"?"mf-active":""}" data-val="all">🌐 통합</button>
                <button class="mf-btn ${current==="kr"?"mf-active":""}" data-val="kr">🇰🇷 국내</button>
                <button class="mf-btn ${current==="us"?"mf-active":""}" data-val="us">🇺🇸 미국</button>
            </div>`;
        containerEl.querySelectorAll(".mf-btn").forEach(btn => {
            btn.addEventListener("click", () => {
                const val = btn.dataset.val;
                MarketFilter.set(val);
                containerEl.querySelectorAll(".mf-btn").forEach(b => b.classList.remove("mf-active"));
                btn.classList.add("mf-active");
                if (onChange) onChange(val);
            });
        });
    },
};

// ============================================================
// 네비 롤링 지수 전광판
// ============================================================

function _tickerColor(changePct) {
    const t = Math.min(Math.abs(changePct) / 3.0, 1.0);
    if (changePct >= 0) {
        const r = Math.round(252 - t * 32);
        const g = Math.round(165 - t * 127);
        const b = Math.round(165 - t * 127);
        return `rgb(${r},${g},${b})`;
    } else {
        const r = Math.round(147 - t * 118);
        const g = Math.round(197 - t * 119);
        const b = Math.round(253 - t * 37);
        return `rgb(${r},${g},${b})`;
    }
}

function _buildTickerHTML(indices) {
    const items = indices.map(idx => {
        const up    = idx.change_pct >= 0;
        const arrow = up ? '▲' : '▼';
        const kind  = idx.kind || '';
        const isPep  = idx.label === '펩트론';
        const tiCls  = kind === 'index_kr' ? 'nav-ti nav-ti-kr'
                     : kind === 'index_us' ? 'nav-ti nav-ti-us'
                     : isPep ? 'nav-ti nav-ti-pep'
                     : 'nav-ti nav-ti-stock';
        const tvCls  = isPep ? 'nav-tv nav-tv-pep' : 'nav-tv';
        const color  = _tickerColor(idx.change_pct);
        const isKRPrice = kind === 'index_kr' || kind === 'stock_kr';
        const price = isKRPrice
            ? Math.round(idx.price).toLocaleString() + '원'
            : idx.price.toLocaleString('en-US', {minimumFractionDigits:2, maximumFractionDigits:2});
        const pctStr = (up ? '+' : '') + idx.change_pct.toFixed(2) + '%';
        return `<span class="${tiCls}">${idx.label}</span>` +
               `<span class="${tvCls}" style="color:${color}">${arrow} ${price} ${pctStr}</span>` +
               `<span class="nav-ts">·</span>`;
    }).join('');
    return items + items;
}

// SSE 업데이트용 버퍼 — 다음 애니메이션 사이클 시작 시 적용
let _pendingTickerData = null;
let _tickerIterListenerAdded = false;

function _initTickerIterListener(inner) {
    if (_tickerIterListenerAdded) return;
    inner.addEventListener('animationiteration', () => {
        if (_pendingTickerData) {
            // 사이클 경계(translateX=0)에서 교체 → 끊김 없음
            inner.innerHTML = _buildTickerHTML(_pendingTickerData);
            _pendingTickerData = null;
        }
    });
    _tickerIterListenerAdded = true;
}

function _applyTickerData(data, forceReset) {
    if (!data || !data.length) return;
    const inner = document.getElementById('nav-ticker-inner');
    if (!inner) return;
    const strip = inner.closest('.ticker-strip');

    const isEmpty = !inner.innerHTML.trim() || inner.innerHTML.includes('--');
    if (isEmpty || forceReset) {
        // 최초 로드 or 강제: animation 리셋 후 즉시 시작
        inner.innerHTML = _buildTickerHTML(data);
        inner.style.animation = 'none';
        void inner.offsetWidth;
        inner.style.animation = '';
        if (strip && strip.style.opacity !== '1') {
            strip.style.transition = 'opacity 0.5s ease';
            strip.style.opacity = '1';
        }
        _initTickerIterListener(inner);
    } else {
        // SSE 업데이트: 다음 사이클 경계에서 교체 (중간 초기화 방지)
        _pendingTickerData = data;
        _initTickerIterListener(inner);
        if (strip && strip.style.opacity !== '1') {
            strip.style.transition = 'opacity 0.5s ease';
            strip.style.opacity = '1';
        }
    }
}

async function fetchNavIndices() {
    try {
        const data = await fetch('/api/market/indices').then(r => r.json());
        _applyTickerData(data);
    } catch(e) {}
    // SSE가 살아있으면 폴링 주기 늘림 (백업용 60초)
    setTimeout(fetchNavIndices, 60 * 1000);
}

// ============================================================
// 네비게이션 활성화
// ============================================================
document.addEventListener('DOMContentLoaded', () => {
    const path = window.location.pathname;
    document.querySelectorAll('.nav-pill').forEach(link => {
        const page = link.dataset.page;
        if (
            (page === 'index' && path === '/') ||
            (page !== 'index' && path === '/' + page)
        ) {
            link.classList.add('active');
        }
    });

    // SSE 연결 (status-bar 업데이트)
    sse.on('status', (data) => {
        const sbSession = document.getElementById('sb-session');
        const sbUptime = document.getElementById('sb-uptime');
        if (sbSession) sbSession.textContent = sessionLabel(data.session);
        if (sbUptime) sbUptime.textContent = formatDuration(data.uptime_seconds);
    });

    // ★ 지수 전광판 실시간 업데이트 (SSE push, 10초 주기)
    sse.on('market_indices', (data) => {
        _applyTickerData(data);
    });

    // 전광판 초기화 (모든 페이지 공통) — 데이터 로드 전까지 숨김
    // SSE 연결 전 초기 데이터는 HTTP 폴링으로 즉시 로드 (1초 후)
    const _tickerInner = document.getElementById('nav-ticker-inner');
    if (_tickerInner) {
        const _strip = _tickerInner.closest('.ticker-strip');
        if (_strip) _strip.style.opacity = '0';
        setTimeout(fetchNavIndices, 1000);  // 최초 1회 즉시 로드
    }
});
