# 10월16일 장전 선정·진입·청산 점검과 사전 고정 계약

## 결론

설계는 장전인 **2026-10-15T14:18:28+00:00**에 최종 수정·고정했고, 실측 자료는 아직 기다리는 상태다. 대상은 study SHA-256 `39957565351aaab0ef4c4ce7f8461206118cb0af09719536a0bb7f0686d22024`, 실제 배포 엔진 `de6c5bf7f6528517d9d7a219ab2e76ba7b0ba581`, 10월16일 KST 09:15~09:41 관측과 09:15~09:25 신규 입장이다. `evaluation_epoch=2026-10-16-engine-llm-window-v1`은 공개 설치기 상수와 study 검증식에 일치한다(`scripts/ops/entry_capture_prepare_20261016.py:30-31`, `:299-324`). 이 문서의 구조화 계약은 [premarket-selection-exit-2026-10-16.json](premarket-selection-exit-2026-10-16.json)이다.

현재 코드와 배포 엔진 사이의 관련 소스 차이는 없다. `de6c5bf7..377f0f52`에는 문서 6개만 바뀌었고 이 문서에서 인용한 선정·게이트·평가기 소스는 동일하다. 운영 배포와 관측 지문은 `docs/research/engine-model-allocation-2026-10-15.md:83-105`에 고정돼 있다.

현재 확인할 수 있는 핵심은 두 가지다. 첫째, 10월6일 진단 뒤 **관측은 개선**됐다. 원천 호출의 실패·부분·빈 결과·캐시와 로컬 경과시간, 반환 후보 전체, 후보별 실제 진입 게이트 결과를 보존한다. 둘째, **의사결정 의미는 아직 개선되지 않았다.** 원천 실패나 순위 미등장이 후보별 순매수 0과 구분되지 않은 채 공급 플래그 `False`로 소비될 수 있고, 알려진 동일 입력 계열도 원천 이름 수만큼 출현 보너스를 받는다. 따라서 내일 자료가 완전해도 관측 가능한 실제 점수의 설명과 원천 제거의 인과적 순수익 평가는 구분해야 한다.

## 현재 코드 재감사

