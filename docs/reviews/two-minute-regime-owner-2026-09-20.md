# 실제 2분 레짐 owner — Plan–Do–See (2026-09-20)

## 범위와 현재 판정

기준 feature HEAD `0fdd073a8b5ff6d4668ebad8ee0d14b55393ad14` 위 실제 2분 수직 경로다. 앞선 [입력 소비 권한](source-authority-followup-2026-09-20.md)의 C2b 한정 승인 뒤 구현했다. **네이티브·Opus 한정 재리뷰, 추가 baseline 경계 수정, 최종 UTC/KST 전체4271시험을 통과했다.** 전체5단계나 운영 활성화 승인은 아니다.

KIS만 주문·잔고를 담당하고 Toss는 관측 전용이다. main/운영·실 API·주문·설정 변경0. `trading_ready=False`, 모든 MODIFY 미지원, 공식 최초 인계·취소 최종성 증거 부족을 유지한다. 운영 factory 설치나 수익성 검증은 이번 범위가 아니다.

## Plan

- Astra/high 구현자: 별도 `feature/regime-owner-two-minute-20260920` 워크트리의 owner·실제 scheduler·source seal·projection과 집중 시험.
- Terra/high 독립 인수자: 같은 기반의 별도 테스트 전용 워크트리. 실제 KIS adapter의 외부 HTTP만 fake하고 loop/limiter·cold reopen·day rollover·prepare/dispatch를 대조.
- 새로운 Astra/xhigh 경계 리뷰어: 별도 읽기 전용 제품 워크트리. baseline 경합·취소·VIX/clock 경계를 독립 재현하고 구현자와 분리해 재검증.
- 부모: captured-read 독립 시험·통합·전체 검증·문서. Opus5/xhigh는 도구 없는 독립 교차 공급자 정적 리뷰. 네이티브 모델은 요청 배정값이며 실제 모델/유효 effort 메타데이터는 별도로 노출되지 않았다.

실제 source/정책 계산 당시의 입력을 보존하고 완료 시 현재 권한과 비교한다. 기존 순수 산식·임계값·조건부 시계 위치·조회 수/순서를 유지하며 **2분 경로에는 ExitManager 파라미터 적용과 보호 replay를 추가하지 않는다.**

## Do

명시 supplied `RegimeBaseline` 등록과 `RegimeOwner` 설치를 추가했다. live adapter의 neutral/빈 pending을 자동 기준선으로 채택하지 않는다. baseline 등록 날짜는 이후 rollover/cold restore에서도 원 사실로 보존한다. sidecar 정본은 `entry_policy_effects.sidecar_active` 하나이며 별도 writable 복제를 만들지 않는다.

실제 scheduler는 설치된 owner로 한 회를 위임한다. 기존 initial60/final120, PRE/REGULAR gate와 두 지수 gather/기존 limiter를 보존한다. 두 지수의 원 출처·필수 OHLC·정합성이 확인돼야 계산하고, 결측/실패는 이전 정책을 보존하며 성공 하트비트를 남기지 않는다. REST의 receipt를 시장 as_of로 만들지 않는다.

기술 계산 → 전문가 score/consensus → source 완료 hook의 순서로 trend/sidecar/engine copy를 같은 commit에 기록한다. opening pending, mid1800초·complacency600초·expert600초, expert absence/error no-op을 유지한다. VIX는 원 accepted 값/시각과 최신 pending/실패를 구분한다. 만료/부재 refresh는 이번 trend 게시 뒤 tracked worker 하나에서 시작하며, 그 결과가 같은 tick의 추세를 재분류하지 않는다.

현재 owned snapshot은 외부 낙관 trend 대신 정본을 소비한다. 진입 prepare/dispatch의 실제 경계에서 최신 failed/pending source를 거부하고 기존 REGIME_PARAMS의 min-cash mapping을 사용한다. 아직 이행하지 않은 qualification/sizing 전체 소비자를 완료로 표시하지 않는다. installed raw writer를 차단하되 미설치 legacy 경로는 보존한다.

### 계산 시점 → seal 시점 경합

