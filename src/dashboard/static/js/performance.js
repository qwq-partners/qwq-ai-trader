/**
 * QWQ AI Trader - 성과 분석 (통합)
 * performance.js v6 — 성과 탭 + 자산 탭 통합
 *
 * 의존: common.js (api, sse, formatPnl, formatPct, pnlClass, formatCurrency, formatNumber, esc)
 */

// ── 전역 상태 ──────────────────────────────────────────────
let currentDays = 7;
let _krSnaps = [];
let _usSnaps = [];
let _fxRate = 1450;
let _refreshTimer = null;
let _kospiData = [];

// ── 로컬 포맷 헬퍼 (common.js에 없는 것) ──────────────────
const fmtKRW    = v => Number(v || 0).toLocaleString('ko-KR');
const fmtUSD    = v => '$' + Number(v || 0).toLocaleString('en-US', { minimumFractionDigits: 2, maximumFractionDigits: 2 });
const fmtPnlKRW = v => (v == null ? '--' : (v >= 0 ? '+' : '') + fmtKRW(v) + '원');
const fmtPnlUSD = v => (v == null ? '--' : v === 0 ? '$0.00' : (v > 0 ? '+' : '-') + '$' + Math.abs(Number(v)).toFixed(2));
const pnlCls    = v => v == null ? '' : (v >= 0 ? 'text-profit' : 'text-loss');
const fmtPctLocal = v => (v == null ? '--' : (v >= 0 ? '+' : '') + Number(v).toFixed(2) + '%');

// ── 환율 조회 ──────────────────────────────────────────────
async function fetchFxRate() {
    try {
        const resp = await fetch('https://query1.finance.yahoo.com/v8/finance/chart/USDKRW=X?interval=1d&range=1d');
        const json = await resp.json();
        const price = json?.chart?.result?.[0]?.meta?.regularMarketPrice;
        if (price && price > 0) { _fxRate = Math.round(price); }
    } catch (_) { /* 실패 시 기본값 1450 유지 */ }
}

// ── 커스텀 날짜 범위 상태 ────────────────────────────────────
let _customDateFrom = '';
let _customDateTo = '';

// ── 메인 로드 (KR) ─────────────────────────────────────────
async function loadPerformance(days) {
    currentDays = days;
    _customDateFrom = '';
    _customDateTo = '';

    // 탭 활성화 표시
    document.querySelectorAll('.tab-btn[data-days]').forEach(b => {
        b.classList.toggle('active', parseInt(b.dataset.days) === days);
    });

    // P1-3: 로딩 UI — 전체 영역에 오버레이 표시 (1~3초 블랭크 방지)
    _showPerfLoading(true);

    try {
        const [stats, equityData] = await Promise.all([
            api('/api/trades/stats?days=' + days),
            api('/api/equity-history?days=' + (days < 7 ? 9999 : days)),
        ]);

        _krSnaps = equityData.snapshots || [];

        renderSummaryCards(stats, _krSnaps);
        renderEquityChart(_krSnaps);
        renderStrategyCards(stats.by_strategy || {});
        renderStrategyChart(stats.by_strategy || {});
        renderExitPnlChart(stats.by_exit_type || {});
        renderStrategyTreemap(stats.by_strategy || {});
        renderStrategyTable(stats.by_strategy || {});
        renderDailyTable(_krSnaps);
    } catch (e) {
        console.error('[성과] KR 로드 오류:', e);
    } finally {
        _showPerfLoading(false);
    }

    // 벤치마크 (비동기, 차트 지연 허용)
    fetchBenchmark(days).then(() => renderBenchmarkChart(_krSnaps, _kospiData));
}

function _showPerfLoading(show) {
    let ov = document.getElementById('perf-loading-overlay');
    if (show && !ov) {
        ov = document.createElement('div');
        ov.id = 'perf-loading-overlay';
        ov.style.cssText = 'position:fixed;top:0;left:0;right:0;height:3px;background:linear-gradient(90deg,#6366f1,#22d3ee,#6366f1);background-size:200% 100%;animation:perfShimmer 1.2s linear infinite;z-index:9999;';
        const style = document.getElementById('perf-shimmer-style');
        if (!style) {
            const s = document.createElement('style');
            s.id = 'perf-shimmer-style';
            s.textContent = '@keyframes perfShimmer{0%{background-position:200% 0}100%{background-position:-200% 0}}';
            document.head.appendChild(s);
        }
        document.body.appendChild(ov);
    } else if (!show && ov) {
        ov.remove();
    }
}

// ── 날짜 범위 로드 (KR) ──────────────────────────────────────
async function loadPerformanceByRange(dateFrom, dateTo) {
    _customDateFrom = dateFrom;
    _customDateTo = dateTo;

    // 탭 비활성화
    document.querySelectorAll('.tab-btn[data-days]').forEach(b => b.classList.remove('active'));

    // days 계산 (벤치마크 + US 기간 동기화용)
    const diffMs = new Date(dateTo) - new Date(dateFrom);
    currentDays = Math.max(1, Math.ceil(diffMs / 86400000));

    try {
        const [stats, equityData] = await Promise.all([
            api('/api/trades/stats?days=' + currentDays),
            api('/api/equity-history?from=' + dateFrom + '&to=' + dateTo),
        ]);

        _krSnaps = equityData.snapshots || [];

        renderSummaryCards(stats, _krSnaps);
        renderEquityChart(_krSnaps);
        renderStrategyCards(stats.by_strategy || {});
        renderStrategyChart(stats.by_strategy || {});
        renderExitPnlChart(stats.by_exit_type || {});
        renderStrategyTreemap(stats.by_strategy || {});
        renderStrategyTable(stats.by_strategy || {});
        renderDailyTable(_krSnaps);
    } catch (e) {
        console.error('[성과] KR 범위 로드 오류:', e);
    }

    fetchBenchmark(currentDays).then(() => renderBenchmarkChart(_krSnaps, _kospiData));
}

