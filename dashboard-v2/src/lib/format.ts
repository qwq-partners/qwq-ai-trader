// 통화 및 수치 포맷팅 유틸리티

export function formatKRW(value: number): string {
  if (Math.abs(value) >= 1e8) return `${(value / 1e8).toFixed(1)}억`;
  if (Math.abs(value) >= 1e4) return `${(value / 1e4).toFixed(0)}만`;
  return value.toLocaleString("ko-KR") + "원";
}

export function formatUSD(value: number): string {
  return "$" + Math.abs(value).toFixed(2);
}

export function formatPnl(value: number, currency: "KRW" | "USD" = "KRW"): string {
  const sign = value >= 0 ? "+" : "-";
  if (currency === "USD") return `${sign}${formatUSD(value)}`;
  return `${sign}${formatKRW(Math.abs(value))}`;
}

export function formatPct(value: number): string {
  return `${value >= 0 ? "+" : ""}${value.toFixed(2)}%`;
}

export function formatTime(iso: string): string {
  if (!iso) return "-";
  return iso.substring(11, 16); // HH:MM
}

export function formatDate(iso: string): string {
  if (!iso) return "-";
  return iso.substring(0, 10); // YYYY-MM-DD
}

export function todayStr(): string {
  return new Date().toISOString().substring(0, 10);
}