처음 계산에 읽은 값과 await 후 seal에 읽은 값이 달라질 수 있어 schema3를 추가했다.

- `capture_reads`는 동기 detached canonical JSON이다. `expected_reads_json=None`은 기존 schema1/2 동작을 보존한다.
- schema3는 원 expected reads와 실제 seal-time reads를 함께 기록한다. 불일치면 `stale/captured_reads_changed`, 완료는 기존 `stale/input_seal`로 hook0이다.
- 정책 ABA·source 변경은 거부하고 무관 owner commit은 허용한다. canonical JSON으로 bool/number 혼동을 차단한다. malformed 요청은 결과 저장 task 전에 거부하지만 SQL/게시 실패를 정상화하지 않는다.
- cold/history 검증은 각 read의 역사 cutoff·generation·원 source를 대조한다. 현재 소비 권한과 과거 accepted receipt를 혼동하지 않는다.

### 독립 리뷰 BR1·BR2

독립 첫 15시험은 UTC/KST 각각 **5 failed / 10 passed / 4.57초**, 격리0이었다. P2 두 종류다.

1. BR1: 첫 검증 뒤 version/day/동시 baseline이 변하는 정상 등록 거부가 result 실패 latch를 켜고 정상 종료까지 차단했다. 명시 admission 거부만 private marker로 전달하고 caller에 원 오류를 반환한다. SQL/lookup/publish 일반 예외는 계속 차단한다. idempotent mutate가 reducer를 생략한 경우도 post-await 원본문을 canonical 비교한다. account 직접 주입 대조는 도달 가능한 운영 버그의 별도 증명으로 세지 않는다.
2. BR2: 첫 index begin SQL 중 취소는 SQL을 drain했지만 durable terminal이 None으로 남았다. 추가 재현에서 expert begin 취소의 caller 지연/취소 오류 가림, commit 뒤 publication 대기와 closing/day-fence 경계도 확인했다.

`begin`의 private observer로 원 admission task를 받는다. runtime은 현재 같은 caller의 live parent token을 검사한 뒤 별도의 child token/task를 즉시 strong tracking에 등록한다. owner 취소 정리는 정확한 원 task와 원 durable ticket을 사용하며 재begin/재GET하지 않는다. caller CancelledError와 건강한 작업의 종결을 함께 보존하고, 실제 SQL 실패·finalizer prestart cancellation/failure는 계속 latch/종료 거부다. 이미 성공 terminal이 접수된 뒤 취소할 때 충돌하는 두 번째 terminal을 만들지 않는다.

추가 독립 13시험의 수정 전 결과는 UTC12failed/1passed10.81초, KST12failed/1passed10.72초였다. 실패 중 실제 행동6과 helper API 부재6을 구분한다. 최종 재검증은 원15+추가13 **UTC28passed5.71초 / KST28passed5.69초**, exit0·격리0, BR1/BR2 한정 승인·신규 P1/P2 0이다.

수정 뒤 최초 무변경 재실행은 1failed/27passed였다. 원 시험이 모든 결과 task를 기다린 뒤 pending을 기대했으나 새 정리 작업이 이미 cancelled terminal까지 마친 정상 결과였다. completion lookup에 별도 Event를 두어 terminal 저장 전 pending과 최종 cancelled/건강한 종료를 각각 검사하도록 시험 순서만 보완했다. 최종 계약을 약화하거나 xfail로 바꾸지 않았다.

## See — 실행 원장

