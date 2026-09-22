# P1 보호 SELL 기반 부품 — Plan·Do·See

## 범위와 현재 상태

사용자 지시: P1 인계를 이어받아 "전체 진행해". 개발 정본은
`feature/engine-safety-design-20260917`, 시작 SHA는 `6ee6e20`이다.
이 보고서는 S1·S1′ 부품의 근거와 잔여 조건을 기록한다. **전체 P1 완료,
main 통합 또는 운영 전환 보고서가 아니다.**

- S1·S1′는 매도 방식 선택과 독립적인 부품으로 개발한다. S2 이후의 시장가·마감
  세션 정책과 외부 Claude 리뷰 예산은 사용자에게 비동기 질문했고 아직 미확정이다.
- main·운영 checkout·배포·재시작·주문·설정·킬스위치·Toss 승인은 변경하지 않는다.
- 거래·잔고의 정본은 KIS이다. 이번 작업에 Toss 또는 실 API 호출은 없다.
- 신규 durable schema, 최종성 완화, 자동 초기 인계 허가, `trading_ready` 강제는 없다.

## Plan

사전 검토의 다섯 충돌은 [계획 §6-1](../superpowers/plans/2026-09-22-p1-protective-sell-parity.md)에
처분했다. 핵심은 다음과 같다.

1. 고아 접수의 폐기·재개·격리는 기존 시장 source 무효화와 한 owner transaction으로 묶는다.
2. 실패 래치는 명령·세대에 귀속한다. 같은 실패의 실제 새 재개 commit과 정상 게시만
   해당 기록을 해소하며, 중복 조회나 다른 종목 성공은 해소 근거가 아니다.
3. 결정적 계산 실패와 검증·저장·게시 실패를 분리한다. 후자는 성공한 격리로 취급하지 않는다.
4. quote→prepare 사이의 정상 pending은 S2 단일 생산자의 lock·shield로 보호해야 한다.
   runtime 부품의 진행 중 task 가드만으로 이 창을 닫았다고 주장하지 않는다.
5. A/C·repair-only·이미 저장됐으나 결과를 잃은 실패의 자동 해소는 남는다.
   C의 degraded 표시는 실제 체결에 결합된 repair 증거를 대신하지 않는다.
6. 다중 admission의 앞 결정이 뒤 오류에 가려지지 않도록 재개 한 호출은 최대 한 행을
   처리한다. S2 sweep은 각 반환을 원 intent와 함께 보관한 뒤 다음 행을 시도해야 한다.

| 역할 | 요청 모델 / effort | 작업 |
|---|---|---|
| S1 구현 | gpt-6-astra / high | runtime.py + 신규 pending/resume 시험 |
| S1′ 구현 | gpt-6-astra / high | gateway.py + 신규 episode intent 시험 |
| 복구 계약 분석 | gpt-6-astra / high | source/래치/저장/직렬화 계약의 읽기 전용 대조 |
| 독립 리뷰·재현 | gpt-6-astra / xhigh | 구현자와 다른 실행, 실제 owner·변이 인수 |
| 최종 문서 감사 | gpt-5.6-sol / medium | 상태·검증 수치·인계 링크의 읽기 전용 대조 |

모델과 effort는 요청값이다. native 실행 메타데이터에 실제 모델/effort가 노출되지 않아
**실제값은 미검증**이다. 외부 Claude 호출은 예산 답변 전 0건이며, native 리뷰를
교차 공급자 리뷰로 세지 않는다. 부품 단계의 축소 보증 허용은 인계 §10에 한정한다.
live 단계 S3·S4 통합에는 실제 모델이 확인된 교차 공급자 리뷰가 필요하다.

병렬 writer는 같은 base의 서로 다른 worktree와 파일을 소유한다. 단위 pytest는 최대
2개, 전체 suite는 읽기 전용 작업자를 포함한 모든 worker 종료 뒤 coordinator 단독으로
UTC·KST 순서로 실행한다.

## Do·See — 진행 기록

| 단계 | 후보 / 제품 파일 | 현재 근거 | 판정 |
|---|---|---|---|
| S1′ | `8b2b7fa` + 시험보강 `7a8dff0` / gateway.py | RED7 → 관련42 GREEN, 독립 변이6종 중 생존1 발견 → spy 보강 → 해당 변이2 RED·원제품2 GREEN | 독립 한정 승인·전체 검증 통과·개발 브랜치 통합 |
| S1 | `a3f522b` + P1-G 수정 `af6c5e3` / runtime.py | 최초 신규55 RED→관련242 GREEN, 독립 변이8종 탐지·실결함1 재현 → 수정 후 관련243 GREEN | 독립 재리뷰 한정 승인·전체 검증 통과·개발 브랜치 통합 |

S1′의 테스트는 실제 owner에서 100주 중 10주 익절 체결 후 90주를 별도 손절/EOD
intent로 준비·예약한다. EOD 판단·발행 자체는 S2 범위이며, 이 시험은 그것을 대신하지 않는다.

S1′의 최초 독립 판정은 제품 결함0·시험 공백1로 수정 요청이었다. `dict.get`만 감시한
spy는 `in`/`[]`로 캐시를 먼저 읽는 변이를 놓쳤다(신규11 GREEN으로 생존).
spy의 멤버십·조회·대입 감시를 보강한 뒤 같은 변이가 2건 실패했고, 원제품은 통과했다.
재리뷰 판정은 `7a8dff0`에 한정한 **APPROVE_THIS_SLICE**다. 제품 수정은 없었다.

