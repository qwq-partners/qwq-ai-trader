// 통화 및 수치 포맷팅 유틸리티 — null/undefined 방어

function safe(value: unknown): number {
  if (value === null || value === undefined || typeof value !== "number" || isNaN(value)) return 0;
  return value;
}

export function formatKRW(value: unknown): string {
  const v = safe(value);
  if (Math.abs(v) >= 1e8) return `${(v / 1e8).toFixed(1)}억`;
  if (Math.abs(v) >= 1e4) return `${(v / 1e4).toFixed(0)}만`;
  return v.toLocaleString("ko-KR") + "원";
}

export function formatUSD(value: unknown): string {
  const v = safe(value);
  return "$" + Math.abs(v).toFixed(2);
}

export function formatPnl(value: unknown, currency: "KRW" | "USD" = "KRW"): string {
  const v = safe(value);
  const sign = v >= 0 ? "+" : "-";
  if (currency === "USD") return `${sign}${formatUSD(Math.abs(v))}`;
  return `${sign}${formatKRW(Math.abs(v))}`;
}

export function formatPct(value: unknown): string {
  const v = safe(value);
  return `${v >= 0 ? "+" : ""}${v.toFixed(2)}%`;
}

export function formatPrice(value: unknown, market: "kr" | "us"): string {
  const v = safe(value);
  if (market === "us") return "$" + v.toFixed(2);
  return v.toLocaleString("ko-KR");
}

export function formatTime(iso: string | null | undefined): string {
  if (!iso) return "-";
  return iso.substring(11, 16);
}

export function formatDate(iso: string | null | undefined): string {
  if (!iso) return "-";
  return iso.substring(0, 10);
}

export function todayStr(): string {
  return new Date().toISOString().substring(0, 10);
}
