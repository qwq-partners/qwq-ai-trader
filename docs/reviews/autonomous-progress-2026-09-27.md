# 09-27 자율 후속 개발 — 진행·인계 원장

갱신: 2026-09-27 16:38 KST, runtime/health 통합 검증 중. **전체 엔진 완료·운영 전환 보고가 아니다.**
사용자 요청은 18:00 KST 이후 복귀 전까지 승인 범위의 후속을 순차 진행하는 것이다.
일상적인 재확인은 생략하되 새 실행 권한이나 증거 없는 안전 게이트를 임의로 만들지 않는다.

## 범위와 현재 위치

- 통합 작업: `feature/owner-ticket-gate-20260926`, 통합 worktree
  `.claude/worktrees/owner-ticket-gate-20260926`.
- OS 최종 검증 코드: `9cf021f`. 문서 마감 및 확인된 원격 feature:
  `af098769d7095c5630bbaa7c98301794ad213fd5`. 실제 push와 ls-remote 대조를 완료했다.
  이 SHA를 공통 base로 후속 세 구현을 격리 작업트리에서 시작했다.
- main·운영·주문·전략·위험 설정·KIS/Toss 자격·서비스는 이번 후속에서 변경하지 않았다.
  main 작업트리의 기존 `config/evolved_overrides.yml` 변경은 읽거나 편집하지 않고 보존했다.
- 기존 두 정확한 성능 관측의 개발 예외는 유지한다. 새 실패·timeout·수명/정합성 결함을
  예외로 확대하거나 기존 시험·임계값을 바꾸지 않는다.

## Plan → Do → See

| 단계 | 실제 진행 | 다음 완료 조건 |
| --- | --- | --- |
| 오프라인 시험 증거 | 개발 완료·독립 리뷰·feature push | OS 종료 및 native 자격과 구분 유지 |
| source 수집·격리 경계 | 개발 완료, UTC/KST 각6089 passed, source108 수집만 확인 | source call-phase0·qualified runtime0 유지 |
| 실제 OS 종료·회수 결속 | 독립 리뷰 보완·전체 UTC/KST 각각6265 passed·문서/push 완료 | 개발 범위 한정 유지 |
| runtime 등록 대조 | 독립 리뷰 보완·최종60건·한정 재승인 후 `aa4ab4c`까지 통합 | 기존 부품/health 결합·fresh broad·전체 검증 |
| cold source 소유권 | 준비 설계 한정 독립 승인·문서 통합 | native/decoder/소비자·독립 할당 관측 기준 미해결 |
| decoder 호환 선택 | consumer inventory·표현 선택 문서 독립 방향 승인 | 실제 bounded 구조/독립 관측 실현 가능성 설계; 첫 RED 여전히 보류 |
| health 재시도 목록 복사 | 독립 승인·M1 시험 보완 재승인·feature 통합 | 두 부품 결합201 passed, 전체 검증 대기; 경보 배선과 별개 |
| health 일자 표시 일관성 | 독립 승인·feature 통합 | 실제 admission 무변경, 두 부품 결합201 passed; 전체 검증 대기 |

controller 기존46건의 coordinator 재실행은21.68s/exit0/격리0이었다. 그 뒤 독립 리뷰가 발견한
부모 symlink 경로 교체·미완료 정리 중 receipt 읽기는 원 작성자의 RED3건으로 재현됐다.
최종 controller `995d59e`는 필수 두 지적을 닫고 독립 재리뷰를 통과했다. coordinator도
최종64 passed/24.90s/exit0/격리0을 확인한 뒤 통합했다. 전체 단계 인수는 아직 아니다.
순수 계약의 최종 coordinator 결과는 새73+기존36=109 passed/0.52s/exit0/격리0이다.

