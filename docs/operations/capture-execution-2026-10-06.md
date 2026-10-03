# 10월6일 관측 설치·예약 — 46차 Plan → Do → See

> **47차 현재 상태:** 사용자 현재 승인으로10월2일22:17:04 KST 엔진 `df1a5af` 일반 모드 배포·재시작 완료. 신규 PID172854·자동 재시작0·매수 중지·기존 설정 유지, 실행 원장/DB 첫 기동 점검 통과. 22:28:56 KST 예약 실행기 교체·독립 리뷰 조건 검증·10월6일 timer 재개까지 완료했다. 아래46차는 이전 설치 당시 이력이다.

상태: **2026-10-02 16:48 KST 비활성 설치·예약·사후 검증 완료. 실제10월6일 기동·수집·수익 비교는 아직 수행하지 않았다.** [45차 적용안](entry-capture-installation-2026-10-06.md)에 대한 사용자의 “ㄱㄱ 해도 돼” 승인을 따른다. 목적은 **어떤 종목을 고르고 어느 가격·시점에 진입해야 거래·운영비 이후 순이익이 좋아지는지** 실제 근거를 확보하는 것이다. 관측 성공은 수익성 확인이 아니다.

## Plan — 승인 범위와 고정 시각

운영 읽기 점검, 새 입력·불변 토스 배포물 설치, 날짜별 예약을 수행한다. 봇에 적용할 대상은 PR #137 병합 `df1a5afb5f3150052df16e7823a7129cf91d2198`, tree `03cd63ada0030047e371270951ba9d30dbaf43a9`다. 운영 기준 `5468208`에서 이 대상에는 관측 외에 취소 후 체결·실행 원장·장부 저장 변경도 포함된다. 기존 override와 KR 매수 중지는 보존한다. 이번 설치 시점에 봇을 재시작하거나 주문하지 않는다.

| 한국 시각 | 예정 작업 |
|---|---|
| 2026-10-06 08:55 이상, 08:57 미만 | 지문·상태·새 grant 확인 후 검토 코드 적용, 봇 재시작 요청 최대1회, 정확한 study/epoch 준비 확인 후 토스 시작 |
| 09:15 이상, 09:17:30 미만 | 첫 반환 스캔 전체 최대100개 보존, 원래 앞3개만 추가 호가 관측 |
| 09:33 | 관측 종료와 원장 봉인 |
| 09:34 | grant 만료; 연결·발급/송신 종료 확인 필요 |
| 2026-11-01 09:46:10 | 기존10월2일 토스 cohort의 보존 만료 정리 |
| 2026-11-05 09:34:10 | 새10월6일 토스 cohort의 보존 만료 정리 |

활성화 timer는 `Persistent=false`다. 창을 놓치거나 실패하면 날짜·영수증을 재사용하지 않는다. 예약은 실제 시장 세션·데이터 품질·수익을 보증하지 않는다. 휴장/세션 근거와 정규장 상태는 당일 확인해야 한다.

## Do — 점검 및 결합

설치 전 봇 PID34408, active/running, NRestarts0, 운영 HEAD5468208을 확인했다. loopback의 broker/risk 미완료 주문·수량·매도 수는0이다. 이 값은 메모리 상태이며 증권사 실제 주문·늦은 체결·저장 대기열의 완전성을 인증하지 않는다. 설정 지문은 default `81b7cd2545e18e6e40dfb1e7586310a5bd8a1ac4e599f8d05b2696569efa1938`, override `4970f2bf8ebfe6c5ba408336204cde07a8c80669cc9e5ba4edb0761b670b6d68`이다. 실행 중 메모리의 모든 실효 설정을 검증했다는 뜻은 아니다.

기존 토스는10월2일09:24:35의 저장 결과에서 `websocket_incomplete` / `frame_limit`이며, 서비스는 실패 종료 상태였다. 토스 인증값은 읽지 않고 ready 상태·generation4·서비스 identity와 UID997/GID987·전용 상태 경계만 대조했다. 토큰·발급/송신/시작 기록은 보존한다.

이전 운영 코드에는 실행 SQLite 원장이 없으므로 실행 원장 파일0개가 예상 상태다. 캐시 부모는 UID1000,0755, 심볼릭 링크 없음이다. PostgreSQL도 읽기 전용 조회에서 신규 실행 관련 열0개로 기존 코드와 일치했다. 신규 버전 기동 후 원장 생성·오류 여부는 별도 확인해야 한다. 단순 broker connected만으로 새 실행 원장이 정상이라고 판정하지 않는다. 살아 있는 SQLite 파일을 임의 복사·초기화하거나 과거 체결을 replay하지 않는다.

