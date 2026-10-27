# R02 — 장후 지수 관측·개인 계좌 시점 보완

**Goal:** 날짜 없는 지수 등락과 오래된 계좌 상태를 현재 사실로 표시하지 않고, 확인 가능한 장후 가격과 마지막 계좌 검증 시점을 전달한다.
**Architecture:** 보고 전용 KIS 분봉 파서 → 날짜별 입력 시도 영수증 → 기존 시장/개인 분리 발송. 거래용 캐시와 주문 판단은 변경하지 않는다.
**Tech stack:** Python asyncio, SQLite, POSIX flock, pytest.
**Spec:** [실제 지수 응답·R02 범위](../../operations/execution-roadmap.md#r02-evening-20261027), [보고 계약](../../agents/reporting.md).

## Plan

- 기준 SHA `71a96a07`. coordinator는 저장/스케줄/본문/통합, Terra/high는 별도 worktree에서 공급자·파서·해당 테스트만 작성한다. 독립 Astra/high는 동시성 설계 및 최종 리뷰, Sol/high는 계좌 시점 소스 분석을 맡는다. 실제 모델/effort metadata 미노출은 unverified로 기록한다. 작업자 최대3명, 비공개 운영 자료·자격증명·API 호출·배포 금지.
- `parse_index_observation(payload, index_code, report_date, received_at)`은 output2 한 행의 날짜·시각·양수 가격만 반환한다. sentinel/미래/다른 날짜/충돌 행 제외. output1 등락률/전일값을 섞지 않는다.
- `fetch_report_index_observations(report_date)`는 KOSPI/KOSDAQ 각각 첫 페이지1GET, 재시도/페이지 추가/redirect 없음. 전역 limiter·직접 요청 지표 유지. 결과 DTO는 code/label/price/source_observed_at/received_at/source/observation_kind/finality만.
- 한국 거래일16:40 보고 창에서만 조회한다. 별도 SQLite 입력 상태를 먼저 영속화한다. 같은 DB/날짜의 nonblocking 파일 잠금을 조회~발송 동안 유지한다. 경합자는 발송하지 않고 대기; 중단된 collecting은 재조회 없이 기존 ETF 자료로 진행한다. 완료된 입력은 재시작 후 재사용한다. 취소 시 이미 시작한 DB thread를 끝까지 기다리고 잠금을 해제한다.
- 분봉은 `참고 관측 · 확정 종가 아님`, 전일비/방향 미산출로 표시한다. 장전·장중 계약을 완화하지 않는다. 1,800자/35줄, 공개 채널에 개인 자료 없음.
- 개인 현재 대응3줄: 매수 중지 상태 / 로컬 KIS 세션 상태·마지막 성공 계좌 동기화(시세 시점 아님) / 현금 분류. 실제 성공 heartbeat만 사용하며15분 초과·실패·보류·미확인·미래·로컬 세션 무효일 때 현금 getter를 호출하지 않는다. 세션 유효는 실제 통신 성공이 아니다.

## Do

1. 실패하는 테스트로 가격 시점·보고 입력 영속성·경합/취소·계좌 상태 계약 재현.
2. 공급자와 소비자 병렬 구현 후 coordinator가 diff/테스트를 확인하여 통합.
3. 독립 중요 리뷰→수정→관련 검사·전체 verify·문서/비밀정보 검사.
4. PR/CI/병합 후 승인된 운영 배포. 새 KIS 자격·보호 설정·킬스위치·월USD100·기존 예약·Toss 미소비 승인을 보존한다. 오래된 credential/deploy 영수증을 재실행하지 않는다.

## See

- 시작 기준: 기존 보고 관련21검사 통과, 운영 API 사용0.
- 수락: 휴장/소비 슬롯 조회0; 한 날짜 최대2GET; 경합자 조기 fallback 금지; 중단/재시작 재조회0; 미확정 가격에 등락률/종가 확정 금지; 현금 신선도 증거 없는 경우 getter0.
- 운영 적용·실제 다음 보고 API 수락·휴대폰 가독성·투자 경제성은 별도 판정한다. 새 자연 보고 전에 완료를 과장하지 않는다.
- 구현/검증/리뷰/병합/운영 결과는 아래에 추가하고 [작업 원장](../../operations/execution-roadmap.md)을 함께 갱신한다.

## 독립 리뷰 및 검증 진행

- Terra/high 요청 공급자 구현을 별도 worktree에서 통합했고, Sol/high 요청 계좌 시점 검토와 Astra/high 요청 중요 통합 리뷰를 받았다. 실제 모델/effective effort metadata는 미노출이다. 기존 사용자의 독립 Codex 리뷰 대체 승인을 적용했으며 교차 공급자 리뷰로 부르지 않는다.
- 리뷰 수정: fetcher 없는 경합자도 AFTER 잠금을 잡도록 변경; `loop_status`에 epoch float 대신 datetime 전달; 잘못된 health dict는 미확인; aiohttp 내부 GET 재시도는 전용 세션에서 명시 차단. 각 결함은 실패 재현 뒤 회귀 검사로 확인했다.
- Astra의 독립 loopback 연결 끊김·취소·미지원 retry 제어·공유 연결 보존4검사 통과, Sol의 개인 context22검사 통과. 소스 중요 리뷰는 미해결P0/P1/P2 없이 승인됐다. 전체 verify/CI는 별도 확정한다.
- 배포 준비는 새 KIS 인증 반영 후 현재 PID/환경을 기준으로 동결한다. 독립 소스 리뷰에서 상속 환경으로 배포 대상/검증 생략을 바꿀 수 있던 runner 경계를 수정했고, 고정 대상·검증 생략 제거·시도 중복·보존 변경 차단의 격리3검사를 통과했다. 실제 서비스/API 호출0이며 배포 성공을 뜻하지 않는다.

- 최초 전체 로컬 verify:9,226 passed/1 failed/2 skipped/2 xfailed(775.54초). 기존 통합 검사의 단일 인자 context 대역이 새 now 인자를 받지 못한1건이다. 대역을 제거하고 실제 context/heartbeat 경로를 검증하도록 수정했다. 테스트 상태도 격리했고 Sol 독립 리뷰 승인, 수정 후 관련96검사 통과·격리 위반0. 최종 전체 판정은 같은 커밋의 CI와 운영 verify를 확인하기 전까지 대기다.

## 운영 적용 완료 — 2026-10-27 22:47 KST

- PR #280을 `c4bc4f0ee45157c088db283c3ed0bb0ef4e89bf4`로 병합했다. PR head `83e42831`의 전체 CI는9,227 passed/2 skipped/2 xfailed, 격리 위반0·비밀정보 검사 통과(run40510987888)다. 병합 main `c4bc4f0e`의 별도 CI도 SUCCESS(run40512711869)다. 최초 로컬 대역 실패1건은 실제 context를 쓰는 회귀 검사로 수정했고, 최종 관련96검사와 CI/운영 전체 검사가 이를 포함해 통과했다.
- 기존 `local_deploy.sh`로 운영 verify9,227검사 후 재기동했다. 새 PID3611280·active/running·자동 재시작0·가동161.1초·브로커 연결·기동 계좌 검증·복구ready·미체결0·기동 ERROR/Traceback/인증발급실패0을 확인했다. 독립 중요 소스/배포 준비 리뷰와 격리 배포 가드3검사도 통과했다.
- 새 KIS 인증정보/REST·WS 캐시를 포함한23파일, 기존 cron3개, Toss 시작 미소비 승인과 매수 중지/월USD100 설정을 보존했다. 도구가 보낸 주문/수동 보고 발송0. 실제 WS 수신은 아직 자연 실행 대기이며 Approval 보존을 수신 성공으로 세지 않는다.
- 증거는 비공개 `~/.local/state/qwq-deploy/report-index-health-20261027/`의 plan/result/acceptance-public.json·deploy.log에 보존한다. source/runtime SHA는 `c4bc4f0e`이고 이 운영 문서 후속만을 위해 재배포하지 않는다.
- 다음은 E02 미관측 조건·비용 후 효과 판독이다. 다음16:40 장후 가격/시점·21시 실제 계좌 상태/전일22시 기록 포함, API 수락과 모바일 수신은 별도로 확인한다. 전일대비·장중 지수/시장 폭·계좌 순수익 확정은 이번 완료 범위가 아니다.
