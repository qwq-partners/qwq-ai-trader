/**
 * QWQ AI Trader - 자산 히스토리 (KR / US / 통합)
 * equity.js v5
 */

// ── 전역 상태 ──────────────────────────────────────────────
let _krSnaps = [];   // KR 스냅샷 배열
let _usSnaps = [];   // US 스냅샷 배열
let _currentTab = '';
let _fxRate = 1450;  // USD→KRW 기본 환율 (런타임 갱신)

// ── 포맷터 헬퍼 ────────────────────────────────────────────
const fmtKRW  = v => Number(v || 0).toLocaleString('ko-KR');
const fmtUSD  = v => '$' + Number(v || 0).toLocaleString('en-US', { minimumFractionDigits: 2, maximumFractionDigits: 2 });
const fmtPct  = v => (v == null ? '--' : (v >= 0 ? '+' : '') + Number(v).toFixed(2) + '%');
const fmtPnlKRW = v => (v == null ? '--' : (v >= 0 ? '+' : '') + fmtKRW(v) + '원');
const fmtPnlUSD = v => (v == null ? '--' : (v >= 0 ? '+' : '-') + '$' + Math.abs(Number(v)).toFixed(2));
const pnlCls  = v => v == null ? '' : (v >= 0 ? 'text-profit' : 'text-loss');

// ── 탭 전환 ────────────────────────────────────────────────
function switchTab(tab) {
    _currentTab = tab;
    ['kr', 'us', 'all'].forEach(t => {
        document.getElementById('section-' + t).style.display = (t === tab) ? '' : 'none';
        const el = document.getElementById('tab-' + t);
        el.className = 'mkt-tab' + (t === tab ? ' active-' + t : '');
    });

    // 탭 전환 시 해당 섹션 데이터 초기화 렌더
    if (tab === 'kr' && _krSnaps.length) {
        renderEquitySection('kr', _krSnaps, true);
    } else if (tab === 'us' && _usSnaps.length) {
        renderEquitySection('us', _usSnaps, false);
    } else if (tab === 'all' && (_krSnaps.length || _usSnaps.length)) {
        renderCombined();
    }
}

// ── 범용 자산 섹션 렌더러 ──────────────────────────────────
/**
 * @param {string} prefix   'kr' | 'us'
 * @param {Array}  snaps    snapshot array
 * @param {boolean} isKR    true=KRW, false=USD
 */
function renderEquitySection(prefix, snaps, isKR) {
    renderSummaryCards(prefix, snaps, isKR);
    renderEquityChart(prefix, snaps, isKR);
    renderEquityTable(prefix, snaps, isKR);
}

// ── 요약 카드 ──────────────────────────────────────────────
function renderSummaryCards(prefix, snaps, isKR) {
    const label = document.getElementById(prefix + '-summary-label');
    if (label) label.textContent = snaps.length > 0 ? `${snaps.length}일 데이터` : '데이터 없음';

    const summary = buildSummary(snaps, isKR);

    const retEl = document.getElementById(prefix + '-s-return');
    if (retEl) { retEl.textContent = fmtPct(summary.period_return_pct); retEl.className = 'mono ' + pnlCls(summary.period_return_pct); }

    const ddEl = document.getElementById(prefix + '-s-dd');
    if (ddEl) { ddEl.textContent = fmtPct(summary.max_drawdown_pct); ddEl.className = 'mono text-loss'; }

    const avgEl = document.getElementById(prefix + '-s-avg');
    if (avgEl) {
        const avgStr = isKR ? fmtPnlKRW(summary.avg_daily_pnl) : fmtPnlUSD(summary.avg_daily_pnl);
        avgEl.textContent = avgStr;
        avgEl.className = 'mono ' + pnlCls(summary.avg_daily_pnl);
    }

    const daysEl = document.getElementById(prefix + '-s-days');
    if (daysEl) daysEl.textContent = snaps.length + '일';
}