| 증거 | 결과와 범위 |
| --- | --- |
| 최초 작성자26 + 관련13파일 | UTC285/23.72초, KST285/23.91초, 격리0. 이후 BR 결함 발견 전 후보이며 최종 승인 아님 |
| 부모 captured-read15 + 최초 작성자26 | 41passed5.39초, 격리0 |
| Terra 실제 호출점12 | UTC12/4.15초, KST12/4.04초, 격리0. dispatch에서 실제 `_snapshot`의 `regime_source_not_current`를 관찰; 준비된 attempt 불변·HTTP0 |
| 중간 전체 | UTC4165/246.33초, KST4165/245.75초, 각 기존xfail2·경고4·격리0. 독립12와 BR 수정/시험 추가 전 결과 |
| BR 수정 작성자41 + 관련13파일 | UTC300/22.98초, KST300/22.62초, 격리0 |
| 부모 수정 통합 집중 | 작성자41+captured15+Terra12=68passed9.14초, 격리0 |
| 독립 경계 최종 | UTC28/5.71초, KST28/5.69초, 격리0; 해당 두 결함 한정 승인 |
| Opus 수정 전 전체 | UTC4220passed/2기존xfail/4경고251.90초, KST4220/2/4 251.76초; 각각 exit0·격리0. 아래 수정 후보의 최종 증거는 아님 |
| 문법/비밀정보/diff | 신규 파일 명시 stage 후 verify(시험은 위 별도 전체 실행)·cached diff 검사 exit0 |
| Opus 교차 공급자 정적 리뷰 | 실제claude-opus-5/requested xhigh, 정상 완료862.624초·child0·$3.7377785. 변경 요청; input335737바이트, SHA `31264bc34133b524ef0676ada850d4821cf2e521a10e07e457cec87e90590ede`. 유효 effort는 미노출 |

테스트는 env-i·외부 API/운영 자격 미사용·임시 SQLite·실제 객체와 외부 경계 fake로 실행했다. 테스트의 임시 startup gate 허용은 dispatch 입력 차단 경로를 관찰하기 위한 제한된 monkeypatch이며 제품 trading_ready를 바꾸지 않았다. 기존 경고4는 pykrx deprecated1 + 의도적인 fork 경계 Python3.12 경고3이다.

첫 작성자 fixture에 임시 Path.home 주입이 빠져 한 실행에서 운영 cache 접근 **차단6건**이 있었다. guard를 끄지 않고 fixture를 고쳤으며 최종 실행은 격리0이다. 초기 import/fixture 오류나 이미 통과한 대조를 실제 결함 수리로 합산하지 않는다.

### Opus 지적 처분

- I1: 별도 source facade의 accepted index가 typed regime transition 없이 먼저 SQL에 저장되는 것을 실제 재현했다. no-hook/no-op/wrong-hook index3건 및 잘못된 VIX1건은 durable 오염4건이다. 별도로 기존 source가 기준선 등록 직전에 접수되거나 완료 직전 callback이 해제되는 경합2건을 재현했다. 새 accepted index만 exact 설치 facade/실제 bound reducer를 첫 task 전과 lookup 뒤 검사하며, 정상 거부 marker만 처리한다. terminal 부착 후 새 transition 한 건을 cold validator와 같은 함수로 저장 전에 검증하고 기존 root/history의 canonical 불변을 확인한다. VIX도 typed 값/원시각을 저장 전에 검증한다. 진짜 SQL/lookup/validator 오류는 계속 차단한다. 수정 후보의 최종 독립/전체 gate는 진행 중이다.
- I2: 리뷰 입력 생성 당시 원15+추가13 재실행은 미완이었다. 이후 실제 최종28개가 양 TZ 통과했고 terminal Event의 순서 수정도 위에 기록했다. 정적 리뷰가 지적한 구 pending 단정은 바로 그 수정 전 사본이다. 재리뷰에 최종 시험·diff·실행 근거를 공급한다.
- I3: history 전체 검증 비용은 알려진 미측정 운영 장벽이다. 이번 오프라인 slice의 정확성 승인과 구분하며, 200/500 이상의 실제 accepted 이력·rollover 부하 및 검증 복잡도 근거 없이 실제 루프를 활성화하지 않는다. watermark/이력 절단은 원증거 손상 위험이 있어 이 수정에 섞지 않는다.
- I4: 공급한 scheduler 구간 이후의 기존 `except Exception`이 failure heartbeat와120초 sleep을 이미 수행한다. 중복 catch를 바로 추가하지 않고 실제 owner 미설치/binding 오류의 루프 인수로 확인한다.
- Minor2/3: 실제 accepted 이력의 detached 사본에서 OHLC 역전과 False/0 payload를 해시 정합시킨2 RED를 재현했다. 원 SQLite는 변조하지 않았으며 정상 writer가 해당 값을 만든다는 주장도 아니다. 복원 계산에도 같은 OHLC 조건과 canonical 비교를 적용했다. Minor1의 미래 dependency 호출, Minor4의 기존 retained VIX 무기한 유지 정책, Minor5의 미완 factory/intraday writer 조립 조건은 별도 경계로 기록하며 임의 TTL/중립 대체/운영 설치를 추가하지 않는다.

