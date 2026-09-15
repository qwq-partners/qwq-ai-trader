# Codex 독립 리뷰 후속 — 자료 보존·획득·시각 계약

## 범위와 승인

사용자가 최종 리뷰 담당을 Claude에서 Codex로 변경했다. 앞선 R1~R8의 `1aba7d7..53ea967` 독립 리뷰는 Astra/xhigh 3관점(동기화·replay / 근거 / 평가·CF)으로 수행했다. 신규 P0/P1은 없었으나 신규 P2 2건과 기존 P2 입력 계약 2건을 재현했다. 이후 사용자가 수정·커밋·push·문서·운영 배포와 재시작을 승인했다. **주문·설정·킬스위치 유지**, 위험 사이징/팀 정책 승격·canary 시작은 범위 밖이다.

수정 기준은 `4b70a34`(이미 머지된 PR #60 포함). 별도 발굴 채널 복구와 Toss 설계는 보존하고 이번 수정으로 재구현하지 않는다. 계획은 [후속 수정 계획](../superpowers/plans/2026-09-15-codex-review-followups.md).

## 재현한 네 결함

| ID | 분류 | 재현·영향 | 수정 인수 조건 |
|---|---|---|---|
| CR1 | 신규 P2, CF | 체결 저널 일시 읽기 실패가 완료된 BUY 성과를 삭제·저장. 복구 때 45봉 밖 표본을 다른 날짜로 재측정해 entry100/r20-20이 entry200/r20+20으로 바뀜 | 미상 상태만 평가에서 제외, 원 측정·ID 보존. 복구 시 재사용. 정확한 날짜 봉이 없으면 가격 대체 금지 |
| CR2 | 신규 P2, DART | status000이지만 report_nm 누락/빈 값인 공시를 fetched=True로 처리. 하위 검증·v2가 획득된 무위험 근거로 오인 가능 | 목록·제목 스키마 검증, 실패 미획득·미캐시. 유효 중립/0건 및 확인된 위험은 보존 |
| CR3 | 기존 P2, replay 만료 | KST-naive 만료는 체결되지만 같은 순간의 aware 만료는 TypeError→CHECKER_ERROR→일반 no_fill로 숨겨짐 | 같은 순간은 같은 결과, 일반 미체결과 검사 오류 구분. no-now host-local 호환 유지 |
| CR4 | 기존 P2, replay 관측 | nested 관측만 검사해 top-level observed_at이 결정 시각 이후여도 score가 통과 | 보고서 관측 시각도 미래/무효 명시값이면 제외, 독립 정상 보고서는 유지 |

기존 검토본의 전체 테스트는 UTC/KST 각각 916 passed / 2 xfailed였다. 위 경계는 전체 테스트가 놓친 반례를 별도로 합성 재현한 것이다. 실거래 손익이 실제 변경됐거나 부적절한 주문이 발생했다고 주장하지 않는다.

## 구현·작업별 리뷰 결과

- 구현: CF·replay Astra/high, DART Terra/high. 별도 feature 워크트리, CF/DART 병렬 후 replay 진행. 공유 문서는 통합 담당만 수정한다.
- 최종 리뷰: 구현에 참여하지 않은 Codex Astra/xhigh. 작업별 요구사항·품질 검토와 통합 브랜치 검토를 구분한다.
- 기준 `4b70a34` 오프라인 전체 verify: **928 passed / 2 xfailed**, 기존 pykrx DeprecationWarning 1건, 24.39초. 문법·비밀패턴 검사 통과, 운영 상태/외부 네트워크 접근 시도 0.
- CR1 구현 `efd3ecb`: RED 9 failed/22 passed → 관련 115 passed → 전체 940 passed/2 xfailed(27.92초), 격리 위반0. tracker와 신규 테스트 12건만 변경. 최초 env-i 전체 실행에서 가짜 SSH fixture의 HOME 기본 경로 확장 실패가 있었고, 비운영 dummy `QWQ_DEPLOY_SSH_KEY`를 명시한 최종 실행에서 해소했다(SSH 연결 없음).
- CR1 독립 리뷰: 원 측정 보존·평가 제외·복구·정확한 날짜 요건은 충족. 다만 정확한 날짜 봉이 없는 오래된 미완성 150건이 기존 첫 150개 조회 한도를 독점해 신규 표본이 영구히 평가되지 않는 추가 P2를 합성 재현했다. 가격 정확성은 유지하면서 조회 대기열의 독점을 막는 후속 수정을 진행한다(Terra/high, 기존 CF 구현자는 별도 replay 워크트리로 이동해 파일 충돌 없음).
- CR2 구현 `86ada2d`: 제목 누락과 실패 결과 캐시를 각각 RED로 확인. 신규 16/관련 82/전체 944 passed, 기존 xfail2·pykrx 경고1·격리 위반0. DART source와 신규 테스트만 변경. 혼합 목록의 알려진 위험/호재 사실은 보존하되 fetched=False로 획득 완전성을 부정한다.
- CR2 독립 리뷰: v2 기권은 정상이나 호재+null행에서 live 조정0→+0.10·스크리너50→65의 새 가산을 실소비자로 재현했다. fetched=False만으로는 가산을 막지 못한다. 불완전 목록의 호재 가산을 제거하는 후속 수정(Astra/high)을 진행하며, 확인된 위험 및 정상 완전 목록의 호재는 보존한다. 위 최초 구현의 호재 보존은 이 후속으로 정정 대상이다.
- CR3/CR4 구현 `bcbd791`: RED37 failed/17 passed → 신규55건 포함 UTC/KST 관련257 passed씩, 격리 위반0. 비교값만 KST 정규화·감사 시각 원형 유지, top/nested 미래·명시 손상 관측 보고서 제외. 검사 오류가 일반 미체결 통계로 다시 흡수되지 않도록 timing의 원 선정 수·오류 수·유효 분모를 함께 출력하며 모두 오류인 셀은 fill_rate=None이다. manifest/임계값은 변경하지 않았다.
- CR3/CR4 독립 리뷰는 명세·품질 승인, 신규 P0/P1/P2 0건. 기존 no-now 호환 파일 무변경, 실제 검사 오류→집계→JSON/Markdown 출력까지 확인했다. 추가 의심 반례가 없어 동일 테스트를 중복 실행하지 않았다.
- CR1 조회 공정성 후속 `2d55315`: 행별 `last_price_attempted_at`을 저장해 미시도·가장 오래전 시도 순으로 선택한다. 메모리 커서만 쓰면 매일 재시작 후 같은 150건에 다시 갇힐 수 있어 영속 필드를 사용했다. 정방향/역방향 기아의 저장·재로드 RED2→신규14/관련117/전체942 passed·xfail2·격리 위반0. 원 측정·unknown 제외·정확한 날짜·150건 한도는 유지. 한정 재리뷰 승인, 기존 P2 해소·신규 지적0.
- CR2 실제 가산 후속 `a7a1274`: 불완전 목록의 positive_list를 비워 validator 조정0·스크리너50을 보장. 위험 block/warning은 유지하며 정상 호재는 +0.10·65. 실제 소비자 RED6→관련103 passed·격리 위반0, DART 회귀 총25건. 한정 재리뷰 승인, 기존 P2 해소·신규 지적0. **일부 공백 혼합처럼 기존에도 남았을 수 있는 긍정 가산까지 억제하므로 실제 정책 의미 변경이다.** shadow-only/모든 live 기준선 불변이 아니며 정상 임계값·설정은 유지한다.

## 통합 검증

- 통합 소스 HEAD `dd77e0b`(아래 문서 커밋 전), 기준 `4b70a34`. 새 회귀는 CF14+DART25+replay55=94건.
- 전체 verify: **Asia/Seoul 1022 passed / 2 xfailed / 26.97초**, **UTC 1022 passed / 2 xfailed / 25.46초**. 양쪽 운영 상태·외부 네트워크 접근 시도0, 기존 pykrx DeprecationWarning1. 기존 xfail은 손절 기준·익절 접촉 parity 차이이며 미해결/승격 보류 그대로다.
- 재현 명령(격리 워크트리, 운영 .env 로드 금지):

```bash
env -i PATH=/usr/bin:/bin LANG=C.UTF-8 TZ=Asia/Seoul \
  PYTHONDONTWRITEBYTECODE=1 PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 \
  PYTEST_ADDOPTS='-p no:cacheprovider --tb=short -rx' \
  QWQ_DEPLOY_SSH_KEY=/tmp/qwq-offline-no-ssh-key \
  QWQ_VERIFY_PYTHON=/home/ubuntu/projects/qwq-ai-trader/venv/bin/python \
  bash scripts/dev/verify.sh
```

UTC는 TZ=UTC로 변경하며 충돌 없는 임시 pytest basetemp를 추가했다. 가짜 SSH 키 경로는 테스트 fixture의 HOME 기본값 확장 회피용이고 실제 연결은 없다. 문서 포함 최종 브랜치 리뷰·보호 PR·실제 배포 결과는 후속 실행 시 기록한다.

## 운영 관찰과 잔여 한계

- 21:31 KST 읽기 전용 사전 점검: 운영 checkout `4b70a34`/clean, 서비스는 이미 21:20:41 재시작된 상태(PID2976010). 브로커 연결, pending0, 하트비트 정체/실패0. 이 재시작은 이번 작업에서 실행한 것이 아니다.
- 배포 직전 다시 pending·청결·시간·설정 지문을 확인한다. 코드 pull을 먼저 하지 않아 배포 스크립트의 이전 SHA 롤백 지점을 유지한다.
- 테스트는 합성 입력 기반 정확성 검증이며 투자 우위·확률 보정의 근거가 아니다. 기존 손절 기준/익절 접촉 parity xfail 2건, CF의 일봉 근사·기존 체결 콜백 예외 계약, 과거 덮어쓴 판단 복원 불가는 별도 한계다.
- 운영 원장과 과거 연구 결과는 수동 수정하지 않는다. 다음 장중 DART/EntryPlan 및 저녁 CF 경로의 실제 스케줄 관찰은 즉시 헬스체크로 대체할 수 없다.
- CF 새 제외 필드를 모르는 구버전으로 추후 롤백하면 판정 불가 행의 평가 제외를 보장하지 않는다. 원 측정값 보존과 구버전 평가 의미 호환은 별개다. 상태 파일을 지워 맞추지 말고 구버전 평가를 성능/승격 근거로 쓰기 전에 확인해야 한다.