function buildSummary(snaps, isKR) {
    if (!snaps || snaps.length === 0) return {};
    const first = snaps[0].total_equity;
    const last  = snaps[snaps.length - 1].total_equity;
    const periodReturn = first > 0 ? (last - first) / first * 100 : 0;
    const pnls = snaps.map(s => s.daily_pnl);
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

// ── 총자산 차트 ────────────────────────────────────────────
function renderEquityChart(prefix, snaps, isKR) {
    const chartId = prefix + '-chart';
    const el = document.getElementById(chartId);
    if (!el) return;

    if (!snaps || snaps.length === 0) {
        el.innerHTML = '<div style="display:flex;align-items:center;justify-content:center;height:100%;color:var(--text-muted);font-size:.85rem;">데이터 없음</div>';
        return;
    }

    const color  = isKR ? '#22d3ee' : '#fbbf24';
    const fill   = isKR ? 'rgba(34,211,238,.08)' : 'rgba(251,191,36,.08)';
    const dates  = snaps.map(s => s.date);
    const equities = snaps.map(s => s.total_equity);

    const markerColors = snaps.map((s, i) => {
        if (i === 0) return '#6366f1';
        return s.total_equity >= snaps[i - 1].total_equity ? '#34d399' : '#f87171';
    });

    const hoverTexts = snaps.map((s, i) => {
        const prev = i > 0 ? snaps[i - 1].total_equity : s.total_equity;
        const change = s.total_equity - prev;
        const sign = change >= 0 ? '+' : '';
        const pctSign = s.daily_pnl_pct >= 0 ? '+' : '';
        if (isKR) {
            return `<b>${s.date}</b><br>총자산 <b>${fmtKRW(s.total_equity)}</b>원<br>` +
                   `변동 ${sign}${fmtKRW(Math.round(change))}원 (${pctSign}${s.daily_pnl_pct}%)`;
        } else {
            return `<b>${s.date}</b><br>총자산 <b>${fmtUSD(s.total_equity)}</b><br>` +
                   `변동 ${sign}${fmtUSD(Math.abs(change))} (${pctSign}${s.daily_pnl_pct}%)`;
        }
    });

    const minE = Math.min(...equities);
    const maxE = Math.max(...equities);
    const pad  = (maxE - minE) * 0.3 || maxE * 0.02;
    const yMin = minE - pad, yMax = maxE + pad;

    const base = { x: dates, y: dates.map(() => yMin), type: 'scatter', mode: 'lines', line: { width: 0 }, showlegend: false, hoverinfo: 'skip' };
    const trace = {
        x: dates, y: equities, type: 'scatter', mode: 'lines+markers', name: '총자산',
        line: { color, width: 2.5, shape: 'spline' },
        marker: { color: markerColors, size: 9, line: { color: '#1a1a2e', width: 2 } },
        fill: 'tonexty', fillcolor: fill,
        hovertext: hoverTexts, hoverinfo: 'text',
    };

    const layout = {
        paper_bgcolor: 'transparent', plot_bgcolor: 'transparent',
        margin: { t: 10, b: 40, l: isKR ? 90 : 70, r: 20 },
        xaxis: { color: '#5a6480', gridcolor: 'rgba(99,102,241,.06)', tickfont: { size: 11, family: 'JetBrains Mono, monospace', color: '#5a6480' }, showspikes: true, spikemode: 'across', spikethickness: 1, spikecolor: 'rgba(99,102,241,.3)', spikedash: 'dot' },
        yaxis: { color: '#5a6480', gridcolor: 'rgba(99,102,241,.06)', tickfont: { size: 11, family: 'JetBrains Mono, monospace', color: '#5a6480' }, tickformat: isKR ? ',.0f' : '$.2f', range: [yMin, yMax], showspikes: true, spikemode: 'across', spikethickness: 1, spikecolor: 'rgba(99,102,241,.3)', spikedash: 'dot' },
        showlegend: false, hovermode: 'closest',
        hoverlabel: { bgcolor: '#1a1a2e', bordercolor: 'rgba(99,102,241,.4)', font: { color: '#e2e8f0', size: 12.5, family: 'DM Sans, sans-serif' }, align: 'left' },
    };

    Plotly.react(chartId, [base, trace], layout, { displayModeBar: false, responsive: true });
}

// ── 일자별 테이블 ─────────────────────────────────────────
function renderEquityTable(prefix, snaps, isKR) {
    const tbody    = document.getElementById(prefix + '-tbody');
    const countEl  = document.getElementById(prefix + '-table-count');
    if (!tbody) return;

    if (!snaps || snaps.length === 0) {
        tbody.innerHTML = '<tr><td colspan="9" style="padding:40px 0;text-align:center;color:var(--text-muted);font-size:.85rem;">데이터 없음</td></tr>';
        if (countEl) countEl.textContent = '0일';
        return;
    }
    if (countEl) countEl.textContent = snaps.length + '일';

    const sorted = [...snaps].reverse();
    const rows = sorted.map(s => {
        const cls = pnlCls(s.daily_pnl);
        const hasPos = s.positions && s.positions.length > 0;
        const equity = isKR ? `${fmtKRW(s.total_equity)}<span style="font-size:.68rem;color:var(--text-muted);">원</span>` : fmtUSD(s.total_equity);
        const pnl    = isKR ? fmtPnlKRW(s.daily_pnl) : fmtPnlUSD(s.daily_pnl);
        const cash   = s.cash != null ? (isKR ? `${fmtKRW(s.cash)}<span style="font-size:.68rem;color:var(--text-muted);">원</span>` : fmtUSD(s.cash)) : '--';
        const wr     = s.trades_count > 0 ? s.win_rate.toFixed(0) + '%' : '--';
        const expandBtn = hasPos
            ? `<button class="expand-btn" onclick="togglePositionDetail(this,'${prefix}','${s.date}')">&#9654;</button>`
            : '';
        return `<tr style="border-bottom:1px solid rgba(99,102,241,.08);" data-date="${s.date}" data-prefix="${prefix}">
            <td style="padding:8px 6px 8px 0;text-align:center;">${expandBtn}</td>
            <td class="mono" style="padding:8px 10px 8px 0;font-size:.82rem;color:var(--text-secondary);white-space:nowrap;">${s.date}</td>
            <td class="mono text-right" style="padding:8px 10px 8px 0;font-weight:500;">${equity}</td>
            <td class="mono text-right ${cls}" style="padding:8px 10px 8px 0;">${pnl}</td>
            <td class="mono text-right ${cls}" style="padding:8px 10px 8px 0;font-weight:500;">${fmtPct(s.daily_pnl_pct)}</td>
            <td class="mono text-right" style="padding:8px 10px 8px 0;color:var(--text-secondary);">${cash}</td>
            <td class="mono text-right" style="padding:8px 10px 8px 0;color:var(--text-secondary);">${s.position_count}</td>
            <td class="mono text-right" style="padding:8px 10px 8px 0;color:var(--text-secondary);">${s.trades_count}</td>
            <td class="mono text-right" style="padding:8px 0;color:var(--text-secondary);">${wr}</td>
        </tr>`;
    }).join('');
    tbody.innerHTML = rows;
}

// ── 포지션 상세 토글 ──────────────────────────────────────
function togglePositionDetail(btn, prefix, dateStr) {
    const tr = btn.closest('tr');
    const next = tr.nextElementSibling;
    if (next && next.dataset.detailRow) { next.remove(); btn.innerHTML = '&#9654;'; return; }

    btn.innerHTML = '&#9660;';
    const snaps  = prefix === 'kr' ? _krSnaps : _usSnaps;
    const isKR   = prefix === 'kr';
    const snap   = snaps.find(s => s.date === dateStr);

    if (snap && snap.positions && snap.positions.length > 0) {
        insertPosDetailRow(tr, snap.positions, isKR, 9);
        return;
    }

    // KR: API 호출
    if (isKR) {
        const row = makeLoadingDetailRow(9);
        tr.after(row);
        api(`/api/equity-history/positions?date=${dateStr}`).then(data => {
            row.remove();
            if (data && data.positions && data.positions.length > 0) {
                insertPosDetailRow(tr, data.positions, true, 9);
            } else {
                const r = makeEmptyDetailRow(9);
                tr.after(r);
            }
        }).catch(() => { row.remove(); tr.after(makeEmptyDetailRow(9)); });
    } else {
        tr.after(makeEmptyDetailRow(9));
    }
}

function makeLoadingDetailRow(cols) {
    const r = document.createElement('tr');
    r.dataset.detailRow = '1';
    r.innerHTML = `<td colspan="${cols}"><div class="position-detail-content" style="color:var(--text-muted);font-size:.82rem;">로딩 중...</div></td>`;
    return r;
}
function makeEmptyDetailRow(cols) {
    const r = document.createElement('tr');
    r.dataset.detailRow = '1';
    r.innerHTML = `<td colspan="${cols}"><div class="position-detail-content" style="color:var(--text-muted);font-size:.82rem;">포지션 데이터 없음</div></td>`;
    return r;
}

function insertPosDetailRow(afterTr, positions, isKR, cols) {
    positions.sort((a, b) => (b.pnl_pct ?? 0) - (a.pnl_pct ?? 0));
    const rows = positions.map(p => {
        const cls = pnlClass ? pnlClass(p.pnl) : pnlCls(p.pnl);
        const avgP  = isKR ? fmtKRW(p.avg_price)    + '원' : fmtUSD(p.avg_price);
        const curP  = isKR ? fmtKRW(p.current_price) + '원' : fmtUSD(p.current_price);
        const mv    = isKR ? fmtKRW(p.market_value)  + '원' : fmtUSD(p.market_value);
        const pnlS  = isKR ? fmtPnlKRW(p.pnl) : fmtPnlUSD(p.pnl);
        return `<tr style="border-bottom:1px solid var(--border-subtle);">
            <td style="padding:4px 10px 4px 0;font-size:.78rem;font-weight:500;color:var(--text-primary);white-space:nowrap;">${esc(p.name || p.symbol)} <span style="color:var(--text-muted);font-size:.65rem;">${esc(p.symbol)}</span></td>
            <td class="mono text-right" style="padding:4px 10px;font-size:.78rem;">${p.quantity}</td>
            <td class="mono text-right" style="padding:4px 10px;font-size:.78rem;color:var(--text-secondary);">${avgP}</td>
            <td class="mono text-right" style="padding:4px 10px;font-size:.78rem;">${curP}</td>
            <td class="mono text-right" style="padding:4px 10px;font-size:.78rem;">${mv}</td>
            <td class="mono text-right ${cls}" style="padding:4px 10px;font-size:.78rem;">${pnlS}</td>
            <td class="mono text-right ${cls}" style="padding:4px 0;font-size:.78rem;font-weight:600;">${fmtPct(p.pnl_pct)}</td>
        </tr>`;
    }).join('');

    const r = document.createElement('tr');
    r.dataset.detailRow = '1';
    r.innerHTML = `<td colspan="${cols}">
      <div class="position-detail-content">
        <table style="width:100%;text-align:left;border-collapse:collapse;">
          <thead><tr style="border-bottom:1px solid var(--border-subtle);">
            <th style="padding:0 10px 6px 0;font-size:.65rem;">종목</th>
            <th style="padding:0 10px 6px;text-align:right;font-size:.65rem;">수량</th>
            <th style="padding:0 10px 6px;text-align:right;font-size:.65rem;">평균가</th>
            <th style="padding:0 10px 6px;text-align:right;font-size:.65rem;">현재가</th>
            <th style="padding:0 10px 6px;text-align:right;font-size:.65rem;">평가액</th>
            <th style="padding:0 10px 6px;text-align:right;font-size:.65rem;">손익</th>
            <th style="padding:0 0 6px;text-align:right;font-size:.65rem;">수익률</th>
          </tr></thead>
          <tbody>${rows}</tbody>
        </table>
      </div>
    </td>`;
    afterTr.after(r);
}

// ── 통합(All) 탭 ──────────────────────────────────────────
function renderCombined() {
    const krSum = buildSummary(_krSnaps, true);
    const usSum = buildSummary(_usSnaps, false);

    // 환율 표시
    document.getElementById('all-fx-rate').textContent = _fxRate.toLocaleString('ko-KR');

    // 요약 카드
    const setCard = (id, val, cls) => {
        const el = document.getElementById(id);
        if (!el) return;
        el.textContent = val;
        if (cls) el.className = 'mono ' + cls;
    };
    setCard('all-kr-return', fmtPct(krSum.period_return_pct), pnlCls(krSum.period_return_pct));
    setCard('all-us-return', fmtPct(usSum.period_return_pct), pnlCls(usSum.period_return_pct));
    setCard('all-kr-dd', fmtPct(krSum.max_drawdown_pct), 'text-loss');
    setCard('all-us-dd', fmtPct(usSum.max_drawdown_pct), 'text-loss');

    // 레이블
    const allDates = new Set([..._krSnaps.map(s => s.date), ..._usSnaps.map(s => s.date)]);
    document.getElementById('all-summary-label').textContent = `${allDates.size}일 데이터`;

    // 통합 차트: 이중 Y축
    renderCombinedChart();
    renderCombinedTable();
}

function renderCombinedChart() {
    const el = document.getElementById('all-chart');
    if (!el) return;
    if (!_krSnaps.length && !_usSnaps.length) {
        el.innerHTML = '<div style="display:flex;align-items:center;justify-content:center;height:100%;color:var(--text-muted);font-size:.85rem;">데이터 없음</div>';
        return;
    }

    // ── % 수익률 환산 (기준일 대비 변동률) ──────────────────
    const toPct = (snaps) => {
        if (!snaps.length) return [];
        const base = snaps[0].total_equity;
        return snaps.map(s => base > 0 ? +((s.total_equity - base) / base * 100).toFixed(2) : 0);
    };
    const krPcts = toPct(_krSnaps);
    const usPcts = toPct(_usSnaps);

    // 전체 범위 계산 (KR + US 합산)
    const allPcts = [...krPcts, ...usPcts];
    const minP = Math.min(0, ...allPcts);
    const maxP = Math.max(0, ...allPcts);
    const pad  = Math.max((maxP - minP) * 0.2, 2);

    const traces = [];

    if (_krSnaps.length) {
        const hoverKR = _krSnaps.map((s, i) =>
            `<b>${s.date}</b><br>🇰🇷 KR ${krPcts[i] >= 0 ? '+' : ''}${krPcts[i]}%<br>총자산 ${fmtKRW(s.total_equity)}원`
        );
        traces.push({
            x: _krSnaps.map(s => s.date),
            y: krPcts,
            name: '🇰🇷 KR',
            type: 'scatter',
            mode: 'lines+markers',
            line: { color: '#22d3ee', width: 2.5, shape: 'spline' },
            marker: { color: krPcts.map(v => v >= 0 ? '#34d399' : '#f87171'), size: 8, line: { color: '#1a1a2e', width: 1.5 } },
            hovertext: hoverKR,
            hoverinfo: 'text',
        });
    }

    if (_usSnaps.length) {
        const hoverUS = _usSnaps.map((s, i) =>
            `<b>${s.date}</b><br>🇺🇸 US ${usPcts[i] >= 0 ? '+' : ''}${usPcts[i]}%<br>총자산 ${fmtUSD(s.total_equity)}`
        );
        traces.push({
            x: _usSnaps.map(s => s.date),
            y: usPcts,
            name: '🇺🇸 US',
            type: 'scatter',
            mode: 'lines+markers',
            line: { color: '#fbbf24', width: 2.5, shape: 'spline' },
            marker: { color: usPcts.map(v => v >= 0 ? '#34d399' : '#f87171'), size: 8, line: { color: '#1a1a2e', width: 1.5 } },
            hovertext: hoverUS,
            hoverinfo: 'text',
        });
    }

    // 0% 기준선
    const allDates = [...new Set([..._krSnaps.map(s=>s.date),..._usSnaps.map(s=>s.date)])].sort();
    traces.push({
        x: [allDates[0], allDates[allDates.length-1]],
        y: [0, 0],
        mode: 'lines',
        line: { color: 'rgba(99,102,241,.25)', width: 1, dash: 'dot' },
        hoverinfo: 'skip',
        showlegend: false,
    });

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
            range: [minP - pad, maxP + pad],
            zeroline: false,
            showspikes: true, spikemode: 'across', spikethickness: 1,
            spikecolor: 'rgba(99,102,241,.3)', spikedash: 'dot',
        },
    };

    Plotly.react('all-chart', traces, layout, { displayModeBar: false, responsive: true });
}

