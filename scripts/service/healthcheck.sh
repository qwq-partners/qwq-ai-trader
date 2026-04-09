#!/bin/bash
# QWQ AI Trader 헬스체크 스크립트
# systemd timer로 5분마다 실행
# 문제 감지 시 서비스 재시작 + 로그 기록

set -uo pipefail

LOG_TAG="[헬스체크]"
SERVICE_NAME="qwq-ai-trader"
DASHBOARD_URL="http://localhost:8080"
PID_FILE="/home/user/.cache/ai_trader/unified_trader.pid"
LOG_DIR="/home/user/projects/qwq-ai-trader/logs"
HEALTHCHECK_LOG="/tmp/qwq-healthcheck.log"
MAX_MEMORY_MB=1200
MAX_RESTART_PER_HOUR=3

log() {
    echo "$(date '+%Y-%m-%d %H:%M:%S') $LOG_TAG $1" | tee -a "$HEALTHCHECK_LOG"
}

do_restart() {
    log "재시작 실행..."
    echo 'user123!' | sudo -S -k systemctl restart "$SERVICE_NAME" 2>/dev/null
    sleep 3
    if systemctl is-active --quiet "$SERVICE_NAME"; then
        log "재시작 성공"
    else
        log "재시작 실패!"
    fi
}

# --- 체크 1: 서비스 상태 ---
check_service_active() {
    if ! systemctl is-active --quiet "$SERVICE_NAME"; then
        log "FAIL: 서비스 비활성 상태"
        do_restart
        return 1
    fi
    return 0
}

# --- 체크 2: 프로세스 존재 ---
check_process() {
    if [ -f "$PID_FILE" ]; then
        local pid
        pid=$(cat "$PID_FILE")
        if ! kill -0 "$pid" 2>/dev/null; then
            log "FAIL: PID $pid 프로세스 없음 (좀비 PID 파일)"
            rm -f "$PID_FILE"
            do_restart
            return 1
        fi
    else
        if ! pgrep -f "run_trader.py" > /dev/null; then
            log "FAIL: run_trader.py 프로세스 없음"
            do_restart
            return 1
        fi
    fi
    return 0
}

# --- 체크 3: 대시보드 응답 ---
check_dashboard() {
    local http_code
    http_code=$(curl -s -o /dev/null -w "%{http_code}" --max-time 10 "$DASHBOARD_URL" 2>/dev/null || echo "000")
    if [ "$http_code" != "200" ]; then
        log "WARN: 대시보드 응답 실패 (HTTP $http_code)"
        return 1
    fi
    return 0
}

# --- 체크 4: 메모리 사용량 ---
check_memory() {
    local pid mem_mb
    pid=$(pgrep -f "run_trader.py" | head -1)
    if [ -n "$pid" ]; then
        mem_mb=$(ps -o rss= -p "$pid" 2>/dev/null | awk '{printf "%.0f", $1/1024}')
        if [ -n "$mem_mb" ] && [ "$mem_mb" -gt "$MAX_MEMORY_MB" ]; then
            log "WARN: 메모리 과다 사용 ${mem_mb}MB (한도 ${MAX_MEMORY_MB}MB)"
            return 1
        fi
    fi
    return 0
}

# --- 체크 5: WS 무한 재연결 감지 ---
check_ws_loop() {
    local ws_count
    ws_count=$(journalctl -u "$SERVICE_NAME" --since "5 min ago" --no-pager 2>/dev/null | \
        grep -c "close_code=1006" 2>/dev/null)
    ws_count=${ws_count:-0}
    ws_count=$((ws_count + 0))
    if [ "$ws_count" -ge 30 ]; then
        log "FAIL: WS 무한 재연결 감지 (${ws_count}회/5분) — 재시작"
        do_restart
        return 1
    fi
    return 0
}

# --- 체크 6: 재시작 횟수 제한 ---
check_restart_limit() {
    local recent_restarts
    recent_restarts=$(journalctl -u "$SERVICE_NAME" --since "1 hour ago" --no-pager 2>/dev/null | \
        grep -c "Started QWQ" 2>/dev/null)
    recent_restarts=${recent_restarts:-0}
    recent_restarts=$((recent_restarts + 0))
    if [ "$recent_restarts" -ge "$MAX_RESTART_PER_HOUR" ]; then
        log "CRITICAL: 1시간 내 ${recent_restarts}회 재시작 — 수동 확인 필요"
        return 1
    fi
    return 0
}

# === 메인 실행 ===
main() {
    local failures=0

    # 재시작 횟수 먼저 체크
    if ! check_restart_limit; then
        log "재시작 한도 초과 — 재시작 보류"
        exit 1
    fi

    check_service_active || ((failures++)) || true
    check_process || ((failures++)) || true
    check_dashboard || ((failures++)) || true
    check_memory || ((failures++)) || true
    check_ws_loop || ((failures++)) || true

    if [ "$failures" -eq 0 ]; then
        # 정상일 때는 로그 생략 (5분마다 OK 로그는 노이즈)
        :
    else
        log "결과: ${failures}개 체크 실패"
    fi
}

main "$@"