새 입력은 개발 설정 참조를 운영 **디스크** 설정 지문으로 바꾸고, 검토 대상의 src/scripts Python284개 전체를 함께 결합했다. 거래 정책·점수·후보 순서·수량을 바꾸지 않았다. `capital_policy`는 여전히 없고 수량/자본 근거가 없으면 경제성은 unknown이다.

| 결합 항목 | SHA256 |
|---|---|
| 설치 입력 제안 원본 | `f51f6a9106b3d3a629b8c49fde2091c51d28cdca3bb1fd33d63d14d6802b0f13` |
| 대상 소스284개 manifest | `e819a2c3f623b41b08828b16789382bbe53169651a423a485c34e5034b148c67` |
| study 원본 | `a4c46b8c509ebc2d98401db3fcbc54388d1a706a52bb85ea991887f2e1b890f8` |
| 엔진 manifest 원본 | `ac88a8b64e85976a6afcc97625bd8924c17da0a2b7a08d48fa4aad34b52324b5` |
| once 요청 원본 | `c85e536ea94a744abc60abc7fcb245335309db0df2542700dd67d25aa96fe533` |
| 토스 plan 원본 | `16d098d4813ba770b7a50c35f63c26c4c7ae7bf99f8b4a9dc1892da375608798` |
| 토스 plan canonical / cohort | `01634caab3fb1e2e390cc04287f13f974fe96e7e2127c356096a97dac460e062` |
| 토스 불변 artifact | `d446656bee5d34e1a1961a224a0409c3d7110142673d0dbd0e6af3f394cbaef5` |
| 활성화 실행기 | `cc5b190af2ed8410a20728625249c9dc6cf880e6c692d9b5ada29620cc72506e` |

공개45차 JSON은 당시 검토안으로 보존한다. 실행용 원본·비공개 identity/등록부·설치 영수증을 저장소에 복사하지 않는다.

### 보존 예약 연결 수정

기존11월1일 timer가 공용 deployment를 읽는 삭제 서비스를 호출했다. 새 deployment로 교체하면 기존 cohort를 정리하지 못하는 문제다. 날짜별 서비스가 해당 날짜의 deployment/plan/registry/launcher/retention5개 파일을 root 소유 스냅샷에서 필수 읽기 전용 bind로 보게 한다. 서비스 내부 고정 경로와 기존 검증·삭제 코드는 유지한다. 각 불변 release도 보존한다.

스냅샷은 `/etc/qwq-toss-observer/retention/20261002`와 `/etc/qwq-toss-observer/retention/20261006`에 둔다. 날짜별 서비스는 같은 UID/GID·격리·자원 제한을 유지하며 쓰기 허용은 기존 토스 상태 디렉터리뿐이다. 매일 실행되는 공용 보존 timer는 disabled/inactive를 유지한다. 당장 삭제하거나 토큰·안전 기록을 지우지 않는다.

검증은 같은 서비스 사용자·격리·5개 bind를 적용한 임시 서비스 안에서 지문·소유권·release·`load_verified_document`까지만 수행한다. 과거 또는 미래 grant를 현재 시각의 `launcher --check`로 검사하지 않는다. 실제 삭제는 각 만료 시점의 기존 시간·서비스 상태·sender 잠금·inode/영수증 검사를 통과해야 한다.

## See — 검증 원장

