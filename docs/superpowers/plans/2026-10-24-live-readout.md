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
- [x] 전체검사 통과 소스의 커밋/푸시 → 고정 artifact로10월26일09:25/09:50,15:59/16:10 점검과16:12 판독 예약 설치.
- 설치 영수증 문서/커밋/푸시 후 PR CI 통과/병합을 확인한다. 최종 통합 상태는 GitHub PR 영수증을 정본으로 삼으며 봇 운영 코드는 배포하지 않는다.

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

## 예약 설치 영수증 — 10월24일11:06 KST

- 고정 소스 `f8ef32ebd6bfc45d3643590e1a845e5469352fd0`를 커밋/SSH 푸시한 뒤
  `~/.local/share/qwq-live-readout/20261026/code/`에428파일을 추출했다.
  source manifest SHA-256은 `0c2f782bf73edab00c8f1194610486f27e4ad220397eaa053a20846ca17eb0fa`다.
- 설치 시각은 `2026-10-24T02:06:52.586730+00:00`(KST11:06:52)이다.
  비공개 `install-receipt.json`·`cron-validation.json`·독립 리뷰·검증 로그는 위 경로의
  상위 디렉터리에 보관했다. 결과는 `~/.local/state/qwq-live-readout/20261026/`에 생성된다.
- 최초 긴 cron 명령은 길이 제한으로 등록되지 않았다(rc1). 기존 crontab 불변을 확인하고
  검토된 본문을7개 `jobs/*.sh`로 옮겼다. 파일0500·구문7개 통과·잘못된 날짜 무동작·
  실제 `crontab -n` rc0·최대137바이트·독립 후속 승인 뒤 설치/읽기 대조를 통과했다.
- 기존 cron3개와 운영 `44483017`·PID3125313·InvocationID·보호4파일 지문을 보존했다.
  봇 재시작/배포·추가 시세/LLM 요청·Telegram 시험 발송은 없다.

| 10월26일 KST | 작업 | 결과 파일 |
|---|---|---|
| 09:25 | 스캔 전 KIS 계수 | `scan-before.json` |
| 09:50 | 스캔 후 KIS 계수 | `scan-after.json` |
| 15:59 | 장후 보고 전 계수 | `report-before.json` |
| 16:10 | 장후 보고 후 계수 | `report-after.json` |
| 16:12 | 스캔 구간 차분 | `scan-delta.json` |
| 16:12 | 보고 구간 차분 | `report-delta.json` |
| 16:12 | 당일 아침 cohort·정확한16:00 slot 판독 | `toss-slot.json` |

모든 작업은 정확한 날짜/분·운영 HEAD·고정 소스 지문을 확인하고 최소 환경·45초 제한으로
실행한다. 결과/로그는 덮어쓰지 않는다. 날짜나 HEAD가 다르면 실행하지 않으므로 결과 파일
부재를0건으로 해석하지 않는다. cron 항목은 자동 삭제되지 않으며 실행 확인 뒤 해당7개만
제거한다. 그때까지 운영 HEAD 변경이 필요하면 이 예약을 함께 재검토한다.

설치 당시 실제10월26일 계수/원장과 수신 보고 대조는 아직 수행하지 않았다. 매수 중지·월USD100·
운영 봇의 실행을 유지한다. 설치 후 문서 커밋/푸시와 PR CI/병합 결과는 해당 PR 영수증을
따른다. 고정 판독 artifact는 문서 커밋만을 위해 교체하지 않는다.

<a id="morning-20261026"></a>
## 10월26일 오전 실제 판독 — T03 부분 완료·T04 후보 없음

### Plan

