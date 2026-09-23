# N3 읽기 전용 복구 진단 — Plan→Do→See

## 범위와 완료 상태

기준 개발선 `3f7a189`, 작업 `feature/recovery-diagnostics-20260923`.
제품은 신규 진단 모듈2개뿐이며 기존 runtime/health/writer/송신/설치 코드는 바꾸지 않는다.
신규 진단 모듈의 구현·독립 검토·오프라인 전체 검증을 완료했다. 정본 개발선에 제품/시험을
FF 통합했으며 아래 단계별 증거를 운영 설치나 거래 재개 승인으로 확대하지 않는다.
main 병합·운영 배포/재시작·실 API·주문·설정·Toss grant 변경은 이번 범위 밖이다.

## Plan

- [설계](../architecture/recovery-diagnostics-2026-09-23.md),
  [계획](../superpowers/plans/2026-09-23-recovery-diagnostics.md),
  [수동 절차](../operations/protection-recovery-diagnostics.md).
- 사전 read-only 감사: Sol/high 계약, Astra/high 위협 모델. native actual model과
  effective effort metadata는 노출되지 않아 미검증. 두 작업자는 코드 변경/실 API0.
- health의 clock 콜백과 restart_retries.intent_ids 공유를 확인하여 private adapter로
  계약을 좁혔다. 기존 health alias 자체를 이번 제품에서 고친 것은 아니다.
- 같은 spec commit `27cfef4`에서 격리 worktree 생성. 제품/자체 시험 Astra/high,
  별도 경계 시험 Terra/high, coordinator 실제 runtime 인수/통합. 파일 소유권 비중복.
- critical 최종 리뷰는 별도 Astra/xhigh, 교차 검토는 tools-off Opus5/xhigh로 범위 고정.
  N3 외부 한도는 새 범위2회×USD5/600초다. 이전 N2 예산/승인을 재사용하지 않는다.

## Do — 현재 확인된 증거

- 기준 focused `test_execution_p1_engine.py` + `test_execution_p1_producer_recovery.py`:
  **146 passed / 28.67초 / rc0 / 격리0**.
- coordinator RED: 모듈 존재 단언으로 **1 failed / 1.69초 / rc1 / 격리0**.
- Terra 초기 RED7건, fixture 교정 뒤 RED8건은 모두 모듈 부재 단언이다.
  초기 시험은 public owner.state 복사본에 손상값을 써서 실제 상태를 건드리지 않았다.
  독립 확인 후 private test RAM으로 교정하고 hostile/cyclic를 분리했다.
- 최초 제품 `be29a13`(통합 `00cca72`): 자체 focused18 passed. 제품 작성자의 증거다.
- 최초 독립 결합: **26 passed / 2 failed / 2.10초 / rc1 / 격리0**.
  실제 제품 결함은 recovery latch=True인데 버전 일치만으로 publication_consistent=True.
  다른 실패는 위 fixture 복사본 오류다. 두 실패를 테스트 약화로 숨기지 않는다.
- 1차 보완 `ec9f676`(통합 `c34c955`): 위 latch와 DTO/수치/배선/알 수 없는 source를
  보완했다. 새 경계 RED9→GREEN31, RED5→GREEN36. coordinator 결합 시험
  **211 passed / 31.79초 / rc0 / 격리0**(신규65+기존146).
- native 독립 리뷰가 custom `__dict__`의 조회 hook과 취소 자식 `command_ref` 범위
  대조 누락을 실제로 재현했다(**3 failed / 1.08초 / rc1 / 격리0**).
  dataclass와 Lock hook은 각각14/2회 실행됐고, 범위가 다른 취소 응답이 부모 종결을
  물려받아 보고서에서 사라졌다. 부작용과 증거 오판을 한 묶음의 '테스트 문제'로 숨기지 않았다.
- 후속 `9220eab`(통합 `4a62412`): 두 adapter에 exact dict/key 검증, 자식의 scope4축·
  parent_order_no·ODNO 검사, ref 부재는 미확정 유지. builder도 변조 DTO를 재검증한다.
  **RED14 failed/37 passed → GREEN51 passed / 2.38초 / rc0 / 격리0**.
- coordinator는 실제 gateway/owner fixture에서 겉보기 배선·핸들러 부재·종료 태스크
  세 경우를 추가했다(**3 passed / 2.05초**). candidate도 거래/설치 허가가 아니다.
- 추가 coordinator 재현은 값 동등성과 타입 안정성을 분리했다. 두 캡처 사이의
  datetime→동일 UTC 문자열, int1→boolTrue가 모두 stable=True로 통과했다
  (**2 failed / 0.96초 / rc1 / 격리0**). 다음 커밋에서 비공개 타입 정보 보존을 인수했다.
- 타입 보완 `3f8d4e4`(통합 `371a89c`): RED4→자체55 passed, coordinator 전체 신규
  **89 passed / 4.53초**. 이후 native delta 리뷰가 exact ZoneInfo.from_file의 임의 key
  객체 비교 hook·단일 문자열 상한 우회를 **2 failed / 1.13초 / 격리0**로 재현했다.