추가 독립12시험은 UTC6failed/6passed3.90초·KST6/6 3.94초, 별도 경합6시험은 UTC2failed/4passed2.94초·KST2/4 3.75초로 원 RED를 고정했다. 부모도 전자는 I4대조5와 함께6failed/11passed2.78초, 후자는2failed/4passed3.31초로 실제 확인했다. 첫 I4 fixture 오류는 제품 결함으로 세지 않는다. generic seal 취소 대조와 실제 `refresh_trend` seal-SQL 취소→shutdown→cold terminal 대조도 별개로 구분한다.

작성자 신규19시험은 처음9 RED와 큰 정수의 `math.isfinite` overflow 추가1 RED, 처음 GREEN인 대조9건이다. overflow는 순수 숫자 검사에서만 ValueError로 정규화했으며 일반 task 예외를 포괄 처리하지 않는다. 동결 수정본 관련17파일 **UTC383passed35.05초/KST383passed34.91초**, 각 격리0이다. 전체/독립 재리뷰는 이 수치로 대신하지 않는다.

수정 후보의 부모 집중138시험은38.71초에 통과했다. 별도 Astra/xhigh 리뷰어의 관련271시험은 UTC66.62초/KST67.18초, Terra의 실제 seal/admission·scheduler11시험은 UTC3.64초/KST4.22초에 각각 통과했다(모두 exit0·격리0). 네이티브 최종 판정은 `APPROVE_THIS_SLICE`, 추가 important0이다.

이후 전체 실행은 **UTC1failed/4261passed/2xfailed/4warnings299.21초, KST1failed/4261passed/2xfailed/4warnings298.70초**, 각각 exit1·격리0이었다. 둘 다 `tests/dev/test_claude_review.py::test_complete_assistant_tool_content_and_error_events_fail_closed`의 `elapsed < 0.3`만 실패했다(0.364초/0.410초). 앞선 nonzero·정확한 오류 사유 검사는 통과했다. SIGTERM 시험 실패라는 최초 추정은 잘못이었으며 전체 로그로 정정했다.

독립 진단은 기존 runner/시험을 바꾸지 않은 격리3회 통과와 시작 지연 대조를 확인했다. runner 인터프리터 시작 전0.35초만 지연해 전체0.563초, fake child 준비 이후0.114초·정확한 `tool_violation` 종료를 관찰했다. 이는 원 시험이 프로토콜 반응과 무관한 시작 시간을 함께 재는 결함의 근거이며, 엔진 실패나 일반적인 flake 비율의 증거는 아니다. 시간 임계 확대/xfail 없이 인과적 종료 검증과 음성 대조를 보강한 뒤 전체 실행을 다시 해야 한다. 아직 전체 통과로 기록하지 않는다.

Opus 수정 범위 재리뷰는 **317.643초/child0/$1.310195**, 실제 `claude-opus-5`·요청xhigh·오류0·stderr0으로 완료했다. 입력181116바이트/SHA `f5e7e2a3766d8162b16f451e1dd681a48fe56a7d92cd002e77aadfa4440489b7`. 판정은 `APPROVE_THIS_SLICE`이며 I1/I2/I4·Minor2/3 보완에 동의했다. 별도 important 관찰은 index ticket의 admission이 명시 regime baseline보다 이른데 설치 후 올바른 writer facade로 완료되는 경계다. 리뷰어는 seal 내부를 공급받지 않아 도달성을 확정하지 않았다. 이후 독립 실제 공개 API 재현은 **UTC2failed/3passed3.56초·KST2/3 3.35초**, 부모도2/3 2.91초였다. admission3/baseline4, seal SQL6→complete SQL7에서 accepted+transition이 먼저 저장되고 게시·새 cold restore가 `regime_projection_conflict`로 실패했다. 현재 고수준 `refresh_trend`는 새 ticket을 만들며 정상이라는 대조도 보존했다.

