# Lightsail 안전 배포

## 개요

배포는 WSL의 최신 `main`에서 수동으로 시작한다. 실행기는 GitHub `origin/main`의 SHA를 고정해 서버에 전달하며, SSH 개인키는 로컬에만 남는다.

## 사전 점검

```bash
cd ~/projects/qwq-ai-trader
git switch main
git pull --ff-only origin main
bash scripts/deploy/deploy_lightsail.sh
```

기본 실행은 서버를 변경하지 않는다. SSH 연결, 운영 저장소의 청결 상태, 서비스, 상태 API, 배포 대상 SHA를 확인한다.

## 실제 배포

```bash
bash scripts/deploy/deploy_lightsail.sh --deploy
```

실제 배포는 다음을 순서대로 수행한다.

1. `origin/main`의 정확한 SHA 확인
2. 운영 저장소 동시 배포 잠금 및 변경 사항 검사
3. 대상 커밋 적용과 의존성 동기화
4. Python 문법, 테스트, 비밀정보 의심 패턴 검증
5. `qwq-ai-trader.service` 재시작
6. `/api/health` 응답 확인

대상 적용 이후 실패하면 직전 SHA로 되돌리고 의존성 및 서비스를 복구한다. `[긴급]` 메시지가 표시될 때만 서버 수동 점검이 필요하다.

## 로컬 설정 재정의

- `QWQ_DEPLOY_SSH_HOST`: 기본값 `ubuntu@52.79.96.24`
- `QWQ_DEPLOY_SSH_KEY`: 기본값 `~/.ssh/lightsail_qwq`

비밀키와 `.env`는 Git에 추가하지 않는다.
