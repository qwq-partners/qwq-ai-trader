# qwq-ai-trader 자가수정 에이전트 (Self-Healer)

## 개요

`journalctl -u qwq-ai-trader -f` 로그를 실시간 감시하다가 심각한 오류 발생 시
Claude Code를 호출해 코드 분석·수정·재배포하는 자율 시스템.

## 오류 분류 (3 티어)

| 티어 | 처리 방식 | 예시 |
|------|----------|------|
| **T1** | 자동 수정 → 커밋 → 재시작 → 60초 검증 | SyntaxError, ImportError, AttributeError |
| **T2** | 수정 후 텔레그램 승인 요청 (5분 타임아웃) | RuntimeError, ValueError, 반복 오류 |
| **T3** | 분석만 → 텔레그램 보고 | DB 연결, 네트워크, 인프라 오류 |

- NOISE: 주말 API 오류 등 무시할 패턴은 자동 필터링
- T1이 30분 내 3회 이상 반복 → T2로 승격

## 안전장치

- 하루 최대 3회 자동 수정
- 수정 간 최소 5분 쿨다운
- 수정 후 60초 모니터링 → 동일 오류 재발 시 자동 롤백 (`git revert`)
- Claude Code 최대 실행 시간 300초
- 프로세스 락 (`/tmp/self_healer.lock`)으로 동시 실행 방지

## 설치

```bash
# systemd 서비스 등록
sudo cp scripts/self_healer/qwq-self-healer.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable qwq-self-healer
sudo systemctl start qwq-self-healer
```

## 운영 명령어

```bash
# 상태 확인
systemctl status qwq-self-healer

# 로그 확인
journalctl -u qwq-self-healer -f

# 재시작
sudo systemctl restart qwq-self-healer

# 중지
sudo systemctl stop qwq-self-healer

# 수정 이력 확인
cat ~/.cache/ai_trader/self_healer_history.json | python3 -m json.tool

# 일일 상태 확인
cat scripts/self_healer/state.json | python3 -m json.tool
```

## 텔레그램 명령어

T2 승인 요청 시:
- `/approve` 또는 `승인` — 수정 적용
- `/deny` 또는 `거부` — 수정 거부 (롤백)
- 5분 타임아웃 → 자동 거부

## 파일 구조

```
scripts/self_healer/
├── error_watcher.py      # 메인 데몬 (journalctl 감시)
├── error_classifier.py   # 오류 분류 + 컨텍스트 추출
├── healer_agent.py       # Claude Code 호출 + 결과 파싱
├── rollback.py           # 롤백 메커니즘
├── notifier.py           # 텔레그램 알림
├── patterns.yaml         # 오류 패턴 라이브러리
├── state.json            # 일일 상태 (런타임)
└── README.md             # 이 파일
```

## 패턴 추가

`patterns.yaml`에 새 패턴 추가:

```yaml
t1:  # 또는 t2, t3, noise
  - pattern: "새로운 오류 정규식"
    description: "설명"
    extract_file: true  # 스택트레이스에서 파일 경로 추출 여부
```

## 주의사항

- Claude Code `--dangerously-skip-permissions` 모드로 실행됨
- 수정 범위는 오류와 직접 관련된 코드만 (Claude에게 지시)
- 롤백 실패 시 `git reset --hard` 사용 (최후 수단)
- 봇 서비스(`qwq-ai-trader`)가 중지되면 self-healer도 중지됨 (`Requires=`)