| 질문 | 판정 | 현재 근거와 의미 |
|---|---|---|
| 원천 결측·0·신선도 관측 | **부분 수정** | `src/analytics/selection_source_status.py:45-55`는 `completed_empty`, `failed`, `partial`, `cached_empty`, `not_called`, `unknown_empty`를 나누고, `:58-83`은 payload/cache/error와 cache age를 기록한다. 그러나 `:169-211`은 거래소 원천시각을 계속 `source_as_of=None`으로 강제한다. 로컬 호출/캐시 신선도는 알 수 있지만 공급자 자료의 시점은 모른다. |
| 결측·0의 점수 소비 | **미수정** | 기관·외국인 순위에 실제 반환된 종목만 각각 `has_inst_buying=True`, `has_foreign_buying=True`가 된다(`src/signals/screener/kr_screener.py:396-466`, `:644-717`). 이후 SPDI는 두 플래그가 모두 거짓인 급등 후보를 수급부재로 감점하고(`:1371-1402`), 개인 단독 매수도 같은 두 거짓값으로 다시 감점한다(`:2227-2242`). 원천 status snapshot은 점수 함수에 전달되지 않는다. 성공한 제한 순위에 종목이 없다는 사실, API 실패, 캐시 빈 결과, 실제 순매수 0이 점수 의미에서 분리되지 않는다. |
| 실제 점수 소비 경로 | **확인됨** | 반환 점수는 장중 최소점수와 scan 등락률을 함께 통과해야 한다(`src/schedulers/kr_scheduler.py:4365-4393`). 통과 뒤 실시간 호가의 가격·등락·시가·거래량을 다시 검사한다(`:4537-4595`). 같은 점수는 Signal의 `score`, `confidence`, metadata에 복사된다(`:4783-4822`). 엔진은 이를 크로스 검증에 넣어 점수를 다시 조정하고(`src/core/engine.py:2017-2057`), 조정점수 85~95 구간은 LLM 검사를 시작한다(`:2059-2080`). LLM 공통 입력에도 그 점수가 들어간다(`src/core/cross_validator.py:858-892`). 선정 점수는 단순 로그가 아니라 후보 입장, 신뢰도, 후속 점수, LLM 비용과 수량 경로에 실제 영향을 준다. |
| 결측 표현의 LLM 전달 | **수정됨, 상류 점수는 별개** | 공통 입력은 `None`을 missing, 숫자 0을 provided/balanced로 구분하고 모든 원천시각을 미확인으로 표시한다(`src/core/entry_llm_context.py:11-21`, `:33-64`). 그러나 여기 들어오는 screening score에는 위 수급 의미 손실이 이미 반영될 수 있다. |
| 상관 원천의 독립 취급 | **진단만 수정, 점수는 미수정** | 진단기는 `premarket_gap`+`kis_fluctuation_rank`, `naver_rise_rank`+`naver_new_high`를 알려진 동일 입력 계열로 묶고 독립성·수익효과를 모두 false로 둔다(`src/analytics/selection_source_status.py:238-248`). 실제 점수는 원천 출현 수 2개에 +10, 3개 이상에 +20을 준다(`src/signals/screener/kr_screener.py:2324-2360`); 선정 근거 검증도 raw occurrence 수를 그대로 보너스 기준으로 요구한다(`src/analytics/selection_basis.py:130-142`). 진단 정보가 운영 점수를 바꾸지는 않는다. |
| 후보 분모와 통과/거절 | **반환 후보는 수정, 원천 전단계 탈락은 미수정** | 스캔 직후 포트폴리오/진입 게이트 전에 반환 후보 전체를 복사한다(`src/schedulers/kr_scheduler.py:4108-4122`). scan 레코드는 모든 반환 후보의 ID·점수·가격·등락률을 저장한다(`src/analytics/entry_observation.py:316-350`). GateTrace는 그 후보 모두에 행을 만들고 종료 때 `blocked`, `not_reached`, `signal_created`, `unknown` 중 하나를 기록한다(`src/analytics/entry_gate_trace.py:79-152`). 반면 원천 내부의 가격·순매수·거래대금·ETF 탈락은 `continue`로 사라지고(예: `src/signals/screener/kr_screener.py:424-466`, `:673-717`), source snapshot은 raw/returned **건수**만 보존한다. 따라서 반환 후보 분모의 실제 진입 결과는 복원할 수 있지만 원천별 탈락 종목 identity와 이유를 포함한 발굴 전체 분모는 복원할 수 없다. |
| 점수+가격 공동 admissibility | **실제 경로 관측은 수정, 반사실 평가는 미구현** | 실제 경로는 score/change를 먼저 검사하고(`src/schedulers/kr_scheduler.py:4365-4393`) 그 후보만 새 호가·가격 조건을 검사한다(`:4537-4595`). GateTrace는 실제 분기 값을 보존한다. 하지만 원천 제거 점수로 같은 시점 가격 게이트까지 다시 재생하는 평가기는 없다. 점수만 빼서 통과/실패를 판정하면 실제 공동 admissibility가 아니다. 특히 `post_merge_adjustment`는 하나의 합계일 뿐(`src/analytics/selection_basis.py:73-80`), 원천 제거로 수급 플래그·SPDI·개인 감점·테마 보정이 어떻게 달라지는지 재생할 자료가 아니다. |
| Mansfield/VCP/뉴스의 실제 위치 | **확인됨** | Mansfield RS는 별도 SwingScreener가 fresh KOSPI 벤치마크일 때 계산한다(`src/signals/screener/swing_screener.py:380-400`; 공식 계산 `src/indicators/technical.py:602-651`). VCP도 swing 경로의 독립 후보 라인이다(`src/signals/screener/swing_screener.py:108-138`, `:510-629`). 10월16일 `live_screening` 점수 원천과 같은 것으로 합치지 않는다. 또한 live screener의 뉴스 원천은 `NewsCurator.get_symbol_sentiment`가 아니라 `theme_detector.get_all_stock_sentiments`다(`src/signals/screener/kr_screener.py:2058-2085`); 이 자료는 로컬 1시간 TTL만 보장한다(`src/signals/sentiment/kr_theme_detector.py:1260-1283`). |

