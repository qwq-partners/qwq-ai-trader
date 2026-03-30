"use client";

import { Table, TableBody, TableCell, TableHead, TableHeader, TableRow } from "@/components/ui/table";
import { Badge } from "@/components/ui/badge";
import { TrendingUp, TrendingDown } from "lucide-react";
import type { Position, Market } from "@/types/dashboard";
import { formatKRW, formatUSD, formatPct } from "@/lib/format";

interface Props {
  positions: Position[];
  market: Market;
}

const strategyLabel: Record<string, string> = {
  sepa_trend: "SEPA",
  rsi2_reversal: "RSI2",
  theme_chasing: "테마",
  gap_and_go: "갭",
  momentum_breakout: "모멘텀",
  core_holding: "코어",
  earnings_drift: "어닝스",
};

const stageLabel: Record<string, string> = {
  none: "-",
  first: "1차",
  second: "2차",
  third: "3차",
  trailing: "트레일링",
};

export function HoldingsTable({ positions, market }: Props) {
  if (!positions.length) {
    return (
      <div className="text-center py-8 text-muted-foreground text-sm">
        보유 종목 없음
      </div>
    );
  }

  const isKR = market === "kr";
  const fmt = isKR ? formatKRW : formatUSD;
  const sorted = [...positions].sort((a, b) => b.pnl_pct - a.pnl_pct);

  return (
    <div className="overflow-x-auto">
      <Table>
        <TableHeader>
          <TableRow>
            <TableHead>종목</TableHead>
            <TableHead className="text-right">수량</TableHead>
            <TableHead className="text-right">평균단가</TableHead>
            <TableHead className="text-right">현재가</TableHead>
            <TableHead className="text-right">평가손익</TableHead>
            <TableHead className="hidden md:table-cell">전략</TableHead>
            <TableHead className="hidden md:table-cell">단계</TableHead>
          </TableRow>
        </TableHeader>
        <TableBody>
          {sorted.map((p) => {
            const pnl = p.pnl ?? 0;
            const pnlPct = p.pnl_pct ?? 0;
            const pnlColor = pnl >= 0 ? "text-green-400" : "text-red-400";
            const Icon = pnl >= 0 ? TrendingUp : TrendingDown;
            return (
              <TableRow key={p.symbol} className="hover:bg-muted/30">
                <TableCell>
                  <div>
                    <span className="font-medium text-foreground">{p.symbol}</span>
                    {p.name && (
                      <span className="text-xs text-muted-foreground ml-1.5">
                        {p.name.length > 8 ? p.name.slice(0, 8) + ".." : p.name}
                      </span>
                    )}
                  </div>
                </TableCell>
                <TableCell className="text-right font-mono">{p.quantity}</TableCell>
                <TableCell className="text-right font-mono text-sm">
                  {isKR ? p.avg_price.toLocaleString() : "$" + p.avg_price.toFixed(2)}
                </TableCell>
                <TableCell className="text-right font-mono text-sm">
                  {isKR ? p.current_price.toLocaleString() : "$" + p.current_price.toFixed(2)}
                </TableCell>
                <TableCell className={`text-right font-mono ${pnlColor}`}>
                  <div className="flex items-center justify-end gap-1">
                    <Icon className="h-3 w-3" />
                    <span>{formatPct(pnlPct)}</span>
                  </div>
                  <div className="text-xs opacity-70">
                    {isKR
                      ? `${pnl >= 0 ? "+" : ""}${pnl.toLocaleString()}원`
                      : `${pnl >= 0 ? "+" : "-"}$${Math.abs(pnl).toFixed(2)}`}
                  </div>
                </TableCell>
                <TableCell className="hidden md:table-cell">
                  <Badge variant="outline" className="text-xs">
                    {strategyLabel[p.strategy] || p.strategy}
                  </Badge>
                </TableCell>
                <TableCell className="hidden md:table-cell text-xs text-muted-foreground">
                  {stageLabel[p.stage] || p.stage || "-"}
                </TableCell>
              </TableRow>
            );
          })}
        </TableBody>
      </Table>
    </div>
  );
}
