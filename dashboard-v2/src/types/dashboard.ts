// QWQ AI Trader Dashboard v2 — 도메인 타입 정의

export interface Portfolio {
  cash: number;
  total_value: number;
  positions_value: number;
  daily_pnl: number;
  daily_pnl_pct: number;
  positions_count: number;
}

export interface Position {
  symbol: string;
  name: string;
  quantity: number;
  avg_price: number;
  current_price: number;
  pnl: number;
  pnl_pct: number;
  strategy: string;
  stage: string;
  market_value: number;
  entry_time: string | null;
  sector?: string;
}

export interface Trade {
  timestamp: string;
  symbol: string;
  name: string;
  side: "buy" | "sell";
  entry_price: number;
  exit_price: number;
  quantity: number;
  pnl: number;
  pnl_pct: number;
  strategy: string;
  reason: string;
  exit_type: string;
  status: string;
  market: string;
}

export interface RiskSummary {
  can_trade: boolean;
  daily_loss_pct: number;
  daily_trades: number;
  wins: number;
  losses: number;
  consecutive_losses: number;
  win_rate: number;
  total_pnl: number;
  market: string;
}

export interface CrossValidatorStats {
  total: number;
  passed: number;
  blocked: number;
  penalized: number;
}

export interface MarketRegime {
  regime: string;
  description: string;
  llm_assessment: string;
}

export type Market = "kr" | "us";
export type TabType = "dashboard" | "trades" | "ai" | "insights";
