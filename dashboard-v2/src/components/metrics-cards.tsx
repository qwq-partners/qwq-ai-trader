"use client";

import { Card, CardContent } from "@/components/ui/card";
import { TrendingUp, TrendingDown, Wallet, BarChart3, Target } from "lucide-react";
import type { Portfolio, Position, Market } from "@/types/dashboard";
import { formatKRW, formatUSD, formatPct } from "@/lib/format";

interface Props {
  portfolio: Portfolio | null;
  positions: Position[];
  market: Market;
}

export function MetricsCards({ portfolio, positions, market }: Props) {
  if (!portfolio) return null;

  const isKR = market === "kr";
  const fmt = isKR ? formatKRW : formatUSD;
  const dailyPnl = portfolio.daily_pnl ?? 0;
  const dailyPnlPct = portfolio.daily_pnl_pct ?? 0;
  const totalValue = portfolio.total_value ?? 0;
  const cash = portfolio.cash ?? 0;
  const posValue = portfolio.positions_value ?? 0;
  const posCount = portfolio.positions_count ?? 0;

  const pnlColor = dailyPnl >= 0 ? "text-green-400" : "text-red-400";
  const totalPnl = (positions ?? []).reduce((s, p) => s + (p.pnl ?? 0), 0);
  const winCount = (positions ?? []).filter(p => (p.pnl ?? 0) > 0).length;
  const lossCount = (positions ?? []).filter(p => (p.pnl ?? 0) < 0).length;
  const investPct = totalValue > 0 ? ((posValue / totalValue) * 100).toFixed(0) : "0";

  const cards = [
    {
      title: "총 자산",
      value: fmt(totalValue),
      sub: `현금 ${fmt(cash)}`,
      icon: Wallet,
      color: isKR ? "text-blue-400" : "text-emerald-400",
    },
    {
      title: "일일 손익",
      value: `${dailyPnl >= 0 ? "+" : ""}${fmt(dailyPnl)}`,
      sub: formatPct(dailyPnlPct),
      icon: dailyPnl >= 0 ? TrendingUp : TrendingDown,
      color: pnlColor,
    },
    {
      title: "미실현 손익",
      value: `${totalPnl >= 0 ? "+" : ""}${fmt(totalPnl)}`,
      sub: `승 ${winCount} / 패 ${lossCount}`,
      icon: BarChart3,
      color: totalPnl >= 0 ? "text-green-400" : "text-red-400",
    },
    {
      title: "포지션",
      value: `${posCount}종목`,
      sub: `투자비중 ${investPct}%`,
      icon: Target,
      color: "text-amber-400",
    },
  ];

  return (
    <div className="grid grid-cols-2 lg:grid-cols-4 gap-3">
      {cards.map((c) => (
        <Card key={c.title} className="bg-card/50 backdrop-blur border-border/50">
          <CardContent className="pt-4 pb-3 px-4">
            <div className="flex items-center justify-between mb-2">
              <span className="text-xs text-muted-foreground">{c.title}</span>
              <c.icon className={`h-4 w-4 ${c.color}`} />
            </div>
            <div className={`text-lg font-bold font-mono ${c.color}`}>{c.value}</div>
            <div className="text-xs text-muted-foreground mt-1">{c.sub}</div>
          </CardContent>
        </Card>
      ))}
    </div>
  );
}
