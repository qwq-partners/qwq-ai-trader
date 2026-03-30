"use client";

import { Button } from "@/components/ui/button";
import { Badge } from "@/components/ui/badge";
import { RefreshCw, Moon, Sun, Activity } from "lucide-react";
import type { Market } from "@/types/dashboard";

interface Props {
  market: Market;
  onMarketChange: (m: Market) => void;
  lastUpdate: string;
  onRefresh: () => void;
  loading: boolean;
}

export function DashboardHeader({
  market,
  onMarketChange,
  lastUpdate,
  onRefresh,
  loading,
}: Props) {
  return (
    <header className="border-b border-border/50 bg-background/80 backdrop-blur-sm sticky top-0 z-50">
      <div className="max-w-7xl mx-auto px-4 py-3">
        <div className="flex items-center justify-between">
          {/* 로고 + 상태 */}
          <div className="flex items-center gap-3">
            <div className="flex items-center gap-2">
              <Activity className="h-5 w-5 text-blue-400" />
              <h1 className="text-lg font-bold tracking-tight">
                QWQ <span className="text-muted-foreground font-normal">AI Trader</span>
              </h1>
            </div>
            <Badge variant="outline" className="text-xs text-muted-foreground">
              v2
            </Badge>
          </div>

          {/* 마켓 선택 + 컨트롤 */}
          <div className="flex items-center gap-2">
            {/* 마켓 토글 */}
            <div className="flex rounded-lg border border-border/50 overflow-hidden">
              <button
                onClick={() => onMarketChange("kr")}
                className={`px-3 py-1.5 text-xs font-medium transition-colors ${
                  market === "kr"
                    ? "bg-blue-500/20 text-blue-400"
                    : "text-muted-foreground hover:text-foreground"
                }`}
              >
                🇰🇷 KR
              </button>
              <button
                onClick={() => onMarketChange("us")}
                className={`px-3 py-1.5 text-xs font-medium transition-colors ${
                  market === "us"
                    ? "bg-emerald-500/20 text-emerald-400"
                    : "text-muted-foreground hover:text-foreground"
                }`}
              >
                🇺🇸 US
              </button>
            </div>

            {/* 최종 업데이트 */}
            <span className="text-xs text-muted-foreground hidden sm:inline">
              {lastUpdate}
            </span>

            {/* 새로고침 */}
            <Button
              variant="ghost"
              size="icon"
              onClick={onRefresh}
              disabled={loading}
              className="h-8 w-8"
            >
              <RefreshCw className={`h-4 w-4 ${loading ? "animate-spin" : ""}`} />
            </Button>
          </div>
        </div>
      </div>
    </header>
  );
}