// ── 요약 카드 6개 ──────────────────────────────────────────
function renderSummaryCards(stats, snaps) {
    // 1) 총자산
    const totalEqEl = document.getElementById('perf-equity');
    if (totalEqEl) {
        if (snaps.length > 0) {
            const lastEq = snaps[snaps.length - 1].total_equity;
            totalEqEl.textContent = fmtKRW(lastEq) + '원';
        } else {
            totalEqEl.textContent = '--';
        }
    }

    // 2) 기간 수익률
    const retEl = document.getElementById('perf-return');
    if (retEl) {
        if (snaps.length >= 2) {
            const first = snaps[0].total_equity;
            const last  = snaps[snaps.length - 1].total_equity;
            const pct   = first > 0 ? (last - first) / first * 100 : 0;
            retEl.textContent = fmtPctLocal(+pct.toFixed(2));
            retEl.className   = 'stat-value mono ' + pnlCls(pct);
        } else {
            retEl.textContent = '--';
            retEl.className   = 'stat-value mono';
        }
    }

    // 3) Max Drawdown
    const ddEl = document.getElementById('perf-mdd');
    if (ddEl) {
        if (snaps.length > 0) {
            let peak = snaps[0].total_equity, maxDD = 0;
            for (const s of snaps) {
                if (s.total_equity > peak) peak = s.total_equity;
                const dd = peak > 0 ? (peak - s.total_equity) / peak * 100 : 0;
                if (dd > maxDD) maxDD = dd;
            }
            ddEl.textContent = maxDD > 0 ? fmtPctLocal(+(-maxDD).toFixed(2)) : '--';
            ddEl.className   = 'stat-value mono text-loss';
        } else {
            ddEl.textContent = '--';
            ddEl.className   = 'stat-value mono';
        }
    }

    // 4) 거래수 (보유중 표시)
    const totalEl = document.getElementById('perf-total');
    if (totalEl) {
        const closed = stats.total_trades || 0;
        const open   = stats.open_trades || 0;
        const all    = stats.all_trades || closed;
        if (open > 0) {
            totalEl.innerHTML = esc(String(all)) + ' <span style="font-size:0.65rem; color:var(--text-muted); font-weight:400;">(보유' + esc(String(open)) + ')</span>';
        } else {
            totalEl.textContent = all;
        }
    }

    // 5) 승률
    const wrEl = document.getElementById('perf-winrate');
    if (wrEl) {
        const closed  = stats.total_trades || 0;
        const winRate = stats.win_rate || 0;
        wrEl.textContent = closed > 0 ? winRate.toFixed(1) + '%' : '--';
        wrEl.className   = 'stat-value mono ' + (winRate >= 50 ? 'text-profit' : winRate > 0 ? 'text-loss' : '');
    }

    // 6) Profit Factor — by_exit_type 합산
    const pfEl = document.getElementById('perf-pf');
    if (pfEl) {
        const byExit = stats.by_exit_type || {};
        let totalGross = 0, totalLoss = 0;
        for (const key of Object.keys(byExit)) {
            const et = byExit[key];
            const avgPnl = et.avg_pnl || 0;
            const trades = et.trades || 0;
            const sumPnl = avgPnl * trades;
            if (sumPnl > 0) totalGross += sumPnl;
            else totalLoss += Math.abs(sumPnl);
        }
        if (totalGross === 0 && totalLoss === 0) {
            pfEl.textContent = '--';
        } else if (totalLoss === 0) {
            pfEl.textContent = totalGross > 0 ? '999' : '--';
        } else {
            pfEl.textContent = (totalGross / totalLoss).toFixed(2);
        }
    }
}

// ── 총자산 차트 (equity.js 방식 + 일별 손익 바) ────────────
function renderEquityChart(snaps) {
    const chartId = 'equity-chart';
    const el = document.getElementById(chartId);
    if (!el) return;

    if (!snaps || snaps.length === 0) {
        el.innerHTML = '<div style="display:flex;align-items:center;justify-content:center;height:100%;color:var(--text-muted);font-size:.85rem;">데이터 없음</div>';
        return;
    }

    const dates    = snaps.map(s => s.date);
    const equities = snaps.map(s => s.total_equity);
    const dailyPnl = snaps.map(s => s.daily_pnl || 0);

    // 마커 색상 (전일 대비 상승/하락)
    const markerColors = snaps.map((s, i) => {
        if (i === 0) return '#6366f1';
        return s.total_equity >= snaps[i - 1].total_equity ? '#34d399' : '#f87171';
    });

    // 호버 텍스트
    const hoverTexts = snaps.map((s, i) => {
        const prev   = i > 0 ? snaps[i - 1].total_equity : s.total_equity;
        const change = s.total_equity - prev;
        const sign   = change >= 0 ? '+' : '';
        const pctSign = (s.daily_pnl_pct || 0) >= 0 ? '+' : '';
        return '<b>' + esc(s.date) + '</b><br>총자산 <b>' + fmtKRW(s.total_equity) + '</b>원<br>' +
               '변동 ' + sign + fmtKRW(Math.round(change)) + '원 (' + pctSign + (s.daily_pnl_pct || 0) + '%)';
    });

    // Y축 범위
    const minE = Math.min(...equities);
    const maxE = Math.max(...equities);
    const pad  = (maxE - minE) * 0.3 || maxE * 0.02;
    const yMin = minE - pad, yMax = maxE + pad;

    // 베이스 트레이스 (fill 기준선)
    const base = {
        x: dates, y: dates.map(() => yMin),
        type: 'scatter', mode: 'lines', line: { width: 0 },
        showlegend: false, hoverinfo: 'skip',
    };

    // 총자산 라인
    const traceLine = {
        x: dates, y: equities,
        type: 'scatter', mode: 'lines+markers', name: '총자산',
        line: { color: '#22d3ee', width: 2.5, shape: 'spline' },
        marker: { color: markerColors, size: 9, line: { color: '#1a1a2e', width: 2 } },
        fill: 'tonexty', fillcolor: 'rgba(34,211,238,.08)',
        hovertext: hoverTexts, hoverinfo: 'text',
    };

    // 일별 손익 바
    const traceBar = {
        x: dates, y: dailyPnl,
        type: 'bar', name: '일별 손익',
        marker: { color: dailyPnl.map(v => v >= 0 ? 'rgba(52,211,153,0.5)' : 'rgba(248,113,113,0.5)') },
        yaxis: 'y2',
        hovertemplate: '%{x}<br>일별 손익: %{y:,.0f}원<extra></extra>',
    };

    const layout = {
        paper_bgcolor: 'transparent', plot_bgcolor: 'transparent',
        margin: { t: 10, b: 40, l: 90, r: 60 },
        xaxis: {
            color: '#5a6480', gridcolor: 'rgba(99,102,241,.06)',
            tickfont: { size: 11, family: 'JetBrains Mono, monospace', color: '#5a6480' },
            showspikes: true, spikemode: 'across', spikethickness: 1,
            spikecolor: 'rgba(99,102,241,.3)', spikedash: 'dot',
        },
        yaxis: {
            color: '#5a6480', gridcolor: 'rgba(99,102,241,.06)',
            tickfont: { size: 11, family: 'JetBrains Mono, monospace', color: '#5a6480' },
            tickformat: ',.0f', range: [yMin, yMax],
            showspikes: true, spikemode: 'across', spikethickness: 1,
            spikecolor: 'rgba(99,102,241,.3)', spikedash: 'dot',
        },
        yaxis2: {
            color: '#a78bfa', overlaying: 'y', side: 'right',
            gridcolor: 'transparent',
            tickfont: { size: 10, family: 'JetBrains Mono, monospace', color: '#a78bfa' },
            tickformat: ',.0f',
        },
        showlegend: true,
        legend: { font: { color: '#8892b0', size: 11, family: 'DM Sans, sans-serif' }, orientation: 'h', y: 1.12 },
        hovermode: 'closest',
        hoverlabel: { bgcolor: '#1a1a2e', bordercolor: 'rgba(99,102,241,.4)', font: { color: '#e2e8f0', size: 12.5, family: 'DM Sans, sans-serif' }, align: 'left' },
        height: 320,
        font: { color: '#e2e8f0', family: 'DM Sans, sans-serif' },
    };

    Plotly.react(chartId, [base, traceLine, traceBar], layout, { displayModeBar: false, responsive: true });
}