- `9935ec2`(통합 `62410d5`): key exact str/None 및 파생 scalar 예산을 검증한다.
  Opus B1~B3의 in-flight·건수 미평가·모드 미상도 보완했다.
  자체 **RED9 failed/71 passed → GREEN80 passed / 3.28초 / rc0 / 격리0**.
- coordinator 실제 인수 추가: paused SQL commit 중 latch/lock, 정상 전량 청산,
  실제 lifecycle ACK cancel + 명시적 합성 최종성, 정상 quote pending, 실제 비어 있지 않은
  counter 경로. clock fixture를 generator처럼 잘못 호출한 시험 오류1건은 fixture 주입으로
  교정했다. 정상 request-bound SELL의 reserved_planned_risk=None 때문에 생긴 추가
  실패는 보호 연결 오류가 아니었다. 실제 None을 단언하고 위험 증거 부족/건수 불완전을
  유지하며 링크·수량 오탐 부재를 별도로 단언했다. 제품의 None→0 변환은 하지 않았다.
- 최신 coordinator 신규 진단 suite: **120 passed / 7.28초 / rc0 / 격리0**.

## See — 독립 리뷰·전체 검증·개발 통합

native 독립 Astra/xhigh 재리뷰는 후보 `93bd744`에 **한정 APPROVE**다. 작성자가 아닌
리뷰어가 새로 실행한 진단 suite **83 passed / 4.75초**, 독립 재현3건 **3 passed /
1.01초**, 모두 rc0·격리0. product/tests가 해당 SHA와 같음을 확인했다. actual model/
effective effort는 metadata 미노출로 미검증이다.

이후 타입 변경 후보 `bb374ba`에는 위 ZoneInfo 지적으로 CHANGES_REQUIRED였고,
현재 후보 `881bcec`는 **native 한정 APPROVE**다. fresh **120 passed / 7.00초**와
독립 timezone 재현 **2 passed / 1.25초**, rc0·격리0. 과거 승인으로 새 diff를 승인한
것이 아니다.

Opus call1은 실제 `claude-opus-5`·요청 xhigh·tools0·child/runner rc0·terminal success,
**563.376초 / USD1.43796 / verdict CHANGES_REQUIRED**다. 입력77,702 bytes,
SHA256 `f5593461297c4118ce36d15356bfa9338ab350d20cf4fc93da92cb07eb53793c`.
실행 성공과 코드 승인을 구분한다. 보완/처분은 다음과 같다.

| 지적 | 처분/근거 |
| --- | --- |
| B1 진행 중 lock 사실 소실 | mutation_in_flight 추가. 실제 owner는 SQL 전 latch를 닫는다. 관측 미확인과 영구 결함을 구분하는 실제 paused-commit 시험 |
| B2 미평가와0건 혼용 가능 | counts_complete 추가. 불안정/미읽음/불충분/in-flight는 False. 구현 범위 밖의 최종성·A/C 등은 포함하지 않음 |
| B3 미읽음도 partial 설치 단정 | failed/volatile은 unknown. 안정된 지원 배선만 모드 판정 |
| F1 datetime 범위 | exact aware만 지원 유지, KST/naive/subclass/custom tz 시험으로 명시 |
| F2 상한 시험 | depth/node/단일·합계 text/int 상한과 파생 scalar·key 예산 회귀 추가 |
| F3 정상 반례 | 실제 clean·전량 청산·ACK cancel/명시적 합성 parent finality·pending quote 인수. 보호 reducer `_closed`가 전량 청산의 보호 행을 제거함을 확인. 임의 zero orphan을 정상으로 허용하지 않음 |
| F4 counter 모양 | 제품 `_skip_reason`/`_apply_outcome`의 실제 int 증가 경로 인수 |
| F5 version2^32 covert-channel 제안 | 채택하지 않음. 임의 count/time도 의도적 은닉 채널이 될 수 있어 임의 상한은 해결책이 아님. genuine capture의 고정 projection·유한256bit 유지; 조작자가 지어낸 DTO의 진실성 인증을 주장하지 않음 |
| F6 intraday source/핸들러 부재 | 일반 producer와 다른 source의 검증은 별도 소비자 범위 유지·문서화. MARKET_DATA 키 부재는 빈 배선으로 읽어 지원되는 partial 사실을 보존 |

call2는 후보 `881bcec`(제품 `62410d5`)에 **APPROVE, 전체 UTC/KST 및 native 종결 조건**이다.
실제 `claude-opus-5`·요청 xhigh·tools0·child/runner rc0·terminal success,
**413.726초 / USD1.13604**. 입력78,864 bytes,
SHA256 `c4bf9ab3f1919690ea834dd5ff198ca6f2973fd79c397b48a1480f6562d4eaaa`.
실행 없이 supplied source만 검토했다. 패킷의 테스트/실제품 발췌가 연속되어 전체 모듈의
수집 가능성을 확인할 수 없다는 한계를 명시했다. 이 검토를 실행 시험으로 세지 않는다.
N3 총2회 실제 비용은 **USD2.57400**, effective effort는 요청값 외 확인 불가다.

