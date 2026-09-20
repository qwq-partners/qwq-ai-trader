# QWQ AI Trader — 기술 문서

> 에이전트 참조용 구조화 문서. 개발/분석 시 카테고리별 참조.

## 문서 목록

### Architecture (아키텍처)
- [KR 주문·체결 안전성 상세 설계](superpowers/specs/2026-09-17-engine-execution-safety-design.md) — 사용자 승인; 경제/보호·core 큐 검증, 운영 경로 설치/전체 인수 미완
- [KR 실행 안전성 구현 계획](superpowers/plans/2026-09-17-engine-execution-safety.md) — Plan–Do–See, 작업 소유권·단계별 검증·잔여 통합 조건
- [정오 레짐·보호 재생 고정 계약](superpowers/specs/2026-09-20-regime-noon-owner-contract.md) — C3 DTO·source/application·horizon·full 보호 재생 인터페이스, 운영 승인과 구분
- [장전 text diagnosis 고정 계약](superpowers/specs/2026-09-20-regime-morning-owner-contract.md) — C4 명시 기준선·원 window/호출 순서·성공일 dedupe·정책/표시 분리; 한정 인수 완료·운영 승인 아님
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
- [agent-routing.md](operations/agent-routing.md) — Codex·Claude 전역 배정, Plan–Do–See·통합 동시성·격리/권한 경계와 실제 로딩 검증 범위
- [퇴역 작업공간 archive/복구](reviews/retired-workspace-archive-2026-09-20.md) — 상시 main·engine 2개, dirty/index/objects 보존·53개 검증·원격7 태그·Claude 인계
- [Claude 마이그레이션 인계](operations/claude-migration-handoff-2026-09-20.md) — 단일 engine 개발선·C4 완료/운영 미승격·다음 B2/B3 Plan→Do→See·모델/검증/금지 경계
- [toss-shadow-runtime.md](operations/toss-shadow-runtime.md) — 별도 Toss 서비스 ON·승인 만료/재시작 금지·원장/health 경계(기존 거래 봇 Toss OFF)
- [local-development.md](operations/local-development.md) — WSL2 기반 Claude Code·Codex 로컬 개발환경 구성과 안전한 PR 흐름
- [github-quality-gate.md](operations/github-quality-gate.md) — PR 자동 검증과 `main` 브랜치 보호 운영 절차
- [lightsail-deployment.md](operations/lightsail-deployment.md) — 수동 승인 배포, 상태 확인, 자동 롤백 절차
- [runbook.md](operations/runbook.md) — 봇 관리, 코드 변경 프로토콜, 트러블슈팅, 캐시/로그 위치
- [monitoring-checkpoints.md](operations/monitoring-checkpoints.md) — 변경 적용 후 검증 체크포인트 (시점·전략별)
- [virtual-office.md](operations/virtual-office.md) — 가상 오피스(`/office`) 픽셀아트 시각화: 역할 매핑, 상태 API, 재빌드

### Research (리서치)
- [exit-policy-ab-2026-09.md](research/exit-policy-ab-2026-09.md) — 청산(ladder/channel)×보유(current/extended)×사이징(nominal/risk) 2×2×2 백테스트 A/B (2026-09-13, 리뷰 권고 1~3 동시 검증): 포지션 단위 R·PF·WF 3구간·KODEX200 초과, 승자 셀과 게이트 경유 권고 파라미터
- [risk-sizing-revalidation-2026-09.md](research/risk-sizing-revalidation-2026-09.md) — 위험 사이징 재검증 (2026-09-14, 계획서 T7): 미래정보 제거·유효 설정·예산 캡 조건에서 nominal/risk/고정 14% 대조군 6셀 오프라인 재실행 — 운영 게이트 조건 충족이나 대조군 동일 통과·parity 미해소로 **승격 보류**, 검증된 것은 노출 축소 효과
- [ai-trading-research-2026-08.md](research/ai-trading-research-2026-08.md) — AI/LLM 트레이딩 문헌 조사 (2024~26): 실증 유효/무효 구분, 적용 Top 5, 금지 6항 — 변동성 타게팅·DART 경보·conviction 부스트·검증 규율의 근거 문서

### Reviews (리뷰)