// ── 벤치마크 데이터 조회 ──────────────────────────────────
async function fetchBenchmark(days) {
    try {
        const resp = await fetch('/api/benchmark?days=' + days);
        _kospiData = await resp.json();
    } catch (e) {
        _kospiData = [];
        console.debug('[벤치마크] 로드 실패:', e);
    }
}

// ── 벤치마크 비교 차트 (포트폴리오 vs KOSPI) ──────────────
function renderBenchmarkChart(snaps, kospiData) {
    const el = document.getElementById('benchmark-chart');
    const alphaEl = document.getElementById('benchmark-alpha');
    if (!el) return;

    if ((!snaps || snaps.length === 0) && (!kospiData || kospiData.length === 0)) {
        el.innerHTML = '<div style="display:flex;align-items:center;justify-content:center;height:100%;color:var(--text-muted);font-size:.85rem;">데이터 없음</div>';
        if (alphaEl) alphaEl.textContent = '';
        return;
    }

    const traces = [];

    // 포트폴리오 누적 수익률 %
    if (snaps && snaps.length > 0) {
        const baseEq = snaps[0].total_equity;
        const dates = snaps.map(s => s.date);
        const rets = snaps.map(s => baseEq > 0 ? +((s.total_equity - baseEq) / baseEq * 100).toFixed(2) : 0);
        traces.push({
            x: dates, y: rets,
            type: 'scatter', mode: 'lines+markers', name: '포트폴리오',
            line: { color: '#22d3ee', width: 2.5, shape: 'spline' },
            marker: { color: rets.map(v => v >= 0 ? '#34d399' : '#f87171'), size: 7, line: { color: '#1a1a2e', width: 1.5 } },
            fill: 'tozeroy', fillcolor: 'rgba(34,211,238,.06)',
            hovertemplate: '<b>%{x}</b><br>포트폴리오: %{y:+.2f}%<extra></extra>',
        });
    }

    // KOSPI 누적 수익률 %
    if (kospiData && kospiData.length > 0 && snaps && snaps.length > 0) {
        const startDate = snaps[0].date;
        const endDate = snaps[snaps.length - 1].date;
        const filtered = kospiData.filter(k => k.date >= startDate && k.date <= endDate);
        if (filtered.length > 0) {
            const baseK = filtered[0].close;
            traces.push({
                x: filtered.map(k => k.date),
                y: filtered.map(k => baseK > 0 ? +((k.close - baseK) / baseK * 100).toFixed(2) : 0),
                type: 'scatter', mode: 'lines', name: 'KOSPI',
                line: { color: '#a78bfa', width: 2, shape: 'spline', dash: 'dot' },
                hovertemplate: '<b>%{x}</b><br>KOSPI: %{y:+.2f}%<extra></extra>',
            });
        }
    }

    if (traces.length === 0) {
        el.innerHTML = '<div style="display:flex;align-items:center;justify-content:center;height:100%;color:var(--text-muted);font-size:.85rem;">데이터 없음</div>';
        return;
    }

    // Alpha 계산 + 헤더 표시
    if (alphaEl && snaps && snaps.length >= 2 && kospiData && kospiData.length > 0) {
        const portRet = (snaps[snaps.length - 1].total_equity - snaps[0].total_equity) / snaps[0].total_equity * 100;
        const filtered = kospiData.filter(k => k.date >= snaps[0].date && k.date <= snaps[snaps.length - 1].date);
        if (filtered.length >= 2) {
            const kospiRet = (filtered[filtered.length - 1].close - filtered[0].close) / filtered[0].close * 100;
            const alpha = portRet - kospiRet;
            alphaEl.textContent = 'Alpha ' + (alpha >= 0 ? '+' : '') + alpha.toFixed(2) + '%p';
            alphaEl.style.color = alpha >= 0 ? 'var(--accent-green)' : 'var(--accent-red)';
        } else {
            alphaEl.textContent = '';
        }
    }

    // Y축 범위
    const allY = traces.flatMap(t => t.y);
    const minY = Math.min(0, ...allY);
    const maxY = Math.max(0, ...allY);
    const pad = Math.max((maxY - minY) * 0.2, 1);

    // 0% 기준선
    const allDates = [...new Set(traces.flatMap(t => t.x))].sort();
    if (allDates.length >= 2) {
        traces.push({
            x: [allDates[0], allDates[allDates.length - 1]], y: [0, 0],
            mode: 'lines', line: { color: 'rgba(99,102,241,.25)', width: 1, dash: 'dot' },
            hoverinfo: 'skip', showlegend: false,
        });
    }

    const layout = {
        paper_bgcolor: 'transparent', plot_bgcolor: 'transparent',
        margin: { t: 10, b: 40, l: 60, r: 20 },
        showlegend: true,
        legend: { x: 0.01, y: 0.99, bgcolor: 'rgba(18,18,30,.8)', bordercolor: 'rgba(99,102,241,.2)', borderwidth: 1, font: { color: '#e2e8f0', size: 12 } },
        hovermode: 'x unified',
        hoverlabel: { bgcolor: '#1a1a2e', bordercolor: 'rgba(99,102,241,.4)', font: { color: '#e2e8f0', size: 12.5, family: 'DM Sans, sans-serif' } },
        xaxis: {
            color: '#5a6480', gridcolor: 'rgba(99,102,241,.06)',
            tickfont: { size: 11, family: 'JetBrains Mono, monospace', color: '#5a6480' },
            showspikes: true, spikemode: 'across', spikethickness: 1,
            spikecolor: 'rgba(99,102,241,.3)', spikedash: 'dot',
        },
        yaxis: {
            color: '#8892b0', gridcolor: 'rgba(99,102,241,.08)',
            tickfont: { size: 11, family: 'JetBrains Mono, monospace', color: '#8892b0' },
            tickformat: '+.1f', ticksuffix: '%',
            range: [minY - pad, maxY + pad], zeroline: false,
        },
        height: 320,
        font: { color: '#e2e8f0', family: 'DM Sans, sans-serif' },
    };

    Plotly.react('benchmark-chart', traces, layout, { displayModeBar: false, responsive: true });
}