S1의 독립 리뷰는 원 H7 계약의 부족을 실제 경로로 반증했다. 보유100주에서 first10주
체결을 관측해 `FINAL_FILLED/observed10/applied0`이 된 뒤 pending을 해제하면,
실큐 경제 적용 후 잔량90주는 맞지만 단계는 `none`으로 남았다. 해제 미호출 대조군은
`first`였다. **미적용 체결은 미체결이 아니다.** 수정본은 관측 체결도0일 때만 해제하며
같은 실경로 시험과 guard 삭제 변이로 검증했다. 경제 reducer나 최종성 규칙은 바꾸지 않았다.

비차단 P3 권고: 재개 계산과 `quote_protection`의 중복 흐름은 향후 함께 관리해야 한다.
현재 손절/무결정/익절 × pending 유무 6개 parity는 통과하지만 모든 전략·지표·시각의
동등성 증명은 아니다. 순수 계산 예외 경계의 공통화는 별도 최소 변경으로 검토한다.

### 최종 통합 검증

제품 통합 커밋은 `77a3641`이다. 변경은 제품2파일·신규 시험3파일(67건)이며,
기존 시험과 live 파일·설정은 무변경이다. 통합 후보의 각 제품·시험 파일이 위 독립 승인
SHA와 동일함을 `git diff`로 확인했고, 그 후보 전체를 UTC/KST 순서로 검증했다.

| 환경 | 결과 | 경고 | 격리 위반 | 시간 | 종료 코드 |
|---|---|---|---|---|---|
| UTC | 5183 passed / 기존 2 xfailed | 4 | 0 | 393.93초 | 0 |
| Asia/Seoul | 5183 passed / 기존 2 xfailed | 4 | 0 | 393.28초 | 0 |

```bash
env -i PATH=/usr/bin:/bin LANG=C.UTF-8 TZ=UTC PYTHONDONTWRITEBYTECODE=1 PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 QWQ_DEPLOY_SSH_KEY=/tmp/qwq-offline-no-ssh-key /home/ubuntu/projects/qwq-ai-trader/venv/bin/python -m pytest tests -q -p no:cacheprovider --tb=short
```

KST는 같은 명령의 `TZ=Asia/Seoul`만 바꿨다. 경고는 기존 pykrx 자원 API1건과
account lease fork 시험3건이다. 전체 suite는 작업자 전원 종료 뒤 단독·직렬로 실행했다.
문법·저장소 비밀정보 의심 패턴 검사도 아래 명령으로 종료0을 확인했다. 테스트 생략은
앞의 두 전체 실행을 대체하지 않으며, 이 명령은 추가 정적 검사에만 사용했다.

```bash
env -i PATH=/usr/bin:/bin LANG=C.UTF-8 QWQ_VERIFY_PYTHON=/home/ubuntu/projects/qwq-ai-trader/venv/bin/python QWQ_VERIFY_SKIP_TESTS=1 bash scripts/dev/verify.sh
```

`git diff --check`와 staged diff 검사도 종료0이다. 비밀정보 검사는 정의된 패턴에
한정하며 모든 민감정보 부재를 증명하지 않는다(아래 기존 문서의 절차상 한계 참조).
로컬 원시 출력은 `.superpowers/sdd/2026-09-22-p1-protective-sell-parity/`의
`utc-full.txt`·`kst-full.txt`, 독립 재현 상세는 `s1-review.md`·`s1-prime-review.md`에
보관했다(커밋 제외). 이 보고서가 원격 인계용 검증 요약이다.

마지막 문서 감사에서 README가 옛 B2/B3 원장과 09-22 시작 상태를 현재 인계 기준으로
안내하는 2곳을 확인해 정정했다. 원래 인계 프롬프트 첫머리에도 현재 재개 위치를 추가해
이미 완료한 S1·S1′을 반복하거나 옛 제품 트리 동일성 검사를 적용하지 않도록 했다.

## 절차상 한계

규칙 문서의 제한된 구간을 읽는 과정에서 기존 민감행의 필터가 누락되어 로컬 도구 출력에
포함됐다. 값을 사용·재기재·커밋하지 않았다. 이후 리뷰어에게는 해당 원문을 읽게 하지 않고
코디네이터가 필요한 규칙을 추출해 전달했다. worktree 격리는 비밀정보 접근을 막는 보안
sandbox가 아니므로, 문서의 민감정보 제거와 해당 자격의 교체 여부는 별도 점검 대상이다.

## 남는 조건

- 차단23: A/C·repair-only·행 없는 결과 불명의 복구 증거와 day fence는 미해소.
- 차단24: 실제 attach MARKET_DATA handler의 직접 가격 writer 예외는 S3 대상.
- 차단25: S1′ intent 분리만으로 닫히지 않는다. S2 생산자와 S4 배선까지 인수해야 한다.
- 차단26: 시간외 보호 SELL 미지원, 차단27: intraday_preemptive 결정 소비자 부재.
- 최초 인계·취소 최종성·미체결 BUY/late-fill·정책 재게시·설치 전제는 기존 인계 정본을 유지한다.

다음 시작점: [P1 계획](../superpowers/plans/2026-09-22-p1-protective-sell-parity.md),
[설치 차단 원장](../operations/claude-migration-handoff-2026-09-20.md).
