"use client";

import { useState } from "react";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { Separator } from "@/components/ui/separator";
import { Briefcase, History } from "lucide-react";
import { DashboardHeader } from "@/components/dashboard-header";
import { MetricsCards } from "@/components/metrics-cards";
import { HoldingsTable } from "@/components/holdings-table";
import { TradeLog } from "@/components/trade-log";
import { useDashboardData } from "@/hooks/use-dashboard-data";
import type { Market } from "@/types/dashboard";

export default function DashboardPage() {
  const [market, setMarket] = useState<Market>("kr");
  const { portfolio, positions, trades, loading, error, lastUpdate, refresh } =
    useDashboardData(market);

  return (
    <div className="min-h-screen bg-background text-foreground">
      <DashboardHeader
        market={market}
        onMarketChange={setMarket}
        lastUpdate={lastUpdate}
        onRefresh={refresh}
        loading={loading}
      />

      <main className="max-w-7xl mx-auto px-4 py-6 space-y-6">
        {/* 에러 표시 */}
        {error && (
          <div className="bg-red-500/10 border border-red-500/30 rounded-lg px-4 py-3 text-sm text-red-400">
            {error}
          </div>
        )}

        {/* 성과 지표 카드 */}
        <MetricsCards portfolio={portfolio} positions={positions} market={market} />

        {/* 2열 레이아웃 */}
        <div className="grid grid-cols-1 lg:grid-cols-5 gap-6">
          {/* 보유 현황 (3/5) */}
          <Card className="lg:col-span-3 bg-card/50 backdrop-blur border-border/50">
            <CardHeader className="pb-3">
              <CardTitle className="text-sm font-medium flex items-center gap-2">
                <Briefcase className="h-4 w-4 text-purple-400" />
                보유 현황
                <span className="text-xs text-muted-foreground ml-auto">
                  {positions.length}종목
                </span>
              </CardTitle>
            </CardHeader>
            <CardContent className="pt-0">
              <HoldingsTable positions={positions} market={market} />
            </CardContent>
          </Card>

          {/* 거래 로그 (2/5) */}
          <Card className="lg:col-span-2 bg-card/50 backdrop-blur border-border/50">
            <CardHeader className="pb-3">
              <CardTitle className="text-sm font-medium flex items-center gap-2">
                <History className="h-4 w-4 text-cyan-400" />
                오늘 거래
                <span className="text-xs text-muted-foreground ml-auto">
                  {trades.length}건
                </span>
              </CardTitle>
            </CardHeader>
            <CardContent className="pt-0">
              <TradeLog trades={trades} market={market} />
            </CardContent>
          </Card>
        </div>

        {/* 푸터 */}
        <Separator className="opacity-30" />
        <div className="text-center text-xs text-muted-foreground pb-4">
          QWQ AI Trader Dashboard v2 — Powered by Next.js + shadcn/ui
        </div>
      </main>
    </div>
  );
}
