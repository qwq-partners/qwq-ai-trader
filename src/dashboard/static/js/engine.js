/**
 * 엔진 탭 — 자가수정 에이전트 + 엔진 로그 + LLM 운영 루프
 */

// ─── 상태 ───
let logLevel = 'error,warning';
let noiseFilter = 'hide';
let autoRefreshLog = null;

// ─── 헬퍼 ───
function esc(s) {
    if (!s) return '';
    const d = document.createElement('div');
    d.textContent = String(s);
    return d.innerHTML;
}

function timeAgo(ts) {
    if (!ts) return '—';
    const d = new Date(ts.includes('T') ? ts : ts.replace(' ', 'T'));
    const diff = Math.floor((Date.now() - d.getTime()) / 1000);
    if (diff < 60) return `${diff}초 전`;
    if (diff < 3600) return `${Math.floor(diff / 60)}분 전`;
    if (diff < 86400) return `${Math.floor(diff / 3600)}시간 전`;
    return `${Math.floor(diff / 86400)}일 전`;
}

function tierBadge(tier) {
    const map = {
        'T1': '<span class="badge badge-blue">T1</span>',
        'T2': '<span class="badge badge-yellow">T2</span>',
        'T3': '<span class="badge badge-red">T3</span>',
    };
    return map[tier] || `<span class="badge">${tier}</span>`;
}

function regimeColor(regime) {
    const map = {
        'trending_bull': 'var(--acc-green)',
        'ranging': 'var(--acc-amber)',
        'trending_bear': 'var(--acc-red)',
        'turning_point': 'var(--acc-purple)',
    };
    return map[regime] || 'var(--text-muted)';
}

function regimeBadgeClass(regime) {
    const map = {
        'trending_bull': 'badge-green',
        'ranging': 'badge-yellow',
        'trending_bear': 'badge-red',
        'turning_point': 'badge-purple',
    };
    return map[regime] || 'badge-blue';
}

function assessBadgeClass(assessment) {
    return { 'good': 'badge-green', 'fair': 'badge-yellow', 'poor': 'badge-red' }[assessment] || 'badge-blue';
}

function boostLabel(val) {
    if (val > 0) return `<span style="color:var(--acc-green)">+${val}</span>`;
    if (val < 0) return `<span style="color:var(--acc-red)">${val}</span>`;
    return '<span style="color:var(--text-muted)">0</span>';
}

// ─── 헤더: 자가수정 에이전트 상태 ───
async function fetchHealerStatus() {
    try {
        const r = await fetch('/api/engine/healer/status');
        const d = await r.json();
        const dot = document.getElementById('healer-dot');
        const statusText = document.getElementById('healer-status-text');
        const fixCount = document.getElementById('healer-fix-count');
        const cooldown = document.getElementById('healer-cooldown');
        const lastFix = document.getElementById('healer-last-fix');

        if (d.service_active) {
            dot.className = 'dot dot-g';
            statusText.textContent = 'ACTIVE';
            statusText.style.color = 'var(--acc-green)';
        } else {
            dot.className = 'dot dot-r';
            statusText.textContent = 'INACTIVE';
            statusText.style.color = 'var(--acc-red)';
        }

        fixCount.textContent = `오늘 ${d.fixes_today}/${d.max_fixes_per_day}회 수정`;

        if (d.cooldown_remaining_secs > 0) {
            cooldown.textContent = `쿨다운: ${d.cooldown_remaining_secs}초`;
            cooldown.style.color = 'var(--acc-amber)';
        } else {
            cooldown.textContent = '쿨다운: 없음';
            cooldown.style.color = 'var(--text-muted)';
        }

        lastFix.textContent = d.last_fix_at ? `마지막: ${timeAgo(d.last_fix_at)}` : '마지막: —';
    } catch (e) {
        console.warn('[engine] healer status error:', e);
    }
}

// ─── 섹션①: 수정 이력 ───
async function fetchHealerHistory() {
    try {
        const r = await fetch('/api/engine/healer/history');
        const list = await r.json();
        const tbody = document.getElementById('healer-history-body');
        const empty = document.getElementById('healer-empty');

        if (!list || list.length === 0) {
            tbody.innerHTML = '';
            empty.style.display = 'block';
            return;
        }
        empty.style.display = 'none';

        tbody.innerHTML = list.slice(0, 20).map(h => {
            const ts = h.timestamp ? h.timestamp.split(' ').pop() || h.timestamp.split('T').pop()?.slice(0, 5) || '' : '';
            const commitLink = h.commit_hash
                ? `<span class="mono" style="font-size:.7rem;color:var(--acc-blue)">${h.commit_hash.slice(0, 7)}</span>`
                : '—';
            const resultIcon = h.rollback ? '⚠️' : (h.success === false ? '❌' : '✅');

            return `<tr>
                <td class="mono">${esc(ts)}</td>
                <td>${tierBadge(h.tier || 'T1')}</td>
                <td>${esc(h.error_type || h.error_key || '—')}</td>
                <td style="max-width:200px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap;">${esc(h.summary || '—')}</td>
                <td>${commitLink}</td>
                <td>${resultIcon}</td>
            </tr>`;
        }).join('');
    } catch (e) {
        console.warn('[engine] healer history error:', e);
    }
}

