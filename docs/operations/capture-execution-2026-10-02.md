# 2026-10-02 첫 관측 실행 기록

현재 상태: **26차 설치·단발 예약 완료 — 실제 새 코드 기동·수집 전**. [25차 실행안](entry-capture-installation-2026-10-02.md)에 대해 사용자가 운영 사전점검·배포·10월2일08:55~08:57 봇1회 재시작·토스09:15~09:45 관측을 승인했다. 매수 중지와 현재 설정을 유지하고 조건 불일치/시간 경과에는 취소한다. 이 승인은 실거래 주문·전략 배분 변경·매수 재개를 포함하지 않는다.

## 확인한 현 상태

- 기존 봇 active/running, PID3875500·NRestarts0. 토스 서비스 inactive/dead·PID0·NRestarts0.
- KR 매수 중지 파일 존재. 운영 기본 설정·override SHA는25차와 일치한다. 운영 gap5%를 작업본15%로 덮지 않는다.
- 허용된 메모리 상태 조회에서 broker connected=true·pending0, risk_manager pending_orders/pending_quantities/pending_sells 모두0이었다. 미래 기동 직전에 다시 확인한다.
- 기존 토스 서비스 UID997/GID987, 단일 보조 그룹987, host 일치. 비밀 아닌 배치 신원과 인증 상태 신원이 일치하고 auth_state는 ready/generation3이다. 기존 bootstrap 소비 기록은 보존한다. 자격증명/토큰 내용·환경·계좌·원시 로그는 읽지 않았다.
- 명시 wheelhouse `/tmp/qwq-observer-wheels-20260917`에서 필요한10개 binary wheel 지문을 모두 대조했다. 오프라인 빌드 및 system Python `-I -S -B`의 WS import를 통과했고 거래 엔진 모듈은 로드하지 않았다. probe artifact는 배포물로 부르지 않으며 최종 artifact는 검토한 커밋으로 다시 만든다.

## 25차 설치안에서 구체화한 실행 방식

기존 `local_deploy.sh`는 전체 검증82초와 최대60초 상태 대기를 기동 시각에 묶어 실행하므로2분 창을 보장하지 못한다. **전체 검증·CI·소스/배포물 고정·토스 비활성 상태 staging을 미리 완료**하고, [제한된 실행기](../../scripts/ops/entry_capture_activate.py)가 승인된 날짜·시각에만 실제 전환한다. 일반 배포 스크립트나 그 검증을 우회하는 새 상시 경로는 만들지 않는다.

실행기는 root 소유 고정 설정만 읽고, 최초 시도 영수증을 배타 생성·동기화한다. 기존 배포 잠금·현재/대상 Git SHA·소스와 보호 입력 지문·매수 중지·미체결0·기존 서비스 상태를 검사한다. 토스 launcher 점검은 grant가 시작된08:55 이후, 실제 UID/GID/그룹으로 `TOSS_API=1`을 명시한 빈 환경에서 수행한다. 설정 없이 성공 반환하는 launcher 동작을 승인 검증으로 오인하지 않는다.

그다음 검토된 커밋을 적용하고 원래 시작 명령에1회 관측 옵션만 붙여 봇에 재시작 요청을 딱 한 번 보낸다. 남은 시간 동안 실제 새 study/epoch/capture ID가 있는 관측 endpoint가 준비됐는지 확인한 뒤에만 토스를 시작한다. 재시작 요청 후에는 오류여도 자동 두 번째 재시작을 하지 않는다. 재시작 전 실패는 기존 checkout/drop-in을 복구하며, 요청 후 실패는 기록을 남기고 토스를 기동하지 않는다. systemd에 이미 전달된 재시작 작업 자체가 늦어지면 호출 취소가 그 작업 취소를 의미하지 않으며,08:57 이후 시작한 봇의 once selector는 관측을 설치하지 않는다.

봇의 기존 Restart 정책은 그대로다. 실행기의 재시작 요청1회와 systemd 자체 장애 재기동 횟수를 구분해 결과에서 NRestarts를 확인한다. 08:55 시각의 단발 timer는 해당 날짜만 지정하고 Persistent=false로 놓는다. 시간 경과 후 따라잡기·자동 날짜 연장·반복 수집을 허용하지 않는다.

기존 공유 배포 잠금 파일은 UID1000의 mode0664다. 기동 준비 단계에서 같은 잠금을 확보한 상태로0644로 제한해 실행기의 소유권 검사를 충족한다. 소유권·내용·inode를 교체하지 않으며 권한을 확대하지 않는다. 봇 drop-in은 기동 직전에만 설치한다. 이전 코드가 관측 옵션을 모르는 상태에서 예기치 않은 야간 재기동에 실패하지 않도록 한다.