function renderCombinedTable() {
    const tbody = document.getElementById('all-tbody');
    const cntEl = document.getElementById('all-table-count');
    if (!tbody) return;

    // 날짜 합집합 (최신순)
    const krMap = Object.fromEntries(_krSnaps.map(s => [s.date, s]));
    const usMap = Object.fromEntries(_usSnaps.map(s => [s.date, s]));
    const allDates = [...new Set([..._krSnaps.map(s => s.date), ..._usSnaps.map(s => s.date)])].sort().reverse();

    if (cntEl) cntEl.textContent = allDates.length + '일';

    if (!allDates.length) {
        tbody.innerHTML = '<tr><td colspan="7" style="padding:40px 0;text-align:center;color:var(--text-muted);">데이터 없음</td></tr>';
        return;
    }

    const rows = allDates.map(dt => {
        const kr = krMap[dt];
        const us = usMap[dt];
        const krEq  = kr ? `${fmtKRW(kr.total_equity)}<span style="font-size:.68rem;color:var(--text-muted);">원</span>` : '<span style="color:var(--text-muted);">--</span>';
        const krPnl = kr ? `<span class="${pnlCls(kr.daily_pnl)}">${fmtPnlKRW(kr.daily_pnl)}</span>` : '--';
        const krPct = kr ? `<span class="${pnlCls(kr.daily_pnl_pct)}">${fmtPct(kr.daily_pnl_pct)}</span>` : '--';
        const usEq  = us ? fmtUSD(us.total_equity) : '<span style="color:var(--text-muted);">--</span>';
        const usPnl = us ? `<span class="${pnlCls(us.daily_pnl)}">${fmtPnlUSD(us.daily_pnl)}</span>` : '--';
        const usPct = us ? `<span class="${pnlCls(us.daily_pnl_pct)}">${fmtPct(us.daily_pnl_pct)}</span>` : '--';
        return `<tr style="border-bottom:1px solid rgba(99,102,241,.08);">
            <td class="mono" style="padding:8px 10px 8px 0;font-size:.82rem;color:var(--text-secondary);white-space:nowrap;">${dt}</td>
            <td class="mono text-right" style="padding:8px 10px;font-size:.82rem;color:rgba(34,211,238,.9);">${krEq}</td>
            <td class="mono text-right" style="padding:8px 10px;">${krPnl}</td>
            <td class="mono text-right" style="padding:8px 10px;">${krPct}</td>
            <td class="mono text-right" style="padding:8px 10px;font-size:.82rem;color:rgba(245,158,11,.9);">${usEq}</td>
            <td class="mono text-right" style="padding:8px 10px;">${usPnl}</td>
            <td class="mono text-right" style="padding:8px 0;">${usPct}</td>
        </tr>`;
    }).join('');
    tbody.innerHTML = rows;
}

