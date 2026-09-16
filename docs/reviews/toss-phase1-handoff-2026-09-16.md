# 토스 Phase 1 중복 구현 인계·Codex 검토

## 결론과 기준

- #67의 오프라인 기반을 유지한다. PR #68은 보류하며 전체 병합·모듈 교체·기계적 충돌 해결을 하지 않는다.
- [후속 런타임 설계](../superpowers/specs/2026-09-16-toss-runtime-shadow-design.md)를 작성했다. **문서 확인 대기**이며 새 실행 코드/테스트는 아직 구현하지 않았다.
- #68 검토 기준: `0b978e095ab1625a2f40b3669f0d06537df3f5e5`, 비교 기준 당시 main `8c159d0`.
- 이번 인계/설계 기준: `c32de93f861694d83f94f09748ebccb12927f0cd` (#69 리뷰 스크립트 수정 포함). #68의 head는 그대로이며 열린 PR을 확인했다.
- #68의 `docs/reviews/prompts/t12-phase1-review-prompt.md`는 해당 브랜치의 과거 범위용이다. 후속 구현에서는 새 baseline/head/변경 파일/인수 범위로 다시 작성한다.

## #68 검토에서 확인한 사항

세 담당자가 인증·보안(Astra/xhigh), shadow·스케줄러(Astra/xhigh), 자료 계약·리미터(Astra/high)를 독립 대조했다. 부모도 핵심 코드를 읽고 revoked 및 통계 분모를 직접 재현했다. 실제 토큰·외부 HTTP·운영 자료가 아닌 가짜 발급기/세션·임시 파일·주입 시계의 합성 결과다.

다음 경로/행은 모두 **#68의 고정 SHA** 기준이며 현재 main의 행 번호가 아니다.

| 우선순위 | 코드 근거 | 확인한 문제와 후속 방향 |
|---|---|---|
| P1 | `client.py:236`, `kr_scheduler.py:351` | 플래그 누락에도 기본 ON, 키만 있으면 첫 조회에서 실제 발급 경로 가능. default OFF + 별도 승인/역할/기간/host 검증 |
| P1 | `token.py:265` | revoked·invalid를 같은/없는/손상 캐시에서 재발급. #67의 mint0/지속 차단 유지 |
| P1 | `token.py:187` | 발급 응답 유실·취소·게시 실패 후 재시작하면 재발급. #67 durable intent/unknown 유지 |
| P1 | `token.py:134` | umask022에서 게시 전 임시 파일0644·부모0755, symlink/다른 client 토큰 수용. #67 보안 저장소 유지. 운영에서 타 사용자가 실제 열람했다는 주장은 아님 |
| P1 | `client.py:180`, `token.py:85` | 임의 HTTP origin·계좌/변형 경로가 전송 함수에 도달, redirect 기본 허용. #67 조회 경계 보존 + 별도 제한된 OAuth |
| P1 | `client.py:214`, `toss_parity.py:98` | 오류 응답 합성 비밀이 실제 client→정규화→관측 경로의 로그/임시 JSONL에 남음. 허용된 오류 코드만 기록 |
| P1 | `toss_parity.py:120,135,298` | 양쪽 시각 None/partial 또는 노후 자료도 정상 p95 표본. 유효성·시각·시장/통화 gate와 제외 분모 |
| P2 | `toss_parity.py:53,98,142` | 취소는 attempt0, 쓰기 실패도 compared1, 재실행 중복. 지속 attempt→terminal 및 재시작 계약 |
| P2 | `kr_scheduler.py:7737` | 가격·캘린더 전량 실패에도 last_success 갱신. 지속 완료/공급자 성공/유효 비교 분리 |
| P2 | `kr_scheduler.py:7709`, `toss_parity.py:239` | CLOSED면 캘린더 대조 자체 누락, 빈/전일/불완전 응답을 정상 휴장/무차이로 처리. 별도 일일 작업과 엄격한 schema |
| P2 | `client.py:149,170`, `rate_limit.py:58,97,112` | 전체 deadline 부재·3회 HTTP·Retry-After3600을30으로 축소·그룹 hold/한도 하향 경합·동시 half-open probe2. 기존 공통 예산/제한기 보존 |
| P2 | `market_data.py:30,45,94,133` | NaN/bool/미요청 종목/미래 시각 수용, 일봉 중복 덮어쓰기·후속 페이지/손상 청크가 앞 성공분 유실. 기존 정규화/부분 결과 계약 보존 |

추가 재현: `expires_in=600`의 짧은 수명 보정은 객체 내부에만 남아 새 객체 3개에서 발급3회였다. #67의 역할/최소 간격 계약을 유지한다. `float(None)`은 기존 try/except로 처리되어 독립 결함에서 제외했다. 실제 API에 위 입력을 보내거나 보안 침해를 일으킨 결과가 아니다.

## 후속 설계의 독립 검토

인증 조립 계약은 Astra/high, 관측/스케줄러 접점은 Terra/high가 병렬로 읽기 전용 대조했고 부모가 설계를 작성했다. 별도 Astra/xhigh가 전체 문서를 독립 검토해 차단2건(승인 파일 I/O의 거래 loop 격리, 실제 GET 응답 크기 제한)과 비차단1건(snapshot 게시 후 attempt 미게시 중단)을 지적했다. 모두 문서에 반영한 뒤 전체 문서를 다시 읽은 한정 재리뷰에서 **해소·신규 지적0·설계 계약 승인**을 받았다.

이 승인은 설계 내부 정합성에 한정한다. 사용자 상세 설계 확인·구현 인수·실관측 승인·운영 배포는 완료되지 않았다.

## 검증 증거와 한계

- 이전 고정 #68에서 기존 테스트 **45 passed**, 격리 위반0. 다만 기본 ON/revoked 재발급을 정답으로 고정한 테스트가 포함돼 통과 자체가 머지 승인은 아니다.
- 당시 지정 CLI 리뷰는 bwrap 오류로 미완료였다. 그 결과를 승인으로 계산하지 않고 실제 코드에 접근한 병렬 Codex 리뷰와 직접 재현을 구분했다.
- 이후 main에는 #69의 CLI 수정이 반영됐다. **이번 문서 작성에서 #68 CLI 리뷰를 다시 실행했다고 주장하지 않는다.** 후속 구현의 새 범위에 대해 별도 리뷰한다.
- 이번 새 격리 작업공간의 기준선 `c32de93`: **1378 passed / 2 known xfailed / 1 existing pykrx warning**, 42.09초. 문법·비밀정보 패턴 검사 통과, 운영 상태·외부 네트워크 접근 시도0.
- 문서 6개를 추가/갱신한 최종 트리에서도 같은 KST 전체 검증을 다시 실행해 **1378 passed / 2 xfailed / 1 warning**, 42.19초·exit0·격리0·문법/비밀정보 패턴 검사 통과를 확인했다. 소스/테스트/설정 diff는 없다.
- 명령은 아래와 같으며 운영 자격증명을 상속하지 않았다. 설치/의존성 변경 없이 기존 venv를 사용했다.

```bash
env -i PATH=/usr/bin:/bin LANG=C.UTF-8 TZ=Asia/Seoul \
  PYTHONDONTWRITEBYTECODE=1 PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 \
  PYTEST_ADDOPTS='-p no:cacheprovider --tb=short -rx' \
  QWQ_DEPLOY_SSH_KEY=/tmp/qwq-offline-no-ssh-key \
  QWQ_VERIFY_PYTHON=/home/ubuntu/projects/qwq-ai-trader/venv/bin/python \
  bash scripts/dev/verify.sh
```

기준선 성공은 새 설계 R01~R14의 테스트 성공이 아니다. 후속 구현은 아직 없으며 UTC 재검증·새 회귀·실관측 결과도 이 기준선에 포함하지 않는다.

## 인계·동시 작업 보호

검토 도중 Claude 작업공간 `code-review-4a372e`가 별도 리뷰 스크립트 수정 작업으로 변경돼, #68 `0b978e0`를 고정한 별도 읽기 전용 리뷰 작업공간으로 전환했다. 다른 세션의 변경을 되돌리거나 원래 브랜치로 강제 전환하지 않았다. 이번 설계도 별도 `feature/toss-runtime-design-20260916`에서만 작성한다.

운영 인계는 09-15 22:37 재시작본이라고 전달받았고, 저장소 배포 기록은 `c9923bf`, 인계 코드 기준은 `8849d92`다. 두 Git 트리의 차이는 CHANGELOG·CLAUDE·docs/README·MCP 리뷰 문서뿐이며 소스는 같다. 이는 현재 운영 프로세스 상태를 재조회한 증거가 아니다. 현재 main을 실행 중 코드로 오인하지 않는다.

다음 단계는 작성된 설계 확인 → 상세 구현 계획 → 책임별 격리 병렬 구현/리뷰 → 통합 검증/PR다. 실자료 승인값 발행·토큰/인증 조회·활성화·배포·재시작·주문/설정 변경은 별도다. #68은 임의 병합·닫기·리베이스하지 않는다.
