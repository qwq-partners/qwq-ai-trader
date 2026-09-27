# 전체 엔진 전달 — 다음 단계와 완료 판정

2026-09-27 KST. 이 문서는 다음 개발 선택을 위한 요약이다. 새 실행 권한이나 설치 승인이
아니며, 정확한 후보·시험·독립 판정은 [당일 원장](../reviews/autonomous-progress-2026-09-27.md)을
먼저 확인한다. 과거 운영 PID/SHA를 현재 운영 사실로 재확인하지 않았다.

## 최종 목적과 오늘 작업의 위치

최종 목적은 **현행 위험 한도를 지키며 비용 차감 후 KODEX200 초과수익을 검증할 수 있는
신뢰 가능한 거래 엔진**이다. 안전한 주문·체결·위험 원장과 최신 판단 입력이 먼저다.
개발 도구의 시험 통과나 에이전트 합의는 수익성, 실계좌 최종성 또는 배포 준비의 증거가 아니다.

오늘의 OS 종료 증거 도구는 "pytest 결과를 썼지만 프로세스가 실패하거나 자손이 남음"을
성공으로 오인하지 않게 한다. runtime 계약은 등록·관측의 불일치를 거부하는 순수 부품이며,
실제 등록부는 비어 있다. health 두 수정은 내부 목록 유출과 한 응답의 날짜 표시 상충을
고친다. 이들 개발 인수가 전체 owner 전환의 미완 기능을 대신하지 않는다.

## 후속의 실제 순서

| 단계 | 다음 산출물 | 아직 열지 않는 경계 |
| --- | --- | --- |
| 1. 검증 부품 마감 | runtime/health18a2196 인수 뒤 B1a 시험198·raw12·독립 critical·UTC/KST 각6527 완료, 통합 코드e41f485 | main/운영·실제 source/native 실행 |
| 2. native/source 근거 | exact runtime 원본·빌드/파일 closure·격리 profile·독립 관측/승인 및 별도 실행계획 | host 버전만으로 QUALIFIED, 합성 fixture의 실제 등록 |
| 3. cold 복구 소유권 | 최초 할당·부분 실패·중복 교체·취소·정리·소비자 인계의 독립 oracle와 인수 | 기존 파서 결과를 나중에 등록해 선소유로 주장 |
| 4. 실제 owner 연결 | 승인된 index/gate와 모든 commit/restore/writer/result drain의 단일 책임·버전 전파 | full scan 숨기기, history 삭제, stale snapshot의 current 처리 |
| 5. 누락 소비자 | intraday 선제 보호의 durable claim, entry policy 변경 전파, 경제 outbox의 실제 소비 계약 | 결정 행을 근거 없이 주문·delivered로 전환 |
| 6. 관측과 인수 | bounded capture/게시·같은 표본 공유·N6 shadow·전체 C/F/G/R·독립 broad | 일부 시험으로 trading_ready 강제·전체 배포 |
| 7. 별도 운영 판단 | main 차이 처분·실제 설치/롤백/재시작 인수와 당시 명시된 실행 범위 | 과거 운영 승인이나 이 문서로 현재 실주문 실행 |

2와3의 서면 설계는 병렬 준비할 수 있다. 하지만 런타임 자격이나 실제 할당/해제 관측이
없는 상태를 더 많은 합성 테스트로 해결한 것으로 세지 않는다. 설치·다운로드·native 실행
등 새 권한이 필요하면 필요한 대상과 근거를 특정해 요청하고 해당 실행은 멈춘다.

원 B1 실행계획 초안 d9d57e5는 독립 R1~R4 변경 요청을 받았다. 이후 da673af의 생성·chain·holder·
unlink B1a 부분 계획만 새 독립 reviewer가 승인했다. [B1a 원장](../reviews/decoder-b1a-2026-09-27.md)의
최소 subject→독립 RED→전이/변이→독립 critical→전체 UTC/KST 인수·feature 통합까지 마쳤다.
결과는 `B1A_PRIMITIVES_OBSERVED_ONLY`다. 이것은 ordinary Python
반환 후 slot 관측이며 원 native A, 전체 decoder, 최초 cold RED 또는 단계3 완료가 아니다.
승인된2MiB/stream 실행기와 대형 전수 관측 예산이 없어 N4097은 계획 단계부터 UNRUN이다.

## 지금 유지하는 결정

- 두 정확한 과거 성능 관측만 개발 예외다. 원시 실패·미실행6셀은 보존하며 새 timeout,
  수명 오류·불완전 정리·새 성능 실패를 자동 면제하지 않는다.
- 거래/잔고는 KIS, Toss는 조회/관측이다. API 응답 형태와 취소·최초 인계의 최종성 증거를
  구분한다. 증거가 없으면 미지원/시작 차단, MODIFY 증가분의 예약 없는 송신 금지를 유지한다.
- legacy/main 개선 사항을 개발 브랜치로 단순 덮어쓰지 않는다. dirty user 파일·동결 후보·
  최초 실패 원문을 보존하고 완료한 부품은 다시 구현하지 않는다.
- `trading_ready=False`, source108 call-phase0, qualified runtime0 상태는 실제 별도 근거가
  확보되기 전 유지한다. 빈 registry가 정상이라는 사실은 실제 실행 준비가 됐다는 뜻이 아니다.

## 다음 세션의 첫 행동

현재 branch/status·원격 SHA, 당일 원장의 마지막 완료 후보와 진행 artifact를 먼저 대조한다.
미완 항목마다 "구현 가능", "서면 설계만 가능", "외부 근거/권한 필요"를 구분하고 가장 앞의
실행 가능한 작업 하나를 선택한다. Plan→Do→See와 역할별 모델/effort, 독립 리뷰를 유지한다.
전체 suite는 모든 worker가 종료한 뒤 coordinator만 직렬 실행한다.

### B1a 마감 이후의 구체적인 첫 작업

1. [B1a 계획 §5](../superpowers/plans/2026-09-27-decoder-b1-structural-probe.md)의
   미완 항목부터 시작한다. CONT/end/lookup/publish/retire/finish/known_failure/dispose의
   모든 중간 상태와 fault 전후 보존값을 독립 전이표로 정의한다. 중첩 a/b/key/tag/nonfinite/
   partial disposal/WORK 변이 인수를 축소하지 않는다. 이 단계는 서면 설계이며 기존의
   검증된 EMPTY cell 전이를 다시 구현하거나 제품 decoder로 간주하지 않는다.
2. 작성자 Astra/high와 다른 critical reviewer Astra/xhigh로 계획을 검토한다. source와
   expected 작성자를 분리하고 허용 파일·원시 RED·예산·종료 조건을 명시한 뒤에만 다음 Do로
   넘어간다. 실제 모델/effort metadata가 없으면 unverified로 기록한다.
3. 대형 N4097 전수 시험은 별도 의존성이 있다. stdout/stderr 각각2MiB 강제·보존이 가능한
   승인 runner와 dedicated1+UTC1+KST1의900초 양립 근거를 먼저 확보한다. 현재의 tee나
   작게 나온 로그 크기를 출력 상한 증거로 사용하지 않는다. 없는 실행기 개발/설치는
   현재 B1a 인수 범위 밖이므로 먼저 별도 범위·설계·권한을 확정한다.
4. native runtime의 실제 자격은 위 합성 구조 시험과 별개다. 빈 registry/실행false를 유지하고
   근거 확보 이전에는 원 source/cold/실제 owner 실행으로 건너뛰지 않는다. 개발 예외 두 건은
   이 수명·정합성·실행 근거를 대신하지 않는다.
