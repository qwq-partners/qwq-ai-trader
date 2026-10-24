# KIS 조회량·Toss 보고 후속 실측 Implementation Plan

> **For agentic workers:** Use superpowers:subagent-driven-development or superpowers:executing-plans. Coordinator owns integration and operational scheduling.

**Goal:** 다음 실제 스캔과 장후 보고의 근거를 자동으로 남기고 정확한 대상만 판독한다.

**Architecture:** 고정 localhost의 기존 계수와 systemd 실행 식별자를 읽는 독립 CLI,
기존 Toss 원장 reader의 exact cohort/slot 판독을 병렬 구현한다. 봇 프로세스는 유지한다.

**Tech Stack:** Python 표준 라이브러리, 기존 ReportCohort/ObservationLedger, pytest.

**Spec:** [실측 판독 설계](../specs/2026-10-24-live-readout-design.md).

## Plan

기준 main `89d7fa79`(운영 `44483017`). 기존 전체9,027검사/CI 통과 트리를 사용한다.
현재 영속 기록에 없는 계수 전후 비교와 exact slot 선택만 보완한다.

| 담당 | 파일 | 인터페이스/검증 |
|---|---|---|
| Terra/high worker A | `scripts/ops/kis_health_delta.py`, `tests/test_kis_health_delta.py` | snapshot/diff, 동일 실행·전 차원·결측/감소·개인정보 제외 |
| Terra/high worker B | `observation_ledger.py`, `review_toss_observation.py`, `tests/test_toss_report_slot_readout.py` | read_only_report_slot, 두 날짜·다른 cohort·미완료·손상·무변경 |
| coordinator | 이 문서·운영 문서·설치 영수증 | 고정 소스의 단발 점검 예약·결과 검증 |
| 독립 reviewer | 최종 diff/검증 근거 읽기만 | 기능/입력/설치 경계 검토 |

작업자는 서로 다른 worktree·같은 기준 SHA를 사용한다. 운영 자료·네트워크·자격 접근과
재위임은 금지한다. 요청 모델/effort와 실제 metadata 노출 여부는 See에 기록한다.

### Review Focus

1. PID 재사용/실행 교체 중 snapshot → 일관된 식별자 없으면 비교 불가.
2. 신규 dimension/기존 dimension 소실/계수 감소 → 신규만0 기준, 소실·감소는 명시 결측.
3. 원장에 다른 날/slot이 추가됨 → 당일 아침 대상만 분모로 사용.
4. 아침 후보0·파일 부재·구형 metadata → 서로 다른 상태, 정상 가격 조회로 승격 금지.
5. 원장 조회 성공과 실제 보고 가격 소비/전송 → 확인 범위를 결과에 명시.

## Do

- [x] A: 실패 회귀시험 → 최소 구현 → 관련 검사.
- [x] B: 실패 회귀시험 → 기존 reader 확장 → 관련 검사.
- [x] coordinator: 통합 diff·문법·전체 verify·비밀정보 검사.
- [x] 독립 리뷰 → 지적 반영 → 최종 소스 승인.
- [ ] 전체검사 통과 소스의 커밋/푸시 → 고정 artifact로10월26일09:25/09:50,15:59/16:10 점검과16:12 판독 예약 설치.
- [ ] 설치 영수증 문서/커밋/푸시 → PR CI 통과/병합. 봇 운영 코드는 배포하지 않는다.

## See

- 구현은 별도 worktree의 Terra/high 두 작업자가 맡았다. 실제 model/effort metadata는 미노출이므로 unverified다.
- 기존 관련72검사와 새 통합 관련93검사가 통과했다(운영 상태/외부 네트워크 접근0).
- 독립 Astra/xhigh 리뷰의 KIS 응답 소유자 확인 누락을 재현했다. 고정 lsof로8080 listener가
  서비스 MainPID인지 조회 전후 검사하도록 보완하고, 잘못된 availability/InvocationID의
  실패 회귀시험6건을 확인한 뒤 수정했다. 전체검사/최종 리뷰/CI 결과는 아래에 갱신한다.
- 시간 가드는 KST를 명시하며, cron 데몬 호스트 시간대도 KST/+0900임을 확인했다.

- 독립 최종 리뷰 APPROVE·미해결 P0/P1/P2 없음. 별도 관련25검사와7개 cron 문법 검사 통과.
  요청 Astra/xhigh, 실제 모델/effort는 미노출(unverified)이며 교차 공급자 리뷰로 부르지 않는다.
- 10:51:53~10:53:05 KST 실제 localhost 두 번과 차분 스모크 rc0, 같은 PID3125313과
  listener 소유자를 확인했다. 토요일 도구 점검이며 장중 조회 절감/수익 근거가 아니다.
- 장래 읽기 전용 예약은 로컬 전체검사와 독립 소스 리뷰 후 고정 feature SHA로 설치하고,
  실제 설치 기록까지 한 PR에 넣어 CI/병합한다. 봇 운영 배포와 구분하며 다음 실행 전까지
  PR 통합을 끝낸다.

- 전체 `scripts/dev/verify.sh` rc0:9,052 passed/2 skipped/2 xfailed/8 warnings,
  727.23초. 운영 상태/외부 네트워크 접근0, 문법·비밀정보 검사 통과.
- 실제10월23일 아침 파일을 무접속으로 판독해 후보0(`no_candidates`)을 확인했다.

설치 영수증은 완료 뒤 갱신한다. 실제10월26일 계수/원장과 수신 보고 대조는
아직 수행하지 않았다. 매수 중지·월USD100·운영 봇의 실행을 유지한다.
