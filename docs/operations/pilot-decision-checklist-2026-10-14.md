# 10월14일 09:05 이후 판단 체크리스트 — 파일럿 판독 → 고정 SHA 배포 → 새 관측

> **10월14일 후속 구현:** [REST 초기 근거 보존 PDS](../research/rest-market-state-evidence-2026-10-14.md)와 [10월15일 단발 LLM 관측 설치기 PDS](../research/llm-capture-installer-2026-10-14.md). 코드 검증 상태와 실제 예약을 구분한다. 배포·설치·실수집은 미실시이며 raw 가용으로 시장 정상성을 대체하지 않는다. 장외 배포 전 재검사, 자원 실측과 운영 study 근거 확보가 남아 있다.

작성 2026-10-13(설치 없음·운영 HEAD `161b394`·08:55 예약 불변). 이 문서는 내일 세션이 **순서대로** 확인할 항목과 각 항목의 근거·판정 규칙이다. 기술 파일럿 성공은 동시 모델 비교의 증명도, 경제성 검증도 아니다.

**10월14일 판독 결과:** [정본·시각 결함·장 상태 공백·남은 순서](../research/market-state-pilot-readout-2026-10-14.md). 예약 활성화 완료·원장 봉인/누락0·호가903/체결 상태856/장운영0. 기록 시각 역행으로 진단기는 실패했다. 수집 완료를 평가 가능으로 승격하지 않는다.

## 0. 전제(09:05 전에는 아무것도 바꾸지 않는다)

- 08:55 활성화·08:57 시작 마감·09:00~09:05 수집은 `qwq-entry-capture-20261014.timer` 가 한 번만 요청한다. 09:05 전 운영 HEAD/설정/소스/unit 변경은 고정 예약을 중단시킨다.
- 09:05 이후에도 **먼저 읽기 전용 판독**, 그 다음 배포 판단이다. 판독 전 재시작·배포 금지.

## 1. 파일럿 판독(읽기 전용)

| # | 확인 | 근거 위치 | 판정 |
|---|---|---|---|
| 1-1 | 활성화 서비스 상태·종료 코드·요청 영수증 소비 여부 | `systemctl status` 출력(읽기), `/var/lib/qwq-entry-capture/20261014-market-state-pilot-v1/status.json`, `/home/ubuntu/.local/state/qwq-entry-observation/20261014-market-state-pilot-v1/startup.receipt` | 영수증 미소비/시작 마감 경과면 **미수집**(UNKNOWN). 자동 재시도·소모된 once 재발급 금지 |
| 1-2 | 봇 건강 | 활성화 영수증·status의 예정된 재기동과 현재 PID·시작시각·NRestarts 대조, `/api/health`(execution_recovery ready, pending 0) | 08:55 활성화는 봇을 한 번 재기동하므로 이전 PID 1114252 유지가 성공 조건은 아니다. 추가 재기동·오류가 있으면 원인 먼저, 배포 보류 |
| 1-3 | 원장 봉인·누락 | `read_observation_journal(path, max_bytes, expected_study_sha256=9720ea6d…c01d)` → `complete`, `incomplete_reasons`, `dropped_records` | `complete=False` 또는 drop>0 이면 용량/기록 결함으로 분류. 결손 개수 불명(무봉인 prefix)은 UNKNOWN |
| 1-4 | 채널별 상태 수신 | 원장 `market_state` 레코드의 `tr_id`(H0STCNT0 47필드 / H0STMKO0 11필드) 건수·`market_state_status`(`raw_only`/`invalid_values`/`unsupported_layout`) | **MKO 0건은 UNKNOWN**(정상 증명 아님). `unsupported_layout`/`invalid_values` 비율이 0 이 아니면 필드 레이아웃 재확인 |
| 1-5 | 호가·체결 창 안 존재 | 63필드 `ws_quote`·47필드 체결의 `received_at` 가 09:00~09:05 KST 안에 있는지, 종목 005490 만인지 | 창 밖·타 종목 레코드는 결함 |
| 1-6 | 연결 세대·ACK | 레코드 `connection_id`·`generation` 분포, 구독 요청/ACK/수신 구분 | 세대 전환이 있으면 재연결 전/후를 섞어 해석하지 않음 |
| 1-7 | 용량·유량 | 레코드 수 vs buffer/queue 36,000·상태 몫 12,000·파일 221,186,048바이트 설정 | 설정 한도 대비 사용률만 보고. 실측 유량의 충분성 인증 아님 |
| 1-8 | 운영 영향 | 09:00~09:10 서비스 로그의 EGW00201/EGW00215/ERROR, 시세 전달 지연 | 0 이 아니면 파일럿 영향 여부를 시간 대조로 분리 |

판독 도구: `scripts/report_decision_quotes.py`(판단 기록이 있을 때만 — 10/14 파일럿은 판단 기록 배포 전이라 판단 레코드 없음), `scripts/report_kis_frame_diagnostics.py`, 원장 strict reader. 결과는 상태·건수·사유만 문서에 적고 원본은 Git 밖에 둔다.