// ── 전략별 성과 카드 ─────────────────────────────────────
function renderStrategyCards(byStrategy) {
    const container = document.getElementById('strategy-cards-container');
    if (!container) return;
    const keys = Object.keys(byStrategy);
    if (keys.length === 0) { container.innerHTML = ''; return; }

    const names = {
        momentum_breakout: '모멘텀', theme_chasing: '테마추종', gap_and_go: '갭상승',
        mean_reversion: '평균회귀', sepa_trend: 'SEPA 추세', rsi2_reversal: 'RSI2 반전',
    };
    const colors = {
        momentum_breakout: '#6366f1', theme_chasing: '#f59e0b', gap_and_go: '#22d3ee',
        mean_reversion: '#a78bfa', sepa_trend: '#34d399', rsi2_reversal: '#f87171',
    };

    const html = keys.map(k => {
        const s = byStrategy[k];
        const name = names[k] || k;
        const color = colors[k] || '#6366f1';
        const wr = (s.win_rate || 0).toFixed(1);
        const wrPct = Math.min(100, Math.max(0, s.win_rate || 0));
        const avgPct = s.avg_pnl_pct != null ? s.avg_pnl_pct : 0;
        const pnlC = s.total_pnl > 0 ? 'text-profit' : s.total_pnl < 0 ? 'text-loss' : '';
        const avgC = avgPct > 0 ? 'text-profit' : avgPct < 0 ? 'text-loss' : '';
        const losses = (s.trades || 0) - (s.wins || 0);

        return '<div class="stat-card" style="text-align:left;padding:16px 18px;">' +
            '<div style="display:flex;align-items:center;gap:8px;margin-bottom:12px;">' +
                '<div style="width:8px;height:8px;border-radius:50%;background:' + color + ';box-shadow:0 0 8px ' + color + ';flex-shrink:0;"></div>' +
                '<span style="font-size:.82rem;font-weight:600;color:#fff;">' + esc(name) + '</span>' +
                '<span class="mono" style="margin-left:auto;font-size:.7rem;color:var(--text-muted);">' + (s.trades || 0) + '건</span>' +
            '</div>' +
            '<div style="margin-bottom:10px;">' +
                '<div style="display:flex;justify-content:space-between;align-items:center;margin-bottom:4px;">' +
                    '<span style="font-size:.68rem;color:var(--text-muted);">승률</span>' +
                    '<span class="mono" style="font-size:.78rem;font-weight:600;color:' + (wrPct >= 50 ? 'var(--accent-green)' : 'var(--accent-red)') + ';">' + wr + '%</span>' +
                '</div>' +
                '<div style="height:4px;background:rgba(99,102,241,.1);border-radius:2px;overflow:hidden;">' +
                    '<div style="height:100%;width:' + wrPct + '%;background:' + (wrPct >= 50 ? 'var(--accent-green)' : 'var(--accent-red)') + ';border-radius:2px;transition:width .3s;"></div>' +
                '</div>' +
            '</div>' +
            '<div style="display:grid;grid-template-columns:1fr 1fr;gap:8px;">' +
                '<div>' +
                    '<div style="font-size:.62rem;color:var(--text-muted);margin-bottom:2px;">평균 수익률</div>' +
                    '<div class="mono ' + avgC + '" style="font-size:.82rem;font-weight:600;">' + fmtPctLocal(avgPct) + '</div>' +
                '</div>' +
                '<div>' +
                    '<div style="font-size:.62rem;color:var(--text-muted);margin-bottom:2px;">총 손익</div>' +
                    '<div class="mono ' + pnlC + '" style="font-size:.82rem;font-weight:600;">' + formatPnl(s.total_pnl) + '</div>' +
                '</div>' +
                '<div>' +
                    '<div style="font-size:.62rem;color:var(--text-muted);margin-bottom:2px;">승 / 패</div>' +
                    '<div class="mono" style="font-size:.78rem;"><span class="text-profit">' + (s.wins || 0) + '</span> / <span class="text-loss">' + losses + '</span></div>' +
                '</div>' +
            '</div>' +
        '</div>';
    }).join('');

    container.innerHTML = html;
}

// ── 전략별 차트 ────────────────────────────────────────────
function renderStrategyChart(byStrategy) {
    const keys = Object.keys(byStrategy);
    if (keys.length === 0) {
        document.getElementById('strategy-chart').innerHTML = '<div style="display:flex;align-items:center;justify-content:center;height:100%;color:var(--text-muted);font-size:.85rem;">데이터 없음</div>';
        return;
    }

    const strategyNames = {
        momentum_breakout: '모멘텀',
        theme_chasing: '테마추종',
        gap_and_go: '갭상승',
        mean_reversion: '평균회귀',
        sepa_trend: 'SEPA',
        rsi2_reversal: 'RSI2',
    };

    const labels     = keys.map(k => strategyNames[k] || k);
    const winRates   = keys.map(k => byStrategy[k].win_rate || 0);
    const tradeCounts = keys.map(k => byStrategy[k].trades || 0);

    const data = [
        {
            x: labels, y: winRates, type: 'bar', name: '승률 (%)',
            marker: { color: '#6366f1' }, yaxis: 'y',
        },
        {
            x: labels, y: tradeCounts, type: 'bar', name: '거래 수',
            marker: { color: '#a78bfa' }, yaxis: 'y2',
        },
    ];

    const layout = {
        paper_bgcolor: 'transparent', plot_bgcolor: 'transparent',
        margin: { t: 10, b: 40, l: 50, r: 50 },
        barmode: 'group',
        xaxis: { color: '#8892b0' },
        yaxis: { color: '#8892b0', gridcolor: 'rgba(99,102,241,0.08)' },
        yaxis2: { color: '#a78bfa', overlaying: 'y', side: 'right', gridcolor: 'transparent' },
        legend: { font: { color: '#8892b0', size: 11, family: 'DM Sans, sans-serif' }, orientation: 'h', y: 1.15 },
        height: 280,
        font: { color: '#e2e8f0', family: 'DM Sans, sans-serif' },
    };

    Plotly.react('strategy-chart', data, layout, { displayModeBar: false, responsive: true });
}

// ── 청산 유형별 차트 ───────────────────────────────────────
function renderExitPnlChart(byExitType) {
    const keys = Object.keys(byExitType);
    if (keys.length === 0) {
        document.getElementById('exit-pnl-chart').innerHTML = '<div style="display:flex;align-items:center;justify-content:center;height:100%;color:var(--text-muted);font-size:.85rem;">데이터 없음</div>';
        return;
    }

    const exitLabels = {
        take_profit: '익절', first_take_profit: '1차익절', second_take_profit: '2차익절',
        third_take_profit: '3차익절', stop_loss: '손절', trailing: '트레일링',
        trailing_stop: '트레일링', breakeven: '본전', stale: '횡보청산', manual: '수동',
        kis_sync: '동기화', profit_taking: '익절', time_exit: '시간청산',
    };

    const labels  = keys.map(k => exitLabels[k] || k);
    const avgPnls = keys.map(k => byExitType[k].avg_pnl_pct || 0);
    const counts  = keys.map(k => byExitType[k].trades || 0);
    const colors  = avgPnls.map(v => v >= 0 ? '#34d399' : '#f87171');

    const data = [{
        x: labels, y: avgPnls, type: 'bar',
        marker: { color: colors },
        text: counts.map(c => c + '건'),
        textposition: 'auto',
        textfont: { color: '#e2e8f0', size: 11, family: 'JetBrains Mono, monospace' },
    }];

    const layout = {
        paper_bgcolor: 'transparent', plot_bgcolor: 'transparent',
        margin: { t: 24, b: 40, l: 50, r: 10 },
        xaxis: { color: '#8892b0' },
        yaxis: { color: '#8892b0', gridcolor: 'rgba(99,102,241,0.08)', zeroline: true, zerolinecolor: 'rgba(99,102,241,0.2)' },
        annotations: [
            { xref: 'paper', yref: 'paper', x: 0, y: 1.08, xanchor: 'left', yanchor: 'bottom', text: '평균 수익률 (%)', showarrow: false, font: { color: '#8892b0', size: 10 } },
        ],
        height: 280,
        font: { color: '#e2e8f0', family: 'DM Sans, sans-serif' },
    };

    Plotly.react('exit-pnl-chart', data, layout, { displayModeBar: false, responsive: true });
}