- 운영 API/자료에 접근하지 않는 임시 입력 검증: study와 plan의 raw/canonical hash, CapturePlan 적재 통과.
- 설치 파일 교체: 성공, 검증 실패, 교체 직후 디렉터리 fsync 실패의3경로에서 예상 게시/복원 확인.
- 활성화·launcher·retention 관련145검사 통과, 운영 상태/외부 네트워크 접근 시도0건.
- 독립 중요 작업 리뷰: 실제 `claude-opus-5`, 요청 xhigh(실효 effort 메타데이터 없음), 도구 비활성,600초/$6 한도, 전역 모델 정책 전달. 첫 광범위 검토는600초/968이벤트에서 시간 초과하여 승인으로 사용하지 않았다. 같은 모델·effort·권한의6.12초 연결 점검 후 이미 검토된 의존성을 제외한 설치 부분 검토가289.34초에 완료됐다.
- 조건부 차단 B1~B4를 한 차례 보완했다. 실제 서비스의5개 bind 일치/마지막 Service 절 확인, 설치 지문3개 고정, 루트 보호 artifact에서만 import·bytecode 생성 금지, umask022 고정이다. 새16검사의 실패를 먼저 확인하고 수정 후16건 모두 통과했다. 교체 성공·검증 실패·fsync 실패3경로도 재확인했다. 구현자가 독립 승인을 했다고 표현하지 않는다.
- 기존 대상 소스 전체4045 passed / 2 known xfailed / 1 warning,122.93초, 문법·비밀정보 검사 통과. 제품 코드는 이번 설치에서 수정하지 않았으며 별도 설치 스크립트 수정은 위16검사와 실제 설치 검증으로 확인했다.
- 실제 root 소유 원본에서259개 inventory 파일+manifest1개(총260개)를 검산했다. 서비스 UID/GID에서 새 release/identity 적재, 양쪽 날짜 namespace의5개 파일·소유권·지문 검증을 통과했다. **공용 설정을 새 날짜로 교체한 뒤에도 이전 namespace가 이전 cohort를 적재하는 것을 재확인했다.**
- 두 보존 서비스의 실행 시작 시각과 InvocationID는 비어 있으며 실제 삭제는 수행하지 않았다. 일반 매일 보존 timer는 disabled/inactive다.11월1일 timer 재적재 후 보이는 LastTrigger는 보존된10월1일22:15:39 stamp의 시각이며 이번 삭제 실행 증거가 아니다. 다음 실행 시각은11월1일09:46:10이고 신규11월5일 timer는09:34:10, 신규 활성화 timer는10월6일08:55:00으로 각각 확인했다.
- 설치 후 봇 PID34408 / NRestarts0 / HEAD5468208, 동일 설정 지문·매수 중지·기존 실행 인자를 확인했다. 소비된10월2일 drop-in은 정확한 원본을 root 전용 백업에 fsync·검산한 뒤 제거하고 daemon-reload만 했다. DropInPaths는 비어 있다. 토스는 inactive/dead, 새 cohort 디렉터리는 비어 있으며 토스 관측 원장 파일, 새 activation/startup receipt와 엔진 원장도 없다.

### 실제 설치 경로와 감사 근거

- 엔진 입력 `/home/ubuntu/.local/share/qwq-entry-observation/20261006-pilot2`: UID1000/0700, study·manifest·once 각각0600.
- 엔진 상태 `/home/ubuntu/.local/state/qwq-entry-observation/20261006-pilot2`: UID1000/0700. 결과 파일은 미리 만들지 않았다.
- 활성화 JSON `/etc/qwq-entry-capture/20261006-pilot2/activation.json`: root/0600, SHA256 `2cfb13b891c8e18fca8651460cc426e55445a9edde469ae32cb1ee2f6bc9e7d5`. 상태는 `/var/lib/qwq-entry-capture/20261006-pilot2`, 실행기는 `/usr/libexec/qwq-entry-capture/activate-20261006.py`다.7개 input hash와 study 원본 hash의 일치를 재확인했다.
- 토스 새 deployment config hash `45579a3eee843390199584fa57819023b794cd09c715944e172e6a8ad21d0c40`. 루트 스냅샷·새 grant/plan이 동일 artifact/cohort에 연결됐다. 서비스는 시작하지 않았다.
- 이전 공용 파일과 소비된 drop-in 백업은 `/var/backups/qwq-entry-capture-20261006`. 설치 스크립트·비공개 입력·리뷰·검증 결과·`installation.receipt.json`은 `/var/lib/qwq-entry-capture/install-20261006` root/0700 아래에 보존한다. 실행 입력이므로 Git에 넣거나 자동 재실행하지 않는다.
- 설치 스크립트 SHA256: `stage_toss.py` = `291ef790b123a9842fb3f94a58f4859089cdcbb8bb4c5a2803adb5427ca6e70b`, `retention_bundle.py` = `27a8010894292ef630f644faa1b8801e2cc32c26d181187b1cf261ea2ddc8b4c`, `arm.py` = `d872f0171c6170ce8ec056fccf3f48763813e369ac66b55726b17a341d87a619`.
- 최종 읽기 전용 verifier SHA256 `50d45cefa9e0372d2aa69e31a2a814421df6bc0a0b81af9a4a29fd083d02885a`, 검증 결과 SHA256 `1f6bdd7459b751e4ebb3f8177d861afb25398c31646eee3e07d6427677ba6f36`.