call2의 비차단 후속은 다음처럼 처리한다. 제품 코드 변경은 없으며 시험/문서만 보강한다.

| 후속 | 처분 |
| --- | --- |
| F1 면제 종목의 보호 행 | 실제 apply_fill은 면제에도 register_position과 보호 수량 원장을 유지한다. 실제 BUY→exempt 정상과 합성 행 삭제→진단의 대조 시험을 추가. 면제가 상태 삭제를 허용한다는 추측으로 예외 처리하지 않음 |
| F2 candidate/순서 | candidate·핸들러 부재·종료 task는 이미 실제 gateway fixture 시험에 존재. 핸들러 순서만 틀린 negative를 추가 |
| F3 실규모 node 여유 | 승인된 실자료 없이 운영 상태를 읽지 않음. 캡처 비용/크기 측정은 운영 소비자 연결 전 미완 게이트로 유지 |
| F4 필수 루트 부재 | 필수 owner/protection mapping 부재는 계약 미지원으로 unavailable, 핸들러 부재는 지원되는 미완 배선이라고 설계에 명시 |

후속 시험/설계 커밋 `29c2f08`: **5 passed / 29 deselected / 2.17초 / rc0 / 격리0**.
별도 native Astra/xhigh가 두 시험의 실제 factory/체결/면제 계약을 재검토해 승인했다.
이 후속 리뷰는 pytest를 실행하지 않았으며 실행 결과를 중복 집계하지 않는다.
`881bcec` 대비 제품 변경0을 확인했다. 전체 검증 후보는 `29c2f08`이다.

검증 대상 tree는 아래와 같다. 이후 문서만 바뀌어도 이 세 tree가 같아야 아래 결과를
통합본의 증거로 사용할 수 있다.

- src: `7f994a918020c1f35517258a05b02e4414f4091c`
- tests: `8d3d9e39109557dc4163d52d33e406415423b9f8`
- scripts: `fc4cc08b6d605616c27b18974184d8a9e932902b`

모든 native/외부 작업자 종료 뒤 coordinator가 `29c2f08`에서 전체 시험을 직렬 실행했다.
이전 N2의5656 GREEN을 N3 결과로 재사용하지 않는다.

| 검증 | 실제 결과 |
| --- | --- |
| UTC 전체 `pytest tests` | 5778 passed / 기존2 xfailed / 4 warnings / 460.52초 / rc0 / 격리0 |
| Asia/Seoul 전체 `pytest tests` | 5778 passed / 기존2 xfailed / 4 warnings / 455.16초 / rc0 / 격리0 |

네 warning은 기존 pykrx resource API deprecated1건과 fork/thread 조합3건이다.
syntax/비밀 패턴 검사는 tests-skip 옵션으로 별도 실행했으며 시험 실행으로 세지 않는다.
최종 문서를 stage한 상태의 문법/비밀 패턴 검사는 rc0, staged diff 검사도 rc0이다.
패턴 검사는 도구가 지정한 private-key/AWS/GitHub token 형식에 한정하며 저장소 전체의
자격증명 부재를 보장하지 않는다. 기존 CLAUDE 민감행은 출력·재기록하지 않았고 header만
추가했다. main의 자격증명 후속 차단은 유지한다.

정본 `feature/engine-safety-design-20260917`은 clean `3f7a189`와 동일한 원격 head를
확인한 뒤 검증 후보 `29c2f08`로 FF 통합했다. 제품/시험 커밋은 원격 정본 개발선으로
푸시했다. 후속 문서 커밋에서도 위 src/tests/scripts tree의 동일성을 확인한다.
작업 브랜치의 별도 원격 복사본은 만들지 않아 원격 개발선을 중복하지 않는다.
루트 운영 체크아웃은 clean detached `afa6e1e`였으며 수정하지 않았다.
main 병합·PR 생성·배포·재시작·실 API·주문·설정·Toss grant 변경은0이다.

## 유지되는 한계와 다음 범위

1. exporter는 라이브 소비자0인 모듈이다. CLI/HTTP/알람 경로 배선·운영 설치는 미완.
2. 두 표본 일치는 atomic/ABA-free/durable store/broker 최종성 보장이 아니다.
3. 성공 설치 receipt가 없어 attached_candidate이며 installation_verified=False다.
4. A stale/C abandoned의 종목별 지속 증거 부재를 RAM 합계/0으로 대체하지 않는다.
5. 다음은 legacy 유령 경보를 만들지 않는 관측 전달과 운영 소비자 인수다. 장외/무보유/
   closing/종료·손상/volatile/unknown을 구분하는 계약부터 작성한다.
6. 기존 health retry alias 교정, append-only 크기에 따른 캡처/보호 sweep 비용 측정,
   기존 설치 차단·전체 C/F/G/R·공식 최종성·초기 인계는 별도 잔여다.
7. PR #90의 운영 관측 수정은 별도 미배포 후보다. N3 성공으로 운영 반영됐다고 말하지 않는다.