// ── 전략 구성 히트맵 (Treemap) ────────────────────────────
function renderStrategyTreemap(byStrategy) {
    const el = document.getElementById('strategy-treemap');
    if (!el) return;
    const keys = Object.keys(byStrategy);
    if (keys.length === 0) {
        el.innerHTML = '<div style="display:flex;align-items:center;justify-content:center;height:100%;color:var(--text-muted);font-size:.85rem;">데이터 없음</div>';
        return;
    }

    const names = {
        momentum_breakout: '모멘텀', theme_chasing: '테마추종', gap_and_go: '갭상승',
        mean_reversion: '평균회귀', sepa_trend: 'SEPA', rsi2_reversal: 'RSI2',
        strategic_swing: '스윙', core_holding: '코어', earnings_drift: '어닝스', unknown: '기타',
    };

    const labels = [], parents = [], values = [], colors = [], texts = [];
    labels.push('전체'); parents.push(''); values.push(0); colors.push(0); texts.push('');

    keys.forEach(function(k) {
        var s = byStrategy[k];
        var trades = s.trades || 1;
        var avgPct = s.avg_pnl_pct || 0;
        var wr = (s.win_rate || 0).toFixed(1);
        labels.push(names[k] || k);
        parents.push('전체');
        values.push(trades);
        colors.push(avgPct);
        texts.push(trades + '건 / 승률 ' + wr + '%<br>평균 ' + (avgPct >= 0 ? '+' : '') + avgPct.toFixed(2) + '%');
    });

    var trace = {
        type: 'treemap', branchvalues: 'total',
        labels: labels, parents: parents, values: values,
        text: texts, textinfo: 'label+text',
        marker: {
            colors: colors,
            colorscale: [
                [0, '#f87171'], [0.35, '#f87171'], [0.45, '#5a6480'],
                [0.55, '#5a6480'], [0.65, '#34d399'], [1, '#34d399']
            ],
            cmid: 0, line: { width: 2, color: '#0b0b14' },
        },
        textfont: { color: '#e2e8f0', size: 13, family: 'DM Sans, sans-serif' },
        hovertemplate: '<b>%{label}</b><br>%{text}<extra></extra>',
    };

    var layout = {
        paper_bgcolor: 'transparent', plot_bgcolor: 'transparent',
        margin: { t: 10, b: 10, l: 10, r: 10 },
        height: 280,
        font: { color: '#e2e8f0', family: 'DM Sans, sans-serif' },
    };

    Plotly.react('strategy-treemap', [trace], layout, { displayModeBar: false, responsive: true });
}

// ── 전략별 테이블 ──────────────────────────────────────────
function renderStrategyTable(byStrategy) {
    const tbody = document.getElementById('strategy-table-body');
    if (!tbody) return;
    const keys = Object.keys(byStrategy);

    if (keys.length === 0) {
        tbody.textContent = '';
        const tr = document.createElement('tr');
        const td = document.createElement('td');
        td.colSpan = 6;
        td.style.cssText = 'padding:40px 0; text-align:center; color:var(--text-muted); font-size:0.85rem;';
        td.textContent = '데이터 없음';
        tr.appendChild(td);
        tbody.appendChild(tr);
        return;
    }

    const strategyNames = {
        momentum_breakout: '모멘텀 브레이크아웃',
        theme_chasing: '테마 추종',
        gap_and_go: '갭상승 추종',
        mean_reversion: '평균 회귀',
        sepa_trend: 'SEPA 추세',
        rsi2_reversal: 'RSI2 반전',
    };

    const fragment = document.createDocumentFragment();
    keys.forEach(k => {
        const s = byStrategy[k];
        const pnlC = s.total_pnl > 0 ? 'text-profit' : s.total_pnl < 0 ? 'text-loss' : '';
        const wrC  = s.win_rate >= 50 ? 'text-profit' : 'text-loss';
        const losses = (s.trades || 0) - (s.wins || 0);
        const avgPct = (s.avg_pnl_pct !== undefined && s.avg_pnl_pct !== null) ? s.avg_pnl_pct : 0;
        const avgC = avgPct > 0 ? 'text-profit' : avgPct < 0 ? 'text-loss' : '';

        const tr = document.createElement('tr');
        tr.className = 'border-b';
        tr.style.borderColor = 'rgba(99,102,241,0.08)';

        const tdName = document.createElement('td');
        tdName.className = 'py-2 pr-4 font-medium';
        tdName.style.color = '#fff';
        tdName.textContent = strategyNames[k] || k;

        const tdTrades = document.createElement('td');
        tdTrades.className = 'py-2 pr-4 text-right mono';
        tdTrades.textContent = s.trades;

        const tdWL = document.createElement('td');
        tdWL.className = 'py-2 pr-4 text-right mono col-hide-mobile';
        const winSpan = document.createElement('span');
        winSpan.className = 'text-profit';
        winSpan.textContent = s.wins;
        const lossSpan = document.createElement('span');
        lossSpan.className = 'text-loss';
        lossSpan.textContent = losses;
        tdWL.appendChild(winSpan);
        tdWL.appendChild(document.createTextNode(' / '));
        tdWL.appendChild(lossSpan);

        const tdWR = document.createElement('td');
        tdWR.className = 'py-2 pr-4 text-right mono ' + wrC;
        tdWR.textContent = s.win_rate.toFixed(1) + '%';

        const tdPnl = document.createElement('td');
        tdPnl.className = 'py-2 pr-4 text-right mono ' + pnlC;
        tdPnl.textContent = formatPnl(s.total_pnl);

        const tdAvg = document.createElement('td');
        tdAvg.className = 'py-2 text-right mono ' + avgC;
        tdAvg.textContent = formatPct(avgPct);

        tr.append(tdName, tdTrades, tdWL, tdWR, tdPnl, tdAvg);
        fragment.appendChild(tr);
    });

    tbody.textContent = '';
    tbody.appendChild(fragment);
}

