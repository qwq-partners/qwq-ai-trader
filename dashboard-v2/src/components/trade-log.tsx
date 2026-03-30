"use client";

import { Badge } from "@/components/ui/badge";
import { ArrowUpRight, ArrowDownRight } from "lucide-react";
import type { Trade, Market } from "@/types/dashboard";
import { formatTime, formatPct } from "@/lib/format";

interface Props {
  trades: Trade[];
  market: Market;
}

export function TradeLog({ trades, market }: Props) {
  if (!trades || !trades.length) {
    return (
      <div className="text-center py-8 text-muted-foreground text-sm">
        오늘 거래 내역 없음
      </div>
    );
  }

  const isKR = market === "kr";

  return (
    <div className="space-y-2">
      {trades.map((t, i) => {
        const isBuy = t.side === "buy";
        const pnl = t.pnl || 0;
        const pnlPct = t.pnl_pct || 0;
        const price = isBuy ? (t.entry_price || 0) : (t.exit_price || 0);

        return (
          <div
            key={`${t.symbol}-${t.timestamp}-${i}`}
            className="flex items-center gap-3 px-3 py-2 rounded-lg bg-muted/20 hover:bg-muted/40 transition-colors"
          >
            {/* 시간 */}
            <span className="text-xs text-muted-foreground font-mono w-12 shrink-0">
              {formatTime(t.timestamp)}
            </span>

            {/* 매수/매도 배지 */}
            <Badge
              variant={isBuy ? "default" : pnl >= 0 ? "default" : "destructive"}
              className={`text-xs w-10 justify-center ${
                isBuy
                  ? "bg-blue-500/20 text-blue-400 border-blue-500/30"
                  : pnl >= 0
                  ? "bg-green-500/20 text-green-400 border-green-500/30"
                  : "bg-red-500/20 text-red-400 border-red-500/30"
              }`}
            >
              {isBuy ? "매수" : "매도"}
            </Badge>

            {/* 종목 */}
            <div className="flex-1 min-w-0">
              <span className="font-medium text-sm">{t.symbol}</span>
              {t.name && (
                <span className="text-xs text-muted-foreground ml-1">
                  {t.name.length > 6 ? t.name.slice(0, 6) + ".." : t.name}
                </span>
              )}
            </div>

            {/* 가격 */}
            <span className="text-xs font-mono text-muted-foreground">
              {isKR ? price.toLocaleString() : "$" + price.toFixed(2)}
            </span>

            {/* 수량 */}
            <span className="text-xs text-muted-foreground w-8 text-right">
              {t.quantity}주
            </span>

            {/* 손익 */}
            {!isBuy && pnl !== 0 ? (
              <div className={`text-right w-16 ${pnl >= 0 ? "text-green-400" : "text-red-400"}`}>
                <div className="flex items-center justify-end gap-0.5">
                  {pnl >= 0 ? (
                    <ArrowUpRight className="h-3 w-3" />
                  ) : (
                    <ArrowDownRight className="h-3 w-3" />
                  )}
                  <span className="text-xs font-mono font-medium">
                    {formatPct(pnlPct)}
                  </span>
                </div>
              </div>
            ) : (
              <div className="w-16" />
            )}
          </div>
        );
      })}
    </div>
  );
}
