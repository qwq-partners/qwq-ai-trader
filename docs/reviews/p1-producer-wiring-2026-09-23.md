# P1 보호 생산자·엔진 배선 — 진행 및 검증 원장

## 범위와 판정

사용자 "니가 판단해서 진행해"에 따라 [실행 계획](../superpowers/plans/2026-09-23-p1-producer-wiring.md)의
S2~S5를 개발 브랜치에서 진행한다. 시작은 `6394d22`, 정책·계획 커밋은 `2ddacf4`다.
**현재 구현 중이며, 전체 P1 완료·main 통합·운영 설치를 뜻하지 않는다.**

거래와 잔고는 KIS 정본이다. 실 API·주문·운영 checkout·배포·재시작·설정·킬스위치·Toss
권한은 변경하지 않는다. 신규 durable schema나 거래 준비 상태 강제도 추가하지 않는다.

## Plan — 위임받은 결정

| 선택 | 결정 | 남는 대가 |
|---|---|---|
| 정규장 보호 매도 | regular MARKET | 실제 체결가·슬리피지 보장 없음 |
| 마감 구간 보호 매도 | closing LIMIT, 취소·MARKET 폴백 없음 | 미체결의 일자 전환 차단22 유지 |
| 보유기간 | ExitManager 영업일 정본 | 달력일 기준 legacy와 차이 유지 |
| 무결정 틱 쓰기 | 20초, 결정 틱은 즉시 기록 | 순간 고점 누락을 미스로틀 대조로 실측; 운영 채택은 별도 |
| 복구 결정 | 원 admission의 intent·명령·가격 보존 | 매도 attempt 증거가 없으면 제출 성공으로 보지 않음 |
| 종료 | 기존 runtime command scope에서 shield 작업 배수 | 종료 후 신규 작업은 거부; 별도 task를 보호 장벽에 등록하지 않음 |
| 시세 출처 전환 | 원 관측으로 REST/WS 분기 | REST의 보호-only 무효화 유지; WS 복귀도 기존 신선도 검증을 통과해야 함 |

## Do·See — 단계별 근거

| 단계 | 구현 범위 | 검증/통합 상태 |
|---|---|---|
| S2 | 신규 보호 생산자와 실제 owner/store 시험 | 구현 중, 승인 전 |
| S3 | engine attach 가격 writer·매도 수량/유형 | 고정 metadata 계약으로 S2와 격리 병렬 구현 중; 통합은 S2 다음 |
| S4 | factory 설치 및 runtime 읽기 전용 health | 미착수 |
| S5 | legacy 주석·문서·범위 통합 리뷰 | 미착수 |

이전 S1·S1′의 5183 passed는 [해당 부품 보고서](p1-components-2026-09-23.md)의
검증이며 후속 후보의 검증으로 재사용하지 않는다. 단계별 전체 시험은 모든 작업자 종료 뒤
coordinator 단독으로 UTC·KST 순서로 실행한다.

사전 대조에서 독립 gateway 인수 E1의 `update_position_price` 예외 단언이 S3 no-op
설계와 모순됨을 확인했다. 그 단언만 owner/live 상태 무변경 검증으로 교체하고,
체결·주문 writer의 명시 거부는 유지한다. 무수정37건 통과를 주장하지 않는다.
또한 재시작 후 현재 보호 intent의 확정 미체결이 증명되면 첫 관측에서60초를 새로 기다린다.
durable 종결 시각이 없는 상황의 보수적 대가이며 UNKNOWN이나 체결을 시간으로 지우지 않는다.

## 모델·예산·외부 리뷰

- critical 구현: native `gpt-6-astra/high`, 독립 재현·최종 리뷰: `gpt-6-astra/xhigh`.
- 후속 배선 목록 분석: `gpt-5.6-sol/high`. 문서 통합은 coordinator.
- native 실제 모델/effective effort는 실행 메타데이터 미노출로 미검증이다.
- 외부 리뷰는 도구0의 `claude-opus-5`, requested xhigh. 회당 CLI 예산 $5,
  총8호출 이내(대조·재시도 포함), 절대600초·시작120초·정체180초다.
  동일 일시 실패 재시도는 최대1회이며, 예산·인증·모델·안전 경계를 우회하지 않는다.
- 외부 입력은 선택한 코드·합성 시험·계약만이다. CLAUDE 원문·자격증명·계좌 실응답은 제외한다.

| 호출 | 목적 | actual model / 결과 | 비용 / 시간 | 입력 근거 |
|---|---|---|---|---|
| 1/8 | 도구 없는 소입력 연결 대조 | claude-opus-5, rc0, CONTROL_OK | $0.030395 / 3.242초 | 629B, SHA-256 `c65e50c08cda036f5e8baba5d76f9b4058470efef020a4888e780c98b3c292c7` |

연결 성공은 코드 승인이 아니다. effective effort는 외부에서도 노출되지 않아 요청값과 구분한다.
원시 실행·리뷰·변이 출력은 커밋 제외 경로 `.superpowers/sdd/2026-09-23-p1-producer-wiring/`에
보관하고, 이 문서에는 원격 인계 가능한 요약만 기록한다.

## 남는 경계

차단23의 A/C·repair-only·행 없는 결과 불명, 시간외 보호 매도(26), intraday_preemptive
소비자 부재(27), 미체결 BUY/late-fill·최초 인계·취소 최종성·정책 재게시는 이번 배선만으로
해소되지 않는다. 설치 차단의 정본은 [운영 인계 표](../operations/claude-migration-handoff-2026-09-20.md)다.
실제 설치 호출자는 계속0건이어야 하며, 승인받은 개발 기능을 운영 사용으로 확대하지 않는다.