09:25/09:50에 생성된 원본을 기존 고정 판독기로 비교하고 아침 보고의 대상 유무를 확인한다.
추가 시세·계좌 API, localhost 재수집, systemctl, 발송, 운영 변경 없이 저장 자료만 읽는다.
16:12 예약 출력은 덮어쓰지 않고 오전 파생 결과를 별도 비공개 경로에 둔다.
현재 상태·NEXT의 정본은 [실행 원장 T03/T04](../../operations/execution-roadmap.md#t03-t04-morning)다.

### Do — 같은 실행의 09:25~09:50 계수

- 원본 `scan-before.json`의 수집 시각은09:25:02.132780~09:25:02.190021,
  `scan-after.json`은09:50:01.519531~09:50:01.645199 KST다.
- 두 snapshot의 PID3167991·InvocationID·시작 monotonic 값이 같고 active/NRestarts0이다.
  이는 **두 수집 시점**의 상태다. 오전 검토 시점의 active를 재조회한 것은 아니다.
- 고정 `f8ef32eb`의 `kis_health_delta.py diff` rc0, `complete=true/comparable=true`.
  비교 차원의 결측·계수 감소는 없었다. 같은 프로세스가 같은 broker 객체/event loop를
  보장하지 않으므로 `analysis_daily_event_loop_epoch=unverified`는 유지한다.

| 계수 범위·차원 | 이번 구간 증가량 | 해석 |
|---|---|---|
| 분석 일봉 wrapper | requests199 / provider_calls199 / cache_hits0 / coalesced0 | 보고된 계수에서 공유 캐시 재사용 효과 미확인 |
| 분석 일봉 entries | 64→64 | 현재 보관 항목 수이며 요청량·퇴출 수가 아님 |
| broker `unknown/daily_prices/FHKST03010100/page1` | 논리 호출199 / HTTP204 / 성공199 / EGW00201·재시도 각5 | 분석 wrapper와 개수가 맞아도 요청별 연결이나 호출자 귀속을 증명하지 않음 |
| broker `unknown/quote/FHKST01010100/page1` | 논리 호출10 / HTTP·성공 각10 | 호출자 unknown 유지 |
| broker `portfolio_sync/account_summary/TTTC8434R/page1` | 논리 호출48 / HTTP50 / 성공48 / EGW00215·재시도 각2 | 계좌 금액 없이 전송 계수만 판독 |
| broker `portfolio_sync/orderable_cash/TTTC8908R/page1` | 논리 호출·HTTP·성공 각48 | 원래 동기화 요청 계수 |
| broker `portfolio_sync/positions` | 논리 호출48 / cache_hits48 / cache_misses0 | 적중은 확인하되 전체 요청 절감률로 확대하지 않음 |
| direct `kr_screener/new_highs`, `volume_surge` | 각각 HTTP5 / 성공5 | broker 계수와 합산하지 않음 |
| direct `market_data/stock_valuation/FHKST01010100` | HTTP37 / 성공36 / EGW00201 1 | 분석 일봉과 다른 조회·캐시 경로 |
| direct `market_data`의 나머지 증가 차원 | fluctuation_rank5, foreign_institution8, index_price27, night_futures_quote5, sector_indices2; 각각 성공 수 동일 | 차원별 기록이며 이 숫자를 전체 KIS 요청량으로 합치지 않음 |

해당 구간의 완료 스캔5회는09:27:54·09:33:14·09:38:25·09:43:35·09:48:45다.
반환 후보 수는 각각3/19/19/20/19, 로그의 신규 수는0/3/1/0/0이다.
합을 고유 후보 수로 세지 않으며, 이 기록만으로 변경 전후 후보 동등성이나 자료 나이를
확인할 수 없다. 일봉 요청별 종목·깊이·시각이 없어 적중0의 개별 원인도 미확정이다.

### See — 효율 개선의 입증 범위와 다음 판단

- **이 구간에서 분석 일봉 공유 캐시의 조회 절감은 입증되지 않았고 오류도 남아 있다.**
  동기화 positions 적중48은 별도 긍정 근거지만, 부하를 맞춘 변경 전 비교가 없어
  전체 절감률이나 제한 문제 해결로 보고하지 않는다.
- 소스 독립 분석에서도 캐시 결함은 재현되지 않았다. 현재 공유 TTL60초·용량64와
  기본 스캔 간격5분, 스캔 내부 별도 `_daily_cache`가 있는 구조다. 긴 간격·내부 재사용·
  요청 깊이/자료 유효성 등은 가능한 설명이며 이번 집계로 어느 하나를 확정하지 않는다.
  적중률을 올리기 위한 TTL 연장이나 Toss 일봉 치환은 하지 않는다. 오늘 봉/수정주가/
  시장 기준과 승인 목적이 달라 현재 보고 REST 가격을 분석 일봉의 대체물로 볼 수 없다.
- 최소 후속은 예정된 오후 자료와 함께 **T03/T04→조건부T05→T06**을 검토하는 것이다.
  그 뒤에도 원인 구분이 필요할 때만 요청별 재사용 조건/실패 사유를 좁혀 확인한다.
  자료가 불충분하면 그 한계를 남겨 E01로 돌아가며 측정 기간을 자동 연장하지 않는다.

### T04 — 아침 대상의 실제 상태

- 08:25:22 보고 시작,08:25:25.096의0종목 발송 완료 로그가 있다.
  당일 추천 JSON은 존재하고 date=2026-10-26, stocks=[], report_cohort 없음이다.
  이는 기존 판독 계약의 **`no_candidates`**이며 파일 부재·quote 실패와 구분한다.
- `ReportCohort`는 빈 목록을 허용하지 않으므로 대상이 없는 상태 자체는 정상이다.
  `_set_report_cohort`가 이 경우에도 `invalid_report_protocol` 경고를 남기는 문구 혼동은
  확인했다. R01/T04 후속에서 후보 없음과 실제 검증 오류의 표시를 구분할 작은 개선으로
  남기며, 오늘 새 후보를 만들거나 장중 후보를 아침 목록으로 대체하지 않는다.
- 16:00 보고·16:12 exact slot 판독은 아직 미래다. 당일 아침 파일만으로 실제 호출/
  가격 소비/폴백/전송 결과를 완료로 세지 않는다. T04의 유효 가격 표본은 아직 없다.

### 근거·검증

- 원본은 `~/.local/state/qwq-live-readout/20261026/`에 보존한다. before SHA-256은
  `bb4d65c3ad004f623d17cdfa0e88e6ed4b8889145c2bc01812020f182178cfab`, after는
  `d435009a68b26170aee19bb5ed70ca1ec65fd2c648632d849be6877d3030c6e5`다.
- 파생 차분·금액 없는 로그 요약·지문·검증 근거는 비공개
  `~/.local/state/qwq-task-audit/20261026/t03-morning/`에 보존한다. 원문 로그·계좌 자료는
  커밋하지 않는다. 고정 판독 source manifest와22시 보고 manifest 검사는 각각rc0이며
  운영 HEAD `44483017`·기존 cron 전체 지문을 대조했다. 설정·예약·배포 변경은 없다.
- 독립 소스 분석은 요청 `gpt-5.6-sol`/high, 실제 모델·실효 effort metadata 미노출이다.
  이 분석을 최종 문서 승인으로 대체하지 않으며 문서 검증·독립 리뷰·CI/통합은
  [실행 원장](../../operations/execution-roadmap.md#t03-t04-morning)에 기록한다.