## 2. 고정 SHA 배포 판단

배포 대상은 **리뷰·CI 를 통과한 하나의 SHA**(이번 PR 의 최종 head 가 Codex 리뷰·사용자 승인 뒤 main 에 병합된 경우 그 머지 커밋). 조건 전부 충족 시에만 사용자 승인으로 `local_deploy.sh`:

- [ ] 1절 판독 완료·기록. 파일럿 결함이 배포 대상 코드와 무관함을 확인(관련이면 수정 PR 먼저).
- [ ] 장외(15:30 이후 또는 08:00 이전)·pending `[]`·`execution_recovery.status == ready`·미체결 0·관측/토스 서비스 inactive.
- [ ] 살아 있는 **수집 활성화** 예약 없음(`systemctl list-timers`의 UNIT와 ACTIVATES를 대조). `qwq-entry-capture-retention-*` 보존기간 정리 타이머는 수집 예약과 구분한다. 미래 수집 예약이 있으면 배포가 고정 SHA 계약을 끊으므로 먼저 확인한다.
- [ ] 보호 경로 지문(설정 3파일·킬스위치·`evolved_overrides.yml` 미커밋 수정) 배포 전후 동일. `KILL_SWITCH_KR` 유지.
- [ ] 배포 후 기동 로그: `[판단기록] llm-decision-v1 기록 활성`, 비교 작업자는 **로그에 나타나지 않아야 정상**(`COMPARISON_WORKER` 미설정 → 기본 OFF).
- [ ] 롤백: `local_deploy.sh` 자동 롤백 로그 확인, 실패 시 이전 SHA 로 복귀하고 원인을 기록.

## 3. 새 관측(판단·호가 동시) 설치 판단 — 배포 뒤 별도 승인

- [ ] 새 날짜·새 SHA 로 study/manifest 를 다시 묶는다(10/8·10/14 입력 재사용 금지). `--check`는 payload 검증이며 실제 runtime 의미 검증과 구분한다. 실제 `CapturePlan.load`는 별도 검증 및 `--apply` 타이머 등록 전 정적 검사에서 통과해야 한다.
- [ ] 미래 날짜의 명시 활성화/준비 프로필과 `runner-llm-window-v1` 설치 시험이 있어야 한다. 새 `20261015-llm-window-v1`의 최종 리뷰/CI 및 날짜를 확인하고 옛 프로필의 설정값만 바꿔 재사용하지 않는다.
- [ ] 새 profile의 가용 디스크 하한8,507,983,872바이트와 합산 구독 수요, 유량/RSS 실측을 확인한다. 설치기의 `memory_safety_verified:false`를 자원 검증 완료로 읽지 않는다.
- [ ] 초기 시장 상태 근거와 사건 연속성을 확인한다. H0STMKO0는 VI 발동·해제 시 수신되는 채널이므로 ACK와 무사건만으로 정상 상태를 확정하지 않는다.
- [ ] 월 예산 원장 `policy_status` 가 `normal` 이고 가용액이 실험 ceiling + 운영 예비를 덮는지 확인(과거 $99.70 은 현재 잔액이 아니다).
- [ ] 실험 manifest `config/experiments/R1-bull-reasoning-20261013.json` 은 **여전히 `enabled:false`**. 유료 비교 활성화는 Codex 리뷰·사용자 명시 승인 뒤 별도 작업이다.
- [ ] 설치는 미래 단발 타이머만, 봇 재시작 없음, 실패 시 자동 재시도 없음.

## 4. 오프라인으로 지금 할 수 있는 것 / 10월14일 실측이 필요한 것

| 오프라인 가능 | 실측 필요 |
|---|---|
| 가짜 공급자·임시 원장으로 입력→작업자→쌍→보고 E2E, 예산 경계·복구 시험 | 실제 채널별 상태 수신·세대·유량·누락 |
| 실험 manifest 상한 계산(토폴로지·예약 추정식) | 실제 응답 지연·실제 토큰(가정 900/400 치환) |
| 장 상태 코드 의미의 공식 문서 대조 | 실제 코드 분포·VI/정지 발생 |
| 계좌 기준선: 2024년 이하 권리조회(봇 정지 창), 코드표 대조 | 시작 순자산·외부 입출금 공식 원장(KIS 조회 가능 기간·완전성 확인) |

## 5. 하지 말 것

- 파일럿 성공을 "모델 비교 가능"이나 "정상 연속매매"로 승격하지 않는다.
- 사건 미수신·빈 값·오래된 상태·다른 세대 값으로 정상 상태를 채우지 않는다.
- 배포와 새 관측 설치를 같은 승인으로 묶지 않는다. 각각 사용자 결정.
- 계좌 TWR·KODEX200/반도체 제외 벤치마크는 자료 없이 계산하지 않는다(미완 과제 유지).