### 코드 결함 판정

재현 가능한 의미 결함은 남아 있다. 기관·외국인 원천이 실패하거나 제한 순위에 후보가 없을 때 후보 플래그는 기본 거짓이고, 그 두 거짓값이 SPDI와 개인 단독 매수 감점에 중복 사용된다. 원천 status를 수집하면서도 scorer가 읽지 않는 것이 소스 증거다. 별도 운영 자료 없이도 합성 호출에서 `failed/empty/not_called`이 같은 후보 플래그와 같은 감점 분기로 수렴함을 코드로 재현할 수 있다. 이 문서 작업에서는 거래 코드를 수정하지 않는다.

다음 패치는 내일 결과를 본 뒤 즉석 최적화하지 않고 별도 변경으로 해야 한다. 후보별 수급 상태를 `positive / observed_nonpositive / unobserved`로 만들고, `unobserved`에서는 SPDI 수급부재와 개인 단독 감점을 적용하지 않으며 실제 `observed_nonpositive`일 때만 적용하는 것이 권고안이다. 순위 API가 후보별 0/음수를 제공하지 않으면 `observed_nonpositive`로 승격하지 않는다. 먼저 봉인된 자료에서 결측률과 영향 후보를 보고, 패치 전후 점수·게이트만 shadow로 대조해야 한다.

## 사전 고정한 원천 제거 계약

분석 단위는 `(scan_id, candidate_id)`다. 같은 종목이 여러 scan에 나오면 서로 다른 기회로 유지한다. 각 scan의 **반환 후보 전부**를 baseline과 반사실의 동일 분모로 사용하며, 실제 LLM 검사를 받은 최초5종목을 전체 후보로 확대 해석하지 않는다. source 내부 전단계 탈락 종목은 현재 원장에 identity가 없으므로 별도 분모라고 명시하고 원천 기여 손익에 합치지 않는다.

변경 변수는 `removed_source_family` 하나다. baseline과 한 번에 한 family만 제거한다. 알려진 상관 계열은 `premarket_gap+kis_fluctuation_rank`, `naver_rise_rank+naver_new_high`를 한 단위로 제거한다. 나머지는 각 원천을 한 family로 두되 서로 독립이라고 주장하지 않는다. 원천별 제거 순서, 날짜, 최초 수집시각, 원래 순위, 선택/거절 후보를 보존한다.

각 반사실은 실제 배포 SHA의 scorer를 정확히 재생해야 한다. 단순히 `weighted_score`를 빼거나 `post_merge_adjustment`를 고정하면 안 된다. 원천 제거에 따라 source count, 수급 플래그, SPDI, 개인 감점, 테마 보정, 정규화가 다시 계산돼야 한다. 필요한 point-in-time 입력이 없으면 그 후보는 unknown이다. 현재 selection basis만으로는 이 조건을 충족하지 못하므로 `full_source_ablation_ready=false`가 사전 상태다.

이 전체 원천 제거와 아래의 제한된 **출현 보너스 shadow**는 서로 다른 계약이다. 출현 보너스 shadow는 기존 selection basis에 기록된 원천 이름을 known family로 접어 raw occurrence bonus만 다시 계산하는 기술 비교라서 설계는 준비됐다. 다른 원천 의존 보정과 이후 게이트를 완전히 재생하지 못하면 경제성은 unknown이며, 이 준비 상태를 `full_source_ablation_ready`로 승격하지 않는다.