// ─── 섹션②: 실시간 엔진 로그 ───
async function fetchLogs() {
    try {
        const r = await fetch(`/api/engine/logs?level=${logLevel}&noise=${noiseFilter}&limit=100`);
        const d = await r.json();
        const container = document.getElementById('log-container');
        const countEl = document.getElementById('log-count');

        countEl.textContent = `${d.total}건 표시 (NOISE ${d.noise_filtered}건 필터)`;

        if (!d.logs || d.logs.length === 0) {
            container.innerHTML = '<div style="padding:20px;text-align:center;color:var(--text-muted);">로그 없음</div>';
            return;
        }

        container.innerHTML = d.logs.map(l => {
            const levelCls = l.level === 'ERROR' ? 'log-error' : l.level === 'WARNING' ? 'log-warn' : 'log-info';
            const levelBadge = l.level === 'ERROR'
                ? '<span class="log-level-badge log-level-error">ERR</span>'
                : l.level === 'WARNING'
                    ? '<span class="log-level-badge log-level-warn">WRN</span>'
                    : '<span class="log-level-badge log-level-info">INF</span>';
            return `<div class="log-line ${levelCls}">
                <span class="log-ts">${esc(l.timestamp)}</span>
                ${levelBadge}
                <span class="log-src">${esc(l.source)}</span>
                <span class="log-msg">${esc(l.message)}</span>
            </div>`;
        }).join('');

        container.scrollTop = 0;
    } catch (e) {
        console.warn('[engine] logs error:', e);
    }
}

function setLogLevel(level) {
    logLevel = level;
    document.querySelectorAll('.log-filter-btn').forEach(b => {
        b.classList.toggle('active', b.dataset.level === level);
    });
    fetchLogs();
}

function toggleNoise() {
    noiseFilter = noiseFilter === 'hide' ? 'show' : 'hide';
    const btn = document.getElementById('noise-toggle');
    btn.textContent = noiseFilter === 'hide' ? 'NOISE 숨김 ●' : 'NOISE 표시 ○';
    btn.classList.toggle('active', noiseFilter === 'hide');
    fetchLogs();
}

// ─── 섹션③: LLM 레짐 ───
async function fetchRegime() {
    try {
        const r = await fetch('/api/engine/llm-regime');
        const d = await r.json();
        const el = document.getElementById('regime-content');

        if (d.empty) {
            el.innerHTML = `<div class="empty-msg">${d.message}</div>`;
            return;
        }

        const regimeLabel = (d.regime || '').replace(/_/g, ' ').toUpperCase();
        const confidence = d.confidence ? (d.confidence * 100).toFixed(0) : '—';

        el.innerHTML = `
            <div class="mr">
                <span class="mr-lbl">레짐</span>
                <span class="badge ${regimeBadgeClass(d.regime)}">${regimeLabel}</span>
            </div>
            <div class="mr">
                <span class="mr-lbl">리드 전략</span>
                <span class="mr-val">${(d.lead_strategy || '—').toUpperCase()}</span>
            </div>
            <div class="mr">
                <span class="mr-lbl">SEPA 점수</span>
                <span class="mr-val mono">${d.sepa_min_score_today ?? '—'}</span>
            </div>
            <div class="mr">
                <span class="mr-lbl">RSI2 점수</span>
                <span class="mr-val mono">${d.rsi2_min_score_today ?? '—'}</span>
            </div>
            <div class="mr">
                <span class="mr-lbl">신뢰도</span>
                <span class="mr-val mono">${confidence}%</span>
            </div>
            <div style="margin-top:8px;">
                <div class="confidence-bar"><div class="confidence-fill" style="width:${confidence}%;background:${regimeColor(d.regime)};"></div></div>
            </div>
            ${d.reasoning ? `<div class="regime-reason">${esc(d.reasoning)}</div>` : ''}
            <div style="text-align:right;margin-top:6px;">
                <span style="font-size:.65rem;color:var(--text-muted);">${d.generated_at ? timeAgo(d.generated_at) : ''}</span>
            </div>
        `;
    } catch (e) {
        console.warn('[engine] regime error:', e);
    }
}

