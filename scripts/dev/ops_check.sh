#!/usr/bin/env bash
# 운영 점검 요약 — journalctl + 캐시 + 대시보드 API (읽기 전용, 2026-09-10)
# 사용: bash scripts/dev/ops_check.sh ["2 hours ago" | "08:15" | "2026-09-08 15:40"]
set -uo pipefail
SINCE=${1:-"2 hours ago"}
SVC=qwq-ai-trader
REPO=/home/ubuntu/projects/qwq-ai-trader
C=$HOME/.cache/ai_trader
J() { journalctl -u "$SVC" --since "$SINCE" --no-pager; }
strip() { sed -E 's/^.*python\[[0-9]+\]: //'; }

echo "=== 운영 점검 $(date '+%F %T') (since: $SINCE) ==="
echo "서비스 $(systemctl is-active "$SVC") · 기동 $(systemctl show "$SVC" -p ActiveEnterTimestamp --value) · 운영 $(git -C "$REPO" log --oneline -1) [$(git -C "$REPO" status -sb | head -1)]"

echo "--- KIS 오류 ---"
echo "HTTP 500 $(J | grep -c 'HTTP 500') · 게이트웨이 EGW00201 $(J | grep -c EGW00201) · 원장 EGW00215 $(J | grep -c EGW00215) · 토큰 EGW00123/133 $(J | grep -cE 'EGW00123|EGW00133')"
J | grep "HTTP 500" | sed -E 's/^.*HTTP 500 //; s/ EGW.*//; s/,.*//' | sort | uniq -c | sort -rn | head -5
echo "리미터 계측(EGW00201 시점 최근 1초 송신): $(J | grep 'KIS리미터\] EGW00201' | sed -E 's/^.*최근 1초 송신 ([0-9]+)건.*/\1건/' | sort | uniq -c | sort -rn | head -4 | tr '\n' ' ')"

echo "--- 오류/트레이스백 (pykrx stderr 제외) ---"
J | grep -Ei "error|traceback|exception" | grep -v "Error occurred in get_\|Unclosed client" | strip | tail -8

echo "--- 동기화/잔고 ---"
J | grep -E "\[동기화\]|잔고 조회 실패|포지션 조회 실패" | strip | tail -4

echo "--- 상위 경고 ---"
J | grep WARNING | sed -E 's/^.*WARNING *\| //' | sed -E 's/[0-9]{3,}/N/g' | sort | uniq -c | sort -rn | head -6

echo "--- 아침 잡 / 주간 블록 ---"
J | grep -E "아침 스캔 시작|변동성타게팅\] KOSPI|수확shadow\]|승격점검\]|shadow-lab\]|\[모니터링\] 완료|\[게이트분석\] 완료" | strip | cut -c1-140 | tail -8
echo "수확 커서 $(tr -d '\n ' < "$C/harvest_shadow/cursor.json" 2>/dev/null) · 변동성 $(cat "$C/vol_targeting.json" 2>/dev/null)"

echo "--- 포트폴리오 ---"
curl -s -m 5 http://localhost:8080/api/portfolio | python3 -c "import json,sys; d=json.load(sys.stdin); print({k:d.get(k) for k in ('cash','cash_ratio','total_equity','unrealized_pnl','position_count','daily_trades')})" 2>/dev/null || echo "대시보드 API 응답 없음"
echo "pending: $(curl -s -m 5 http://localhost:8080/api/orders/pending | head -c 200)"
