# Toss 보고 가격의 실제 실행 연결

사용자는 남은 Toss 기동·보고 연결 보완을 진행하도록 승인했다. 목표는 당일 아침
보고에 실린 종목의 참고 가격을 기존 검증된 Toss 실행 경로에서 받아 사용하는 것이다.
KR 매수 중지·주문/청산·월USD100·기존 observer 서비스/비공개 상태는 유지한다.
현재 보고의 KIS 호출은0이며 첫 효과는 pykrx/Naver 조회 생략이다.

## 검토한 방식과 선택

기존 artifact를 mutable 거래 봇에 import하면 이미 로드한 src/의존성과 실제 검증
artifact가 달라진다. 전체 거래 봇을 immutable release로 전환하면 보고 연결에 비해
배포 변경이 크다. coordinator는 **검증된 Toss 전용 자식 프로세스 + bounded JSONL**을
선택한다. 기존 in-process 소비자는 유지하되 authority 실행만 격리하는 설계 수정이다.
임의 HEAD를 시작 증명으로 꾸미거나 기존 observer 권한을 넓히지 않는다.

## 실행 계약

- 부모는 `/usr/bin/python3 -I -S /usr/libexec/qwq-toss-report/launcher.py`만
  shell 없이 실행한다. `TOSS_REPORT_API=1` 명시 opt-in에서만 생성하며 자식 환경은
  Toss flag/client ID/secret와 최소 PATH/LANG만 허용한다. KIS/LLM/Telegram 키는 넘기지 않는다.
- 새 `/etc`, `/opt`, `/var/lib`, `/usr/libexec`의 `qwq-toss-report` namespace를
  사용한다. root 소유 manifest/소스/설정·모든 부모·전체 inventory 검증 뒤에만 import한다.
  현재 봇 UID/GID/보조 그룹을 정확히 결합한다. 사용자/그룹/systemd unit을 만들지 않는다.
- 새 v3/report_display 전용 조립기는 명시 날짜와16:00–16:05의 보고 세션, 최대20종목,
  가격 TTL60초, query20초·cleanup10초·preflight5초·발급4회 상한을 검사한다.
  하루 prices 슬롯 한 번만 처리하며 calendar/일봉/임의 명령은 받지 않는다.
- 자식은 부모 수명 중 최대 한 번 생성하고 자동 재시작하지 않는다. grant의 시작
  영수증을 기존 O_EXCL/fsync 규칙으로 한 번 소비한다. 날짜별 결과는 원장 완료 뒤만
  반환한다. 실패한 시작 영수증을 지우거나 같은 슬롯을 재조회하지 않는다.
- 부모 요청은45초 총 기한(초기 spawn/검증 포함), 요청16KiB·응답64KiB 상한이다.
  한 번에 하나만 처리하고 중복·취소·EOF·기한 초과는 가격을 지우고 기존 조회로 돌아간다.
  자식 종료/flag OFF/봇 종료·벽시계 역행에도 원래 monotonic 기한과 TTL을 연장하지 않는다.

## 당일 보고 종목과 가격

`ReportCohort`는 날짜·원래 aware 생성 시각·순위/종목 순서·내용 해시를 보존한다.
아침 저장 파일에 함께 쓰고 재시작 때 그대로 복원한다. 구형/파손 metadata를 현재
시각으로 메우지 않는다. 최대20개·중복 없는6자리 KR 코드·양의 오름차순 rank를 검증하며
과대 목록을 절단하거나 장중 후보/보유 종목으로 대체하지 않는다.

부모는 `prepare_report(cohort)` 완료 뒤 기존 `_update_results()`를 실행한다. 자식에는
cohort·request ID·요청 시각만 보내고 경로/URL/grant/실행 명령은 보내지 않는다.
응답은 request ID/cohort hash에 결합하고 원장 완료·ReportQuotes 검증을 통과한
Decimal 가격·관측/수신 시각·시장 기준·통화만 담는다. valid_until은 grant/세션/원래
시각 TTL 중 최솟값이다. 부모도 미요청 종목/중복/비유한 값/미래·오래된 시각을 거부한다.
미확인 시장 기준은 그대로 표시하며 참고가 대비 등락률이나 체결 가능성으로 바꾸지 않는다.

## 검증과 운영

실제 launcher→v3 조립→claim→worker/parser/원장→JSONL→보고 생성기 경로를
합성 HTTP로 시험한다. 파일 위변조·권한·OFF·구형 승인·오류/취소·늦은 응답·재시작 복원과
정확한 종목 순서가 인수 기준이다. 로컬 외부 API/운영 자격 접근0, 전체verify·비밀 검사·
독립 중요 리뷰 후 문서/커밋/푸시/CI를 완료한다. 실제 artifact 설치·활성화와 실측은
각 영수증으로 구분하며 검증만으로 활성화/요청 감소/수익 개선을 주장하지 않는다.
