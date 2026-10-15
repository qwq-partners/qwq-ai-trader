# 10월16일 장전 R1 모델 비교 재감사

## 판정

최신 R1 `config/experiments/R1-bull-reasoning-20261015-pricing.json`은 **설계는 존재하지만 실행은 꺼진 초안**이다. manifest가 `enabled=false`, `draft_off_not_registered_not_scheduled`이고(`:3-6`), 현재 작업자도 환경 변수 두 개와 `manifest.enabled=true`가 모두 있어야 생성된다(`src/analytics/model_comparison_worker.py:560-572`). 이 감사는 모델·설정·스케줄을 켜지 않는다.

한 번에 바꾸는 변수는 Bull의 `reasoning_effort` 하나다. 현행 `gpt-5-mini minimal/400`과 후보 `gpt-5-mini low/400`을 비교하고 Bear `gemini-3.1-flash-lite-preview/400`, 입력, 집계, fallback은 고정한다(`config/experiments/R1-bull-reasoning-20261015-pricing.json:7-47`, `:67-73`). `gpt-5-mini`가 minimal과 low를 모두 허용하는 현재 검증표도 확인된다(`src/utils/llm.py:245-280`). Argon 같은 새 모델은 이 R1에 섞지 않고 별도 사전 고정 실험으로 둔다. 현재 Argon 제어 경로도 별도 분기다(`src/utils/llm.py:560-585`).

## 현재 코드와 가격 기준

현재 가격표의 `gpt-5-mini` 단가는 입력 $0.25/M, 출력 $2.00/M이고 날짜별 가격 조회를 쓴다(`src/utils/llm_pricing.py:20-75`). coordinator가 현재 보수적 예약식으로 계산한 최악치는 **10 provider attempts/쌍, $0.5374550/쌍, 최대 20쌍 $10.7491000**이며 manifest의 고정 상한과 일치한다(`config/experiments/R1-bull-reasoning-20261015-pricing.json:131-148`, `:221-223`). 이는 invoice 예측이나 지출 승인이 아니다.

R1의 날짜는 KST **2026-10-15~2026-11-11 양끝 포함**으로 유지한다. 늦게 시작해도 남은 날만 쓰며 날짜 연장이나 한도 보충은 하지 않는다(`config/experiments/R1-bull-reasoning-20261015-pricing.json:138-141`). 운영 월 원장은 $100이고 80%에서 절약 모드, 95%에서 필수 전용, 100%에서 유료 호출 중단이다(`config/default.yml:19-23`; `src/utils/llm_budget.py:278-312`). R1은 정상 모드만 요구하고 이번 예약까지 포함해 80%를 넘으면 멈춘다(`config/experiments/R1-bull-reasoning-20261015-pricing.json:49-64`, `:231-238`).

기존 manifest의 base file 지문은 현재 HEAD와 달라졌다. 활성화 검토 전 아래 현재값으로 다시 봉인해야 한다.

| 파일 | manifest base SHA-256 | 현재 SHA-256 | 판정 |
|---|---|---|---|
| `src/utils/llm.py` | `5b429e712f488705c046c9ad69bbbe25d336fbbef51db91dd714f4c1797a93fa` | `0b6afc9dde5ebd15a97fe5653bd59cba5b1e819555796d2bbf953c71ec648492` | drift |
| `src/utils/llm_pricing.py` | `af47f3ba814375674fc4e469bab9cbb3bfa721270f5c6913c8eda67e9a48a90b` | `af7a14f947cf6caa22ec262bb8fd63fa1010c58bcc3025c61b500b7f27d24033` | drift |
| `src/utils/llm_budget.py` | `fad85a0bd12634625112d2f0faf7ed7570af8d82003afa59deedb37ddaa9b0d9` | `39be52592cb7ce7a1c34dbab14f15e4dd7e4521b5022bc22d639d6a13b2b5ffd` | drift |
| `config/default.yml` | `c7cc2b76645277107dfdb82532e398af61bb1e37b7248ca4cfc2a5657b545a5c` | `6918da6355d089e0703843b3a0fb290ae2439a848175903fb609fd66d8bd5190` | drift |

반면 neutral sizing replay가 계산하는 `policy_source_hash()`는 현재도 `903126b82860506bb18f2375f695985c13b97dd61e6e1d3e638930ab557dd78b`로 manifest 값과 같다. 이 해시는 position-size, hybrid parameters, ATR/risk helpers와 buy-fee 함수 소스를 묶는다(`src/analytics/model_comparison_sizing.py:50-58`, `:215-233`). 네 파일 drift가 무해하다는 뜻은 아니며, 활성화 전 통합 HEAD와 운영 배포 SHA의 전체 지문 재검증이 필요하다.

