# 40·41차 — 관측 용량과 오류 범위 설계

목적은 좋은 후보와 진입 시점의 비용 후 효과를 평가할 실제 자료를 확보하는 것이다. 사용자의 “순서대로 전부” 요청에 따라 로컬 구현·합성 검증·독립 리뷰를 진행한다. 운영 주문/계좌 API/자격증명/SSH/서비스/배포/재시작/새 수집·예약은 범위 밖이다. 기준 main `1f3b10e59c56706b3c2f5270121fe0b4b7455c98`이며 기존 pilot은 소급 변경하지 않는다.

## 40차: 현재 수집기 용량의 합성 재생

현재 실측 집계는 10,000프레임/435.343147초이며 ACK→계획 종료는1659.665122초, 최초 허용시각→종료는1800초다. 평균의 선형 외삽은 가정이고 미래 필요량/최대치 증거가 아니다. 단순 상한 확대·시간 축소·틱 요약을 먼저 도입하지 않고 실제 collector, receive_orderbooks, CaptureArtifact 저장/재읽기를 합성 입력으로 통과시켜 병목을 측정한다.

- 합성 전용 모듈/CLI: 소켓·인증·원본 탐색 없음. 주입 시계와 메모리 소켓, 명시 임시 디렉터리의 합성 artifact만 사용한다. 현재 frame hard cap≤50,000·artifact≤64MiB 유지.
- 입력은 고정 시나리오의 기간, 생성 총 프레임 수, frame cap, 종목 배분/시간 burst, 호가 깊이뿐. 테스트용 소형 사례도 엄격히 제한한다. 모든 프레임을 미리 쌓지 않고 생성한다.
- pilot10k 조기 종료 재현,900초 뒤 첫 호가를 위한 명시1초 여유, 실제ACK→종료, 전체1800초, 전체창2배 가정, 한 종목 편중, burst, 정확한 cap 경계를 구분한다. 생성 프레임 수가 cap보다 많을 때 정상 기대 결과는 frame_limit이며 수집 성공은 false다.
- record count/종목별 count·first/last·종료/cleanup·900초 이후 호가 존재·직렬화/전체 파일 bytes·저장/재읽기·wall/CPU·tracemalloc peak를 기록한다. 측정 범위에는 export와 저장/재읽기를 포함한다. RSS/실장중 지연·수신 완전성 보장은 하지 않는다.
- 선언 자원 검사: Python 추적 peak256MiB, case wall120초/CPU120초, artifact64MiB. 합성 입력 contract를 통과했다는 사실과 이 예산을 만족했다는 결과를 분리한다. 메모리 초과는 측정상 실패이며 강제 메모리 sandbox 보장이 아니다.
- `synthetic=true`, `production_eligible=false`, `profit_comparison_available=false`, `stream_complete=null` 고정. 예상된 cap 실패를 실수집 완료로 승격하지 않는다. 현재 한도로 전체창 stress가 안 되면 그 사실과 다음 설계 선택지를 보고한다.

## 41차: KIS 오류의 제한된 진단 근거

40차 결과를 먼저 확인한 뒤 구현한다. 원문 없이 frame 오류의 고정 reason/TR종류/count종류/암호화종류/관측범위를 기록한다. 기존 파싱·구독·ACK·반환·매매·connection_gap의 incomplete 의미를 유지한다. 잘못된 count/encrypted일 때 첫 symbol을 전체 다중 프레임의 귀속으로 추정하지 않는다.

새 관측 계약에서만 선택적으로 설치한다. 구체적인 필드/버전·후보 귀속·보고기 검증은 구현 전에 별도 계약으로 확정한다. 기존 v1/v2/v3 입력은 자동 변경하지 않는다. 구독 관측 coordinator 안에서만 작동하며 진단 오류가 정상 처리나 기존 gap 기록을 막지 않는다.

## 계좌 순수익과 통합 See

기존 계좌 대사/선정·진입/동일진입 청산 도구를 먼저 재사용한다. 실제 계좌 원본 경로·기간·현금흐름/분배금·실제 비용·평가액·벤치마크 동일 기준이 없으면 손익을 생성하지 않는다. 이미 요청한 원본 경로는 미응답이며 파일 부재로 단정하지 않는다. 가능한 로컬 준비·합성 전체 흐름 검증을 완료하고 외부 입력 대기는 명시한다.

최종 변경 전체를 별도 Astra/xhigh 독립 검토·전체 tests/·문법/비밀정보/문서 링크 검증 후 PR+동일 head CI+병합한다. 실제 모델/effective effort는 노출된 메타데이터만 증거로 삼는다.

## 41차 확정 계약

- 새 `runner-first-scan-v4`만 기존 v3 선언+`frame_diagnostics={version:kis-frame-diagnostics-v1}`을 요구한다. 버퍼에 같은 선택 설정을 전달하고, 명시 설치 원장은 format v2 헤더에 이를 보존한다. 기존 원장 v1과 capture v1/v2/v3는 그대로 읽으며 자동 활성화하지 않는다.
- 기존 quote_subscription/connection_gap에 선택 nested `frame_diagnostic`을 추가한다. version, tr_class(기존 KRX/NXT PRICE/BOOK 네 종류 또는 other/unknown), count_class(single/multiple/out_of_range/invalid), count(1..999 또는 null), encryption(plain/encrypted/unknown), scope(candidate_lease/registered_non_candidate/unregistered/candidate_tr_unattributed/non_candidate_tr/unknown_tr)만 허용한다.
- 후보 scope는 plain 단일 KRX BOOK·유효6자리symbol에서 현재 미만료 lease만 뜻한다. ACK/완전수신 증거가 아니다. 여러 건/암호화/비정상 count는 candidate_tr_unattributed이며 첫 symbol로 전체 frame을 귀속하지 않는다. 다른 알려진 TR은 non_candidate_tr, 모르는 TR은 unknown_tr다. 원문·예외 메시지·symbol·서버 문자열은 nested 자료에 넣지 않는다.
- 기존 malformed_frame/invalid_frame_count/unsupported_frame_shape/short_orderbook_frame/orderbook_parse_failed에서만 계측한다. 파싱·early return·ACK·구독·실제콜백은 불변이다. 분류 실패는 기존 gap을 한 번 기록하고 추가 incomplete 표시를 남긴다.
- strict 오프라인 보고기는 원래 sequence·시각≤봉인≤보고시각·fixed enum/조합·명시 opt-in을 검증한다. reason/TR/count/encryption/scope 집계를 원래 gap행과 대사하고 구형/누락 상세는 unavailable로 보존한다. 진단이 있어도 connection_gap은 항상 불완전이다. study 지문·v4 계약을 CLI에서 대조하고 raw 파일을 변경하지 않는다.
