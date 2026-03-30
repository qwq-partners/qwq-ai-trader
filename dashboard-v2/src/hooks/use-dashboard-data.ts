"use client";

import { useState, useEffect, useCallback } from "react";
import { krApi, usApi } from "@/lib/api";
import type { Portfolio, Position, Trade, Market } from "@/types/dashboard";
import { todayStr } from "@/lib/format";

// API 응답을 내부 Portfolio 타입으로 정규화
function normalizePortfolio(raw: any, positions: any[]): Portfolio | null {
  if (!raw) return null;
  return {
    cash: raw.cash ?? 0,
    total_value: raw.total_equity ?? raw.total_value ?? 0,
    positions_value: raw.total_position_value ?? raw.positions_value ?? 0,
    daily_pnl: raw.daily_pnl ?? 0,
    daily_pnl_pct: raw.daily_pnl_pct ?? (raw.total_equity > 0 ? (raw.daily_pnl / raw.total_equity * 100) : 0),
    positions_count: raw.positions_count ?? (positions ? positions.length : 0),
  };
}

export function useDashboardData(market: Market) {
  const [portfolio, setPortfolio] = useState<Portfolio | null>(null);
  const [positions, setPositions] = useState<Position[]>([]);
  const [trades, setTrades] = useState<Trade[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [lastUpdate, setLastUpdate] = useState<string>("");

  const refresh = useCallback(async () => {
    try {
      setLoading(true);
      setError(null);

      const api = market === "kr" ? krApi : usApi;
      const [rawP, pos, t] = await Promise.all([
        api.portfolio(),
        api.positions(),
        market === "kr"
          ? krApi.tradeEvents(todayStr(), "KR")
          : usApi.trades(todayStr()),
      ]);

      const posArr = (pos ?? []) as Position[];
      setPortfolio(normalizePortfolio(rawP, posArr));
      setPositions(posArr);
      setTrades((t ?? []) as Trade[]);
      setLastUpdate(new Date().toLocaleTimeString("ko-KR"));
    } catch (e: any) {
      setError(e.message || "데이터 로드 실패");
    } finally {
      setLoading(false);
    }
  }, [market]);

  useEffect(() => {
    refresh();
    const interval = setInterval(refresh, 30_000);
    return () => clearInterval(interval);
  }, [refresh]);

  return { portfolio, positions, trades, loading, error, lastUpdate, refresh };
}
