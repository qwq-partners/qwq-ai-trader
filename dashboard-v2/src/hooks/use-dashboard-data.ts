"use client";

import { useState, useEffect, useCallback } from "react";
import { krApi, usApi } from "@/lib/api";
import type { Portfolio, Position, Trade, Market } from "@/types/dashboard";
import { todayStr } from "@/lib/format";

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
      const [p, pos, t] = await Promise.all([
        api.portfolio(),
        api.positions(),
        market === "kr"
          ? krApi.tradeEvents(todayStr(), "KR")
          : usApi.trades(todayStr()),
      ]);

      if (p) setPortfolio(p as Portfolio);
      if (pos) setPositions(pos as Position[]);
      if (t) setTrades(t as Trade[]);
      setLastUpdate(new Date().toLocaleTimeString("ko-KR"));
    } catch (e: any) {
      setError(e.message || "데이터 로드 실패");
    } finally {
      setLoading(false);
    }
  }, [market]);

  useEffect(() => {
    refresh();
    const interval = setInterval(refresh, 30_000); // 30초
    return () => clearInterval(interval);
  }, [refresh]);

  return { portfolio, positions, trades, loading, error, lastUpdate, refresh };
}