// ── 데이터 로드 ────────────────────────────────────────────
async function loadKR(from, to) {
    try {
        const url = from && to ? `/api/equity-history?from=${from}&to=${to}` : '/api/equity-history?days=9999';
        const data = await api(url);
        _krSnaps = data.snapshots || [];
        renderEquitySection('kr', _krSnaps, true);
        if (_currentTab === 'all') renderCombined();
    } catch (e) { console.error('[KR equity] 로드 실패:', e); }
}

async function loadUS(from, to) {
    try {
        const url = from && to ? `/api/us/equity-history?from=${from}&to=${to}` : '/api/us/equity-history?days=9999';
        const data = await api(url);
        _usSnaps = data.snapshots || [];
        renderEquitySection('us', _usSnaps, false);
        if (_currentTab === 'all') renderCombined();
    } catch (e) { console.error('[US equity] 로드 실패:', e); }
}

function krSearch() {
    const from = document.getElementById('kr-date-from').value;
    const to   = document.getElementById('kr-date-to').value;
    if (from && to) loadKR(from, to);
}
function usSearch() {
    const from = document.getElementById('us-date-from').value;
    const to   = document.getElementById('us-date-to').value;
    if (from && to) loadUS(from, to);
}

// ── 공통 헬퍼 (common.js의 pnlClass와 중복 방지) ─────────
function pnlClass(v) { return v == null ? '' : (v >= 0 ? 'text-profit' : 'text-loss'); }
function esc(s) { return String(s || '').replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;'); }

// ── FX Rate 조회 ──────────────────────────────────────────
async function fetchFxRate() {
    try {
        const resp = await fetch('https://query1.finance.yahoo.com/v8/finance/chart/USDKRW=X?interval=1d&range=1d');
        const json = await resp.json();
        const price = json?.chart?.result?.[0]?.meta?.regularMarketPrice;
        if (price && price > 0) { _fxRate = Math.round(price); }
    } catch (_) { /* 실패 시 기본값 1450 유지 */ }
}

// ── 초기화 ─────────────────────────────────────────────────
document.addEventListener('DOMContentLoaded', async () => {
    sse.connect();

    const today = new Date().toISOString().slice(0, 10);

    // 날짜 입력 기본값
    document.getElementById('kr-date-to').value  = today;
    document.getElementById('us-date-to').value  = today;

    // 기본 탭: KR
    switchTab('kr');

    // 환율 조회
    fetchFxRate();

    // KR + US 동시 로드
    await Promise.all([loadKR(), loadUS()]);

    // KR 날짜 from: oldest_date
    if (_krSnaps.length) {
        document.getElementById('kr-date-from').value = _krSnaps[0].date;
    } else {
        document.getElementById('kr-date-from').value = today;
    }
    // US 날짜 from: oldest
    if (_usSnaps.length) {
        document.getElementById('us-date-from').value = _usSnaps[0].date;
    } else {
        document.getElementById('us-date-from').value = today;
    }

    // Enter 키 지원
    ['kr-date-from','kr-date-to'].forEach(id =>
        document.getElementById(id)?.addEventListener('keydown', e => e.key === 'Enter' && krSearch()));
    ['us-date-from','us-date-to'].forEach(id =>
        document.getElementById(id)?.addEventListener('keydown', e => e.key === 'Enter' && usSearch()));

    // 30초 자동 갱신
    setInterval(async () => {
        await Promise.all([loadKR(), loadUS()]);
    }, 30000);
});