토스는 기존 v1 설치기를 재실행하지 않는다. 검토한 정확한 staging 절차로4개 비밀 아닌 기존 파일(launcher/plan/registry/deployment)을 root 비공개 디렉터리에 백업하고, 새 immutable release와 새 cohort를 만든다. 비활성 상태에서 새 파일 묶음을 교체하고 신원/지문을 검증하며 실패하면 기존 파일로 복원한다. 여러 파일 교체가 한 번에 원자적이라고 주장하지 않는다. 기존 token/sender/start/과거 cohort/자격 파일은 보존한다. 실제 승인 시각의 launcher 검사와 정적 release 검사를 구분한다.

## 검증·실행 결과

최종 전체 검증은3,047 passed·2 xfailed·기존 pykrx 경고1개·84.60초·exit0, 운영 상태/외부 네트워크 접근 시도0건이다. 독립 Astra/xhigh 검토에서 활성화33개·staging 실패 주입3개·주요 통합309개·원장/자본/호가153개를 각각 통과했다. 실제 모델/effective effort metadata는 미노출로 미검증이며 교차 공급자 검토로 부르지 않는다.

독립 검토의 P2 두 건(교체 직후 동기화 실패의 복구 목록 누락, 부분 기록된 drop-in 복구 실패)을 재현 후 수정했다. 최초 저장소+/tmp 혼합 테스트는 탐색 범위가 넓어져 비밀 파일 메타데이터 조회2건을 격리 장치가 차단해 exit4였고 내용은 읽지 않았다. 이후 경로를 분리한 검증은 통과했다. 현재 미해결 P0/P1/P2는 없다.