이에 typed admission에2줄·append 검증에3줄을 추가하여 `admission_version > baseline_version`을 SQL 전에 대칭 검사한다. 공개 정상 거부는 기존 narrow marker, 예상 밖 append/hook 오류는 일반 치명 오류로 구분한다. 원 독립5와 작성자4는 **6RED/3대조→9passed2.86초**, 관련392시험은 UTC39.37초/KST39.21초에 통과했다. equality 대조2건 중 동일 version은 detached validator 시험이지 실제 같은 SQL version 등록 사건이 아니다. 이전 역사 전체 재계산이나 정책 변경은 추가하지 않았다. Astra/xhigh는 원9 **UTC6.00초/KST5.66초** 재실행 뒤 `ADDRESSED/APPROVE_THIS_SLICE`로 승인했다.

5줄 델타의 Opus 재리뷰도 **142.012초/child0/$0.53361**, actual `claude-opus-5`·요청xhigh·오류0·stderr0·`APPROVE_THIS_SLICE`로 완료했다. 입력55914바이트/SHA `423262eaa5f5ed32b239f1dc96778c776dd3a2a2cbc2928ec763818bf46268f5`. 도구 출력 한 구간260토큰이 잘린 발췌를 보존했으며 전문 보존으로 주장하지 않는다. 반환된 최종 판정은 명확하며, 네이티브 리뷰의 전체 보고서와 실제 시험은 별도다. 추가 의견은 다음과 같이 처분했다: append의 root 동일성 검사 전후 오류 라벨 차이는 저장 전 차단을 유지하므로 보류; detached 방어 검증과 공개 재현을 구분; 정상 거부/치명 검증이 공유하는 오류 문자열은 예외 유형/latch와 함께 해석. 기존 오염 checkpoint의 자동 수리는 범위 밖이며 예방 수정이다. 부모가 실제 `register_baseline`을 확인한 결과 기존 root의 동일 supplied는 원 version을 반환하고 다른 supplied는 거부하므로 새 version으로 재등록해 기존 transition을 범위 밖으로 만드는 정상 경로는 없다. 이 확인은 재등록에 대한 새 시험 실행을 뜻하지 않는다.

개발용 시간 시험은 금지 이벤트의 정확한 nonzero/사유와, 별도 continuation gate 이전에 wrapper가 종료하여 자식이 후속 동작을 못 함을 확인하도록 바꿨다. 시작 지연 대조는 통과하고 검증 훅을 끈 대조는 정상 oracle을 만족하지 못한다. 시간 확대/xfail·제품 runner 수정은 없다. marker가 자식 회수까지 증명한다는 과도한 표현은 실제 wait 생략 대조 뒤 정정했다(미회수 자체는 미입증). 전역 interpreter 교체도 함수 인자로 제거했다. 정확한 최종 시험 SHA `c1b936ac43f768bbed8bbff12fdad4dda063f95f0b9ef8b7a0cba80fde933af7`에 대해 Sol/high 재리뷰는 `APPROVE_THIS_SLICE`; UTC/KST 집중 각1passed5.65초다. 엔진 guard 전·시험 이전 후보의 전체 UTC4262passed204.87초는 중간 결과로만 보존한다.

최초 리뷰 본문이 이어쓰기 문장으로 시작하더라도 마지막 통합 판정은 변경 요청으로 명확하다. 실행 성공을 코드 승인으로 해석하지 않았다. Opus는 시험을 실행하지 않은 정적 리뷰다.

### 현재 동결 지문