점수 결과만으로 매수 가능을 선언하지 않는다. 반사실 점수로 실제 순서의 score, 보유/pending/쿨다운/횟수, scan change, 전략, 실시간 가격·거래량·ATR·추격, 뉴스, 크로스 검증을 같은 point-in-time 입력으로 함께 재생한다. 실제 분기 이전에는 존재하지 않았던 외부 호출이나 LLM 응답을 사후 생성하지 않는다. 그 지점은 unknown이다.

경제성은 study의 정확한 수수료·슬리피지·자본·수량·첫 KRX ask·900초 KRX bid 계약을 재사용한다. 숫자를 이 문서에서 새로 만들지 않는다. deterministic gate reject이고 필수 입력이 완전할 때만 cash를 0원 결과로 둘 수 있다. 자료가 없거나 분기를 재생할 수 없는 후보를 cash 또는 0원으로 바꾸지 않는다. 알려진 합계와 전체 합계를 분리하고 unknown이 하나라도 있으면 전체 `complete_delta_net_pnl=null`이다. 하루 기술 표본은 계좌 순수익, KODEX200 초과수익 또는 채택 근거가 아니다.

### 제한된 다음 가설

첫 적용 후보는 **출현 보너스의 단위를 raw source count에서 known input family count로 바꾸는 것** 하나다. 원천 점수·가중치, 후속 보정, 임계값, 가격/진입/청산/비용은 고정한다. 이 가설은 현재 basis의 known-family 진단으로 shadow 재계산할 수 있지만, 전체 분모의 경제성 입력이 부족하면 점수·게이트 영향만 보고 순수익 판정은 보류한다. 이 결과와 수급 unknown 패치를 동시에 적용하거나 같은 날 임계값을 다시 맞추지 않는다.

## 같은 진입의 청산 계약

선정과 청산을 섞지 않기 위해 기존 `build_exit_comparison`을 그대로 재사용한다. 정책은 JSON에 고정한 `same-entry-initial-stop-bid-v1`, 동일 study SHA, `fixed_at=2026-10-15T14:18:28+00:00`, `max_quote_gap_seconds=5`다. 현재 평가기는 received와 observed 양쪽 연속 quote 간격이 이 값을 넘으면 pair를 unknown으로 만든다(`src/analytics/entry_exit_comparison.py:42-79`). 5초는 라이브 손절 허용 지연을 주장하는 값이 아니라, 현재 캡처 계약의 최대 입력/markout 신선도 5초(`scripts/ops/entry_capture_prepare_20261016.py:382-388`)에 맞춘 보수적인 **경로 증거 결측 cutoff**다. 10분을 허용하는 기존 600초 테스트 fixture는 경계 동작을 시험할 뿐 15분 보유 중 실시간 stop 비교의 허용치 근거가 아니다(`tests/test_entry_exit_comparison.py:49`).

`max_quote_gap_seconds=5`는 인접 quote 경로의 밀도를 검사하고, study의 `max_delay_seconds=5`는 정확한 +900초 markout 뒤 첫 quote가 얼마나 늦게 왔는지 검사한다. 숫자가 같아도 서로 다른 조건이다. 어느 한쪽이라도 넘거나 경로가 희소하면 손절 발생 여부를 추정하지 않고 pair 전체를 unknown으로 둔다. 이 문서는 라이브 stop 코드를 바꾸지 않는다.

이 평가는 같은 후보·첫 진입 ask·수량·비용에서 **초기 순손익 손절과 900초 bid 보유**만 비교한다. 전체 `ExitManager` 재생이 아니다. ATR/추적 손절, 부분 익절, LLM/EOD 청산, 미체결, 체결 후 stop 조정, 자본 재사용을 재현했다고 표현하지 않는다. bid proxy는 실제 매도 체결이나 계좌 수익이 아니다.

