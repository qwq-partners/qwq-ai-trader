# 운영 매뉴얼 (Runbook)

> 최종 갱신: 2026-04-15

## 봇 관리

```bash
# 재시작
echo 'user123!' | sudo -S -k systemctl restart qwq-ai-trader

# 중지
echo 'user123!' | sudo -S -k systemctl stop qwq-ai-trader

# 상태
systemctl is-active qwq-ai-trader

# 실시간 로그
journalctl -u qwq-ai-trader -f

# 최근 로그
journalctl -u qwq-ai-trader -n 50 --no-pager
```

## 코드 변경 프로토콜

1. `python3 -m py_compile <수정파일>` — 문법 검증
2. 봇 재시작 (위 명령)
3. `systemctl is-active qwq-ai-trader` — 상태 확인
4. `journalctl -u qwq-ai-trader -n 20 --no-pager` — 에러 확인

**절대 금지**: `nohup python scripts/run_trader.py` 직접 실행 (systemd 충돌)

## 긴급 전량 매도

```bash
source venv/bin/activate
python scripts/liquidate_all.py --market kr    # KR
python scripts/liquidate_all.py --market us    # US
python scripts/liquidate_all.py --force        # 확인 없이
```

## 로그 파일 위치

| 경로 | 내용 |
|------|------|
| `logs/YYYYMMDD/trader_*.log` | 메인 트레이더 로그 |
| `logs/YYYYMMDD/error_*.log` | 에러 전용 |
| `logs/YYYYMMDD/screening_*.log` | 스크리닝 상세 |
| `logs/YYYYMMDD/trades_*.log` | 거래 이벤트 |

## 캐시 파일 위치

| 경로 | 내용 |
|------|------|
| `~/.cache/ai_trader/wiki/` | Trade Wiki (교훈 축적) |
| `~/.cache/ai_trader/trade_memory/` | L1/L2/L3 거래 메모리 |
| `~/.cache/ai_trader/evolution/` | 진화 상태 |
| `~/.cache/ai_trader/journal/` | 거래 저널 + LLM 리뷰 |
| `~/.cache/ai_trader/unified_trader.pid` | PID 파일 |
| `~/.cache/ai_trader/kis_token_prod.json` | KIS 토큰 캐시 |

## 설정 파일

| 경로 | 역할 | 주의 |
|------|------|------|
| `config/default.yml` | 기본 설정 | evolved_overrides가 덮어쓸 수 있음 |
| `config/evolved_overrides.yml` | 진화 오버라이드 | **양쪽 모두 확인 필요** |
| `.env` | API 키 | 커밋 금지 |

## 트러블슈팅

### 봇 미응답
```bash
systemctl status qwq-ai-trader
journalctl -u qwq-ai-trader -n 50 --no-pager
```

### 싱글톤 락 충돌
```bash
echo 'user123!' | sudo -S -k systemctl stop qwq-ai-trader
rm -f ~/.cache/ai_trader/*.lock ~/.cache/ai_trader/*.pid
echo 'user123!' | sudo -S -k systemctl start qwq-ai-trader
```

### 매수 미실행 체크리스트
1. 가용 현금 확인 (`get_available_cash()`)
2. 일일 손실 한도 (-5% KR, -3% US)
3. 포지션 수 한도 (8 KR, 10 US)
4. 일일 거래 횟수 (10회 KR)
5. ATR=0 차단 여부 (로그에서 `ATR 누락/0 차단` 검색)
6. 크로스검증 차단 (`[크로스검증] 차단` 검색)
7. LLM 거부 (`LLM 이중검증 거부` 검색)

### RLAY 유형 매도 반복 실패
- `[US 매도 주문] {symbol} 수량 보정` 로그 확인
- 3회 연속 실패 시 자동 동기화
- 지속 시: 포트폴리오 수동 확인 → ExitManager stage 리셋

### 알려진 이슈
- **pykrx 간헐적 실패**: `Stock master: pykrx failed` → DB 폴백 자동 전환
- **MCP 모듈 없음**: `No module named 'mcp'` → 기능 영향 없음 (폴백 동작)
- **Yahoo Finance 지연**: KOSPI 데이터 2~3일 지연 → KIS 실시간 보충