## 표본과 결과 계약

분모는 그날 제공된 **전체 candidate envelope**다. 응답·호가·손익을 보기 전에 attestation, `received_at-input_available_at` 0~5초, context 크기와 실제 worker admission을 통과한 첫 1건을 `(input_available_at, decision_id)` 순서로 뽑는다. 제공·적격·표본·부적격을 모두 남기며 일 한도를 넘은 후보도 `daily_quota`로 보존한다(`src/analytics/model_comparison_daily_report.py:85-143`, `:275-294`). 최초 적격 1건의 결과를 전체 후보나 다른 날로 확대하지 않는다.

각 arm은 **응답 사용 가능 시각 뒤 첫 quote**를 별도로 붙이고 API 비용을 함께 보존한다(`src/analytics/model_comparison_daily_report.py:153-175`). 종료점은 `input_available_at+900초`다(`config/experiments/R1-bull-reasoning-20261015-pricing.json:131-140`). 응답·호가·비용·외부 gate 또는 세션 증거가 없으면 0으로 채우지 않고 null/unknown을 유지한다. 보고서도 outcome이 없으면 `not_evaluable`, `net_delta_krw=null`로 만들고(`src/analytics/model_comparison_daily_report.py:315-329`), unresolved ledger 비용을 추정값으로 메우지 않는다(`:398-448`).

과거 문서의 “W1 admission hook과 W2 envelope/worker 통합 미구현”은 현재 코드에는 더 이상 맞지 않는다. 작업자는 manifest를 읽어 공유 월 원장과 runner를 설치하고(`src/analytics/model_comparison_worker.py:560-580`), offer admission과 저널을 갖는다(`:224-235`). 따라서 그 두 항목은 **구현됨으로 supersede**한다. 남은 경제성 차단은 다른 것이다. runner 결과는 `downstream_admission_not_replayed`와 `continuous_session_not_certified`를 명시하고(`src/analytics/model_comparison_runner.py:250-273`), source join 뒤에도 둘을 강제로 유지한다(`:554-615`). 실제 보유·pending·쿨다운·매수중지·증권사 한도 등 외부 admission과 연속장 session evidence가 생기기 전 `net_delta_krw`는 unknown이다.

## 장전 완료와 자료 대기

장전에는 R1의 단일 변수, Bear 고정, 전체 후보 분모와 첫 적격 1건/일, 응답 뒤 첫 quote, 비용, unknown 규칙, 10회/$0.5374550/$10.7491000 상한, 고정 날짜와 80/95/100 예산 중단선을 재감사했다. 코드·설정·스케줄·유료 호출은 바꾸지 않았다. 파일 지문 drift 때문에 현재 manifest를 그대로 활성화할 수 없다.

자료 대기는 세 갈래다. 첫째, 10월16일 실제 표본과 비용 대사가 없다. 둘째, 외부 admission과 연속장 증거가 없다. 셋째, 실제 07:00 미국장 마감 보고와 07:30 전문가 판단 구간에서 요청 모델·resolved model·fallback·비용을 역할별로 대조하는 morning role audit가 아직 없다. 공개 코드상 07:00 보고 창은 07:00~07:15이고(`src/schedulers/kr_scheduler.py:6087-6101`), 전문가 morning 슬롯은 07:30이다(`src/schedulers/kr_scheduler.py:407-420`, `:688`). 비공개 로그를 읽지 않았으므로 실제 실행 모델은 pending이다.

주간 post-exit 복기는 금요일이 아니라 **토요일**이다. 등록부와 docstring은 토요일로 명시하고(`src/schedulers/kr_scheduler.py:304-306`, `:6980-6988`), 실제 loop는 토요일 09:30~09:44에 실행한다(`:7022-7039`). 이 시간 차이는 후속 운영 문서 정리 대상이지만 R1의 변수를 바꾸지 않는다.

실측과 위 차단 근거가 채워지기 전에는 experiment를 enable하지 않는다. 그 뒤에도 R1만 먼저 판정하고 Argon 또는 다른 모델 비교는 새 experiment ID, 새 지문, 새 비용 상한으로 따로 사전 고정한다.

## 검증 범위

감사 HEAD는 `377f0f52fcb9d6fd63af17d7d10ebf3b8556ef77`이다. 요청 모델/노력은 Sol/high이고 실제 런타임 모델·실효 effort는 metadata가 없어 unverified다. 공개 저장소 코드와 문서만 읽었고 비공개 study, 로그, 캐시, 계좌, 환경변수, 외부 API·네트워크를 사용하지 않았다.
