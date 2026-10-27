# QWQ AI Trader — 기술 문서

> **2026-10-27 22:47 KST R02 운영 적용 완료:** 고정`c4bc4f0e`·PID3611280·CI/운영 각각9,227검사 통과·복구ready·미체결0. 새 KIS 자격/보호23파일·cron3개·Toss 미소비·매수 중지 유지. [운영 영수증·NEXT](operations/execution-roadmap.md#r02-index-health-20261027). 다음은 E02 미관측 조건/비용 후 효과이며 자연 보고/모바일은 별도 대기다. 문서 후속만을 위한 재배포는 하지 않는다.

> **10월27일 R02 후속:** 장후 지수 같은 행 참고 가격·계좌 동기화 시점·보고 입력 중복 방지를 보완한다. [현재 구현/검증/운영 상태](operations/execution-roadmap.md#r02-index-health-20261027)와 [계획](superpowers/plans/2026-10-27-report-index-health.md)을 따른다.22시 기존 cron의 다음날 요약용1행 저장은 확인했고 실제 다음날 포함은 대기다. 새 KIS 자격과 기존 보호/예약을 보존한다.

> **10/27 KIS 인증 전환·21시 후속:** [O03 운영 적용](operations/execution-roadmap.md#kis-rotation-20261027) · [R02 개인 요약/지수 실응답](operations/execution-roadmap.md#r02-evening-20261027). 새 자격 검증·같은SHA 재기동·계좌 조회 복구 완료. 개인 요약은 API 수락/원문 대조까지, WS 수신·모바일 확인은 별도다. 지수 비정상 시각/요약-분봉 가격 차이를 다음 보고 설계에 반영하며 문서만 재배포하지 않는다.

> **현재 할 일:** [실행 대기열·우선순위·착수 조건·완료 기준](operations/execution-roadmap.md#next-tasks-20261027). 완료/READY/시각·자료 WAIT를 구분하고, 기한별 실제 점검은 [체크포인트](operations/monitoring-checkpoints.md)에서 인수한다. task의 정본은 하나로 유지한다.

> **R02 운영 적용 완료:** [16:25 배포·보고 예약 전환·사후 검증·자연 보고 인수](operations/execution-roadmap.md#r02-live-20261027). 운영`6d132809`이며 아래 예약/미적용은 당시 상태다. 문서 후속은 재배포하지 않는다.

> **R02 16:10 단발 배포 예약 설치:** [고정SHA·16:10 단발 실행·Toss 조건·복구·결과 확인](operations/execution-roadmap.md#r02-deploy-20261027). 보고7파일 준비/검사와 실제 운영 전환을 구분한다.

> **Telegram 보고 개편 R02:** [구현·검증·운영 인수 계획](superpowers/plans/2026-10-27-telegram-audience.md). 채널 한국시장3슬롯·개인 엔진1회+중요 즉시 알림, 원천 날짜/결측 보존. 현재 운영·고정 cron은 미적용이며 국내 장중 지수의 원천시각 확보가 남아 있다. [상태 정본](operations/execution-roadmap.md#r02-telegram).

> **10월27일 첫 자연 실행 판독:** [E02 배치·장중 수급·보고 결과](operations/execution-roadmap.md#e02-morning-20261027). 후보6→SEPA 점수 통과0의 위치를 확인했고 F1 양성 가점을 실측했다. 스크리너 순위와 진입 점수는 다른 척도이며 선정 조건을 낮추지 않는다. 표시/설명 수정은 후속 PR, 운영은 `804e7b22`다.

> **E02 인수 경로 구분:** [후보 점유·수급 연속성·위키 날짜의 실제 확인 조건](operations/execution-roadmap.md#e02-natural-acceptance). 배치/아침 보고는 F2, 장중 스캔은 F1, 실제 판단 문맥은 W1의 증거다. 경로 실행·결함 조건 해결·경제성을 각각 판정한다.

> **10월26일 운영 적용 완료:** [실행 원장 E01/O02](operations/execution-roadmap.md#e01-application). 선정·위키·보고·관리형 호가 수신 개선을 고정 `804e7b22`로 배포했고 정상 기동/보존 검증을 마쳤다. 소비 cron7개만 제거·반복3개/원본 보존. 다음은 E02 자연 스캔 반영 확인이며 실제 지연·선정·순수익 효과는 미검증이다. T04의 남은 슬롯과 retention 기한은 원장을 따른다.

> **E01 분석·선행 개선:** [104후보 분석·ACK 대기 해소 설계/검증](research/entry-readiness-offline-2026-10-24.md). 사용자 승인으로 관리형 관측 수신 지연 개선을 개발했다. [원장 E01-ACK](operations/execution-roadmap.md#e01-ack)과 [10/26 적용](operations/execution-roadmap.md#e01-application)에서 개발/운영 적용·미검증 경제성을 구분한다. 기존 실측 원본을 보존한다.

> **개발 Claude 모델:** [구독 CLI Opus5.5 검증·현재 선택](operations/local-development.md#claude-opus55) · [D01 적용 상태](operations/execution-roadmap.md#d01-opus55). 새 개발/리뷰 호출은 전체 모델 ID를 명시하며, 엔진 API 모델과 과거 Opus5 기록은 유지한다.

> **Claude 사용 검증 D01:** [실제 재현·결과 수락·한계](reviews/claude-cli-usage-audit-2026-10-24.md). 실행 성공과 리뷰 정확성을 구분하며 도구 없는 호출의 가짜 실행 결과를 거부한다. 개발 협업은 계속하되 운영·엔진 API를 변경하지 않는다.

> **위키 E02-W1:** [실사용·리서치 수집일 검사](reviews/wiki-context-audit-2026-10-24.md). 오래된 연구행의 문맥 재사용을 제한하고 역사적 거래 교훈과 구분한다. 10/26 운영 적용 완료이며 실제 효과는 실행 원장에서 별도로 추적한다.

> **선정 입력 E02:** [장중/배치 원천 지도·연속 수급 가점·비활성 전략 후보 누락 수정](reviews/selection-source-map-2026-10-24.md). 현재 실측 대기 중 가능한 입력 점검이며, 비용 후 성과 판정과 운영 적용은 별개다.

> **개발 CLI 협업과 엔진 호출 경로:** [D01/L01 상태](operations/execution-roadmap.md#d01l01) · [Claude/Codex 역할·확인 결과·엔진 적용 검토](operations/local-development.md#cli-collaboration). 개발 협업은 적용하고 엔진 API는 유지한다.

> **10월24일 후속 작업:** [실행 원장](operations/execution-roadmap.md)의 O01 보고 전용 예약 설치·T02 역할표 검토·O02 비삭제 사전점검·A01 기존 자료 대사는 완료했다. 자연 보고와 향후 정리·경제성 결측은 각 ID에 남겼다. 실제 추가 공급자 대체와 계좌 순수익은 미판정이며 다음 입력은10/26 T03/T04다.

> **작업을 이어갈 때 먼저 읽기:** [엔진 개선 실행 원장 — 현재 목표·작업 ID·완료 범위·예약·NEXT](operations/execution-roadmap.md). 아래 날짜별 링크는 근거/이력이며, 남은 작업의 순서는 실행 원장을 따른다. 구현·배포·실측·순수익 검증을 구분한다.

> [10월24일 후속 실측 예약 설치 완료: KIS 계수·Toss 보고 exact slot](superpowers/plans/2026-10-24-live-readout.md). 고정 `f8ef32eb`의10월26일 읽기 전용 작업7개를 설치했다. 전체9,052검사·독립 리뷰 완료, 운영 재시작 없음. 실제 자료 생성·조회 절감과 Telegram 수신 확인은 남아 있다.

> **10월24일 Toss 보고 운영 반영 완료:** [설치·활성화·배포 영수증](superpowers/plans/2026-10-24-toss-report-runtime.md#운영-적용-완료) · [다음 실측 순서](operations/monitoring-checkpoints.md#다음-실제-실행의-판독-순서). 운영 `44483017`·9,027검사·복구ready·미체결0, 기존 설정/원장 보존.10월26~30일의 보고 실행을 허용하며 실제 가격 공급·요청 절감은 별도 확인한다. 아래 미설치 상태는 이전 이력이다.

> **10월24일 Toss 보고 연결:** [실행 설계·Plan → Do → See·적용 순서](superpowers/plans/2026-10-24-toss-report-runtime.md). 신뢰된 helper부터 실제 보고 가격 소비까지 합성 통합 검증. 기본 OFF·운영 미설치/미활성화이며 API 절감·순수익 실측과 구분한다.

> **10월23일22:54 운영 `a512268e`:** [KIS 배포·Toss 기동 공백·다음 검증](superpowers/plans/2026-10-23-kis-toss-read-offload.md#10월23일-후속--운영-배포와-toss-실제-기동-경로-점검). 운영8,910검사·복구ready·미체결0·보호7파일/기존 비용행 보존. Toss는 설정만으로 활성화할 수 없으며 실제 시작 주입·조립·보고 종목·완료 대기가 추가 구현 대상이다. 장외 오류0을 요청 절감으로 판정하지 않는다. 아래 이전 운영 상태는 당시 이력이다.

> **10월23일18:24 운영 `4f39c90c`:** [보고 후속 배포·실제 종료·남은 확인](superpowers/plans/2026-10-22-report-content.md#10월23일-보고-후속-운영-배포-완료). 8,826검사·독립 리뷰 통과, 보호7파일·기존 원장 보존. 정상 종료 HTTP 경고0을 확인했다. KIS 제한은 이후 재발했으며 배포 전부터 반복된 자료 수집 문제로 별도 추적한다. 실제 수신·표본 보고·순수익 검증은 별도이며 아래 상태는 당시 이력이다.

> **10월23일16:50 운영 `fcb05bbe`:** [종료 경로 보완 배포·아침 자연 실행·남은 확인](superpowers/plans/2026-10-22-report-content.md#10월23일-연결-종료-보완-운영-배포). 8,819검사·독립 리뷰 통과, 기존 자료·매수 중지·월USD100 보존. 이미지 도착/가독성 및 이후 실제 종료는 별도 확인이다.

> [10월22일 보고·시장자료 연결 종료 후속](superpowers/plans/2026-10-22-report-content.md#보고-연결-종료-후속): 정상 종료에 빠졌던 HTTP 소유자 정리와 오류 격리. 개발 검증·운영 적용·다음 보고 전달을 구분한다.

> **10월22일17:47 결측 보완 운영 적용 완료:** [고정 `73c171cd` 배포·보존·사후 점검](superpowers/plans/2026-10-22-report-content.md#결측-보완-운영-배포-완료). 8,798검사·독립 중요 리뷰 통과, 새 PID2932832·복구ready·미체결0. 매수 중지·월USD100 유지. 다음 아침의 실제 이미지·본문 전달은 아직 미확인이다.

> [10월22일 장후 실제 발송·미국자료 결측 보완](superpowers/plans/2026-10-22-report-content.md#10월22일-장후-발송-점검과-결측-보완): 전문가 DM API 수락, 후보 0개 본문 생략, 실제 08:25/16:00 설정 확인. 비유한 입력의 오분류를 수정하며 검증·배포 결과는 정본을 따른다.

> **10월22일 보고서 운영 배포 완료:** [15:35 실행·전체검사·새 기동·사후 대조](superpowers/plans/2026-10-22-report-content.md#10월22일-실제-운영-배포-완료) · [실제 발송 확인](operations/monitoring-checkpoints.md#10월22일-보고서-개선--배포와-실제-발송-확인). 운영`072f4c9e`·매수 중지 유지. 배포 성공과 실제 보고 전달·순수익 검증을 구분하며 아래 미배포 안내는 당시 이력이다.

> **10월22일 실측 판독 완료:** [완전 원장·시장 방어 차단·보조 가격 쌍 4개·후속 우선순위](operations/pregate-capture-2026-10-22.md#10월22일-실측-판독--완전-원장과-보조-가격-쌍-4개). 선정·가격 관측의 진전과 순수익 미검증을 구분한다. 보고서15:35 고정 배포 예약은 유지하며 아래 미수집/미판독 안내는 당시 이력이다.

> **10월22일 보고서 배포 예약 설치 완료:** [리뷰 대체 승인·고정 SHA·15:35 단발 예약](superpowers/plans/2026-10-22-report-content.md#10월22일-리뷰-대체-승인과-단발-배포). PR #240 병합/CI 통과, 운영은 오전 관측을 보존하며 실제 배포 전 상태다.

> [거래 보고서 본문·근거·다음 확인 계약](agents/reporting.md) · [10월22일 전체 보고 개선 및 배포 상태](superpowers/plans/2026-10-22-report-content.md). 기존 관측 예약과 실제 배포 상태를 구분한다.

> **10월21일 전문가 브리핑 개선:** [Plan → Do → See·근거·검증](superpowers/plans/2026-10-21-briefing-reliability.md) · [전문가 시스템 계약](agents/expert-system.md). 시장 입력과 참고 의견, 당일 등락과 누적 추세를 구분한다. 운영 미배포이며10월22일 고정 예약은 유지한다.

> **10월21일 기존 자료 기반 개선:** [종목 선정 입력 오류 수정·API 집계·검증·다음 순서](reviews/selection-input-followthrough-2026-10-21.md). 모의투자·가상자금은 사용자 결정으로 진행하지 않는다. ATR 자료 길이와 배치 판단 지표 전달을 정비하며 운영·10월22일 고정 예약은 유지한다. 개발 완료와 실거래 순수익 검증을 구분한다.

> **10월22일 관측 설치 완료:** [운영 배포·단발 예약·판독 입력·실행 영수증](operations/pregate-capture-2026-10-22.md#10월21일-운영-적용-영수증).10월21일15:48 기준 운영 `84fc2b3a` 정상,10월22일09:25 자동 활성화·09:51 종료 예정·09:55~10:30 수동 판독 준비 완료. 실수집/판독은 아직이며 아래 준비 문구는 당시 이력이다.

> **10월22일 후속:** [고유량 단발 관측·고정 날짜·검증·배포/예약 순서](operations/pregate-capture-2026-10-22.md).10월21일 원문/시도 자료를 보존하고 새 용량 계약으로 관측한다. 개발·운영 적용·예약·실수집/판독 상태는 이 정본 영수증을 따른다.

> **10월21일 후속 개발:** [원문 보존 용량 보완·흐름·검증·새 실측 준비](research/pregate-readout-2026-10-21.md#원문-보존-용량-보완--plan--do--see). 새 명시 예산만 적용하며 기존 원장·매매 정책을 유지한다. 운영 미배포·새 예약 미설치, 검증·통합 상태는 PR 영수증을 따른다.

> **10월21일 실측:** [장 상태 수량 상한·현금 차단·후속 용량 설계](research/pregate-readout-2026-10-21.md). 고정판독blocked/rc2·순수익미산출, 원문보존예산과실제유량검증이우선이다.

> **10월21일 관측 설치 완료:** [운영 배포·예약·고정 판독 입력·실행 영수증](operations/pregate-capture-2026-10-21.md#10월20일-운영-적용-영수증).10월20일15:58 기준 운영 `d5dd37bc` 정상, 내일09:25 자동 활성화·09:51 종료·09:55~10:30 수동 판독 준비 완료. 실수집/판독은 대기 중이며 아래 준비 문구는 당시 이력이다.

> **10월21일 단발 준비:** [사전 정책·고정 수집 시간·판독·운영 적용 전제](operations/pregate-capture-2026-10-21.md). KIS 거래일 확인, 09:25 활성화/09:51 종료안을 검증한다. 개발 완료와 실제 배포·설치·예약·수집은 별도 상태다.

> **10월20일 후속 개발:** [체결 관측 후 호가 이동: 정책·흐름·CLI·검증·다음 순서](research/pregate-readout-2026-10-20.md#후속-구현--체결-관측-후-호가-이동-plan--do--see). 기존 strict를 보존하는 별도 진단이며 미래 수집 전에 명시 정책을 고정해야 한다. 새 수집 설치/예약과 순수익 검증은 별도다.

> **10월20일 판독 완료:** [실측 결과·장 상태 병목·다음 순서](research/pregate-readout-2026-10-20.md). 첫 스캔21개 모두시장 방어 차단, 상위5 호가 쌍확보·장 상태 조건을 충족한 비교0·순수익null. 초기 상태/사건형 VI 계약을 보완하며 이번 문서는 운영 재배포하지 않는다. 아래 준비·미수집 안내는 당시 이력이다.

> **10월20일 판독 준비:** [후보 전용 실행·시장 방어 비교 사전 고정·남은 우선순위](operations/pregate-readout-2026-10-20.md). 운영 `89537b32` 배포/09:25 단발 예약은 PR #228 영수증에서 완료 확인. 이번 후속은 별도 오프라인 판독이며 실제 수집·판독과 구분하고 운영 재배포하지 않는다.

> **10월20일 단발 후보 관측 후속:** [준비 상태·소비된 v5 교체·운영 절차](operations/pregate-capture-2026-10-20.md). 명시 첫 스캔의 실제 메모리/채널 연결을 확인하고, 호환 코드 선행 배포 뒤 소비된 설정만 교체한다.09:25 활성화·09:30~09:35 첫 스캔·09:50:08 종료의 한 번 관측이며 매수 중지는 유지한다. 구현/검증과 실제 배포/예약/수집을 구분하고 최종 실행 상태는 정본과 PR 영수증을 따른다.

> **10월19일 후속:** [시장 방어 이전 후보의 메모리 보호·유량 검증](research/pregate-candidate-observation-2026-10-19.md#후속-plan--첫-스캔의-메모리-보호와-저장판독-한도). 명시 v4의 보유 레코드/종류별 개수·바이트 보호와 합성37,443기록 수집·판독을 검증했다. 운영 미배포·새 설치/예약 없음. 다음은 준비 상태·소비된 v5 교체 절차 후 배포·짧은 실측이다.

> **10월19일 현재:** [v5 실측·시장 방어·선정 근거·다음 순서](research/selection-readout-2026-10-19.md). 봉인/지연 판독 완료, 후보41건(23종목) 모두regime 차단·LLM0·후보호가0. 선정 단일 단계 비교까지 완료, 순수익 미확정. 운영ba29b903/PID2434183·매수 중지 유지. 아래10월18일 미수집·옛 PID는 당시 이력이다.

> [10월18일 동일 진입의 첫 청산 판단 재생](reviews/first-exit-replay-2026-10-18.md): 실제 ExitManager의 장중 첫 판단만 비교하는 오프라인 도구. 체결·순수익 미계산, 운영/10월19일 v5 무변경. 검증·통합 상태와 실제 입력 요구는 정본 참조.

> **10-18 검토(문서만):** [종목별 실시간 RAG 매매 파이프라인 대조 검토](research/realtime-rag-pipeline-review-2026-10-18.md). 전체 구조 미적용 — 회전/수수료 구조·측정 기준선 공백·KIS 41건/appkey 1세션 상한. 가져올 조각은 수집 시점 종목 태깅·후보 공시 거절 근거·맥락 확장 오프라인 비교·토스 관측 전용 재가동. 로컬 GPU(RTX 5080)는 오프라인 재생/수집기 역할, 운영 계좌 2호기 금지. 사용자 결정(10-18): 제안은 실반영하지 않고 기록만 남긴다. 코드·운영 변경 없음.

> **10월18일 준비 이력:** [귀속 배포·v5 설치 당시 상태](operations/selection-capture-2026-10-19.md#10월18일-주문-귀속-배포와-v5-교체). 고정 `ba29b903`·PID2369151·NRestarts0·복구ready·미체결0. 호환 reader `710af553` 선행 배포 후 최종 기능을 적용했다. 운영 전체7956검사·기동150초 이상·보호1187경로/기존 비용행 보존 확인. 매수 중지·월USD100·unknown1/normal·log_and_continue 유지. 10월19일09:25 v5 자동 수집 예약 설치 완료, 10:05~10:20 수동 판독 입력 사전검사 완료. 실제 수집·판독과 순수익 평가는 아직이다. 문서 후속은 재배포하지 않는다.

> 아래 미배포·v4 정본 링크는 이전 시점의 이력이다. 현재 절차는 위 v5 정본을 따른다.

> [10월18일 귀속 배포·v5 통합 검토](reviews/attribution-rollout-2026-10-18.md): reader7314·최종7956검사와 중요 리뷰 승인, 실제 배포/예약은 별도 영수증으로 구분한다.

> [10월18일 귀속 기능 배포 계획](superpowers/plans/2026-10-18-attribution-rollout.md): 호환 읽기 복구 기준을 먼저 확보한 뒤 기능 배포와 v5 관측 입력 교체를 진행한다. 실제 적용 상태는 실행 근거를 따른다.

> [10월18일 주문 귀속·체결 ID 대사](reviews/order-attribution-2026-10-18.md): 판단 ID의 의도 원장 보존과 정확한 체결 연결. 운영 미배포, 구버전 reader 롤백 호환 조건과 입력 출처 한계를 구분한다.

> [10월18일 수급 근거 후속](reviews/supply-evidence-correctness-2026-10-18.md): Swing 결측/중복 수급·일별 날짜·새 정책 관측 형식. 운영 미배포이며 검증·리뷰·후속 계측 순서를 구분한다.

> [10월18일 청산·선정 입력 정비](reviews/exit-selection-correctness-2026-10-18.md): 설정 전달·활성 ATR 상한·수급 결측/중복 감점·MRS 날짜 조인. 운영 미배포, 로컬 전체7452검사·독립 중요 리뷰 승인 완료. 남은 일봉 parity·관측 스키마·실험 순서를 구분한다.

> [10월18일 직접 KIS 계측 배포·v4 예약 완료](operations/selection-capture-2026-10-19.md#10월18일-직접-kis-계측-배포와-v4-교체). 고정 `a0bc9c94`·PID2208722·NRestarts0·복구ready·미체결0. 운영 전체7290검사와 독립 중요 리뷰 통과, 매수 중지·월100·unknown1/normal·기존 설정/자료 보존.10월19일09:25 v4 예약과10:05~10:20 판독 입력 사전검사 완료. 실수집·판독은 아직이며 자동 판독은 미예약이다. 후속 문서 커밋은 재배포하지 않는다. 아래는 각 시점 이력이다.

> [시장자료·스크리너 KIS 직접 GET 계측](integrations/external-apis.md#2026-10-17--시장자료스크리너-직접-get-계측-운영-미배포): 브로커 밖12개 조회 경로의 시도/오류를 별도로 관측하는 후속 개발이다. 운영 미배포이며 월요일 v3 수집·판독 입력을 보존한다.

> [10월19일 v3 판독 준비](operations/selection-capture-2026-10-19.md#10월17일-후속--판독-입력-고정): 고정 소스/입력 사전검사, 후보 선정→판단 후 호가→장 상태→900초 bid의 판독 순서. 실제 판독은 미실행·미예약이며 수집 예약과 운영을 유지한다.

> **2026-10-17 15:51 KST 이후 배포·v3 설치 확인:** [운영·계측·예약 근거](operations/selection-capture-2026-10-19.md#10월17일-대시보드-배포와-v3-교체). PR #212 고정 `8003acbd`·PID2140438·NRestarts0·복구ready·미체결0. 운영 전체7105검사 통과, KR 매수 중지·월100·unknown1/normal·기존 원장과 설정 보존. 월요일10월19일09:25 v3 예약 enabled/active, 이전 v1/v2는 정지/미소비 보존. 실수집·순수익은 아직 미확정이다. 아래 운영·예약 상태는 각 시점의 이력이며, 후속 문서만 재배포하지 않는다.

> [10월17일 대시보드 KIS 조회 절약·출처 계측](integrations/external-apis.md#2026-10-17--대시보드-kis-조회-절약출처-계측-운영-미배포): 무접속 수집 중단·휴장 캐시·동시 조회 합치기·시세 TR/source 집계. 개발 검증과 운영 실측을 구분하며 월요일 고정 관측 전 배포하지 않는다.

> **10월17일 오후:** [주간 복기 실제 결과·비용·KIS 진단·월요일 준비](operations/selection-capture-2026-10-19.md#10월17일-오후--주간-복기-실행-확인). 지정한 복기 모델의 정상 응답과 사용량, 예산 normal·unknown 보존, 새 v2 지문 일치를 확인했다. 운영 변경 없이 남은 진입 가격/선정 검증으로 이어간다. 아래는 각 시점 이력이다.

> **10월17일03:17 최신:** [비용 미확인 로그 후 계속·운영 반영](research/gemini-timeout-fallback-2026-10-17.md#10월17일-사용자-결정--비용-미확인-로그-후-계속) · [월요일 v2 설치·다음 점검](operations/selection-capture-2026-10-19.md). PR #208 운영 `846c3215`·전체7034검사 통과. 기존 unknown1 보존·월 정책 normal·새 사용량 정산1건 확인, 비용 자료 제출은 재개 조건이 아니다. 새 v2는10월19일09:25 예약 완료·실수집 미완료다. 문서 후속은 재배포하지 않는다. 아래는 각 시점의 이력이다.

> **10월17일02:27 운영:** [PR #207 배포·기존 비용 보류·관측 재개 조건](research/gemini-timeout-fallback-2026-10-17.md#10월17일-운영-후속--plan--do--see). 운영f844d9d6/PID2047470·전체6918검사·매수중지 유지. 기존unknown1로 유료 호출 차단,10월19일v1 타이머disabled/미소비 보존. 현재 월요일 자동 관측 없음. 아래는 당시 이력이다.

> **10월17일 후속 개발:** [Gemini 시간 초과 폴백·불확실 비용 보류](research/gemini-timeout-fallback-2026-10-17.md). 구현/검증과 기존unknown 복구·운영 배포를 구분한다.

> **10월17일 최신:** [주말 지문 확인·LLM 비용 미확정·복구 순서](operations/selection-capture-2026-10-19.md). 예약은 유지되지만 유료 판단은 차단 상태다. 공급자 비용 근거 대사가 우선이다.

> **최신 운영(10월16일16:05):** [2046e729 배포·10월19일 예약·남은 점검](operations/selection-capture-2026-10-19.md). 주말 설정 지문 확인→월요일09:25 활성화→10:00 이후 판독. 실제 수집/수익 비교는 미완료이며 아래는 당시 이력이다.

> **최신 휴장 전 후속:** [연결 시험·원문 수급·24단계 보정·10월19일 후보](research/selection-evidence-followthrough-2026-10-16.md). 10월17일 거래 관측 없음, 기존 토요일 복기는 별도다. 개발 검증과 운영 배포/예약/실수집을 구분한다.

> **최신 실측·수정 정본:** [10월16일 판독과 LLM 시간 방어](research/llm-capture-readout-2026-10-16.md). 예약 실행·원장 확인 완료와 가격/수익 비교 미완료를 구분한다.

> **최신 장전 작업 정본:** [10월16일 판독·점검·비교 계약](operations/premarket-followthrough-2026-10-16.md). 새 도구의 설치/예약/실행 상태는 이 정본을 확인한다. 아래 과거 미완료 표시는 해당 날짜 이력이며, 기존 권리 조회·경제적 29건 대사·비교 러너·예산 기능을 다시 미구현으로 취급하지 않는다.

> **최신 운영(10월15일22:33):** [역할별 모델 배분 배포·10월16일 관측 설치 완료](research/engine-model-allocation-2026-10-15.md). 운영 `de6c5bf7`, 내일09:10 준비·09:15 관측·09:41 종료. 후속 문서는 운영에 재배포하지 않는다. 아래 미배포/미설치는 각 시점 이력이다.

> **2026-10-15 밤:** [거래 엔진 모델 배분·검증·배포/예약 정본](research/engine-model-allocation-2026-10-15.md). 보고/복기 역할을 별도로 제한하고 기존 월100 원장·KR 매수 중지를 유지한다. 운영의 최종 SHA와 10월16일 관측 설치 상태는 이 정본의 후속 기록을 따른다. 아래는 각 작성 시점의 이력이다.

> [Claude 자동 판독 확인·10월16일 설치 준비](operations/llm-capture-followup-2026-10-16.md): 09:35 실제 판독/전송/cron 제거를 확인하고 해석을 보완했다. 다음 관측의 새 날짜·epoch·활성화 연결을 준비하며 운영 미설치·새 AI 판독 미예약이다. 아래 미등록 표시는 각 작성 시점의 이력이다.

> [10월15일09:39 실측 판독·관측 시간창 수정](research/llm-capture-readout-2026-10-15.md): 원장 봉인(전체 구간·디스크 영속성 미확정)·후보3건의 시간 차단·LLM시작0, 설치 전 시간/종료 여유 방어와 다음 미설치 단발안. 가격 비교·순수익은 아직 미확정이다. 아래 항목은 당시 이력이다.

> [가격 평가기 LLM 관측 연결·검증·다음 실측](research/profitability-kickoff-2026-10-14.md#10월15일--가격-평가기-연결-보완): 원본 레코드와 전체 후보를 보존하며 기존 가격 평가기에 연결한다. 실제 주문 준비/장 상태 검토가 없으면 비교 불가를 유지한다. 오프라인 변경이며 운영 재배포는 필요하지 않다.

> [순수익 개선 일정 및 착수](superpowers/plans/2026-10-14-profitability-priority-schedule.md): 기존10월15일 수집 후09:35 판독, 진입 가격→선정 기여→위험·교체·청산 순서. [실제 준비 근거와 입력 결측](research/profitability-kickoff-2026-10-14.md). 후속 AI 자동 재개 미등록, 운영 재배포 없음.

> [10월14일22:12 main Verify 실패 후속](operations/llm-capture-2026-10-15.md#plandosee--2212-main-verify-실패-후속): 시험의 동시 DB 조회 제거·종료 후 영속 결과 확인, 테스트/문서 변경만. 운영 버전과 내일 예약을 유지하며 병합 후 main 검사까지 확인한다.

> **10월14일21:59 운영 확정:** [10월15일 단발 관측 배포·설치·인계](operations/llm-capture-2026-10-15.md). 운영5444509/PID1720809, 08:55 활성화·09:26 종료 예약 완료, once 미소비. 다음은 실제 봉인·판단/호가 조인 판독이며 문서 후속 병합으로 운영 SHA를 바꾸지 않는다. 아래 개발 안내의 미배포·미예약 표시는 당시 이력이다.

> [10월15일 단발 LLM 관측: 자원 검증·메모리 보호·배포/설치](operations/llm-capture-2026-10-15.md): 전체 대표108,804건 부하와 실제 대용량 BOOK 반례, 보유 상한512MiB, 단계별 운영 결과와 다음 판독.

> [최초 LLM 판단 단발 관측 설치기 PDS·코드 흐름·남은 자원 검증](research/llm-capture-installer-2026-10-14.md): 10월15일 고정 후보창·최초5종목·실제 준비 판정·배타 설치/한 번 활성화. 개발과 실제 예약을 구분하며 운영 미배포·미설치다.

> [기존 REST 상태 원문 보존·공식 근거·흐름·다음 실측](research/rest-market-state-evidence-2026-10-14.md): 추가 호출 없이 명시 관측에 원문과 논리 호출 구간을 보존한다. 원문 가용성 진단이며 정상시장/순수익 판정은 아니다. 기본 OFF·운영 미배포.

> [10월14일 파일럿 판독·시각 정합화·배포 판단](research/market-state-pilot-readout-2026-10-14.md): 호가903/체결상태856/장운영0, 봉인과 평가 가능 분리, 초기 상태 근거·미래 동시 관측 설치기 우선순위.

> [PR #190 결함 6건 수정 PDS·흐름·검증](research/model-comparison-review-fixes-2026-10-14.md): 늦은 기록 보존·저널 완전성·비용 대사 복구·표본 분모. 기본 OFF·운영 미배포, 실수익 입증은 별도다.

> [실시간 비교 준비 — 입력 봉투·작업자(W2)](research/model-comparison-live-input-2026-10-13.md) · [R1 실험 명세·진입 조건·장 상태·일별 보고(W3)](research/model-comparison-experiment-R1-2026-10-13.md) · [구현 분담·인터페이스 계약](superpowers/plans/2026-10-13-live-comparison-implementation-plan.md) · [인계 원문](superpowers/plans/2026-10-13-live-comparison-claude-handoff.md) · [10월14일 판단 체크리스트](operations/pilot-decision-checklist-2026-10-14.md): 예산 보호/비용 복구는 러너 문서 "실패·복구 절차" 절. 기본 OFF·운영 미배포·유료 비교 없음.

> [비교 실행·중립 수량 재생 PDS/복구/다음 순서](research/model-comparison-runner-2026-10-13.md): 중복 과금 방지·판단 전 기존 사이징 재생·명시 별도 비교 작업자/읽기 전용 보고. 순수익/진입 가능성 미검증, 운영/파일럿 보존.

> [66차 후속 PDS·검증·남은 작업](superpowers/plans/2026-10-13-comparison-evidence-remediation.md): LLM 대상 신규 관측 계약·용량, 비용 계산/실행 경계, 실제 수량 정책의 조건부 증분 계산. 운영·10월14일 파일럿 보존. 실제 시세/정책 재생 입력·공급자 어댑터와 순수익 검증은 별도다.

> [66차 B 판단·호가 동시 관측 준비](research/concurrent-observation-design-2026-10-13.md): signal-window+장 상태 증거 초안의 CapturePlan 통합 검사 통과(미설치)와 사전 판독 규칙. [66차 C 모델 비교 비용 계획](research/model-comparison-plan-2026-10-13.md): 공표 요율·예비 비율 기반 비교 쌍 상한, 유료 호출 없음.

> [판단 뒤 첫 호가 수신 시각·종료 기록·다음 실측 PDS](research/decision-quote-receipt-time-2026-10-13.md): PR #183/184 병합·운영 미배포, 10월14일 기술 파일럿과 이후 판단/호가 동시 실측을 구분한다. [KIS 공식 계좌 기준선 조사](operations/account-baseline-status-2026-10-13.md).

> **73차:** [한 종목 기술 파일럿·실효 구독 가드·남은 순수익 검증 작업](research/market-state-pilot-2026-10-13.md). 별도 기술 계약·실제 수요 확인·관측만 중단·10월14일 단발 설치 경로. 실제 배포/예약 상태는 정본 확정 기록을 따른다.

> **72차:** [KIS 장 상태 원천 수집·PDS·파일럿 전 남은 작업](research/kis-market-state-capture-2026-10-13.md). 선택3채널·정확47/11필드·상태 기록 몫/저장 예산·가격 전달 격리. PR #179/`1fa4064c`를02:32 운영 적용했고 로컬/CI/운영5224검사·독립 실제 Opus5 기본OFF 승인을 통과했다. 한 종목 파일럿의 기술 계약/실효 여유/설치와 실제 수집은 다음이다.

> **71차:** [진입 LLM 공통 사실 입력·PDS·실측 전 남은 구현](research/entry-llm-common-input-2026-10-13.md). 결측/0·ATR·종목 노트/결합 국면 전달을 정비한다. PR #177/`6be9abe9`를00:35 운영 적용했고 로컬/CI/운영5182검사·독립 실제 Claude Opus5 종결 리뷰를 통과했다. 실제 장 상태 쌍·완전한 재생/모델 성능 비교는 아직 남아 있다.

> **70차:** [월USD100 자동 절약·필수 예산 보호·텔레그램·월초 복귀](research/monthly-budget-economy-2026-10-12.md).PR #175/`e096e705`를23:45 운영 적용했다.80/95/100·텔레그램·KST 월초 복귀 정상 기동, 기존 활성화/사용액·매수 중지 보존.5164 검사·독립 실제 Claude Opus5 승인, 실제 결과/남은 순서는 정본을 따른다.

> **69차:** [월USD100·활성화 기준선0·호가 장 상태 원천·운영 적용·다음 PDS](research/monthly-budget-and-market-evidence-2026-10-12.md). PR #173/`27635c5`를22:35 운영 적용했다. 월100·baseline0·첫 비용 정산과 기동 점검 완료. 최종 상태/다음 순서는 정본을 따른다. 새 관측 미예약·매수 중지 유지.

> [68차 LLM와 호가 관측의 통합 우선순위·비용·일별 개선](research/llm-and-observation-priorities-2026-10-12.md): 호출/목표/비용 결함 정비 → 공통 입력/동시점 장 상태 → 소규모 실측·모델 비교 → 응답 이후 가격을 포함한 순성과.20일을 기다려야 결함을 고칠 수 있는 것은 아니다. 운영 배포/모델 변경/새 관측/실수익 검증은 별도이며 최종 시험·리뷰·통합 상태는 정본을 따른다.

> [67차 H1 입력 준비·장 상태 근거 공백](research/h1-observation-preparation-2026-10-12.md):20일/60파일·정책 SHA의 오프라인 준비. 후보 날짜는 미등록·미예약이다. 다음은 동시점 장 상태 수집과 실제 진입/+900초 호가 한 쌍의 평가 가능성 검증이며, 이후 적용 비용/설정/채널/설치기를 확인하고 새20일을 확정한다.

> [66차 H1 다일·동시 자본 평가](research/h1-multiday-capital-2026-10-12.md): 원본20일·최초5개를 각자 현금/예약/전략 한도로 비교하는 오프라인 기능. [구현 계획](superpowers/plans/2026-10-11-h1-multiday-capital.md). 새 관측 미예약·실수익 미검증. 아래65차 이하는 당시 이력이다.

> [65차 주문 준비 호가 배정·배포](research/next-observation-design-2026-10-11.md#65차--주문-준비-호가-배정과-운영-배포): PR #168/32f52dd·23:32 운영 적용(PID889750). 실제 order_ready 최초5개 identity의 비동기 배정과 엄격 판독. [구현 계획](superpowers/plans/2026-10-11-h1-order-ready-observation.md). 새 수집 일정·실제 비용 후 수익은 미확정이며 배포 상태는 정본의 완료 근거를 따른다.
> [64차 귀속 검증](research/next-observation-design-2026-10-11.md#64차--후속-귀속-검증과-h1-계약) · [H1 첫 호가 가격 조건 계약](research/h1-entry-price-contract-2026-10-11.md): 소유 전환을 진입 시각으로 확정하지 않고 귀속 미측정으로 남긴다. H1 선택/규칙은 고정, 다음은 order_ready 전용 호가 배정 구현/검증이다. 소스 운영 미배포·새 관측 미예약, 실제 수익 검증 미완료. 아래 기록은 당시 이력이다.

> [62차 인계 재검토·다음 관측 설계](research/next-observation-design-2026-10-11.md): 첫4스캔 cooldown9건·5번째 신호3건·이후11건으로 정정하고, 추가 신호/LLM/효과0 단정을 철회했다. 소유 전환의 귀속 표시 P2는 재현·수정안 기록 단계이며 소스 미수정이다. 초기 상태/호가 배정/자본 조건을 포함한 다음 창과 판독 표5개, 가설 후보/기준을 제시했다. 운영83be6ac·매수 중지 유지, 새 예약/가설 검증 미착수. [61차 정정 정본](research/rejection-retry-policy-2026-10-11.md).

> [64차 LLM 판단 재생 기록](research/llm-decision-record-2026-10-13.md): 판단 ID↔신호 ID↔시도 ID, 공통 입력 원문·역할 지문·요청/실제 모델·비용·예산 토큰·결과·사용 가능 시각을 월별 JSONL 로 기록. 모델·규칙 불변, 운영 미배포. [계좌 기준선 결측 상태](operations/account-baseline-status-2026-10-13.md).

> [58차10월8일 신호 관측 결과](research/current-engine-signal-result-2026-10-10.md): 봉인/누락0·9스캔279후보/신호3·주문준비0. 손실제한/수량병목과선정원천을분리하고평가시각오류를수정했다. 비교쌍0·순손익null,운영1d45704·매수중지유지·새예약없음. 아래는당시이력이다.

> [57차10월8일 신호 관측·용량·배포 PDS](operations/entry-capture-installation-2026-10-08.md): 용량/독립 리뷰/로컬·CI·운영 검증 후1d45704 배포·10월8일08:55 일회 활성화 예약 완료. 10:00~10:50 스캔·11:10 종료, 매수 중지·설정·기존 자료 보존.

> [56차 실제 신호 시간대 경제성 관측](research/current-engine-signal-window-2026-10-07.md): 여러 스캔의 전체 후보·진입 조건과 신호별 KIS 호가/자본을 연결하는 명시 계약. 로컬 구현·검증 완료이며 운영 미배포·새 날짜 미예약. 다음은 고정 프로필과 전체 유량 검증이다.

> [55차10월7일 실제 관측 결과](research/current-engine-capture-result-2026-10-07.md): 양쪽 정상 종료·엔진누락0·첫3후보 점수85 미달·토스 첫 호가/900초 자료 확보. 장중48시장차단/18자동진입점검/신호2를 분리했다. 다음은 실제 신호 시간대·자본/비용을 포함한 경제성 관측 설계이며 신규 예약은 없다.

> [54차 장중 진입 경로](research/current-engine-intraday-coverage-2026-10-06.md#54차-plan--고득점-이후-실제-신호차단을-추적한다): 추격 차단147회/13종목·신호10회와 후속 매수 중지8회/수량 0으로 종료 2회를 대조하고, 실제 신호 시간대·자본 제약을 다음 경제성 관측 조건으로 분리했다. 운영/예약 변경 없음.

> **53차:** [첫 스캔과 장중 후보의 차이·20:30 원장·내일 분석 절차](research/current-engine-intraday-coverage-2026-10-06.md). 장중66개 로그 중65개에75점 이상 후보가 있었으며, 다음은 고득점 이후의 실제 탈락 위치다. 내일 첫 스캔 품질 관측과 순수익 평가의 자료 요건을 구분했다. 운영·예약 변경 없음.

> **52차20:03 설치·예약 확정:** [결과·다음 세션 인계](operations/entry-capture-installation-2026-10-07.md#see--2003-설치예약-확정).10월7일08:55 활성화·09:15~09:33 관측은 서버 타이머가 수행한다. 현재 엔진4384b28/PID504793·매수 중지 유지, 토스inactive·수집 예정. 로컬/CI4449 passed/2 xfailed, 실제 설치rc0·과거 원본/보존 예약 보존. 아래는 당시 이력이다.

> **52차:** [10월7일 관측 설치/예약 정본](operations/entry-capture-installation-2026-10-07.md) · [실제 점수61의 구성과 선정·진입 불일치](research/current-engine-selection-diagnosis-2026-10-06.md). 현재 엔진4384b28·매수 중지를 유지하며 새 날짜의 완결 관측을 준비한다. 실제 설치/예약은 정본의 실행 결과, 수익성은 별도 검증을 따른다. 아래는 당시 이력이다.

> [51차 19:00 복구 배포 확정·다음 PDS](operations/capture-execution-2026-10-06.md#see--51차-복구-배포-확정): 운영 `4384b28`/PID504793, CI·배포 각각4296 passed/2 xfailed, 실행 원장 ready·로컬 pending0·매수 중지/설정/원본 보존. 별도 토스 불변 release 미교체·오늘 관측 불완전은 유지한다. 다음은 새 관측 준비→선정 점수 근거→진입 가설1개와 실제 비용 후 검증이다. 아래 진행형·예정 표시는 각 당시 이력이다.

> [51차수신 결함 수정·선정/진입 다음 검증](operations/capture-execution-2026-10-06.md#51차--수신-결함-수정과-종목-선정진입의-다음-검증): KIS PRICE 배치 전달, 토스 일시 조회 실패/고정 진단, 배포 umask 권한 fixture를 보완한다. 코드·프로세스 검토와 실제 배포는 정본의 결과를 따른다. 관측7후보 점수 탈락을 임계값 완화 근거로 삼지 않는다.

> [50차10월6일 실행 결과·배포 복구](operations/capture-execution-2026-10-06.md#50차--10월6일-실행-결과와-배포-복구): 예약 배포는 테스트 수집 경로 오류로 롤백, 관측은 불완전 종료. 사용자 후속 승인으로 원인 수정·검증·별도 재배포와 선정/진입 분석을 진행한다. 아래 예정 상태는 당시 이력이다.

> [49차 P1 후속 리뷰·수정 검증](reviews/codex-claude-followup-review-2026-10-03.md) · [관측 보존과 배포 예약 갱신](operations/capture-execution-2026-10-06.md#49차--p1-수정과-관측-후-배포-예약-갱신): 거래 복구/날짜 파일 보존, 비차단 장부 확인, 기동 현금 검증과 최종 BUY 가드. 운영 `2645820`·관측 입력 유지. 아래 단계별 운영 상태는 당시 이력이다.

> [47차 엔진 즉시 배포·검증·다음 PDS](operations/capture-execution-2026-10-06.md#47차-do--see--10월2일-일반-모드-배포):10월2일22:17 일반 모드df1 적용·재시작·실행 원장/DB 점검 완료.10월6일08:55 예약 재개·미수집 확인 완료, 매수 중지·기존 설정 유지. 실제 관측/수익 비교는 예정.

> [46차10월6일 설치·예약·인계](operations/capture-execution-2026-10-06.md): 명시 승인 후 비활성 설치·미래 예약·날짜별 보존 분리·실제 검증 완료. 현재 봇/설정/매수 중지 유지, 실제 기동·관측·수익 비교는 예정. 아래 단계별 상태는 당시 이력이다.

> [45차10월6일 v4 관측 준비](operations/entry-capture-installation-2026-10-06.md): 후보 선정·진입 탈락 근거를 확보할 고정 입력, 전체 저장 구간 용량, 적용 범위와 실패 절차. 운영 미설치·미예약·미수집.

> [44차 KIS 실제 비용 기준·개선 우선순위](research/current-engine-observed-cost-next-step-2026-10-02.md): 직접 읽기 전용 조회·내부 대사 이후의 현재 상태와 다음 가격 비교 조건. 계좌 상세는 Git 밖에 보존하며 전체 TWR/수익성 승격은 별도다. 아래 원본 위치 요청은 이전 단계의 기록이다.

> [43차 계좌 입력 대사·다음 관측 창 검산](research/current-engine-next-evidence-2026-10-02.md):17/18분의 용량과 실제 첫 후보·진입 호가 시간 조건을 분리한다. 원본 위치 확인이 다음 실제 행동이며 새 관측/전략/운영 변경은 없다.

> [40·41·42차 관측 용량·오류 진단·계좌 순손익 PDS](research/current-engine-capture-and-account-pds-2026-10-02.md): 합성 용량 검증, 명시v4 KIS 원문 없는 오류 범위, 읽기 전용 순손익/TWR과 코드·처리 흐름. [계좌 입력/실행 절차](operations/account-net-return-input.md). 실제 성과와 운영 적용은 별도다.

> [39차 실제 진입 단계·원인 보고·PDS](research/current-engine-integrated-pds-2026-10-02.md#39차-plan--선정에서-진입까지-실제-평가-근거): 명시 v3에서 전체 후보의 실제 검사·탈락·미도달을 기록한다. [설계](superpowers/specs/2026-10-02-entry-gate-trace-design.md), [계획](superpowers/plans/2026-10-02-entry-gate-trace.md). 로컬 구현이며 실제 관측·계좌 수익 증거는 별도다.

> [38차 실제 관측 품질·진입 병목·다음 PDS](research/current-engine-integrated-pds-2026-10-02.md#38차-plan--첫-실제-관측의-품질과-진입-병목) · [집계 근거](research/current-engine-capture-quality-2026-10-02.json): 후보9개·신호0, 토스 조기 종료와 KIS 완전성 실패를 확인했다. 조기 종료 보고를 수정했고 순수익은 미확정이다. 우선 진입 탈락 사유·관측 기간·오류 범위의 근거를 확보한다.

> [37차 체결 identity·장부 commit와 통합 흐름](research/current-engine-integrated-pds-2026-10-02.md#37차-plan--체결-identity와-장부-commit) · [설계](superpowers/specs/2026-10-02-journal-durability-design.md): 모든 식별 KR 체결 증분의 멱등 기록, 실제 DB transaction 완료와 보호 청산을 연결한다. 운영 미배포이며 실제 계좌 대사·baseline·수익성은 별도 검증한다.

> [36차 복구 증거 대사와 통합 흐름](research/current-engine-integrated-pds-2026-10-02.md#36차-plan--복구-증거의-읽기-전용-대사) · [입력/실행/판독 절차](operations/execution-recovery-evidence.md): 독립 원장·증권사 원주문·거래 장부·잔고의 누락/중복/차이를 읽기 전용으로 보고한다. 실제 계좌 대사·보류 해제·순수익 검증은 남아 있으며 운영/예약은 보존한다.

> [35차 영구 실행 원장과 재시작 경계](research/current-engine-integrated-pds-2026-10-02.md#35차-plan--영구-실행-원장과-재시작-경계): 주문/체결/적용/후처리의 저장 구분, crash 보류와 보호 청산 예외, 정상 종료 fence. 과거 자동 복구·계좌 순수익은 미완료이며 운영/예약은 보존한다.

> [34차 취소 체결·장부 대사와 통합 흐름](research/current-engine-integrated-pds-2026-10-02.md#34차-plan--취소-뒤-체결-누락과-재시작-복구-상태): 취소 후 최종 수량 관측, 주문/단계 소유권과 부분 재주문 잔량 보존, 명시 ODNO 장부 비교. 재시작 자동 복구·정정 계보·거래소 시점·계좌 순수익은 남아 있다. 운영/예약/매수 중지 보존. 아래는 단계별 이력이다.

> [33차 잔고·지연 체결과 통합 흐름](research/current-engine-integrated-pds-2026-10-02.md#33차-plan--잔고와-지연-체결의-중복-반영): 로컬 처리 중 덮어쓰기 방지, 적용 확인 뒤 순서대로 청산·기록, 현금0/누락 구분. 취소 직전 미관측 체결·수동/재시작·거래소 시점 대사와 실수익 검증은 남아 있다. 아래는 단계별 이력이다.

> [32차 코드·프로세스 통합 PDS](research/current-engine-integrated-pds-2026-10-02.md): 평단·체결 조회·성과 계산 수정, 고정 기간 통계, 선정→주문→체결→청산→계좌 평가 흐름과 남은 우선순위. 수량 대사와 실제 순수익 검증은 미완료다.

> [31차 같은 진입의 초기 손절 효과](research/current-engine-exit-comparison-2026-10-02.md): 초기 손절의 손실 방어와 반등 기회손실을 같은 진입/수량/비용으로 대조한다. 전체 경로 검증과 후보 분모를 보존하는 오프라인 진단이다.

> [30차 원천 상태·중복 입력 진단](research/current-engine-source-status-2026-10-02.md): 명시 v2에서 스캔 전체 원천의 실패/부분/빈 결과/캐시/불명을 구분한다. 현재 시도와 과거 후보 근거를 분리하며 운영/예약에는 미적용이다.

> [29차 선정 점수 보존·근거 기록](research/current-engine-selection-basis-2026-10-02.md): 원천 캐시 오염 수정과 선택적 후보별 계산 근거. 새 수집 v2만 활성화 가능하며10월2일 기존 예약/운영에는 미적용. 실제 수익 기여는 아직 미확정이다.

> [28차 호가 근거 연결·연구 기준](research/current-engine-session-evaluation-2026-10-01.md): 오프라인 연결 도구와 후속 가격 진단 기준을 추가했다. 실제 장 상태 원천·계좌 수익은 미확보이며10월2일 예약 대상은 그대로다. 아래 표시는 단계별 이력이다.

> [27차 남은 평가 PDS](research/current-engine-remaining-pds-2026-10-01.md): 합성 분석 경로와 비용 민감도 검증 완료, 공식 반도체 제외 자료는429로 부분 확보에 그침. 실제 관측·계좌 총수익은 미확정이며26차 설치·10월2일 예약을 보존한다. 아래 단계별 최신 표시는 당시 이력이다.

> [26차 첫 관측 실행 기록](operations/capture-execution-2026-10-02.md): PR #119 병합·토스 비활성 설치·10월 2일 08:55 KST 단발 예약 완료. 현재 봇/매수 중지/설정 보존, 실제 기동·관측·수익 비교는 예정.

> 25차 설계 이력: [첫 관측 설치안](operations/entry-capture-installation-2026-10-02.md) · [10월2일 고정 입력 제안](research/current-engine-capture-proposal-2026-10-02.json). 전체 후보 보존/상위3개 관측·1회 시작을 로컬 검증한 당시 기록이다. 운영 identity/승인/release 결합과 비활성 설치·예약은26차에서 완료했다. 현재 실행 상태는 위26차 실행 기록이 정본이며 아래 단계별 표시는 이력이다.

> 에이전트 참조용 구조화 문서. 개발/분석 시 카테고리별 참조.
> 24차 구현 이력은 [선정·진입 규약](research/current-engine-selection-entry-protocol-2026-09-30.md)과 [평가 기록](research/current-engine-evaluation-2026-09-30.md) 참조. 토스 승인·접속·후보 전달·종료 저장 경로를 연결한 단계이며, 실제 기동/자료 확보/비용 후 수익 비교의 최신 상태는26차 실행 기록을 따른다.

## 문서 목록

### Architecture (아키텍처)
- [system-overview.md](architecture/system-overview.md) — 실행 수명주기, KR 이벤트 경로와 US 직접 스케줄러 경로, 설정·스케줄·상태 경계
- [harness-evolution-design.md](architecture/harness-evolution-design.md) — 하네스 엔지니어링 적용 설계 (2026-08-10, Claude+Codex 협업): weakness_miner·기각 후보 원장·Wiki ACE 격상·편집 표면 정책·4페이즈 로드맵

### Strategies (전략)
- [kr-strategies.md](strategies/kr-strategies.md) — KR 6개 전략: SEPA, RSI2, Theme, Gap, Strategic Swing, Core (스코어링, 가드, 사이징)
- [us-strategies.md](strategies/us-strategies.md) — US 3개 전략 + 시장체제 + 크로스검증 6규칙

### Risk (리스크)
- [risk-and-exit.md](risk/risk-and-exit.md) — 리스크 한도, 크로스검증 9규칙, 분할익절, ATR 동적손절, 포지션 사이징

### Evolution (진화)
- [evolution-system.md](evolution/evolution-system.md) — 3계층 메모리, Trade Wiki, 전략 진화, 일일 복기, 품질 검증, 거래 원칙

### Operations (운영)
- [09-23 제한 운영 릴리스](operations/release-2026-09-23.md) — PR #90·비대화형 인증 수정, 전체 엔진 승격 제외, 검증/배포 원장과 후속 health·경보/누적 성능 경계
- [퇴역 작업공간 archive/복구](reviews/retired-workspace-archive-2026-09-20.md) — 상시 main·engine 2개, dirty/index/objects 보존·53개 검증·원격7 태그·Claude 인계
- [Claude 마이그레이션 인계](operations/claude-migration-handoff-2026-09-20.md) — 단일 engine 개발선·C4 완료/운영 미승격·다음 B2/B3 Plan→Do→See·모델/검증/금지 경계
- [toss-shadow-runtime.md](operations/toss-shadow-runtime.md) — 별도 Toss 서비스(09-22 18:00 grant 만료, 현재 inactive)·승인 만료/재시작 금지·원장/health 경계(기존 거래 봇 Toss OFF)
- [local-development.md](operations/local-development.md) — WSL2 기반 Claude Code·Codex 로컬 개발환경 구성과 안전한 PR 흐름
- [github-quality-gate.md](operations/github-quality-gate.md) — PR 자동 검증과 `main` 브랜치 보호 운영 절차
- [lightsail-deployment.md](operations/lightsail-deployment.md) — 수동 승인 배포, 상태 확인, 자동 롤백 절차
- [runbook.md](operations/runbook.md) — 봇 관리, 코드 변경 프로토콜, 트러블슈팅, 캐시/로그 위치
- [monitoring-checkpoints.md](operations/monitoring-checkpoints.md) — 변경 적용 후 검증 체크포인트 (시점·전략별)
- [virtual-office.md](operations/virtual-office.md) — 가상 오피스(`/office`) 픽셀아트 시각화: 역할 매핑, 상태 API, 재빌드

### Research (리서치)
- [현재 엔진 평가 기준과 증거](research/current-engine-evaluation-2026-09-30.md) — 버전별 표본·체결 품질 의미 정정·가격/총수익 구분·5구간 반도체 제외/시장 폭 진단·다중 소스/실시간 경로별 기록 공백·최근9건 동일진입 청산 대조와 수익개선 우선순위, 09-30 후속 승인 정본
- [current-engine-selection-entry-protocol-2026-09-30.md](research/current-engine-selection-entry-protocol-2026-09-30.md) — 선정·진입 측정 초안, 가격 계산기·기본 비활성 관측·수신 기준 진단·사전 고정 보유시간의 bid 평가 사용법(운영 미설치)
- [실거래 KODEX200 초과수익 원장 설계](superpowers/specs/2026-09-28-kodex200-excess-return-ledger-design.md) — 설계 A(서면 설계만): DB 왕복 포지션 × KODEX200 동일기간, 비용 차감 초과·손절 클립 행을 20:30 에 DB 전체 재계산 스냅샷 + 일별 요약 이력으로 누적, 기존 exporter·canary 식 재사용, 자동 판정 없음
- [exit-policy-ab-2026-09.md](research/exit-policy-ab-2026-09.md) — 청산(ladder/channel)×보유(current/extended)×사이징(nominal/risk) 2×2×2 백테스트 A/B (2026-09-13, 리뷰 권고 1~3 동시 검증): 포지션 단위 R·PF·WF 3구간·KODEX200 초과, 승자 셀과 게이트 경유 권고 파라미터
- [long-window-sepa-vs-kodex200-2026-09.md](research/long-window-sepa-vs-kodex200-2026-09.md) — 장기 구간(2019-06~2026-09) SEPA 단독 6셀, 포지션별 KODEX200 초과수익(원장 정의): 전 셀 평균 −0.79~+0.41%·|t|<1.7·top3 제외 ≤0, 연도·KOSPI 국면 분해에서도 유의하게 이기는 국면 없음 — 편향 유니버스에서도 **엣지 미입증**
- [risk-sizing-revalidation-2026-09.md](research/risk-sizing-revalidation-2026-09.md) — 위험 사이징 재검증 (2026-09-14, 계획서 T7): 미래정보 제거·유효 설정·예산 캡 조건에서 nominal/risk/고정 14% 대조군 6셀 오프라인 재실행 — 운영 게이트 조건 충족이나 대조군 동일 통과·parity 미해소로 **승격 보류**, 검증된 것은 노출 축소 효과
- [ai-trading-research-2026-08.md](research/ai-trading-research-2026-08.md) — AI/LLM 트레이딩 문헌 조사 (2024~26): 실증 유효/무효 구분, 적용 Top 5, 금지 6항 — 변동성 타게팅·DART 경보·conviction 부스트·검증 규율의 근거 문서

### Reviews (리뷰)
- [Codex 최근 작업(PR #119~#138) 교차 리뷰](reviews/codex-recent-work-review-2026-10-02.md) — 실행 원장 영구 보류·전량 SELL 차단 P1 수정, 식별 체결 저널 실패·무경보 보류는 미수정 기록, 10월 6일 적용본과의 관계
- [09-23 운영 관측 결함](reviews/operational-findings-2026-09-23.md) — EGW00215 중복 집계 정정·호출 주체/페이지 계측·매도 원인·benchmark 신선도·macro 경고, main 정합화와 운영 설치의 분리
- [브랜치 정리·운영 동기화](reviews/branch-consolidation-2026-09-20.md) — 병합 완료 ref/worktree 정리·복구 bundle·보존 목록·main 동기화/재시작0·미완 migration 배포 차단
- [toss-pr-integration-2026-09-17.md](reviews/toss-pr-integration-2026-09-17.md) — #70 병합·중복 #68 정합화, 배포/활성화 승인과 실제 실행 상태 분리
- [toss-runtime-2026-09-16.md](reviews/toss-runtime-2026-09-16.md) — 승인 기반 관측 런타임 Plan→Do→See·독립 리뷰·인수 근거/한계·운영 미활성화
- [Toss 런타임 교차 리뷰 프롬프트](reviews/prompts/toss-runtime-review-2026-09-16.md) — source `984dbdf`·main 기준 SHA·재현 범위를 고정한 외부 리뷰 인계문
- [toss-phase1-handoff-2026-09-16.md](reviews/toss-phase1-handoff-2026-09-16.md) — PR #68 중복 구현 교차 리뷰·보류·Codex 인계 당시 기록; 후속 구현은 위 런타임 보고서
- [toss-phase1-offline-2026-09-16.md](reviews/toss-phase1-offline-2026-09-16.md) — 토스 Phase 1 오프라인 구현 PR #67·최종 코드 리뷰·검증 원장·합성 CLI·실수집 전 사전점검(기본 OFF·운영 미배선·실자료 승격 아님)
- [toss-design-codex-2026-09-15.md](reviews/toss-design-codex-2026-09-15.md) — 토스 설계 `85a4266` 교차 리뷰·14개 보완 당시 기록(문서만); 후속 오프라인 구현 상태는 위 원장 참조
- [mcp-retirement-2026-09-15.md](reviews/mcp-retirement-2026-09-15.md) — 미사용 MCP 런타임 제거·직접 공급자/미획득 계약 보존, PR #63 `c9923bf` 22:37 배포·MCP 경고 0건 확인
- [codex-followups-2026-09-15.md](reviews/codex-followups-2026-09-15.md) — Codex 최종 독립 리뷰 후속·추가 P2 재리뷰 승인, UTC/KST 1022건 통과·PR #61 `82b5039` 21:56 배포·운영 관찰 원장
- [codex-remediation-2026-09-15.md](reviews/codex-remediation-2026-09-15.md) — 금일 커밋 교차 리뷰 R1~R8 후속 수정·검증·PR #58 `53ea967` 배포 결과(당시 인계 기록 보존, 후속 Codex 리뷰로 대체)
- [strategy-architecture-review-2026-09.md](reviews/strategy-architecture-review-2026-09.md) — 전략·아키텍처 종합 리뷰 (2026-09-13): 트랙레코드 실측(수수료 전 총손익 ≈ 0), 청산·회전·사이징·배분 구조 원인, 게이트 스택 가치(베타 혼동), 코드 건전성, 권고 8·금지 목록
- [remediation-2026-09-14.md](reviews/remediation-2026-09-14.md) — 리뷰 후속 수정·재검증 결과 보고 (2026-09-14): F1~F8 수정 원장(PR #33~#38, #40), 검증 SHA, 6셀 재검증 → **승격 보류**, 운영 미배포(de111b7)·canary 미시작, 잔여 advisory·다음 단계

### Integrations (연동)
- [토스 독립 관측 서비스 설계](superpowers/specs/2026-09-17-toss-observer-service-design.md) — 상세 승인: 기존 봇 무재시작·보유 종목 관측·단일 발급·추가 KIS0·고정 릴리스/승인
- [토스 독립 관측 서비스 실행 계획](superpowers/plans/2026-09-17-toss-observer-service.md) — 격리 병렬 구현·TDD·독립 리뷰·새 서비스만 설치/활성화
- [토스 독립 관측 서비스 검증 원장](reviews/toss-observer-service-2026-09-17.md) — 독립 리뷰·UTC/KST 각1809건 검증·별도 서비스 ON/초기 발급·장외 대기 및 남은 실관측 인수
- [토스 관측 Plan→Do→See 구현 계획](superpowers/plans/2026-09-16-toss-runtime-shadow.md) — 역할별 병렬 모델·파일/API 소유권·TDD·독립 리뷰·오프라인 인수
- [토스 승인 기반 관측 런타임 설계](superpowers/specs/2026-09-16-toss-runtime-shadow-design.md) — #67 보존·기본 OFF·별도 승인·추가 KIS 조회0·지속 원장/worker 설계, 구현/실관측 승인과 구분
- [external-apis.md](integrations/external-apis.md) — KIS, 토스 Open API(별도 제한 관측 — 09-22 grant 만료·서비스 inactive, 거래 소비자 미연결), pykrx, yfinance, Finnhub, Finviz, LLM(OpenAI/Gemini/Perplexity), Telegram, DART
- [토스 2차 데이터 소스 설계](superpowers/plans/2026-09-15-toss-securities-fallback.md) — T12 단계별 승인·KIS 돈 경로 유지·공식 필드 계약·향후 구현 인수 명세
- [토스 Phase 1 오프라인 구현 계획](superpowers/plans/2026-09-16-toss-phase1-offline.md) — 보안 토큰·조회 경계·정규화·합성 비교 4작업, 이번 범위와 미완 실자료 단계 분리

### Legacy
- [ROADMAP_AGENT_TEAM.md](ROADMAP_AGENT_TEAM.md) — 에이전트 팀 6-Phase 로드맵 (초기 설계)

## 에이전트별 참조 가이드

| 에이전트 | 우선 참조 문서 |
|---------|-------------|
| **trade-analyst** | kr-strategies.md, risk-and-exit.md, evolution-system.md |
| **market-analyst** | us-strategies.md (시장체제), external-apis.md |
| **strategy-advisor** | kr-strategies.md, us-strategies.md, evolution-system.md |
| **engine-monitor** | runbook.md, system-overview.md, virtual-office.md |
| **risk-auditor** | risk-and-exit.md, runbook.md |
| **param-optimizer** | evolution-system.md, kr-strategies.md |
