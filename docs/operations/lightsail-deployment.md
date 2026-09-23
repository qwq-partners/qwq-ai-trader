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

## 서버 내부 로컬 배포

`scripts/deploy/local_deploy.sh <고정 SHA>`는 서버 내부용이며 위 SSH 실행기와 별개다.
현재 요청의 배포/재시작 권한, 장외 시간, pending0, 깨끗한 운영 checkout과 롤백 SHA를
먼저 확인한다. 운영 checkout을 미리 새 SHA로 당기면 직전 롤백 기준을 잃으므로 피한다.
이 스크립트는 의존성 설치를 수행하지 않는다.

동시 배포 lock 뒤 비파괴 `sudo -n -l systemctl restart <service>` 검사를 한다.
거부되면 fetch/checkout/검증/서비스 호출 이전에 종료한다. 실제 restart도 `sudo -n`만
사용하며 평문·표준입력·대화형 인증으로 우회하지 않는다. 권한 검사는 이후의 restart
성공을 보장하지 않으므로 실제 실패는 rollback으로 처리한다. 성공 exit0, 복구 exit1,
복구 실패 exit2를 구별하고 HTTP 응답만으로 전체 거래 안전성을 단정하지 않는다.
목록 조회 자체가 금지된 호스트에서도 중단한다. exit1은 사전검사 실패에도 사용하므로
exit1만으로 복구가 실행됐다고 단정하지 않고 `[복구]` 표식과 실제 상태를 함께 본다.

`QWQ_DEPLOY_LOCK_FILE`은 격리 시험용 재정의이며 기본 운영 lock은 유지한다.
새 코드에서 인증 폴백을 없애도 과거 노출 인증정보나 Git 이력이 폐기되지는 않는다.
값을 재출력하지 않고 별도 계정 인증정보 교체 절차를 따른다.
