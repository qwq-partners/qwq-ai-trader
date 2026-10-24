# KIS 조회량·Toss 보고 실측 판독 설계

목적은 이미 적용한 조회 절약과 보고 가격 연결이 실제 실행에서 작동하는지 확인하는 것이다.
매매 수익 검증과 구분하며, 새 주문·모의자금·LLM 호출을 만들지 않는다.

## 범위와 인터페이스

- `scripts/ops/kis_health_delta.py snapshot --output <absolute>`는 고정 localhost
  health와 봇 서비스 실행 식별자를 전후 확인하고 KIS 계수만 기록한다. 전체 health의
  잔액·예약금·계좌 자료는 저장하지 않는다. 기존 파일을 덮어쓰지 않는다.
- 같은 도구의 `diff --before <absolute> --after <absolute>`는 source/component,
  operation/TR/page 등 원래 차원을 보존한다. 실행 식별자 불일치·계수 감소·필드 결측은
  비교 불가다. analysis `entries`는 현재 캐시 크기이며 누적 차분 대상이 아니다.
- `ObservationLedger.read_only_report_slot`과 `review_toss_observation.py`의
  `--report-cohort`/`--slot-id`는 기존 안전 reader를 사용한다. 아침 파일의 날짜·순위·종목·
  원생성 시각·cohort 해시와 정확한 당일16:00 prices slot을 검증한 뒤 그 attempt만 판독한다.
  다른 날/slot은 대상 terminal 수에 더하지 않는다. 결측 파일과 후보0을 성공 시세0건으로
  바꾸지 않는다. 기존 whole-ledger CLI는 유지한다.

## 경계와 증거의 한계

- 운영 봇은 `44483017`을 유지한다. 별도 판독 소스를 고정하여 실행하며 재배포/재시작은
  필요하지 않다. helper를 시험 기동하거나 외부 시세·LLM을 추가 호출하지 않는다.
- 동일 PID만으로 재기동을 판별하지 않고 InvocationID/시작 시각도 비교한다. analysis의
  event-loop 재생성은 기존 지표에 epoch가 없어 완전히 증명할 수 없다. 감소는 차단하며
  이 한계와 계측 범위를 결과에 남긴다.
- broker와 direct 계수는 합산하지 않는다. `unknown`을 스크리너/보고 전용으로 귀속하지
  않는다. 현재 보고의 KIS 호출은0이므로 Toss 효과는 pykrx/Naver 조회 생략과 따로 본다.
- evening 본문은 현재 로컬에 보관되지 않는다. 원장의 조회 완료는 Telegram 전송/수신이나
  실제 사용 출처의 증거가 아니다. 수신 본문과 직접 대조하는 항목을 남긴다.
- 자동 실행은10월26일의 유한 읽기 전용 점검만 준비한다. 시간 창 밖/중복 실행은 거부하고
  결과는 비공개 경로에 남긴다. 설치 완료와 실제 실행 완료를 따로 기록한다.

## 성공 기준

재시작·결측·감소·다른 cohort/slot·불완전 원장 회귀시험, 관련 및 전체 verify,
독립 리뷰와 CI를 통과한다. 운영·매수 중지·예산을 유지한 채 점검 예약과 명령을 재현할 수
있어야 한다. 실제 계수/가격은 다음 허용일 실행 뒤에만 확정한다.
