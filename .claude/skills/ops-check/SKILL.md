---
name: ops-check
description: 운영 서버 봇 건강 점검 — journalctl의 오류·KIS 거절(EGW00201/00215/토큰) 집계, 아침 잡·토요일 주간 블록 실행 여부, 포트폴리오를 한 번에 요약. "점검해줘", "상태 봐줘", 배포 직후 재집계, 예약 점검에 사용.
---
# 운영 점검 (ops-check)

1. `bash scripts/dev/ops_check.sh "<since>"` 실행 — 예: `"08:15"`, `"2 hours ago"`, `"2026-09-08 15:40"`. 읽기 전용.
2. 해석 기준 (2026-09 실측):
   - **원장 EGW00215**: 09:00~09:15에만 ~20건이면 정상 범위 — 코드로 해결 불가(runbook 알려진 이슈), 관측 유지. **그 외 시간대**에 나오면 원장 TR 연속 호출 코드 의심 → 경고의 `tr_id`로 추적, `utils/kis_rate_limit.LEDGER_TR_IDS` 누락 확인.
   - **게이트웨이 EGW00201**: 계측이 "최근 1초 송신 9~10건"이면 우리 상한(10/s) 도달 — 관측. 5건 이하에서 반복되면 외부 클라이언트/서버 요인.
   - **토큰 EGW00123/133 > 0**: 회전 토큰 채택 경로(`kis_kr._recover_token`) 회귀 의심.
   - **ERROR/Traceback**: 0이 정상. 종료 시 `Unclosed client session`은 기지. pykrx `Error occurred in get_*` stderr는 스크립트가 제외함.
   - **아침**: 08:20 배치 시작 / 08:30 변동성 갱신 / 08:40 수확 shadow(커서가 전 영업일로 전진) 세 줄이 모두 있어야 함. **토요일 09:30**: 승격점검·shadow-lab·수확 주간 발송 로그 3종.
   - **포트폴리오** `cash_ratio < 0.05`: 봇 매수 불가 상태(CLAUDE.md 운영 상태) — 매수 0건은 버그 아님.
3. 이상 시: 원인 파악 → 수정은 브랜치+PR(`/pr-merge`) → 배포는 `/deploy-local`.
