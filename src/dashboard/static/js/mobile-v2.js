/**
 * QWQ AI Trader — Mobile-First v2
 * 하단 Fixed Nav + 스티키 요약 바 + Quick KPI + Trades 카드 + Performance 탭
 * 2026-04-23 도입
 */
(function () {
  "use strict";

  // ───────────────────────────────────────────────────────
  // 유틸
  // ───────────────────────────────────────────────────────
  const $ = (sel) => document.querySelector(sel);
  const $$ = (sel) => document.querySelectorAll(sel);
  const isMobile = () => window.matchMedia("(max-width: 768px)").matches;
  const fmtKRW = (v) => {
    if (v == null || isNaN(v)) return "—";
    const n = Number(v);
    if (Math.abs(n) >= 1e8) return (n / 1e8).toFixed(1) + "억";
    if (Math.abs(n) >= 1e4) return Math.round(n / 1e4).toLocaleString() + "만";
    return Math.round(n).toLocaleString();
  };
  const fmtPct = (v, digits = 2) => {
    if (v == null || isNaN(v)) return "—";
    const sign = Number(v) > 0 ? "+" : "";
    return sign + Number(v).toFixed(digits) + "%";
  };
  const sign = (v) => (v == null || isNaN(v) ? "muted" : (Number(v) > 0 ? "pos" : (Number(v) < 0 ? "neg" : "muted")));

  // ───────────────────────────────────────────────────────
  // 페이지 식별 (body 클래스 부여)
  // ───────────────────────────────────────────────────────
  function tagBodyPage() {
    const path = location.pathname.replace(/\/$/, "") || "/";
    const map = {
      "/": "page-home",
      "/trades": "page-trades",
      "/performance": "page-performance",
      "/themes": "page-themes",
      "/evolution": "page-evolution",
      "/engine": "page-engine",
      "/principles": "page-principles",
      "/settings": "page-settings",
    };
    const cls = map[path] || "page-other";
    document.body.classList.add(cls);
    return path;
  }

  // ───────────────────────────────────────────────────────
  // 하단 Fixed Nav 렌더
  // ───────────────────────────────────────────────────────
  const BOTTOM_NAV_ITEMS = [
    { href: "/", icon: "🏠", label: "홈", match: /^\/?$/ },
    { href: "/trades", icon: "📋", label: "거래", match: /^\/trades/ },
    { href: "/performance", icon: "📊", label: "성과", match: /^\/performance/ },
    { href: "/themes", icon: "🔥", label: "테마", match: /^\/themes/ },
    { href: "/engine", icon: "⚙️", label: "엔진", match: /^\/engine/ },
  ];

  function renderBottomNav() {
    if ($(".mv2-bottom-nav")) return;
    const path = location.pathname;
    const nav = document.createElement("nav");
    nav.className = "mv2-bottom-nav";
    nav.setAttribute("aria-label", "하단 네비게이션");
    nav.innerHTML = BOTTOM_NAV_ITEMS.map((it) => {
      const active = it.match.test(path) ? "active" : "";
      return `
        <a class="mv2-bn-item ${active}" href="${it.href}" data-bn="${it.label}">
          <span class="mv2-bn-icon">${it.icon}</span>
          <span class="mv2-bn-label">${it.label}</span>
        </a>`;
    }).join("");
    document.body.appendChild(nav);
  }

  // ───────────────────────────────────────────────────────
  // 스티키 요약 바 (홈 전용)
  // ───────────────────────────────────────────────────────
  function insertStickySummary() {
    if (!document.body.classList.contains("page-home")) return;
    if ($(".mv2-sticky-summary")) return;
    const bar = document.createElement("div");
    bar.className = "mv2-sticky-summary";
    bar.innerHTML = `
      <div class="mv2-ss-item">
        <span class="mv2-ss-lbl">총자산</span>
        <span class="mv2-ss-val mv2-skel" id="mv2-ss-equity">₩ —</span>
      </div>
      <div class="mv2-ss-item">
        <span class="mv2-ss-lbl">오늘</span>
        <span class="mv2-ss-val muted mv2-skel" id="mv2-ss-daily">+0.0%</span>
      </div>
      <div class="mv2-ss-item">
        <span class="mv2-ss-lbl">포지션</span>
        <span class="mv2-ss-val muted mv2-skel" id="mv2-ss-positions">—</span>
      </div>
    `;
    // <main> 바로 위에 삽입
    const main = document.querySelector("main");
    if (main && main.parentElement) {
      main.parentElement.insertBefore(bar, main);
    } else {
      document.body.insertBefore(bar, document.body.firstChild);
    }
  }

  async function refreshHomeSticky() {
    if (!document.body.classList.contains("page-home")) return;
    try {
      const [pf, risk] = await Promise.all([
        fetch("/api/portfolio").then((r) => r.json()).catch(() => null),
        fetch("/api/risk").then((r) => r.json()).catch(() => null),
      ]);
      const equityEl = $("#mv2-ss-equity");
      const dailyEl = $("#mv2-ss-daily");
      const posEl = $("#mv2-ss-positions");

      if (equityEl && pf) {
        equityEl.textContent = "₩" + fmtKRW(pf.total_equity);
        equityEl.classList.remove("mv2-skel");
      }
      if (dailyEl && pf) {
        const pnlPct = pf.daily_pnl_pct != null
          ? pf.daily_pnl_pct
          : (pf.total_equity > 0 && pf.daily_pnl != null ? (pf.daily_pnl / pf.total_equity * 100) : 0);
        dailyEl.textContent = fmtPct(pnlPct, 2);
        dailyEl.classList.remove("mv2-skel", "pos", "neg", "muted");
        dailyEl.classList.add(sign(pnlPct));
      }
      if (posEl && risk) {
        const cur = risk.position_count ?? risk.current_positions ?? "—";
        const max = risk.config_max_positions ?? risk.max_positions ?? "—";
        posEl.textContent = `${cur}/${max}`;
        posEl.classList.remove("mv2-skel");
      }
    } catch (e) {
      console.warn("[mv2] sticky summary refresh failed", e);
    }
  }

  // ───────────────────────────────────────────────────────
  // 홈 Quick KPI 그리드 (모바일 전용, 상단)
  // ───────────────────────────────────────────────────────
  function insertHomeKPI() {
    if (!document.body.classList.contains("page-home")) return;
    if ($(".mv2-home-kpi")) return;
    const kpi = document.createElement("div");
    kpi.className = "mv2-home-kpi";
    kpi.innerHTML = `
      <div class="mv2-kpi-card">
        <span class="mv2-kpi-label">총자산</span>
        <span class="mv2-kpi-value mv2-skel" id="mv2-kpi-equity">₩—</span>
        <span class="mv2-kpi-sub" id="mv2-kpi-cash">현금 —</span>
      </div>
      <div class="mv2-kpi-card">
        <span class="mv2-kpi-label">오늘 손익</span>
        <span class="mv2-kpi-value mv2-skel" id="mv2-kpi-daily">—</span>
        <span class="mv2-kpi-sub" id="mv2-kpi-daily-pct">—</span>
      </div>
      <div class="mv2-kpi-card">
        <span class="mv2-kpi-label">일일 리스크</span>
        <span class="mv2-kpi-value mv2-skel" id="mv2-kpi-risk">—</span>
        <span class="mv2-kpi-sub" id="mv2-kpi-risk-sub">—</span>
      </div>
      <div class="mv2-kpi-card">
        <span class="mv2-kpi-label">포지션</span>
        <span class="mv2-kpi-value mv2-skel" id="mv2-kpi-pos">—</span>
        <span class="mv2-kpi-sub" id="mv2-kpi-pos-sub">—</span>
      </div>
    `;
    const main = document.querySelector("main");
    if (main) main.insertBefore(kpi, main.firstChild);
  }

  async function refreshHomeKPI() {
    if (!document.body.classList.contains("page-home")) return;
    try {
      const [pf, risk, posResp] = await Promise.all([
        fetch("/api/portfolio").then((r) => r.json()).catch(() => null),
        fetch("/api/risk").then((r) => r.json()).catch(() => null),
        fetch("/api/positions").then((r) => r.json()).catch(() => null),
      ]);
      if (pf) {
        $("#mv2-kpi-equity").textContent = "₩" + fmtKRW(pf.total_equity);
        $("#mv2-kpi-equity").classList.remove("mv2-skel");
        const cashPct = pf.total_equity > 0 ? (pf.cash / pf.total_equity * 100) : 0;
        $("#mv2-kpi-cash").textContent = `현금 ${fmtKRW(pf.cash)} (${cashPct.toFixed(0)}%)`;

        const daily = pf.daily_pnl ?? 0;
        const dailyPct = pf.daily_pnl_pct != null
          ? pf.daily_pnl_pct
          : (pf.total_equity > 0 ? (daily / pf.total_equity * 100) : 0);
        const dailyEl = $("#mv2-kpi-daily");
        dailyEl.textContent = (daily > 0 ? "+" : "") + fmtKRW(daily);
        dailyEl.classList.remove("mv2-skel", "pos", "neg");
        dailyEl.classList.add(sign(daily));
        const dPctEl = $("#mv2-kpi-daily-pct");
        dPctEl.textContent = fmtPct(dailyPct, 2);
        dPctEl.classList.remove("pos", "neg");
        dPctEl.classList.add(sign(dailyPct));
      }
      if (risk) {
        const lossPct = risk.daily_loss_pct ?? 0;
        const limit = risk.daily_loss_limit_pct ?? risk.daily_max_loss_pct ?? 5;
        const usage = limit > 0 ? Math.abs(lossPct) / Math.abs(limit) * 100 : 0;
        const r = $("#mv2-kpi-risk");
        r.textContent = `${Number(lossPct).toFixed(1)}%`;
        r.classList.remove("mv2-skel", "pos", "neg");
        r.classList.add(lossPct < 0 ? "neg" : (lossPct > 0 ? "pos" : ""));
        $("#mv2-kpi-risk-sub").textContent = `한도 ${limit}% · 사용률 ${usage.toFixed(0)}%`;

        const cur = risk.position_count ?? risk.current_positions ?? 0;
        const max = risk.config_max_positions ?? risk.max_positions ?? 8;
        const p = $("#mv2-kpi-pos");
        p.textContent = `${cur}/${max}`;
        p.classList.remove("mv2-skel");
        const consec = risk.consecutive_losses ?? 0;
        const cv = risk.cross_validator || {};
        const signals = cv.total ?? risk.signals_today ?? 0;
        $("#mv2-kpi-pos-sub").textContent = `연속손실 ${consec} · 신호 ${signals}`;
      }
    } catch (e) {
      console.warn("[mv2] home KPI refresh failed", e);
    }
  }

  // ───────────────────────────────────────────────────────
  // 홈 포지션 스와이프 카드
  // ───────────────────────────────────────────────────────
  function insertPosSwipe() {
    if (!document.body.classList.contains("page-home")) return;
    if ($(".mv2-pos-swipe")) return;
    const wrap = document.createElement("div");
    wrap.className = "mv2-pos-swipe";
    wrap.id = "mv2-pos-swipe";
    wrap.innerHTML = `<div style="color:var(--text-muted);font-size:.75rem;padding:14px;">포지션 로딩 중…</div>`;
    const kpi = $(".mv2-home-kpi");
    if (kpi && kpi.parentElement) kpi.parentElement.insertBefore(wrap, kpi.nextSibling);
  }

  async function refreshPosSwipe() {
    if (!document.body.classList.contains("page-home")) return;
    const wrap = $("#mv2-pos-swipe");
    if (!wrap) return;
    try {
      const positions = await fetch("/api/positions").then((r) => r.json()).catch(() => []);
      const list = Array.isArray(positions) ? positions : (positions.positions || []);
      if (!list.length) {
        wrap.innerHTML = `<div style="color:var(--text-muted);font-size:.75rem;padding:14px;">보유 포지션 없음</div>`;
        return;
      }
      // 2026-04-23: 수익률 내림차순 정렬 (수익 큰 종목 먼저)
      const sorted = [...list].sort((a, b) => {
        const pa = a.pnl_pct ?? a.unrealized_pnl_pct ?? 0;
        const pb = b.pnl_pct ?? b.unrealized_pnl_pct ?? 0;
        return pb - pa;
      });
      wrap.innerHTML = sorted.map((p) => {
        const pnlPct = p.pnl_pct ?? p.unrealized_pnl_pct ?? 0;
        const sName = p.name || p.symbol;
        return `
          <div class="mv2-pos-card">
            <span class="mv2-pc-sym">${sName}</span>
            <span class="mv2-pc-pnl ${sign(pnlPct)}">${fmtPct(pnlPct, 1)}</span>
            <span class="mv2-pc-sub">${p.quantity || 0}주 · ${p.strategy || "-"}</span>
          </div>`;
      }).join("");
    } catch (e) {
      console.warn("[mv2] position swipe refresh failed", e);
    }
  }

  // ───────────────────────────────────────────────────────
  // P2. Trades 카드 렌더 (모바일 전용)
  // 기존 trades.js 테이블 렌더 결과를 읽어 카드로 재렌더
  // ───────────────────────────────────────────────────────
  function insertTradesCardsContainer() {
    if (!document.body.classList.contains("page-trades")) return;
    if ($(".mv2-trades-cards")) return;
    // tbody#trades-body 기준으로 부모 카드(.card) 찾기
    const tbody = $("#trades-body");
    if (!tbody) return;
    const card = tbody.closest(".card, .card-inner");
    if (!card) return;
    const wrap = document.createElement("div");
    wrap.className = "mv2-trades-cards";
    wrap.id = "mv2-trades-cards";
    wrap.innerHTML = `<div style="color:var(--text-muted);font-size:.8rem;padding:14px;">거래 내역 로딩 중…</div>`;
    // 카드 header 아래, 테이블 위에 삽입
    const header = card.querySelector(".card-header");
    if (header && header.nextSibling) {
      card.insertBefore(wrap, header.nextSibling);
    } else {
      card.insertBefore(wrap, card.firstChild);
    }
  }

  function renderTradesCards() {
    if (!document.body.classList.contains("page-trades")) return;
    const wrap = $("#mv2-trades-cards");
    if (!wrap) return;
    // 기존 테이블 tbody에서 데이터 추출
    const rows = $$("#trades-body > tr");
    if (!rows.length) return;
    const cards = [];
    rows.forEach((tr) => {
      const tds = tr.querySelectorAll("td");
      if (tds.length < 4) return;
      // 텍스트 기반 파싱 (기존 테이블 구조 의존)
      const time = (tds[0]?.textContent || "").trim();
      // 종목 컬럼: <div>name</div><div>code</div> 구조 → 첫 div(name)만 사용, 티커 코드 생략
      const symTd = tds[1];
      let symbolText = "";
      if (symTd) {
        const nameDiv = symTd.querySelector("div:first-child");
        if (nameDiv) symbolText = (nameDiv.textContent || "").trim();
        else symbolText = (symTd.textContent || "").trim();
      }
      const type = (tds[2]?.textContent || "").trim();
      const price = (tds[3]?.textContent || "").trim();
      const qty = (tds[4]?.textContent || "").trim();
      const pnl = (tds[5]?.textContent || "").trim();
      const pnlPct = (tds[6]?.textContent || "").trim();
      const strategy = (tds[7]?.textContent || "").trim();
      const isBuy = /매수|buy|BUY/i.test(type);
      const isSell = /매도|sell|SELL/i.test(type);
      const pnlClass = pnl.includes("+") ? "pos" : (pnl.includes("-") ? "neg" : "muted");
      cards.push(`
        <div class="mv2-trade-card">
          <span class="mv2-tc-icon ${isBuy ? "buy" : (isSell ? "sell" : "")}">${isBuy ? "매수" : (isSell ? "매도" : type)}</span>
          <div class="mv2-tc-main">
            <span class="mv2-tc-sym">${symbolText}</span>
            <span class="mv2-tc-meta">
              <span>${time}</span>
              <span>${qty}주</span>
              <span>@ ${price}</span>
              ${strategy ? `<span>· ${strategy}</span>` : ""}
            </span>
          </div>
          <div>
            <div class="mv2-tc-pnl ${pnlClass}">${pnl || "—"}</div>
            <div class="mv2-tc-pnl-sub">${pnlPct || ""}</div>
          </div>
        </div>`);
    });
    wrap.innerHTML = cards.length
      ? cards.join("")
      : `<div style="color:var(--text-muted);font-size:.8rem;padding:14px;">거래 없음</div>`;
  }

  // DOM 변화 감시해서 trades 테이블이 업데이트되면 카드도 재렌더
  function watchTradesTable() {
    if (!document.body.classList.contains("page-trades")) return;
    const tbody = $("#trades-body");
    if (!tbody) return;
    const observer = new MutationObserver(() => {
      clearTimeout(watchTradesTable._t);
      watchTradesTable._t = setTimeout(renderTradesCards, 200);
    });
    observer.observe(tbody, { childList: true });
    renderTradesCards();
  }

  // ───────────────────────────────────────────────────────
  // P3. Performance 탭 (모바일 전용)
  // 기존 섹션에 .perf-section 클래스 부여하고 탭으로 전환
  // ───────────────────────────────────────────────────────
  function buildPerfTabs() {
    if (!document.body.classList.contains("page-performance")) return;
    if ($(".mv2-perf-tabs")) return;

    // 대상 섹션 식별 — 헤더 텍스트로 자동 탐지
    const sections = [];
    $$("h2, h3").forEach((h) => {
      const card = h.closest(".card, .section, div");
      if (!card) return;
      const txt = h.textContent || "";
      let tabName = null;
      if (/요약|overview|총괄/i.test(txt)) tabName = "요약";
      else if (/전략별|strategy/i.test(txt)) tabName = "전략별";
      else if (/일별|daily/i.test(txt)) tabName = "일별";
      else if (/차트|chart|그래프/i.test(txt)) tabName = "차트";
      if (tabName) {
        card.classList.add("perf-section");
        card.setAttribute("data-perf-tab", tabName);
        if (!sections.find((s) => s.tab === tabName)) {
          sections.push({ tab: tabName, el: card });
        }
      }
    });

    if (!sections.length) return; // 구조 감지 실패 시 탭 미적용

    const tabs = document.createElement("div");
    tabs.className = "mv2-perf-tabs";
    tabs.innerHTML = sections.map((s, i) =>
      `<button class="mv2-perf-tab-btn ${i === 0 ? "active" : ""}" data-perf-target="${s.tab}">${s.tab}</button>`
    ).join("");

    const main = document.querySelector("main");
    if (main && main.firstChild) main.insertBefore(tabs, main.firstChild);

    // 초기 활성화
    showPerfTab(sections[0].tab);

    tabs.addEventListener("click", (e) => {
      const btn = e.target.closest(".mv2-perf-tab-btn");
      if (!btn) return;
      tabs.querySelectorAll(".mv2-perf-tab-btn").forEach((b) => b.classList.remove("active"));
      btn.classList.add("active");
      showPerfTab(btn.dataset.perfTarget);
    });
  }

  function showPerfTab(name) {
    $$(".perf-section").forEach((el) => {
      if (el.getAttribute("data-perf-tab") === name) el.classList.add("mv2-active");
      else el.classList.remove("mv2-active");
    });
  }

  // ───────────────────────────────────────────────────────
  // P5. Skeleton 자동 해제 (데이터 로드 완료 감지)
  // ───────────────────────────────────────────────────────
  function removeSkeletons() {
    $$(".mv2-skel").forEach((el) => {
      const txt = (el.textContent || "").trim();
      if (txt && txt !== "—" && txt !== "₩—" && txt !== "+0.0%" && txt !== "") {
        el.classList.remove("mv2-skel");
      }
    });
  }

  // ───────────────────────────────────────────────────────
  // Init
  // ───────────────────────────────────────────────────────
  function init() {
    tagBodyPage();
    renderBottomNav();
    if (isMobile()) {
      insertStickySummary();
      insertHomeKPI();
      insertPosSwipe();
      insertTradesCardsContainer();
      buildPerfTabs();

      // 초기 데이터 로드
      refreshHomeSticky();
      refreshHomeKPI();
      refreshPosSwipe();

      // 주기 갱신 (10초)
      setInterval(() => {
        refreshHomeSticky();
        refreshHomeKPI();
        refreshPosSwipe();
        removeSkeletons();
      }, 10000);

      // Trades 카드 감시
      watchTradesTable();
    }
  }

  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", init);
  } else {
    init();
  }
})();
