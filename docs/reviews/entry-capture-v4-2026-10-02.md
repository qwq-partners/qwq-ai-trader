# 10월6일 v4 관측 — 독립 검토·검증 원장

기반 `6a22b82d46bb21414c21a5b4eb7c90e22d25a920`, 격리 개발선 `feature/v4-capture-20261006`. [고정 입력·전체 적용/실패 흐름](../operations/entry-capture-installation-2026-10-06.md)을 함께 읽는다. 목표는 선정/진입 개선에 필요한 실제 원인을 확보하는 것이며, 이번 결과는 운영 실행이나 순수익 입증이 아니다.

## 역할과 범위

통합·구현은 루트 Astra, 용량 검증과 제안/절차 분석은 각각 별도 Sol/high 작업공간이다. Sol 실제 모델/실효 effort는 메타데이터 미노출로unverified다. 독립 중요 소스/프로세스 검토는 외부 `claude-opus-5`, 요청effort xhigh, 응답 model 및 terminal modelUsage에서 실제Opus5를 확인했다. 실효effort는unverified다. 작성자가 자기 중요 변경의 최종 리뷰어 역할을 맡지 않았다.

외부 검토에는 canonical model-routing 정책과 공개 소스/테스트/절차만 전달했다. tools0·MCP없음·safe mode·settings 제외·no-session-persistence, 최대600초/$6, startup60초/idle150초/절대시간 제한과 부분 스트림 진행을 검사했다. hidden thinking 내용은 저장하지 않았다. 첫 준비 호출은 로컬python 경로 오류로 prompt 생성 전에 실패했으며 교정 후 실행했다. 모델/권한 변경이나 실패 우회는 없었다.

1차 검토208.37초, 최종 delta 검토262.25초, 모두exit0/성공terminal/actual model `claude-opus-5`. 최종 판정은 **소스 차단 결함 없음, 아래D1~D4 근거 확인을 조건으로 로컬 통합 허용**이다. 실제 운영 사전점검·승인·설치는 여전히 미완료다. 모델의 검토 완료와 운영 권한을 구분한다.

## 검토 지적과 처리

| 항목 | 최종 근거와 처리 |
|---|---|
| 날짜별 입력/상태·owner·일회 실행 | 두 날짜 모두 전체 상태 기계 테스트. 모듈의 기존 state를 소비된 다른 디렉터리로 바꿔도 선택 날짜 receipt/status만 사용하는 검사로 보완 |
| 연구 파일 hash 불일치 | `load_config`에서 원본 study hash와 고정 input hash 교차 확인. 신규/기존 프로필의 불일치4검사가 수정 전 실패하고 이후 통과 |
| D1 원본/정규화 hash 의미 | `CapturePlan.load`는 읽은 bytes에 `hashlib.sha256(raw)`를 적용해 plan.study_sha256에 저장한다. projection이 그 값을 그대로 전달한다. 실제 activation JSON도 이 invariant를 시간 창 전에 검증하도록 명시 |
| 기존 parser/epoch 호환 | `scripts/run_trader.py`의 `--entry-observation-once`→claim_once_manifest→CapturePlan.load 경로 확인. `_WindowBuffer`는 context epoch를 그대로 받고 projection schema는v2 유지. 임시 경로로 새 v4 plan 실파서 검증. 실제 최종 release SHA/운영 loopback은 설치 전 별도 조건 |
| D2 50k 원장 전달 한도 | 명시 상한80k로 조정.50,001/80,000행의 뒤늦은 signal/order_ready 보존,80,001행 방어 거부. 실제 capacity80k publish 경로도 마지막 두 행 전달과 초과drop1·complete=false 검사 |
| D2 처리 비용·조회 빈도 | 제안poll_seconds=1, buffer_capacity=80000. 실제 publish 합성80k·100후보·출력7행의 projection+JSON100회: p95 10.38ms/max11.29ms. 실서비스 최악 지연은 미검증, 적용 전후 loop지연/메모리 점검 및 토스 관측 중단 조건 유지 |
| D3 오래된 배포물 hash | 최초9f2fe14f…를 폐기 대상으로 남기고 새d446656b…로 제안·설치 문서를 갱신. 새 immutable artifact inventory/import/80k상한 확인. 실제root소유/grant는 미검증 |
| D4 문서와 전체 회귀 | 원본 study hash invariant·새 프로필/전달 검증·전체 검사 결과를 이 원장과 설치안에 기록 |
| 이전 drop-in·중첩root디렉터리 | 소비/정확한hash·fsync보관·변경없는PID/NRestarts·empty DropInPaths 확인 뒤 준비. 부모root권한·신규private폴더·receipt부재 확인. 실패 시 옛drop-in 자동 복원/재시작 반복 금지 |
| receipt에epoch추가 제안 | 변경하지 않음. 날짜별 디렉터리·attempted_at·new_head로 구분하며 불필요한 영수증 형식 변경을 피함 |