| 파일 | SHA-256 |
| --- | --- |
| regime_owner.py (baseline scope 보강 후보) | 6446d76b74437a4c2d97537ca56a604fbe38b7d7e14167eae99c630ff0b39152 |
| runtime.py | 9e2e7aa90239ca508166f9c5bf0c76f5f754b59ce71ba00309d04854f6cccee9 |
| risk_sources.py (baseline scope 보강 후보) | 896056acec125208abfca11eb9c7950f56ac9c1ba69bcde3284ae8177b665176 |
| risk_input_seal.py | e9a635cb85887c1a8d8e03819705c5c8211f26e4c14456c24e4230cc10398c27 |
| author41 tests | 61af6b0ce26b2e35a7981bf01d126cef4ee6b9aca78f6208494748709b4873af |
| captured-read15 tests | 7b99dd334f9e03b99490623277a626a4cacddf5c8cd3245da5954b5e2ee62cc4 |
| Terra12 tests | eba0f9c93b1f73807258f88661ad09cecd76a3d69a8cfa5716d62139f9ad42af |
| independent boundary15 tests | f01566b6ced7d4a9de09411611a34e68944cae3be42c52bc4f3e89e0e1097f16 |
| independent cancellation13 tests | 1ab670377be677ec8a684a989191ca8220f3219ec6b1122d3a36c549fc4d8667 |
| independent precommit12 tests | 7a5b0fa54124e762d60eb278f8b0ac6d3e39ed81244d42089e995a9fa229ad60 |
| independent scheduler/generic-seal5 tests | c65bee871d875684934e0003a679f9a7bfdad9d65626963e20e23cd33f806664 |
| independent actual-seal/admission6 tests | 8d959bdd28897ff6f8ff5a1bbaeeb289a291ab96418f6010e0aa7c3a84251ad7 |
| author remediation19 tests | 8d3dcda8c25766f5850c5fdf48ca1088409987a5cbb7924b82254f6d90dff0cb |
| independent baseline scope5 tests | 45c4fbe58e87c1d199d78e5595175f2e2fc6cb8670f81d7a065d3c13eee128c9 |
| author baseline scope4 tests | 347d571e4011844b020033f64e9789b6006885d49fdd3ea02b1825b2c3b2cf29 |

## 최종 gate

동결 제품10파일과 신규11시험 모듈147건, 별도 개발용 프로토콜 시험 보완을 포함한 부모 전체 실행: **UTC4271passed/2knownxfail/4기존경고222.71초**, **KST4271passed/2knownxfail/4기존경고206.95초**, 모두 exit0·운영/외부 네트워크 접근 시도0. 신규 파일을 명시 stage한 뒤 문법·비밀정보 의심 패턴·cached diff 검사를 통과했다. 문법 pycache는 별도 임시 경로를 사용했고, verify의 시험 생략 옵션은 이 두 별도 전체 실행을 대체하거나 누락한 것이 아니다. KST 결과까지 확인 후 feature 체크포인트로만 통합한다.

## 이후 순서와 남은 장벽

1. 위 실제 2분 slice의 최종 전체/교차 리뷰를 닫았다. feature checkpoint 커밋·푸시 뒤 같은 기준의 격리 워크트리에서 다음 단위를 이어간다.
2. 승인된 계약으로 정오 cap 선행 commit → JSON LLM → 실제 보호 적용·typed replay·cold repair를 구현하고 독립 인수한다. C3 API/세부 계약·원 method probe는 준비됐으며 아직 제품 구현 완료가 아니다.
3. 별도 morning diagnosis(C4), qualification/sizing/gateway·나머지 writer·실제 설치, 전체 C/F/G/R 및 broad 리뷰가 남는다. long-history 비용/부하 성능과 모든 장애 조합을 이 집중 시험으로 입증하지 않는다.
4. 공식 최초 인계·취소 체인 증거가 없는 범위는 미지원/시작 차단을 유지한다. main 통합·운영 전환은 별도 판단이다.

중기 추세와 정오 JSON 분류는 다른 정책 경로다. 실제 읽지 않는 별도 trend/expert/VIX lane을 C3의 자동 의존으로 추가하지 않는다. 실제 읽은 upstream의 선언된 의존 변경만 전파하고 US 원자료의 VIX는 detached 원 provenance로 보존한다. 모델 간 동의나 단위시험 수를 수익성/실거래 준비 증거로 해석하지 않는다.