// ── 일별 히스토리 테이블 ───────────────────────────────────
function renderDailyTable(snaps) {
    const tbody   = document.getElementById('daily-tbody');
    const countEl = document.getElementById('daily-table-count');
    if (!tbody) return;

    if (!snaps || snaps.length === 0) {
        tbody.innerHTML = '<tr><td colspan="9" style="padding:40px 0;text-align:center;color:var(--text-muted);font-size:.85rem;">데이터 없음</td></tr>';
        if (countEl) countEl.textContent = '0일';
        return;
    }
    if (countEl) countEl.textContent = snaps.length + '일';

    const sorted = [...snaps].reverse();
    const rows = sorted.map(s => {
        const cls    = pnlCls(s.daily_pnl);
        const hasPos = s.positions && s.positions.length > 0;
        const equity = fmtKRW(s.total_equity) + '<span style="font-size:.68rem;color:var(--text-muted);">원</span>';
        const pnl    = fmtPnlKRW(s.daily_pnl);
        const cash   = s.cash != null ? (fmtKRW(s.cash) + '<span style="font-size:.68rem;color:var(--text-muted);">원</span>') : '--';
        const wr     = s.trades_count > 0 ? s.win_rate.toFixed(0) + '%' : '--';
        const expandBtn = hasPos
            ? '<button class="expand-btn" onclick="togglePositionDetail(this,\'' + esc(s.date) + '\')">&#9654;</button>'
            : '';
        return '<tr style="border-bottom:1px solid rgba(99,102,241,.08);" data-date="' + esc(s.date) + '">' +
            '<td style="padding:8px 6px 8px 0;text-align:center;">' + expandBtn + '</td>' +
            '<td class="mono" style="padding:8px 10px 8px 0;font-size:.82rem;color:var(--text-secondary);white-space:nowrap;">' + esc(s.date) + '</td>' +
            '<td class="mono text-right" style="padding:8px 10px 8px 0;font-weight:500;">' + equity + '</td>' +
            '<td class="mono text-right ' + cls + '" style="padding:8px 10px 8px 0;">' + pnl + '</td>' +
            '<td class="mono text-right ' + cls + '" style="padding:8px 10px 8px 0;font-weight:500;">' + fmtPctLocal(s.daily_pnl_pct) + '</td>' +
            '<td class="mono text-right" style="padding:8px 10px 8px 0;color:var(--text-secondary);">' + cash + '</td>' +
            '<td class="mono text-right" style="padding:8px 10px 8px 0;color:var(--text-secondary);">' + (s.position_count ?? '--') + '</td>' +
            '<td class="mono text-right" style="padding:8px 10px 8px 0;color:var(--text-secondary);">' + (s.trades_count ?? '--') + '</td>' +
            '<td class="mono text-right" style="padding:8px 0;color:var(--text-secondary);">' + wr + '</td>' +
            '</tr>';
    }).join('');
    tbody.innerHTML = rows;
}

// ── 포지션 상세 토글 ───────────────────────────────────────
function togglePositionDetail(btn, dateStr) {
    const tr   = btn.closest('tr');
    const next = tr.nextElementSibling;
    if (next && next.dataset.detailRow) { next.remove(); btn.innerHTML = '&#9654;'; return; }

    btn.innerHTML = '&#9660;';
    const snap = _krSnaps.find(s => s.date === dateStr);

    if (snap && snap.positions && snap.positions.length > 0) {
        insertPosDetailRow(tr, snap.positions, true, 9);
        return;
    }

    // API 호출로 포지션 데이터 조회
    const row = makeLoadingDetailRow(9);
    tr.after(row);
    api('/api/equity-history/positions?date=' + dateStr).then(data => {
        row.remove();
        if (data && data.positions && data.positions.length > 0) {
            insertPosDetailRow(tr, data.positions, true, 9);
        } else {
            tr.after(makeEmptyDetailRow(9));
        }
    }).catch(() => { row.remove(); tr.after(makeEmptyDetailRow(9)); });
}

function makeLoadingDetailRow(cols) {
    const r = document.createElement('tr');
    r.dataset.detailRow = '1';
    r.innerHTML = '<td colspan="' + cols + '"><div class="position-detail-content" style="color:var(--text-muted);font-size:.82rem;">로딩 중...</div></td>';
    return r;
}
function makeEmptyDetailRow(cols) {
    const r = document.createElement('tr');
    r.dataset.detailRow = '1';
    r.innerHTML = '<td colspan="' + cols + '"><div class="position-detail-content" style="color:var(--text-muted);font-size:.82rem;">포지션 데이터 없음</div></td>';
    return r;
}

function insertPosDetailRow(afterTr, positions, isKR, cols) {
    positions.sort((a, b) => (b.pnl_pct ?? 0) - (a.pnl_pct ?? 0));
    const rows = positions.map(p => {
        const cls  = pnlClass(p.pnl);
        const avgP = isKR ? fmtKRW(p.avg_price) + '원'    : fmtUSD(p.avg_price);
        const curP = isKR ? fmtKRW(p.current_price) + '원' : fmtUSD(p.current_price);
        const mv   = isKR ? fmtKRW(p.market_value) + '원'  : fmtUSD(p.market_value);
        const pnlS = isKR ? fmtPnlKRW(p.pnl)              : fmtPnlUSD(p.pnl);
        return '<tr style="border-bottom:1px solid var(--border-subtle);">' +
            '<td style="padding:4px 10px 4px 0;font-size:.78rem;font-weight:500;color:var(--text-primary);white-space:nowrap;">' + esc(p.name || p.symbol) + ' <span style="color:var(--text-muted);font-size:.65rem;">' + esc(p.symbol) + '</span></td>' +
            '<td class="mono text-right" style="padding:4px 10px;font-size:.78rem;">' + p.quantity + '</td>' +
            '<td class="mono text-right" style="padding:4px 10px;font-size:.78rem;color:var(--text-secondary);">' + avgP + '</td>' +
            '<td class="mono text-right" style="padding:4px 10px;font-size:.78rem;">' + curP + '</td>' +
            '<td class="mono text-right" style="padding:4px 10px;font-size:.78rem;">' + mv + '</td>' +
            '<td class="mono text-right ' + cls + '" style="padding:4px 10px;font-size:.78rem;">' + pnlS + '</td>' +
            '<td class="mono text-right ' + cls + '" style="padding:4px 0;font-size:.78rem;font-weight:600;">' + fmtPctLocal(p.pnl_pct) + '</td>' +
            '</tr>';
    }).join('');

    const r = document.createElement('tr');
    r.dataset.detailRow = '1';
    r.innerHTML = '<td colspan="' + cols + '">' +
        '<div class="position-detail-content">' +
        '<table style="width:100%;text-align:left;border-collapse:collapse;">' +
        '<thead><tr style="border-bottom:1px solid var(--border-subtle);">' +
        '<th style="padding:0 10px 6px 0;font-size:.65rem;">종목</th>' +
        '<th style="padding:0 10px 6px;text-align:right;font-size:.65rem;">수량</th>' +
        '<th style="padding:0 10px 6px;text-align:right;font-size:.65rem;">평균가</th>' +
        '<th style="padding:0 10px 6px;text-align:right;font-size:.65rem;">현재가</th>' +
        '<th style="padding:0 10px 6px;text-align:right;font-size:.65rem;">평가액</th>' +
        '<th style="padding:0 10px 6px;text-align:right;font-size:.65rem;">손익</th>' +
        '<th style="padding:0 0 6px;text-align:right;font-size:.65rem;">수익률</th>' +
        '</tr></thead>' +
        '<tbody>' + rows + '</tbody>' +
        '</table></div></td>';
    afterTr.after(r);
}