Sol의 별도 절차 리뷰에서 capital_policy 부재, 넓은 최신 release 범위, 토스 start 재시도 금지와 과거drop-in 처리 증거를 확인해 문서에 반영했다. 선언된 capital_evidence_ref만으로 수량/현금/비중 한도를 검증했다고 보지 않는다.

## 확인된 검증

- 새 프로필 전49검사, 리뷰 보완 뒤101검사. study hash 거부RED4/97→GREEN101.
- 원장 전달 경계RED2/1→GREEN, 실제 publish/overflow 경로 추가 뒤 관련203검사/3.67초/exit0.
- 마지막 publish회귀를 포함한 전체4045passed/2knownxfail/1warning/121.60초, Python 문법·비밀정보 패턴 검사 통과. 운영 상태/외부 네트워크 접근 시도0건.
- [전체 저장 용량 근거](../research/current-engine-kis-v4-capacity-2026-10-06.json): 전체63,691행/close5초 성공·drop0, 별도201행 payload burst 재현. writer fsync와 strict reader 자체 durability 미확정을 구분한다.
- 제안의283 capture source 지문·별도 실행기 지문, raw/canonical study/plan 지문, temp-remapped v4 CapturePlan와 Toss ObservationPlan 실파서 검증. 운영 경로 stat/API/자격증명 접근 없음.
- 토스 최종artifact `d446656bee5d34e1a1961a224a0409c3d7110142673d0dbd0e6af3f394cbaef5`:260파일 inventory 동일, aiohttp3.13.5·서비스 imports·80k상한 확인. 실제 운영 소유권/권한 인증과 구분.

| 최종 소스/검사 | SHA-256 |
|---|---|
| `scripts/ops/entry_capture_activate.py` | `cc5b190af2ed8410a20728625249c9dc6cf880e6c692d9b5ada29620cc72506e` |
| `src/observation/entry_anchor_input.py` | `220b28bea5e977d973ca1370aece032391083257934453b9a188b422f87128b9` |
| `tests/test_entry_capture_activate.py` | `4b7003963acf5865d1d6618fc1a61cca5374e3c3625996aa562adabbef8d4366` |
| `tests/test_toss_ws_runtime.py` | `55b8709d5052ab89f123354f4399c988574190bf5249ce9d3906cca2d64a3407` |

모든 probe·검사 상세 로컬 영수증은 `/tmp/qwq-v4-review-20261002`에 있으며 운영 계좌 자료는 포함하지 않는다. 공개 JSON은 측정값과 probe/source 지문을 보존한다. 임시 원장 자체의 보존이나 재생 가능성을 Git 산출물이라고 주장하지 않는다.

## 실제 운영 전 남은 항목

최종 통합 commit/tree와 원본hash재결합, 실운영 설정·서비스/원장·미완료체결·자원 점검, 새grant/identity/승인, 과거drop-in 보관, 비활성stage, 날짜고정timer 및 시간창검사를 수행해야 한다. 운영 승인은 이번 소스 리뷰나 이전10월2일 승인에서 상속되지 않는다. 새 매수·매수중지 해제·전략조건 완화는 이 단계의 범위가 아니다.