- [장전 진단·소비 경로 C4](reviews/morning-regime-owner-2026-09-20.md) — 실제 caller·schema 활성 경합·게시 읽기 보완, 신규53·UTC/KST4403통과·native/Opus 한정 승인; 전체 writer/운영 미완
- [정오·JSON LLM·보호 재생 C3](reviews/noon-regime-protection-replay-2026-09-20.md) — 독립 결함·원시각/결측 보완, 당시 신규79·UTC/KST4350통과·네이티브/Opus 한정 승인; C4는 별도 후속 보고, 전체 writer/운영 미완
- [실제 2분 레짐 owner](reviews/two-minute-regime-owner-2026-09-20.md) — 실제 caller·captured reads·등록/취소·typed source·baseline 경계 보완, 네이티브/Opus 한정 승인·당시UTC/KST4271통과; 후속 정오/LLM/보호 replay는 별도 C3 보고서
- [입력 소비 권한 후속](reviews/source-authority-followup-2026-09-20.md) — N1·C2b 지속 전파의 실제 Opus 한정 승인, 리뷰 I1/I2 보완·최종UTC/KST4124통과; 실제2분·정오/LLM/replay는 후속
- [Opus 리뷰 실행기 진단·수정](reviews/opus-review-runner-2026-09-20.md) — 360초 로컬 종료 원인·동일 입력692초 정상 완료. 실행기 한정 재리뷰 승인·신규53회귀·전체 KST/UTC4021통과·실제 smoke 확인; 엔진 Opus 수정 요청은 별도 후속
- [C2a Opus 후속 수정·인수](reviews/policy-generation-remediation-2026-09-20.md) — B1/B2/B4 재현·수정, 실제 Opus 한정 승인·UTC/KST 각각4053통과. 다음은 retained source 요청 경계와 지속 소비 권한; 운영 전환 승인은 아님
- [**B2/B3 단계 원장 (현재 진행 정본)**](reviews/b2b3-stage-ledger-2026-09-20.md) — request-bound qualification·최종 사이징 S1~S5 의 단계별 Plan/Do/See·재현 명령·리뷰 판정·변이 결과·이어받는 체크리스트·잔여·다음 진입 조건. 리뷰·인계는 여기부터
- [B2/B3 실행 계획·계약](superpowers/plans/2026-09-20-b2b3-request-bound-qualification.md) — immutable decision facts 계약(현재 경제는 snapshot, 판단 시점 값은 facts), 단계 S1~S5·파일 소유권·인수 조건·S1 리뷰 처분·S3 사전 확인
- [KR 실행 안전성 후속 Plan–Do–See](reviews/engine-execution-followup-2026-09-18.md) — 기반·정책 generation·지속 source 권한·실제2분 구간별 한정 승인. 정오·LLM·qualification/gateway·나머지 writer/설치·전체 인수·공식 증거는 잔여
- [KR writer 이행 세분 계획](superpowers/plans/2026-09-18-engine-writer-migration.md) — 계좌 lease/종료 drain→원시각·정책 publisher→기존 sizing/qualification·실제 큐→SAFE/USER/IPC·fill·sync/day; 운영 설치 미완
- [KR 실행 안전성 구현 중간 리뷰](reviews/engine-execution-safety-2026-09-17.md) — 기반·Task4b 실제 경제/보호/큐 검증과 운영 writer 전체 이행 미완을 구분; 공식 KIS 계약/잔여 인수
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
- [external-apis.md](integrations/external-apis.md) — KIS, 토스 Open API(별도 제한 관측 ON·거래 소비자 미연결), pykrx, yfinance, Finnhub, Finviz, LLM(OpenAI/Gemini/Perplexity), Telegram, DART
- [KIS 실행 증거 계약 조사](integrations/kis-execution-contract-2026-09-17.md) — 공식 legacy 현행 TR·연속조회·고정 출처와 최초 인계/취소 최종성의 미입증 범위
- [KIS 부족 증거 인수 목록](integrations/kis-execution-evidence-request-2026-09-18.md) — 공식 확인 질문·승인된 비식별 자료 범위·최초 인계/취소/정정 인수; 실 API/시험 주문 요청이나 증거 확보 완료가 아님
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