초기 원본 복사 준비는 inventory259개와 manifest 포함260개를 혼동한 검사에서 파일 생성 전에 중단했다. 지문 불일치가 아니며 개수 구분을 바로잡은 뒤 동일 artifact를 검산했다. 최종 verifier도 기존 persistent timer의 stamp를 신규 timer처럼 비어 있다고 가정한 검사를 수정했다. 실제 기존 stamp와 두 삭제 서비스의 미실행을 확인했고 stamp/원장을 초기화하지 않았다. systemd 검증의 기존 `claude-session.service` KillMode 경고는 이번 서비스와 무관하며 해당 사용자 세션을 변경하지 않았다.

활성화 서비스의 TimeoutStartSec는 독립 리뷰 조건에 따라120초로 제한했다. 실행기는08:57 절대 경계를 각 명령과 재시작 직전에 검사한다. 예외 경로의 rollback과 프로세스 강제 종료는 구분한다. 강제 종료·전원/I/O 장애에서는 rollback 완료를 보장하지 않으므로 receipt만 있고 status가 없으면 HEAD·drop-in·PID를 읽기 점검하고 임의 재시작/재실행하지 않는다.

## 다음 세션 인계 — 순이익 개선으로 연결할 순서

1. 장전 실행 결과: 정확한 target/study/epoch, 재시작 요청 횟수와 실제 NRestarts, 새 원장 생성·저장 오류, pending·매수 중지를 확인한다. 실패 receipt 삭제, 자동 재시작 반복, 토스 단독 실패로 봇 재시작을 하지 않는다. 기동 후 DB/원장 확인은 읽기 전용 메타데이터·오류 건수로 제한하고 실자료를 출력하지 않는다.
2. 09:33~09:34 종료 후 KIS 봉인/드롭·토스 frame/file cap·cleanup·소켓 종료를 확인한다. 후보 전체를 분모로 원천별 점수·캐시·누락과 첫 진입 탈락/미도달/unknown을 보고한다. 버전·국면·거래장 차이를 유지한다.
3. 유효 수량·자본·호가 쌍이 있으면 같은 종목/수량에서 사전 고정한 가격 조건 하나를 비교한다.900초 bid, 비용과0·10·30bp 슬리피지, 회피 손실과 놓친 이익을 함께 본다. 없으면 수익을0으로 만들지 말고 관측된 병목 하나를 개선 대상으로 고른다.
4. 독립 기간 검증 후 같은 진입으로 청산을 대조하고, 열린 보유·현금·입출금·거래비용·AI/자료/서버비를 포함한 계좌 순이익을 확인한다. 전체 KODEX200과 대칭적인 반도체 제외 보조 비교를 함께 유지한다. 반도체 급등을 이유로 전체 기회비용을 없애거나 과거 미완성 엔진의 성과를 현재 버전의 성과로 간주하지 않는다.

실제 관측·유효 가격 비교·수익 우위·매수 재개는 각각 별도 판정이다. 이 설치로 매수 중지를 해제하지 않는다.

## 47차 Plan — 엔진 선배포와 예약 관측 분리

검토된 엔진 `df1a5afb5f3150052df16e7823a7129cf91d2198`을 현재 호스트에 일반 모드로 먼저 적용한다. 루트 전용 예약 실행기는 별도 지문으로 설치하고,10월6일 고정 프로필에 한해 `old_head == new_head`를 허용한다.10월2일 계약은 계속 거부한다. 엔진 소스284개/study/once/grant/시간/코호트는 변경하지 않는다. 엔진 저장소의 실행기 파일과 systemd가 호출하는 루트 설치 실행기의 버전은 의도적으로 다르므로 두 지문을 따로 기록한다.

이미 배포된 경우 checkout을 생략하되 현재 HEAD·실제 소스 바이트·설정·입력·매수 중지·pending·grant 검증을 유지한다. 재시작 전 실패는 checkout 여부와 관계없이 자신이 만든 drop-in inode만 정리한다. 재시작을 한 번 요청한 뒤에는 rollback·재요청을 하지 않는다.10월6일08:55 관측 연결을 위한 별도 재시작1회는 여전히 필요하다.

실행 순서는 기존 설치 상태 확인 → 미래 활성화 timer 일시 중지 → 기존 로컬 배포 절차로 엔진 적용/전체 검증/재시작 → 신규 PID·원장 메타데이터·DB 열·저장 오류 점검 → 루트 실행기/활성화 JSON 원본 백업·지문 고정 교체 → 전체 계약 확인 → 미래 timer 재개다. 일반 배포 스크립트의 실패 시 코드 rollback·재시작은 원장/추가 DB 스키마를 되돌리지 않는다. 배포 또는 사후 점검 실패 시 미래 timer를 보류하고 저장 근거를 보존하며 자동 재시도·원장 초기화를 하지 않는다.