// ── US 성과 ────────────────────────────────────────────────
async function loadUSPerformance() {
    try {
        const [trades, portfolio, equityData] = await Promise.all([
            fetch('/api/us/trades').then(r => r.json()).catch(() => []),
            fetch('/api/us/portfolio').then(r => r.json()).catch(() => ({})),
            api('/api/us/equity-history?days=' + (currentDays < 7 ? 9999 : currentDays)).catch(() => ({ snapshots: [] })),
        ]);

        _usSnaps = equityData.snapshots || [];

        const total   = trades.length;
        const wins    = trades.filter(t => (t.pnl || 0) > 0).length;
        const winRate = total > 0 ? (wins / total * 100).toFixed(1) : '-';
        const totalPnl = trades.reduce((s, t) => s + (t.pnl || 0), 0);
        const sign     = totalPnl >= 0 ? '+' : '';
        const pnlColor = totalPnl >= 0 ? 'var(--accent-green)' : 'var(--accent-red)';

        const set = (id, val, color) => {
            const el = document.getElementById(id);
            if (el) { el.textContent = val; if (color) el.style.color = color; }
        };
        set('us-perf-total', total + '건');
        set('us-perf-winrate', total > 0 ? winRate + '%' : '-');
        set('us-perf-pnl', sign + '$' + Math.abs(totalPnl).toFixed(2), pnlColor);
        set('us-perf-positions', (portfolio.positions_count || 0) + '개');

        // _usSnaps 갱신 후 KR/US 통합 비교 섹션 재렌더링
        renderCombined();
    } catch (e) {
        console.warn('[US성과] 로드 실패:', e);
    }
}

// ── KR/US 통합 비교 섹션 ───────────────────────────────────
function renderCombined() {
    const krSum = buildSummary(_krSnaps, true);
    const usSum = buildSummary(_usSnaps, false);

    const krDatesSet = new Set(_krSnaps.map(s => s.date));
    const commonCount = _usSnaps.filter(s => krDatesSet.has(s.date)).length;
    const labelEl = document.getElementById('combined-label');
    if (labelEl) {
        const parts = [];
        if (krSum.period_return_pct != null) parts.push('KR ' + fmtPctLocal(krSum.period_return_pct));
        if (usSum.period_return_pct != null) parts.push('US ' + fmtPctLocal(usSum.period_return_pct));
        labelEl.textContent = commonCount + '일 | ' + parts.join(' / ');
    }

    renderCombinedChart();
    renderCombinedTable();
}

function buildSummary(snaps, isKR) {
    if (!snaps || snaps.length === 0) return { period_return_pct: null, max_drawdown_pct: null, avg_daily_pnl: null };
    const first = snaps[0].total_equity;
    const last  = snaps[snaps.length - 1].total_equity;
    const periodReturn = first > 0 ? (last - first) / first * 100 : 0;
    const pnls = snaps.map(s => s.daily_pnl || 0);
    const avgPnl = pnls.reduce((a, b) => a + b, 0) / pnls.length;

    let peak = snaps[0].total_equity, maxDD = 0;
    for (const s of snaps) {
        if (s.total_equity > peak) peak = s.total_equity;
        const dd = peak > 0 ? (peak - s.total_equity) / peak * 100 : 0;
        if (dd > maxDD) maxDD = dd;
    }
    return {
        period_return_pct: +periodReturn.toFixed(2),
        max_drawdown_pct:  +(-maxDD).toFixed(2),
        avg_daily_pnl:     +avgPnl.toFixed(isKR ? 0 : 2),
    };
}

function renderCombinedChart() {
    const el = document.getElementById('combined-chart');
    if (!el) return;
    if (!_krSnaps.length && !_usSnaps.length) {
        el.innerHTML = '<div style="display:flex;align-items:center;justify-content:center;height:100%;color:var(--text-muted);font-size:.85rem;">데이터 없음</div>';
        return;
    }

    const toPct = (snaps) => {
        if (!snaps.length) return [];
        const base = snaps[0].total_equity;
        return snaps.map(s => base > 0 ? +((s.total_equity - base) / base * 100).toFixed(2) : 0);
    };
    const krPcts = toPct(_krSnaps);
    const usPcts = toPct(_usSnaps);

    const allPcts = [...krPcts, ...usPcts];
    const minP = Math.min(0, ...allPcts);
    const maxP = Math.max(0, ...allPcts);
    const pad  = Math.max((maxP - minP) * 0.2, 2);

    const traces = [];

    if (_krSnaps.length) {
        const hoverKR = _krSnaps.map((s, i) =>
            '<b>' + esc(s.date) + '</b><br>KR ' + (krPcts[i] >= 0 ? '+' : '') + krPcts[i] + '%<br>총자산 ' + fmtKRW(s.total_equity) + '원'
        );
        traces.push({
            x: _krSnaps.map(s => s.date), y: krPcts,
            name: 'KR', type: 'scatter', mode: 'lines+markers',
            line: { color: '#22d3ee', width: 2.5, shape: 'spline' },
            marker: { color: krPcts.map(v => v >= 0 ? '#34d399' : '#f87171'), size: 8, line: { color: '#1a1a2e', width: 1.5 } },
            hovertext: hoverKR, hoverinfo: 'text',
        });
    }

    if (_usSnaps.length) {
        const hoverUS = _usSnaps.map((s, i) =>
            '<b>' + esc(s.date) + '</b><br>US ' + (usPcts[i] >= 0 ? '+' : '') + usPcts[i] + '%<br>총자산 ' + fmtUSD(s.total_equity)
        );
        traces.push({
            x: _usSnaps.map(s => s.date), y: usPcts,
            name: 'US', type: 'scatter', mode: 'lines+markers',
            line: { color: '#fbbf24', width: 2.5, shape: 'spline' },
            marker: { color: usPcts.map(v => v >= 0 ? '#34d399' : '#f87171'), size: 8, line: { color: '#1a1a2e', width: 1.5 } },
            hovertext: hoverUS, hoverinfo: 'text',
        });
    }

    // 0% 기준선
    const allDates = [...new Set([..._krSnaps.map(s => s.date), ..._usSnaps.map(s => s.date)])].sort();
    if (allDates.length >= 2) {
        traces.push({
            x: [allDates[0], allDates[allDates.length - 1]], y: [0, 0],
            mode: 'lines', line: { color: 'rgba(99,102,241,.25)', width: 1, dash: 'dot' },
            hoverinfo: 'skip', showlegend: false,
        });
    }

    const layout = {
        paper_bgcolor: 'transparent', plot_bgcolor: 'transparent',
        margin: { t: 10, b: 40, l: 60, r: 20 },
        showlegend: true,
        legend: { x: 0.01, y: 0.99, bgcolor: 'rgba(18,18,30,.8)', bordercolor: 'rgba(99,102,241,.2)', borderwidth: 1, font: { color: '#e2e8f0', size: 12 } },
        hovermode: 'closest',
        hoverlabel: { bgcolor: '#1a1a2e', bordercolor: 'rgba(99,102,241,.4)', font: { color: '#e2e8f0', size: 12.5, family: 'DM Sans, sans-serif' } },
        xaxis: {
            color: '#5a6480', gridcolor: 'rgba(99,102,241,.06)',
            tickfont: { size: 11, family: 'JetBrains Mono, monospace', color: '#5a6480' },
            showspikes: true, spikemode: 'across', spikethickness: 1,
            spikecolor: 'rgba(99,102,241,.3)', spikedash: 'dot',
        },
        yaxis: {
            color: '#8892b0', gridcolor: 'rgba(99,102,241,.08)',
            tickfont: { size: 11, family: 'JetBrains Mono, monospace', color: '#8892b0' },
            tickformat: '+.2f', ticksuffix: '%',
            range: [minP - pad, maxP + pad], zeroline: false,
            showspikes: true, spikemode: 'across', spikethickness: 1,
            spikecolor: 'rgba(99,102,241,.3)', spikedash: 'dot',
        },
        height: 340,
        font: { color: '#e2e8f0', family: 'DM Sans, sans-serif' },
    };

    Plotly.react('combined-chart', traces, layout, { displayModeBar: false, responsive: true });
}