// ─── 섹션④: Daily Bias ───
async function fetchDailyBias() {
    try {
        const r = await fetch('/api/engine/daily-bias');
        const d = await r.json();
        const el = document.getElementById('bias-content');

        if (d.empty) {
            el.innerHTML = `<div class="empty-msg">${d.message}</div>`;
            return;
        }

        const assessLabel = { 'good': 'GOOD', 'fair': 'FAIR', 'poor': 'POOR' }[d.assessment] || d.assessment?.toUpperCase() || '—';

        el.innerHTML = `
            <div class="mr">
                <span class="mr-lbl">평가</span>
                <span class="badge ${assessBadgeClass(d.assessment)}">${assessLabel}</span>
            </div>
            <div class="mr">
                <span class="mr-lbl">SEPA boost</span>
                <span class="mr-val">${boostLabel(d.sepa_score_boost || 0)}</span>
            </div>
            <div class="mr">
                <span class="mr-lbl">RSI2 boost</span>
                <span class="mr-val">${boostLabel(d.rsi2_score_boost || 0)}</span>
            </div>
            <div class="mr">
                <span class="mr-lbl">진입 제한</span>
                <span class="mr-val">${d.avoid_entry_before ? d.avoid_entry_before + ' 이전 금지' : '—'}</span>
            </div>
            ${d.top_lesson ? `<div class="bias-lesson">💡 ${esc(d.top_lesson)}</div>` : ''}
            <div style="text-align:right;margin-top:6px;">
                <span style="font-size:.65rem;color:var(--text-muted);">${d.generated_at ? timeAgo(d.generated_at) : ''}</span>
            </div>
        `;
    } catch (e) {
        console.warn('[engine] daily-bias error:', e);
    }
}

// ─── 섹션⑤: False Negative ───
async function fetchFalseNegatives() {
    try {
        const r = await fetch('/api/engine/false-negatives');
        const d = await r.json();
        const el = document.getElementById('fn-content');

        if (!d.latest) {
            el.innerHTML = '<div class="empty-msg">FN 분석 데이터 없음</div>';
            return;
        }

        const latest = d.latest;
        const patterns = (latest.patterns || []).map(p => `<li>${esc(p)}</li>`).join('');
        const suggestions = (latest.suggestions || []).map(s => `<li>${esc(s)}</li>`).join('');

        // 미니 바차트 (CSS only)
        const history = d.history || [];
        const maxMissed = Math.max(...history.map(h => h.missed_count), 1);
        const bars = history.slice(-12).map(h => {
            const pct = (h.missed_count / maxMissed * 100).toFixed(0);
            const dateLabel = h.date ? h.date.slice(5) : '';
            return `<div class="fn-bar-col">
                <div class="fn-bar" style="height:${pct}%"></div>
                <div class="fn-bar-label">${dateLabel}</div>
            </div>`;
        }).join('');

        el.innerHTML = `
            <div class="mr">
                <span class="mr-lbl">최근 분석</span>
                <span class="mr-val">${latest.date || '—'}</span>
            </div>
            <div class="mr">
                <span class="mr-lbl">놓친 종목</span>
                <span class="mr-val mono" style="color:var(--acc-red)">${latest.missed_count}개</span>
            </div>
            ${patterns ? `<div class="fn-section"><div class="fn-section-title">공통 패턴</div><ul class="fn-list">${patterns}</ul></div>` : ''}
            ${suggestions ? `<div class="fn-section"><div class="fn-section-title">개선 제안</div><ul class="fn-list">${suggestions}</ul></div>` : ''}
            ${bars ? `<div class="fn-chart-wrap"><div class="fn-section-title">추이</div><div class="fn-chart">${bars}</div></div>` : ''}
        `;
    } catch (e) {
        console.warn('[engine] false-negatives error:', e);
    }
}

// ─── 초기화 ───
document.addEventListener('DOMContentLoaded', () => {
    // 초기 로드
    fetchHealerStatus();
    fetchHealerHistory();
    fetchLogs();
    fetchRegime();
    fetchDailyBias();
    fetchFalseNegatives();

    // 자동 폴링
    setInterval(fetchHealerStatus, 5000);
    autoRefreshLog = setInterval(fetchLogs, 30000);

    // 로그 레벨 버튼 — HTML onclick으로 처리 (중복 등록 방지)
});
