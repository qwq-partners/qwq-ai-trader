# 09-27 자율 후속 개발 — 진행·인계 원장

갱신: 2026-09-27 19:00 KST, runtime/health 후속에 이어 B1a 기본 구조 시험까지 개발 인수·feature 통합 완료. **전체 엔진 완료·운영 전환 보고가 아니다.**
사용자 요청은 18:00 KST 이후 복귀 전까지 승인 범위의 후속을 순차 진행하는 것이다.
일상적인 재확인은 생략하되 새 실행 권한이나 증거 없는 안전 게이트를 임의로 만들지 않는다.

## 범위와 현재 위치

- 통합 작업: `feature/owner-ticket-gate-20260926`, 통합 worktree
  `.claude/worktrees/owner-ticket-gate-20260926`.
- OS 최종 검증 코드: `9cf021f`. 문서 마감 및 확인된 원격 feature:
  `af098769d7095c5630bbaa7c98301794ad213fd5`. 실제 push와 ls-remote 대조를 완료했다.
  이 SHA를 공통 base로 후속 세 구현을 격리 작업트리에서 시작했다.
- 후속 통합 검증 코드는 `18a2196301fe6421192f409c2f87fd25481bc96d`다.
  두 전체 검증과 fresh broad 승인 뒤 이 코드를 feature에 push했고 `git ls-remote`로 정확히
  같은 SHA를 확인했다. 아래 마감 문서는 그 뒤 같은 feature의 별도 commit으로 인계한다.
  `af09876`은 앞 OS 단계의 원격 확인 이력이며 현재 소스 HEAD와 구분한다.
- runtime/health 마감 문서도 `be9196c5e9cff4e78e2cc3026191b656c35dfeb4`로 push·원격 대조를 완료했다.
  후속 B1a 계획 문서 통합은 코드/시험이 아니라 다음 부분 Do의 승인 기록이다.
- B1a 계획 `c88b107`·최초 RED 기록 `1e6de86`도 feature push·원격 exact 대조를 완료했다.
  후속 검증 후보 `29210dc`는 두 전체 검증 후 `e41f4859b2694d2ec4bfa81282fee84a4d35d2d3`로
  개발 통합했다. 후보와 통합 코드의 비문서 tracked 파일은 전부 diff0이다. 마감 문서 commit과
  최종 feature 원격 확인은 Git 이력 및 `.superpowers/sdd/2026-09-27-decoder-b1a/progress.md`를 따른다.
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
| runtime 등록 대조 | 최종60건·통합 결합610건·fresh broad 승인·전체 UTC/KST 각각6329 passed | 개발 인수 완료, 실제 registry 빈 상태·실행 허가false 유지 |
| cold source 소유권 | 준비 설계 한정 독립 승인·문서 통합 | native/decoder/소비자·독립 할당 관측 기준 미해결 |
| decoder 기본 구조 시험 | 독립 oracle/전이/actual mutant·critical 승인,198건·raw12·전체 UTC/KST 각6527 passed, e41f485 통합 | B1a 부분 인수만 완료; full B1·N4097·native/첫 cold RED는 미완 |
| health 재시도 목록 복사 | 독립 승인·M1 시험 보완 재승인·통합 전체 UTC/KST 각각6329 passed | 개발 인수 완료; bounded health/경보 배선과 별개 |
| health 일자 표시 일관성 | 독립 승인·통합 전체 UTC/KST 각각6329 passed | 실제 admission 무변경·요청 간 fresh 관측 유지 |

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

후속 `18a2196` 코드에서는 runtime 계약60건과 health 새 회귀4건이 추가됐다. 새 Astra/xhigh
broad는 필수 지적0, 문서 자문M1 보완 뒤 C0/I0/M0이다. 작업자 종료 후 root 단독으로
전체 UTC6329 passed/641.35s 및 KST6329 passed/643.67s를 직렬 실행했다.
각각 기존16 skipped·2 xfailed·4 warnings·실제 도구 exit0·격리0이다. 두900초 cap과 기존
env/plugin/공통 lock을 유지했다. compile-only545·비밀 패턴·diff·원 source/guard/성능 예외/
controller 지문을 확인했으며 실행 중 변경은 진행 체크 문서뿐이다.
최초 writer RED/첫 GREEN의 raw 공백은 별도 인수 원장에 그대로 남긴다.

이후 B1a는 독립 literal expected의 예약 parameter명·foreign operation 관측 공백을 고치고
재승인받았다. 최초 missing-chain RED부터 전이 구현·actual mutant 연결까지 여섯 커밋을
순서대로 보존했다. 작은198건과 raw12개의 의도한 slot/terminal 실패를 확인했고, 별도
Astra/xhigh 최종 critical 판정은 지적0이었다. root 단독 전체 UTC6527 passed/644.41s,
KST6527 passed/664.16s, 각각16skip/2xfail/4warnings·workload0/tee0/tool0·격리0이다.
후보코드 불변·compile550·비밀 패턴·보존 지문을 확인했다. 상세는 [B1a 원장](decoder-b1a-2026-09-27.md).
`double_write_restore`는 runtime 관측 한계로 보존하며 변이 kill 수에 더하지 않는다.
source108 call-phase0·qualified runtime0, 실제 거래 제품 경로 변경0이다.

역할은 bounded 계약 Terra/high, OS 수명 구현·아키텍처 Astra/high, 소비 경로 조사 Sol/high,
별도 critical 리뷰 Astra/xhigh로 나눴다. 요청 모델과 실제 metadata 입증을 구분하며,
actual model/effective effort 미노출은 미검증이다. 같은 공급자 독립 리뷰를 교차 공급자로
표현하지 않는다. root+최대3workers, 격리 worktree/파일 소유권/시험 lock을 유지한다.

## 다음 실제 작업

1. 완료한 OS/runtime/health 부품을 다시 구현하지 않는다. 검증 코드18a2196의 push/원격
   대조는 완료했다. 마감 문서까지 포함한 실제 branch/status·원격 SHA는 재개 때 대조한다.
   main 통합·운영 전환은 이 개발 인수의 일부가 아니다.
2. 완료한 B1a EMPTY cell 생성/chain/holder/unlink 시험을 반복 구현하지 않는다.
   [B1a 계획 §5](../superpowers/plans/2026-09-27-decoder-b1-structural-probe.md)에 남은
   CONT/end/lookup/publish/retire/finish/known_failure/dispose의 모든 중간 상태·fault
   전이표와 독립 인수 계획부터 확장한다. 작성자와 critical reviewer를 분리한다.
   N4097의2MiB/stream 강제 runner와 전수3회 예산은 별도 gate이며 현재 UNRUN이다.
3. B1a 부분 인수가 끝나도 전체 B1·CONT/semantic 교체·retire·대형 인수는 완료가 아니다.
   원 source/native 실행·첫 cold RED는 계속 별도 근거가 필요하다. 없는 실행기를 가정하거나
   skip/deselect/표본 축소·과거 성능 예외로 새 실패를 통과시키지 않는다.
4. cold 복구·실제 owner/writer/consumer 및 health/경보는
   [전체 전달 순서](../operations/engine-delivery-next-2026-09-27.md)의 별도 gate를 따른다.

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
