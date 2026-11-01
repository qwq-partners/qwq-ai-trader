# E04 현행 GPT/Claude 오프라인 비교 계약

> 실행: `superpowers:subagent-driven-development`. 사용자 승인 범위는 계약 보완·실제 WSL 검증·커밋/푸시다.

**목적:** 현재 진입 독립 검토의 모델·시도·완료·비용을 일관된 의미로 판독할 기반을 만든다. 합성 계약 검사를 실제 모델 우위나 순수익 증거로 취급하지 않는다.

**기준:** main `1eb023a9`, 사전 감사 `36a44126`. 기존 비교기는 GPT/Gemini와 단독 승인/fallback 의미를 갖고 있으므로 역사적 입력·독자를 바꾸지 않는다. 현행 계약은 별도 버전의 순수 오프라인 검증기와 명시 파일 CLI로 표현한다.

**범위:** GPT Bull/Claude Bear의 요청 제어, 역할별 빈 응답 재시도, entry 패널 12초와 Bear 내부 11.5초, CLI→API 전송 순서, 실제 응답 신원, 두 검토 중 하나라도 사용할 수 없을 때 hold, 전송별 비용/결측을 검증한다. Team Bear의 19.5초/800토큰 계약과 혼합하지 않는다. 비용은 API 추정치·구독 표시 추정치·실제 추가 과금 여부를 분리하고 미상을 0으로 만들지 않는다.

**불변 경계:** 기존 worker/R1 OFF·Gemini 차단·실제 adapter/예산 정산 연결 없음. 설정·매수 중지·운영 checkout·예약·주문·배포·발송 변경 없음. 운영 DB/자격/로그 자동 탐색 없이 명시 입력과 합성 fixture만 사용한다.

## 작업 순서

1. **설계 확정:** Astra/high가 현행 소스와 역사적 독자 경계를 읽고 최소 스키마/API·실패 조건을 제안한다. coordinator가 범위와 인터페이스를 확정한다.
2. **계약·CLI 구현:** 같은 기준 SHA의 별도 feature worktree에서 Terra6.1/high가 새 모듈·명시 파일 CLI·합성 검사를 소유한다. 구형 스키마를 새 의미로 자동 승격하지 않는다. RED 근거 후 최소 구현과 관련 회귀 검사를 수행한다.
3. **독립 리뷰:** 구현자와 다른 리뷰어가 계약·거절/hold·비용·오프라인 경계를 검토한다. Claude는 명시 `claude-opus-5-5`/high와 실제 응답 모델을 확인하고 도구/MCP 없이 소스·합성 검사만 제공한다.
4. **실제 WSL 검증:** 사용자 WSL의 격리 worktree에 검토된 소스만 전달한다. `QWQ_VERIFY_PYTHON=/home/user/projects/qwq-ai-trader/venv/bin/python bash scripts/dev/verify.sh`를 선택/병렬/skip 없이 직렬 전체 실행한다. 문법·전체 검사·비밀정보·격리 결과를 고정 소스와 연결한다. AWS 부분 검사는 보조 근거다.
5. **인계:** 원장에 구현/독립 리뷰/WSL 근거와 NEXT를 갱신하고 커밋/푸시한다. 운영 적용이나 유료 비교를 완료로 표시하지 않는다.

## 수락 검사

- 정상 GPT/Claude 합의·불일치 정책과 역할별 retry/timeout/transport를 표현한다.
- 누락·거절·모델 불일치·제한시간 초과·비정상 입력은 승인으로 승격하지 않는다.
- CLI 실패 후 API 완료를 중복 최종 성공/중복 비용으로 세지 않는다. 알 수 없는 사용량·추가 과금은 null로 보존한다.
- 잘못된 버전·Gemini·과대 입력·비유한 수치 등은 명확히 거절한다.
- CLI는 입력 파일만 읽으며 엔진/LLMManager/예산 DB/네트워크를 호출하지 않는다.
- 역사적 비교 회귀 검사와 현행 소비자 검사를 유지한다. runtime worker의 기존 활성화 거부도 보존한다.

구현 결과와 검증 영수증의 정본은 [실행 원장](../../operations/execution-roadmap.md#e04-current-contract-20261101)이다.
