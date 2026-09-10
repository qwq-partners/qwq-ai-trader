---
name: deploy-local
description: 운영 서버(이 머신) 안에서 직접 배포 — 머지된 origin/main 또는 핫픽스 SHA를 pending 가드·verify·재시작·헬스체크·자동 롤백으로 반영. "배포해줘", "서버에 반영해", 재시작이 필요한 코드 변경 후 사용. WSL의 deploy_lightsail.sh 와 같은 절차의 서버 측 버전.
---
# 서버 측 배포 (deploy-local)

전제: **장중(09:00~15:30) 재시작 금지**(P0 예외). 주말·장 마감 후 언제든 가능.

1. pending 확인: `curl -s localhost:8080/api/orders/pending` → `[]` 아니면 보류·보고
2. 청결 확인: `git -C /home/ubuntu/projects/qwq-ai-trader status --porcelain` 비어 있어야 함
3. 대상 SHA: PR 머지 후면 `git rev-parse origin/main`(fetch 후); 핫픽스는 브랜치 SHA(detached → 머지 후 main 복귀 필요)
4. `bash scripts/deploy/local_deploy.sh <SHA>` — fetch → detached checkout → verify.sh → restart → /api/health 12×5초 대기, 실패 시 이전 SHA 자동 롤백
5. 시작 로그: `journalctl -u qwq-ai-trader --since -2min --no-pager | grep -E "KIS API 연결 완료|엔진 시작"` + ERROR/Traceback 0건(pykrx `Error occurred in get_` 제외)
6. 150초 뒤 `/ops-check "<재시작 시각>"`으로 재집계 (동기화 사이클 5회분)
7. main 복귀(핫픽스였을 때): `git -C /home/ubuntu/projects/qwq-ai-trader checkout main && git -C /home/ubuntu/projects/qwq-ai-trader pull --ff-only origin main` — 트리 동일이면 재시작 불필요