그 뒤 실제11개 tiny case의 결합 시험과 broad 보완을 포함한349건이 통과했다. 첫 전체 UTC는
unit이 host의 task count를 가정해 실패했다(1 failed/163 passed). 전체수집 진단에서4tasks를
확인하고, 제품 경계가 아닌 세 unit의 입력·금지 호출만 보완했다. 작성자 controller100건,
root 전체수집 선택11건과 독립 재리뷰를 통과한 `9cf021f`에서 전체 UTC6265 passed/641.79s,
KST6265 passed/639.81s, 각각16 skipped·2 xfailed·4 warnings·exit0·격리0을 확인했다.
이 새 실패는 기존 두 성능 관측의 예외에 포함하지 않았고 원문을 보존했다.

역할은 bounded 계약 Terra/high, OS 수명 구현·아키텍처 Astra/high, 소비 경로 조사 Sol/high,
별도 critical 리뷰 Astra/xhigh로 나눴다. 요청 모델과 실제 metadata 입증을 구분하며,
actual model/effective effort 미노출은 미검증이다. 같은 공급자 독립 리뷰를 교차 공급자로
표현하지 않는다. root+최대3workers, 격리 worktree/파일 소유권/시험 lock을 유지한다.

## 다음 실제 작업

1. 실제 producer→OS 관측→순수 consumer 결합, fresh broad 보완과 전체수집 unit의
   별도 보완·독립 재리뷰까지 통합했다. 고정된 후보의 전체 검증을 마감했다.
2. OS 단계의 문서·feature push를 마감하고 원격 SHA를 대조한다.
3. 승인된 runtime 계약을 RED부터 구현한다. 실제 registry는 빈 상태, 성공 판정도
   실행/native/운영 허가false이며 source 차단을 해제하지 않는다.
4. cold 복구의 첫 할당·부분 실패·소비자 인계를 설계한다. 기존 파서 후등록/hook는 해법으로
   간주하지 않으며 exact dict/list·canonical bytes·중복키·unknown field·실제 live identity를 대조한다.
5. 별도 확인된 health `restart_retries[*].intent_ids`의 alias를 명시 list 복사로 분리한다.
   read-only 관측 결과 수정이 내부 RAM을 바꾸는 문제만 고치며 clock/cooldown/주문·설정,
   기존 누적 상태 성능 게이트와 health/경보 배선의 미완료는 바꾸지 않는다.
6. health 한 호출의 두 `day_admission_closed` 표시는 첫 기존 필드 자리에서 얻은 값을
   공유한다. 실제 property/admission·KST 시계·오류 short-circuit와 요청 간 fresh 판정은
   유지한다. 위 두 health 후보와 순수 runtime 계약은 파일/인터페이스가 독립적이므로
   앞 단계 See 뒤 같은 base의 격리 작업트리에서 병렬 작성·독립 리뷰할 수 있다.

## 이후에도 남는 경계

현재 host의 버전이나 과거 local139를 qualified runtime으로 재사용하지 않는다. 실제 capsule의
원본/빌드/파일 closure·실행 격리·독립 자격 증거, source를 허용하는 별도 안전 실행 경로와
등록 신뢰가 필요하다. 설치·다운로드·새 외부 실행 권한이 필요한 단계는 현재 개발 권한으로
강행하지 않는다. full cold/decoder/consumer 수명과 모든 writer의 실제 owner 배선이 남아 있다.
따라서 개발 도구 통과는 전체 엔진의 main 통합·운영 전환 허가가 아니다.

상세 정본: [source 경계](source-proof-boundaries-2026-09-27.md),
[OS 진행](os-process-evidence-2026-09-27.md),
[runtime 계획](../superpowers/plans/2026-09-27-runtime-admission-contract.md),
[cold 준비 설계](../superpowers/specs/2026-09-27-recovery-cold-source-ownership-design.md).
실행 원문·독립 리뷰·최초 실패는 통합 worktree의 `.superpowers/sdd/2026-09-27-*`와
각 작성 worktree에 보존하며 Git에 운영 자료나 원시 자격정보를 넣지 않는다.