function renderCombinedTable() {
    const tbody = document.getElementById('combined-tbody');
    const cntEl = document.getElementById('combined-table-count');
    if (!tbody) return;

    const krMap = Object.fromEntries(_krSnaps.map(s => [s.date, s]));
    const usMap = Object.fromEntries(_usSnaps.map(s => [s.date, s]));
    const krDates = new Set(_krSnaps.map(s => s.date));
    const usDates = new Set(_usSnaps.map(s => s.date));
    // 교집합: 양쪽 모두 데이터가 있는 날짜만
    const allDates = [...krDates].filter(d => usDates.has(d)).sort().reverse();

    if (cntEl) cntEl.textContent = allDates.length + '일';

    if (!allDates.length) {
        tbody.innerHTML = '<tr><td colspan="7" style="padding:40px 0;text-align:center;color:var(--text-muted);">데이터 없음</td></tr>';
        return;
    }

    const rows = allDates.map(dt => {
        const kr = krMap[dt];
        const us = usMap[dt];
        const krEq  = kr ? fmtKRW(kr.total_equity) + '<span style="font-size:.68rem;color:var(--text-muted);">원</span>' : '<span style="color:var(--text-muted);">--</span>';
        const krPnl = kr ? '<span class="' + pnlCls(kr.daily_pnl) + '">' + fmtPnlKRW(kr.daily_pnl) + '</span>' : '--';
        const krPct = kr ? '<span class="' + pnlCls(kr.daily_pnl_pct) + '">' + fmtPctLocal(kr.daily_pnl_pct) + '</span>' : '--';
        const usEq  = us ? fmtUSD(us.total_equity) : '<span style="color:var(--text-muted);">--</span>';
        const usPnl = us ? '<span class="' + pnlCls(us.daily_pnl) + '">' + fmtPnlUSD(us.daily_pnl) + '</span>' : '--';
        const usPct = us ? '<span class="' + pnlCls(us.daily_pnl_pct) + '">' + fmtPctLocal(us.daily_pnl_pct) + '</span>' : '--';
        return '<tr style="border-bottom:1px solid rgba(99,102,241,.08);">' +
            '<td class="mono" style="padding:8px 10px 8px 0;font-size:.82rem;color:var(--text-secondary);white-space:nowrap;">' + esc(dt) + '</td>' +
            '<td class="mono text-right" style="padding:8px 10px;font-size:.82rem;color:rgba(34,211,238,.9);">' + krEq + '</td>' +
            '<td class="mono text-right" style="padding:8px 10px;">' + krPnl + '</td>' +
            '<td class="mono text-right" style="padding:8px 10px;">' + krPct + '</td>' +
            '<td class="mono text-right" style="padding:8px 10px;font-size:.82rem;color:rgba(245,158,11,.9);">' + usEq + '</td>' +
            '<td class="mono text-right" style="padding:8px 10px;">' + usPnl + '</td>' +
            '<td class="mono text-right" style="padding:8px 0;">' + usPct + '</td>' +
            '</tr>';
    }).join('');
    tbody.innerHTML = rows;
}

// ── 마켓 필터 ──────────────────────────────────────────────
function applyPerfMarketFilter(filter) {
    const krSec  = document.getElementById('kr-performance-section');
    const usSec  = document.getElementById('us-performance-section');
    const allSec = document.getElementById('combined-section');

    if (filter === 'all') {
        if (krSec)  krSec.style.display  = 'block';
        if (usSec)  usSec.style.display  = 'block';
        if (allSec) allSec.style.display = 'block';
        renderCombined();
    } else if (filter === 'us') {
        if (krSec)  krSec.style.display  = 'none';
        if (usSec)  usSec.style.display  = 'block';
        if (allSec) allSec.style.display = 'none';
    } else {
        if (krSec)  krSec.style.display  = 'block';
        if (usSec)  usSec.style.display  = 'none';
        if (allSec) allSec.style.display = 'none';
    }
}

// ── 자동 갱신 ──────────────────────────────────────────────
function startAutoRefresh() {
    if (_refreshTimer) clearInterval(_refreshTimer);
    _refreshTimer = setInterval(() => {
        if (_customDateFrom && _customDateTo) {
            loadPerformanceByRange(_customDateFrom, _customDateTo);
        } else {
            loadPerformance(currentDays);
        }
        const filter = MarketFilter.get();
        if (filter !== 'kr') loadUSPerformance();
    }, 30000);
}

// ── 탭 이벤트 ──────────────────────────────────────────────
document.querySelectorAll('.tab-btn[data-days]').forEach(btn => {
    btn.addEventListener('click', () => {
        loadPerformance(parseInt(btn.dataset.days));
    });
});

// ── 날짜 범위 선택 이벤트 ────────────────────────────────────
document.getElementById('perf-date-apply')?.addEventListener('click', () => {
    const from = document.getElementById('perf-date-from')?.value;
    const to = document.getElementById('perf-date-to')?.value;
    if (!from || !to) return;
    if (from > to) { alert('시작일이 종료일보다 늦습니다.'); return; }
    loadPerformanceByRange(from, to);
    const filter = MarketFilter.get();
    if (filter !== 'kr') loadUSPerformance();
});

// ── 초기화 ─────────────────────────────────────────────────
document.addEventListener('DOMContentLoaded', async () => {
    sse.connect();
    fetchFxRate();

    // KR 로드 (기본 1주)
    await loadPerformance(7);

    // 마켓 필터 바
    const filterBar = document.getElementById('market-filter-bar');
    if (filterBar) {
        MarketFilter.render(filterBar, (filter) => {
            applyPerfMarketFilter(filter);
            if (filter !== 'kr') loadUSPerformance();
        });
    }
    const initFilter = MarketFilter.get();
    applyPerfMarketFilter(initFilter);
    if (initFilter !== 'kr') loadUSPerformance();

    // 30초 자동 갱신
    startAutoRefresh();
});