정확한 `entry-session-review-v1`이 필요하다. 진입 quote와 900초 quote 두 개만 검토해서는 부족하고, entry sequence부터 horizon sequence까지의 **모든 KRX quote**가 `KRX_REGULAR_CONTINUOUS`로 검토돼야 한다. 각 행은 journal quote ID·record hash·symbol과 결속하고, 공식 per-quote status 또는 외부 event history 근거의 유효구간이 received/observed 시각을 포함해야 한다(`src/analytics/entry_evaluation_bundle.py:43-85`). 경로 중 한 quote라도 검토가 없거나 VI/HALT/AUCTION/UNKNOWN이면 pair 전체가 unknown이다. 이 session review는 현재 이용할 수 없으므로 `same_entry_exit_ready=false`다.

## 장전 완료와 09:41 이후 통합 흐름

**장전 완료:** 코드/배포 동등성, installer의 epoch 결속, 현재 결함 판정, 동일 전체 반환 후보의 전체 원천 제거 규약, 그와 분리한 출현 보너스 shadow, 점수+가격 공동 재생 조건, 같은 진입 청산 정책, 5초 경로 결측 cutoff, unknown/null 규칙을 고정했다. 연구 JSON의 `fixed_at`은 이 최종 수정의 실제 UTC이며 관측 시작보다 앞선다. 운영·설정·주문·배포·예약은 바꾸지 않았다.

**자료 대기:** 09:41 이후에도 자동으로 경제성 판정이 생기지 않는다. 봉인·complete·drop0와 study/capture/epoch 결속, 각 scan의 selection basis/source status/gate trace, `order_ready` 수량·자본, 후보 KRX entry/+900 quote, 모든 중간 quote의 session review가 필요하다. 최초5종목만 quote/LLM 자료가 있고 나머지 반환 후보가 미짝이면 전체 원천 제거 순수익은 null이다.

통합 순서는 다음으로 고정한다.

1. 원장을 읽기 전용으로 봉인 검증하고 study SHA, deployed source/configuration ref, capture ID, epoch, 전체 scan 후보 수를 대조한다.
2. 각 반환 후보에 selection basis와 GateTrace가 정확히 하나씩 있는지 확인한다. source status의 `failed/partial/empty/cached/not_called`을 보존하고 missing을 0으로 바꾸지 않는다.
3. 기존 가격 입력 조립기로 실제 `order_ready`, 자본 snapshot, entry/+900 quote와 장 상태 검토 필요 목록을 만든다. 비교쌍0도 정상 이익0이 아니라 `no_comparable_pairs`다.
4. 모든 중간 KRX quote의 별도 session review가 완성된 pair에만 고정 exit policy를 넣어 초기 손절 대 900초 보유를 계산한다.
5. 정확한 scorer point-in-time replay 입력이 모두 있는 scan에서만 source family를 하나씩 제거한다. 같은 후보, 같은 가격, 같은 수량, 같은 청산, 같은 비용으로 known/unknown과 선택 변화의 손익을 보고한다.
6. 전체 반환 후보에 자료가 없으면 최초5종목의 기술 결과만 별도 표로 두고 원천 가중치·임계값·매매 정책을 바꾸지 않는다. 다일·동일 엔진/국면의 완전 표본이 생긴 뒤 다음 변경 후보 하나만 고른다.

## 검증과 한계

- 요청 모델/노력: Sol/high. 실제 모델과 실효 effort는 런타임 metadata가 노출되지 않아 **unverified**다.
- 읽기 전용 코드 감사 기준 HEAD: `377f0f52fcb9d6fd63af17d7d10ebf3b8556ef77`; 관련 제품 소스는 실제 배포 SHA와 동일하다.
- 문서 계약은 JSON strict parse, 두 파일 diff 검사, 참조 line 확인으로 검증한다. 거래 코드·설정·시험은 바꾸지 않았고 mirrored test는 추가하지 않는다.
- 비공개 원장·계좌·캐시·로그를 읽지 않았고 외부 API·네트워크·주문·SSH·재시작·배포를 실행하지 않았다.
- 이 문서는 결과 전 설계다. 실제 10월16일 수집, session review, full candidate 경제성, 전체 ExitManager replay, 계좌 순수익은 아직 증거가 없다.