새 동일 버전 경로는 성공·현재 HEAD/소스 변조·입력/매수 중지/pending·창 만료·부분 파일 쓰기·inode 교체·재시작 요청 실패·재실행 거부를 검증한다. 두 날짜 프로필 관련125검사와 전체4069 passed/2 known xfailed를 확인했다. 이는 준비 시점의 검증 기록이며 실제 적용·독립 리뷰·최종 서비스 상태는 아래47차 Do / See에 기록한다.

## 47차 Do / See — 10월2일 일반 모드 배포

사용자 현재 요청으로22:17:04 KST 검토된 엔진 `df1a5af`를 기존 로컬 배포 절차로 적용했다. 운영 checkout 자체에서 전체4045 passed / 2 known xfailed / 1 warning,127.14초와 문법·비밀정보 검사를 통과했다. 정상 적용 경로로 재시작했으며 rollback은 없었다. 이전 PID34408에서 PID172854, active/running, NRestarts0이다. NRestarts는 systemd 자동 재시작 횟수이며 이번 명시 재시작0회라는 뜻이 아니다.

실행 인자에 관측 옵션이 없고 DropInPaths도 비어 있다. default/사용자 override의 지문과 KR 매수 중지를 그대로 보존했다. broker connected와 broker/risk 미완료 주문·수량·매도 집계4개 모두0이다. 이는 메모리 집계이며 증권사 원격 주문·저장 대기열의 완전성 인증은 아니다. 토스는 inactive/PID0이다.

실행 SQLite 파일은0→1개, UID1000/0644·일반 파일·하드링크1·크기32768바이트로 첫 생성 메타데이터를 확인했다. PostgreSQL 읽기 전용 열 수는0→7이다. 살아 있는 SQLite 내용은 열거나 복사하지 않았다. 현재 서비스 InvocationID의 시작 로그147행에서 장부 DB 연결 성공1개, 실행 원장/장부 저장/ERROR·CRITICAL·traceback 오류0개를 확인했다. 첫 점검은 journal의 MESSAGE144개가 UTF-8 바이트 배열인 것을 문자열처럼 비교하여 성공 로그0으로 거부했다. 문자열3개와 바이트 배열을 엄격히 해독하고 ANSI 표시를 제거하는 점검기를8검사로 검증했다. 정상/실패 문구를 모두 보존하며 잘못된 형식은 계속 거부한다. 운영 엔진 오류 수정이나 성공 조건 완화가 아니다.