[PR #119](https://github.com/qwq-partners/qwq-ai-trader/pull/119)의 필수 [verify](https://github.com/qwq-partners/qwq-ai-trader/actions/runs/36866465559)는 3,047 passed / 2 xfailed·84.48초·격리0·비밀정보 검사 통과다. 병합 커밋 `5468208e2bf481eabae5c54da490c7da972756e1`의 전체 tree가 검토한 `2ae76e40`와 같고 소스263개 지문도 일치한다. 이 커밋을 실제 활성화 대상으로 고정했다.

최초 staging은 새 release를 기존 런처와 비교하는 검사에서 중단됐다. 기존 launcher/plan/registry/deployment 네 파일은 교체 전이었고 지문 불변, 새 cohort/활성화 설정도 없었다. 검사를 네 파일 교체 후의 복구 가능한 validation 안으로 옮겼다. 명시 inventory·root 소유권·기존 파일 지문이 일치하는 새 미완료 release와 백업만 정리한 뒤 다시 설치했다. 독립 Astra/xhigh delta 리뷰의 staging4개+정리12개=16개 합성 시험이 통과했으며 P0/P1/P2는 없다. 실제 모델 metadata는 미노출로 미검증이다.

수정 후 실제 service UID/GID/그룹으로 새 배포물의 정적 검증이 성공했다. 토스 서비스는 inactive를 유지했고, 기존 인증/토큰/발급 소비 기록은 보존했다. 이어 연구/manifest/once 입력과 root 실행 설정을 설치하고 날짜가 고정된 두 timer만 활성화했다. 별도 Astra/xhigh 예약 절차 리뷰도 승인됐다. `systemd-analyze verify`는 exit0이었고 기존 무관한 claude-session unit의 KillMode 폐기 예정 경고가 1개 나왔으며 해당 unit은 수정하지 않았다.

| 확인 항목 | 실제 결과 |
|---|---|
| 실행 예약 | `qwq-entry-capture-20261002.timer`: enabled·active/waiting, NextElapse **2026-10-02 08:55:00 KST**, Persistent=no, LastTrigger 없음 |
| 보존 만료 예약 | `qwq-entry-capture-retention-20261101.timer`: enabled·active/waiting, NextElapse **2026-11-01 09:46:10 KST**, Persistent=yes |
| 기존 봇 | active/running·PID3875500·NRestarts0·DropInPaths 없음, 운영 HEAD `7ddfb52` 유지 |
| 토스/실행기 | 둘 다 inactive/dead·PID0. 새 토스 서비스 자체는 disabled 유지; 단발 실행기가 필요 시 start |
| 보호 입력 | 기존 기본/override SHA·KR 매수 중지 유지. 설치 입력7개 지문과 소유권/모드 일치 |
| 일회 상태 | activation.receipt와 실제 봇 drop-in 없음. 설치된 연구/manifest를 읽기 검증했으며 엔진 원장은 아직 생성 전 |

배포물 SHA는 `28d25f2d867683cbc3ed4dd7ebd7b8429ec2ba764b6ec1931051bcfcf3ff13f3`, root 활성화 설정 SHA는 `f1318be4e077ccc9ac2f514744cad4ed85a276b1f301159445375b27157a3017`이다. 설치된 study SHA는 `d29a09634707e9e40b05f2f31d0a913d812ff06265fbac017c63d244557a1660`, 토스 plan SHA는 `2f11dc3cd6f3cbf43ac1381df0b2f879ae2ddc94eb20e49d1b2fbba493e59b1a`이다. 원본 공개 제안 JSON은 설치 전 고정 입력으로 보존하며 그 안의 준비 상태 표시는 이 실행 기록으로 갱신한다.

검토한 일회 운영 절차 지문: staging `2a86b9228a0d3f1caf8070609e0ed4f04ce0db0073ec88557212390156285f57`, 미완료 정리 `f85a78551130c48efee4b2e0a1171c82f72c8b6896eae55f52f21bee314e9b3b`, 예약 `ba236989efca501d5cdf950985558fbfcd8e3f38dc3816cf8d7b6de91d3fbf8b`. 미래 실행기 SHA는 `33dc0b40152f61817038591937e82f8886fd47d44528760e3ad935256ec50751`이다. 새 서비스 기동·토큰 발급·실호가 수집·주문은 아직 실행하지 않았다.

성공 기준은 자료 수집 자체가 아니라, 동일 후보·수량의 비용 후 진입 비교에 쓸 수 있는 원천을 확보하는 것이다. 종료 후 엔진 원장·토스 봉인 파일·종료코드를 대조하고 피한 손실·놓친 이익·unknown을 집계한다. 토스 KRX+NXT 통합 호가는 KIS 체결 증거로 바꾸지 않으며, 계좌 순수익은 청산·현금 재사용·운영비·KODEX200 기회비용까지 확인한 뒤 판단한다.


## 다음 세션: 수집 확인 뒤 가격 비교

[최신 체크리스트](monitoring-checkpoints.md)의 실행/봉인/모집단/출처 검사를 먼저 한다. 활성화 결과는 root 전용 `/var/lib/qwq-entry-capture/status.json`·`activation.receipt`에 남으며 예약 자체는 성공 증거가 아니다. 같은 승인/파일로 두 번째 시도를 하거나, 자료가 적다고 임의로 매수 중지를 해제하지 않는다.

첫 scan 전체를 분모로 유지하고, 미관측 부분집합·자료 결측·자본 한도 실패는 cash/0원으로 바꾸지 않는다. A는 전송 전 order_ready 진입 대리치, B는 같은 후보·수량의 비용 후 가격 게이트다. B가 allow면 같은 가격·수량이므로 차이0, B가 cash일 때 A의 손실은 피한 손실이고 A의 이익은 놓친 이익이다. baseline_cash는 가격 게이트의 개선으로 세지 않는다. 게이트 불명 비율과 900초 손익 미짝 비율을 따로 보고하고, 미짝이 남으면 complete_delta_net_pnl은 null이다.

토스 품질 보고는 아래 읽기 전용 CLI를 사용한다. 실행 전 실제 deployment의 plan/ledger 결합과 종료 상태를 대조한다. 토스 전용 UID의 허용된 읽기 범위로 실행하며 파일 권한을 넓히지 않는다.

```bash
/home/ubuntu/projects/qwq-ai-trader/venv/bin/python scripts/report_toss_candidate_observation.py \
  --artifact /var/lib/qwq-toss-observer/cohorts/2f11dc3cd6f3cbf43ac1381df0b2f879ae2ddc94eb20e49d1b2fbba493e59b1a/observations.jsonl \
  --plan-sha256 2f11dc3cd6f3cbf43ac1381df0b2f879ae2ddc94eb20e49d1b2fbba493e59b1a
```

이 보고는 KRX+NXT LOSSY 후보 품질용이며 profit_comparison_available=false·kis_execution_evidence=false다. KIS 가격 A/B에는 별도 검토된 세션/실효 판단/비용 보충 입력이 필요하다. 아직 실제 자료가 없으므로 그 파일 경로나 결과를 만들어 두지 않는다.

```bash
/home/ubuntu/projects/qwq-ai-trader/venv/bin/python scripts/compare_entry_price_shadow.py \
  --journal /home/ubuntu/.local/state/qwq-entry-observation/20261002-pilot1/engine.jsonl \
  --study /home/ubuntu/.local/share/qwq-entry-observation/20261002-pilot1/study.json \
  --evaluation-inputs <검토된-명시-보충입력.json> \
  --max-journal-bytes 67108864
```

명령은 해당 릴리스의 저장소 루트에서 실행한다. 위 꺾쇠 항목은 실제 검토 파일로 치환하는 설명 자리이며 그대로 실행하지 않는다. 공개 KRX 참조 요율과 양방향 추가 슬리피지0/10/30bp를 각각 표시하고, 이미 ask/bid에 포함된 스프레드를 이중 가산하지 않는다. 900초 가격 대리치는 손절/익절/부분체결·현금 재사용·계좌 수익 재생이 아니다. 첫 표본으로 새 전략이나 순수익을 확정하지 않고, 현재 엔진의 선정/진입 개선 후보를 고르는 데 사용한다.
