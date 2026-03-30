// QWQ AI Trader API 클라이언트 — 기존 엔진(포트 8080)에서 데이터 조회

// Next.js rewrites로 CORS 프록시 — 상대 경로 사용
const API_BASE = "";

async function fetchJSON<T>(path: string): Promise<T | null> {
  try {
    const res = await fetch(`${API_BASE}${path}`, { cache: "no-store" });
    if (!res.ok) return null;
    return await res.json();
  } catch {
    return null;
  }
}

// KR API
export const krApi = {
  portfolio: () => fetchJSON<any>("/api/portfolio"),
  positions: () => fetchJSON<any[]>("/api/positions"),
  risk: () => fetchJSON<any>("/api/risk"),
  trades: (date: string) => fetchJSON<any[]>(`/api/us/trades?date=${date}`),
  tradeEvents: (date: string, market: string) =>
    fetchJSON<any[]>(`/api/trade-events?date=${date}&market=${market}`),
};

// US API
export const usApi = {
  portfolio: () => fetchJSON<any>("/api/us/portfolio"),
  positions: () => fetchJSON<any[]>("/api/us/positions"),
  trades: (date: string) => fetchJSON<any[]>(`/api/us/trades?date=${date}`),
  risk: () => fetchJSON<any>("/api/us/risk"),
  statistics: (days: number) => fetchJSON<any>(`/api/us/statistics?days=${days}`),
};

// 공통
export const commonApi = {
  health: () => fetchJSON<any>("/api/health"),
  stream: () => `${API_BASE}/api/stream`, // SSE 엔드포인트
};