예약 실행기 코드 변경은 [PR #139](https://github.com/qwq-partners/qwq-ai-trader/pull/139), 검토 코드 commit `e686949f0a9334e152070f2b31dd3c4f4fc166d4`, 병합 `c60ce3a8bf0416cbc8be3b1b277cdeeb1ba5a032`다. 관련125검사, 전체4069 passed/2 known xfailed, CI도4069 passed/2 known xfailed/33 warnings,94.82초로 통과했다. 운영 엔진은 계속df1이며 루트 실행기만 별도 배포한다.

첫 독립 리뷰는 실제 `claude-opus-5`, 요청xhigh(실효 effort 미확인), 도구 비활성·전역 정책 전달,433.61초에 완료했다. 엔진 배포/실행기 변경은 승인됐으나 운영 설치 도구의 환경 격리·교체 전 선행 점검·실제 출력 경로·systemd 실행 연결4조건은 보완 전 차단됐다. 루트 설치 도구를 실행하기 전에4조건을 보완하고18개 검사를 통과했다. 원본 입력은 `.local/share`, 실제 출력은 `.local/state`로 고정되어 있으며 출력 경로와 디렉터리 존재/소유권을 입력에서 대조한다.

관측 timer는 전환 중 disabled/inactive로 두었고,22:28:37 KST 루트 실행기/활성화 JSON 교체와 정적 적재·원본 백업을 완료했다. 중지 상태의 전체 검증을 통과한 뒤22:28:56 KST enabled/active로 재개했다. 다음 실행은10월6일08:55:00 KST, Persistent=no, LastTrigger 비어 있음이다. 활성화 서비스도 inactive, InvocationID/시작 시각 비어 있음, NRestarts0으로 실제 미실행을 확인했다. 기존/신규 보존 timer와 원본 지문·격리 연결은 유지된다.

최종 독립 리뷰는 실제Opus5/xhigh 요청,487.12초에 설치 도구를 승인하고 timer 재개에는 세 조건을 부여했다. 추가 실행/환경 지시 부재·실행 파일/argv/유일 명령·service 미실행을 확인하고, 실행기 bytes의 literal 지문을 receipt와 함께 대조하도록 보완했다. systemd의 실제 속성명 `EnvironmentFiles`를 사용했으며, 빈 복합 배열 속성은 출력에서 생략되는 것을 확인해 빈 값으로 처리하되 unit 원본 지문도 별도로 고정했다. 입력 환경 격리·선행 점검·경로/소유권·파일 교체·실행 연결31검사와 로그 형식8검사(총39개)를 통과했고, 실제 중지/예약 상태 검증도 모두 통과했다. 이는 리뷰 조건을 보완하고 검증한 기록이며 구현자가 독립 승인을 했다는 뜻이 아니다.

로그 해독 변경만의 독립 리뷰는 별도로 승인됐다(실제Opus5, 요청xhigh,28.67초). 앞선 넓은 로그 점검 검토는180초/283이벤트에서 시간 초과했으므로 승인으로 사용하지 않았다. 동일 모델/effort/권한의5.59초 연결 점검 후 순수 해독 함수와 검사로 범위를 좁혔다. 모델/권한 변경이나 실패 우회는 없었다.

최종22:28:56 검증에서 실행 원장1개·DB 열7개·장부 연결1·현재 InvocationID 로그157행의 저장/실행/오류0을 재확인했다. 동일 PID172854/자동 재시작0, 매수 중지·설정 유지, 봇 관측 인자/drop-in 없음, 토스 inactive·새 cohort 비어 있음, activation/startup receipt/engine 원장 부재를 확인했다. 현재 서비스 건강 상태가 수익성·과거 체결 대사·원장 내용 완전성의 증거는 아니다.

### 루트 설치 지문과 보존 근거

- 운영 엔진은 `df1a5afb5f3150052df16e7823a7129cf91d2198`로 고정한다. 루트 실행기 구현은PR139의 `e686949f0a9334e152070f2b31dd3c4f4fc166d4`에 해당하며 별도 설치됐다.
- 루트 실행기 SHA256 `6d87de20ad5536decf801b22d7049c458b6a8109507a242dc34185d3f3c69ff2`.
- 활성화 JSON SHA256 `98b72aceebb2bc2883dd8ae76bc42a7c8d1d97c41fd0bd559aca573ee9fb92da`. 기존 JSON 의미 중 old_head만df1로 바꿔 old/new를 같게 했다.284개 소스·설정·7입력/study/once/grant/cohort/시각은 보존됐다.
- 루트 updater SHA256 `6c85df9372a7ab8dec65dc4ce2ed2ef86b6cff2454050370b15420c74258ed02`, 최종 verifier SHA256 `eff3d5f626067abb48dad208064546652a651ede3d5af1e7125f19843c5b997c`.
- 활성화 service 원본 SHA256 `6b5f2d6d52c8be1125d6ada6a4b04f24cf5819c29bd7b71a7f7eab88829080e7`. 호출은 고정 루트 실행기와 `--profile 20261006-pilot2`로 연결된다.
- 설치 코드·검사·리뷰·이전 executor/activation 원본·`update.receipt.json`·배포 후 근거는 `/var/lib/qwq-entry-capture/predeploy-20261002` root0700에 보존한다. 첫23개 파일의 stage manifest SHA256 `52916d36be73a1b6807dcc79e2317bf05991f955f48cbe92ea207613c5bbb4b9`. 이후 최종 결과/문서 통합 영수증은 별도 추가하고 기존 파일을 덮어쓰지 않는다.
- 이번 검증기는10월2일의 일반 모드/미수집 상태를 확인하는 고정 검증기다.10월6일 관측 기동 뒤에는 이 상태를 기대하지 않으며 위 인계 순서의 당일 활성화/수집 결과를 판독한다.46차의 이전 PID/HEAD 검증기를 현재 상태 검사로 재실행하지 않는다.

### 이후 설정 변화·실패 처리

- 엔진 코드 배포와10월6일 관측 기동은 다르다.10월6일08:55에는 같은df1 소스 확인 후 별도 재시작1회가 필요하고, 실제 수집은09:15~09:33이다.
- 운영 진화가 override를 바꾸면 현재 고정 지문과 달라져10월6일 관측이 중단될 수 있다. 이 가능성을 수용하며 설정/지문을 자동으로 재고정하지 않는다. 새 설정을 유지하려면 변경 내역 검토 후 별도 실행 입력 갱신이 필요하다.
- `rollback` 문구만 믿지 않고 실제 HEAD·drop-in 부재·PID를 확인한다. 부분 stage/restore 파일이나 이미 소비한 백업/영수증이 있으면 조사하며 임의 삭제·자동 재시도하지 않는다.
- code rollback은 실행 원장/DB 추가 구조를 되돌리지 않는다. 원장을 초기화하거나 과거 체결을 replay하지 않는다.
- 동일 HEAD의 git diff는 변경 검증 근거가 될 수 없으므로 실제284개 소스와 두 설정 지문을 계속 확인한다.

### 다음 PDS 우선순위

1.10월6일 관측 기동/봉인 품질 확인 → 전체 후보의 원천 상태와 첫 진입 탈락 원인 집계. 선정과 진입을 각각 평가한다.
2.실제 수량/자본과 호가가 확보된 후보만 동일 조건에서 가격 가설1개를 비용 후 비교한다. 유효쌍이 없으면 수익률을 만들지 않고 확인된 병목을 개선한다.
3.독립 기간에서 같은 진입의 청산 차이를 검증하고 실제 계좌 순이익에 연결한다. 전체 KODEX200과 대칭 반도체 제외 보조 비교, 버전/국면 차이, 열린 보유분/현금/입출금/거래·운영비를 유지한다.

## 48차 Do / See — 10월3일 엔진 재배포와 관측 입력·토스 grant 재등록

사용자 결정("P1-3·P1-4도 수정, 문서·커밋·푸시·배포까지" → 배포 시점 질문에 "지금 배포 + study 재등록" → 범위 질문에 "study + 토스 grant 전체 재등록")에 따라 수행했다. 정본 리뷰는 [48차 리뷰](../reviews/codex-recent-work-review-2026-10-02.md).

**엔진 배포(01:13~01:15 KST).** `scripts/deploy/local_deploy.sh 2645820`(PR #141 머지 커밋). 운영 체크아웃에서 전체 4086 passed / 2 xfailed / 132.35초·문법·비밀정보 검사 통과 후 재시작, rollback 없음. PID 172854 → **249914**, 01:15:45 KST 기동, NRestarts 0, HEAD `2645820` detached, `config/evolved_overrides.yml` 미커밋 수정 보존. 시작 로그 KIS 연결·TR 세트 legacy·엔진 시작 정상, 새 PID ERROR/Traceback 0, 이전 세션은 `종료 완료`로 정상 종료. 기동 뒤 ops 점검: KIS 오류 0, pending `[]`, 정체/실패 없음, 현금 비중 22.9%·보유 2종목. 새 세션에 원장 보류 로그 없음. 매수 중지·설정 지문 불변.

**재등록 필요성.** 10월 6일 프로필은 old/new HEAD·284개 소스 해시를 df1a5af로 고정했고, 사전등록 `study.json`의 `capture.source_version_ref`가 `commit:df1a5af;capture-source-sha256:e819a2c…`(활성화 실행기 포함 284 파일 `{path: sha256}` compact sorted JSON의 SHA-256)를 담았다. 독립 리뷰가 추가로 잡은 것: 토스 `plan.json`의 `websocket.engine_study_sha256`이 옛 study 해시라 study만 바꾸면 토스 WS 서비스가 첫 엔진 응답에서 `engine_identity_or_time_invalid`로 실패하고, plan 해시는 `deployment.json`·`registry.json` grant·보존 스냅샷·활성화 `input_hashes`에 묶여 있다.

**재등록(01:41:46 KST, 루트, `/var/lib/qwq-entry-capture/rereg-20261003/rereg_20261006.py`).** 바꾼 것과 지문:

| 파일 | 변경 | 새 SHA256 |
|---|---|---|
| `~/.local/share/qwq-entry-observation/20261006-pilot2/study.json` | 두 `source_version_ref` → `commit:2645820760d0…;capture-source-sha256:e2104b9e…`(285 파일) | `f77d450d…` |
| 같은 디렉터리 `manifest.json` | `study_sha256` | `b8e6bf55…` |
| `/etc/qwq-toss-observer/plan.json` | `websocket.engine_study_sha256` | raw `9c156615…` / canonical `1a454e7e…` |
| `/etc/qwq-toss-observer/deployment.json` | plan 두 해시·cohort 경로·`grant_id` → `toss-entry-20261006-pilot2-r2`·`config_hash` `473c667e…` | `03b44a31…` |
| `/etc/qwq-toss-observer/registry.json` | grant 1개 재발급(같은 identity·시각·capabilities, `approval_reference=user-explicit-phase48-reregister-20261003`) | `0e70cfc1…` |
| `/etc/qwq-toss-observer/retention/20261006/{plan,deployment,registry,hashes}.json` | 라이브와 동일 바이트로 갱신(11/05 보존 timer bind) | — |
| `/etc/qwq-entry-capture/20261006-pilot2/activation.json` | old/new HEAD `2645820…`, `source_hashes` 285개, `input_hashes` 5개, `study_sha256` | `04be26ce…` |
| 새 cohort `/var/lib/qwq-toss-observer/cohorts/1a454e7e…` | 997:987/0700 생성 | — |

바꾸지 않은 것: `once.json`, 런처·retention.py·활성화 실행기 바이트, 토스 release artifact, 토큰·발급 기록·시작 영수증, 시각(`not_before` 10/5 23:55Z)·epoch·`request_id`, 보호 설정 지문(default `81b7cd25…`, override `4970f2bf…`), systemd unit. 옛 cohort `01634caa…`(빈 디렉터리)는 남는다. 봇 재시작·토스 시작·KIS/토스 호출 없음.

검증: 기존 파일을 같은 직렬화로 바이트 재생산 확인 뒤 교체(실패 시 역순 복원·cohort 제거·타이머 재개·rollback 영수증). 교체 후 런처 `verify_release`(루트)와 서비스 UID에서 `load_verified_document` + 승인 로더(`load_authority`, grant 창 안 시각 주입) + `_validate_policy`, 활성화 실행기 `load_config` + 08:55 preflight 동등 검사(HEAD·detached·dirty·소스/보호/입력 해시·drop-in·pending·kill·상태 디렉터리) 통과. 타이머는 enable 유지 상태로 stop/start만 했고 다음 실행 `Tue 2026-10-06 08:55:00 KST` 확인. 독립 리뷰 2회(요청 Opus/high): 1차가 토스 plan 결합 P0를 잡아 범위를 확장했고, 2차가 승인 로더의 시각 창 검사(현재 시각 `approval_expired`)·작업본 import·타이머 disable 문제를 잡아 반영했다. 영수증·원본/신규 바이트·스크립트는 `/var/lib/qwq-entry-capture/rereg-20261003/`(root 0700).

한계: 재등록은 10월 6일 08:55 실행기의 사전 검사와 토스 결합을 복원한 것이며 실제 기동·수집·수익 비교는 여전히 예정이다. 그 전 운영 override 지문이 바뀌면 관측은 중단된다. 공개 45차 제안 JSON의 base 지문은 당시 검토안으로 보존한다.


## 49차 — P1 수정과 관측 후 배포 예약 갱신

사용자는 독립 리뷰의 P1 네 건을 격리 브랜치에서 수정·검증하고, 통과한 커밋으로 기존 10월6일 16:00 배포 예약의 대상만 갱신하는 순서를 승인했다. 즉시 운영 배포·재시작·매수 재개는 포함하지 않는다.

Plan: 운영 `2645820`과 08:55 관측 체인의 HEAD·소스·study·manifest·토스 grant·보호 설정을 보존한다. `~/.local/share/qwq-deploy/deploy_after_capture_20261006.sh`의 `TARGET`만 검증된 후속 SHA로 교체하며, cron 시각·`EXPECT_HEAD=2645820`·한 번 실행 영수증·기존 가드는 유지한다.

Do / See: 제품 수정과 독립 리뷰 근거는 [49차 리뷰](../reviews/codex-claude-followup-review-2026-10-03.md)를 따른다. 설치된 배포 스크립트는 실행하지 않고 격리된 DRY 사본에서 정상/날짜/시각/HEAD/미체결/봇/관측 active·activating/토스/이미 시도한 영수증의 10개 가드를 확인했다. 실제 예약 대상 교체는 제품 검증·리뷰·CI 통과 뒤 수행하고 아래에 확정 SHA와 보존 검증을 기록한다.

현재 이 개발 기록 시점에는 예약 `TARGET=2f5cbae`가 남아 있으며, **예약 갱신 완료 기록이 생기기 전에는 수정본의 자동 배포가 준비됐다고 판단하지 않는다.** 실제 교체 기록과 완료 근거는 후속 문서 갱신으로 남긴다. 운영 HEAD가 관측 전에 변경되면 기존 관측과 배포의 선행 HEAD 검사가 중단되므로 임의 재등록하지 않는다.
