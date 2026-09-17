# Toss 독립 관측 서비스 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 기존 거래 봇을 재시작하지 않고 단일 발급·추가 KIS0·주문 무연결인 별도 Toss 관측 서비스를 ON한다.

**Architecture:** 승인된 기존 Toss worker/app/token/ledger를 독립 프로세스에서 조립한다. root 보호 launcher가 고정 소스/의존성을 검증하고, 전용 UID가 `/api/positions` 보유 코드만 읽어 정해진 슬롯에서 관측한다. 원격 main과 별도 릴리스만 갱신하며 실행 거래 checkout은 유지한다.

**Tech Stack:** Python 3.12 표준 라이브러리 bootstrap, 기존 aiohttp 버전+해시 고정 의존성, systemd, pytest.

**Spec:** `docs/superpowers/specs/2026-09-17-toss-observer-service-design.md` (사용자 09-17 후속 `ㄱㄱ`로 상세 설계 승인).

## Global Constraints

- 기존 거래 봇 재시작·설정·킬스위치·주문 변경0. `scripts/run_trader.py` 호출/import0, KIS 추가 호출0. 기존 루트 checkout/main도 pull하지 않는다.
- 기존 `src/data/providers/toss/`의 승인·토큰·전송·원장 스키마를 유지한다. grant당 단일 worker, 수명당 auth_max_issues=4, POST retry0, revoked mint0, token 경로 고정.
- 새 서비스만 TOSS_API=1. application OFF는 관측 파일/credential accessor/worker/network0; systemd 환경 전달과 application 자격 사용은 별개.
- launcher `-I -S`, 검증 전 stdlib-only, root trust UID/GID0, 전용 비-root UID. artifact/config/plan 원본+canonical hash 대조 후 import/관측.
- 입력은 정확히 `GET http://127.0.0.1:8080/api/positions`, 2초/연결0.5초/64KiB/64행, depth8/nodes4096/string1024; 종목6자리·양수 유한 수량. 후보0, 보유상한20, source_success_at=None, kis_quotes={}.
- Toss 작업20초·cleanup10초·preflight5초·artifact30초, GET 전체retry1/max_pages2/그룹1req/s, 회로3실패/300초, body256KiB/depth8/nodes10000/string16384/parse0.2초.
- 원장16MiB·상태16KiB/0600·보존30일. `production_eligible=False`, 유효 KIS 비교0/insufficient/통계None 유지.
- 모든 로컬 테스트는 env-i+임시 경로·가짜 HTTP/시계, 운영 자격/네트워크 접근0. `.env`·로그·캐시·PID·실제 grant/토큰은 커밋하지 않는다.
- User가 병렬·격리 구현을 요청했으므로 task1/2/3은 서로 다른 feature worktree에서 수행한다. 부모만 통합한다. 구현자는 리뷰어가 아니며 하위 에이전트를 재위임하지 않는다.
- 20:30 KST 착수: 설계의 첫날 종료로 09/18·21·22 일정 및 09/22 18:00 만료 변경을 비동기 확인 중. 구현/합성 테스트는 날짜 주입, 사용자 응답 전 운영 날짜 자동변경 금지.

## 공통 인터페이스 (세 작업의 고정 접점)

Deployment JSON은 아래 키만 허용한다. 비밀은 JSON에 넣지 않는다.

```python
DEPLOYMENT_KEYS = {
    'schema_version', 'release_id', 'artifact_sha256', 'release_root',
    'manifest_path', 'registry_path', 'plan_path', 'grant_id', 'config_hash',
    'client_identity', 'host_identity', 'service_uid', 'service_gid', 'service_gids',
    'state_directory', 'token_directory', 'sender_lock_path', 'ledger_path',
    'status_path', 'receipt_directory', 'retention_at', 'policy',
}
# config_hash = SHA256(canonical JSON of document without config_hash)
# identity.release_id/config_hash는 위 값으로 구성; grant는 이 값을 bind.
```

`policy`는 task1의 `service_policy()` 정본을 task3이 사용하고 task2가 소비한다. strict 값은 다음과 같다:

```python
{
 'positions_url':'http://127.0.0.1:8080/api/positions',
 'input_timeout_seconds':2, 'input_connect_timeout_seconds':0.5,
 'input_max_bytes':65536, 'input_max_rows':64, 'input_max_depth':8,
 'input_max_nodes':4096, 'input_max_string':1024,
 'status_max_bytes':16384, 'artifact_timeout_seconds':30,
 'restart':'no', 'cpu_quota_percent':25, 'memory_max_bytes':201326592,
 'tasks_max':16, 'nice':10, 'io_weight':10,
 'cleanup_seconds':10, 'stop_seconds':20,
}
```

release 내 배치는 `app/src/...`, `deps/`, `bootstrap/launcher.py`, `manifest.json`이다. manifest는 `schema_version=1, release_id, python_version`(major.minor.patch 문자열), `import_paths=['app','deps']`, `files=[{path,sha256,size,mode}]`의 strict JSON이며 files는 path순, manifest 자신 제외 모든 파일을 포함한다. 모드는 파일0644/디렉터리0755로 root-owned·group/other 쓰기 금지다. hash는 manifest canonical JSON이다. launcher의 신뢰 bootstrap 복사본은 `/usr/libexec/qwq-toss-observer/launcher.py`이며 자기 파일 내용도 manifest의 `bootstrap/launcher.py`와 대조한다. python/stdlib/CA는 root 관리 OS 신뢰 경계다.

런타임 전용 경로: root `/etc/qwq-toss-observer/{deployment,plan,registry}.json`, 자격 `credentials.env`0600, state `/var/lib/qwq-toss-observer`0700. `tokens/`, `sender.lock`, `starts/`는 grant/release 간 고정. 원장/상태는 `cohorts/<plan-canonical-hash>/{observations.jsonl,status.json}`. 실제 값은 설치 때 승인 plan/UID/hostname으로 계산한다.

## Task 1: 실행 신뢰·승인 조립·단일 시작 (Astra/high)

**Files:**
- Create `scripts/ops/toss_observer/launcher.py` (stdlib-only, manifest/배치 검증·entrypoint)
- Create `src/observation/toss_deployment.py` (검증 후 기존 Deployment 조립·시작 영수증)
- Test `tests/test_toss_observer_launcher.py`, `tests/test_toss_observer_deployment.py`

**Interfaces:**
- Produces `launcher.canonical_bytes(value)->bytes`, `configuration_hash(document)->str`(config_hash 제외), `service_policy()->dict`.
- Produces `launcher.build_manifest(release_root:Path, *, release_id:str, python_version:str)->dict`, `verify_release(document:dict)->None`, `load_verified_document(path:Path)->dict`. 실제 실행은 root anchor·실제 UID/EUID/GID/groups·hostname 대조를 우회할 인자를 CLI로 제공하지 않는다.
- Produces `toss_deployment.make_deployment(document:dict)->Deployment`, `claim_start(document:dict, authority:ApprovedAuthority)->None`.
- Consumes task2 `async run_service(*, deployment, settings:dict, claim_start:Callable)->int`. launcher main은 OFF 즉시0, ON 검증 뒤 lazy import/`asyncio.run`; `--check`는 실제 승인까지 검증하되 receipt/credential/worker/network0.

- [ ] **Step 1: 실패 테스트 작성/확인.** 앱 import 전 변조 거부, 같은 grant receipt 두 번 거부, fsync 실패 후 재시도 금지를 임시 경로·가짜 authority로 검증한다. 예:

```python
def test_changed_release_rejected_before_service_import(tmp_path, sealed_release):
    module, document = sealed_release
    (tmp_path / 'app' / 'probe.py').write_text('changed')
    with pytest.raises(module.LaunchError):
        module.verify_release(document)

def test_grant_start_is_consumed_after_claim_failure(receipt_fixture, monkeypatch):
    document, authority = receipt_fixture
    monkeypatch.setattr(os, 'fsync', lambda fd: (_ for _ in ()).throw(OSError()))
    with pytest.raises(Exception):
        claim_start(document, authority)
    assert list(Path(document['receipt_directory']).iterdir())
```

실제 테스트는 helper fixture도 이 task 테스트파일에 정의하고, tempfile의 운영자 UID를 테스트 내부에서만 치환한다. 테스트용 우회 플래그를 production CLI에 추가하지 않는다. Run: 격리 pytest 위 두 파일, 예상 missing module/feature 실패를 보고서에 남긴다.

- [ ] **Step 2: 최소 구현.** 중복키/NaN/unknown keys·파일 종류/하드링크/부모권한/미등재파일 거부, bound read/hash·30초 검증, UID와 grant/config/hash 바인딩. code 실행 전 stdout에는 고정 에러코드만. 시작 receipt O_EXCL/NOFOLLOW·파일/부모 fsync 완료 뒤만 반환, 삭제/자동복구 없음.

```python
def main(argv=None):
    if os.environ.get('TOSS_API', '0') in ('0', ''):
        return 0
    # strict flag and isolated/no_site check, then load_verified_document
    # add verified app/deps paths without site initialization
    # make_deployment -> check-only OR run_service with claim_start closure
```

- [ ] **Step 3: GREEN/보강.** OFF 파일0, 악성 .pth/sitecustomize0, env/PYTHONPATH injection, root identity mismatch, extra files/symlink/hardlink, grant expiry 및 시작 receipt 경쟁·fsync 실패를 인수한다. actual stdlib bootstrap subprocess(-I -S)도 실행한다.
- [ ] **Step 4: 명시 파일 commit+push, 자체 리뷰·RED/GREEN/파일목록 보고.** task1 worktree만 변경; 공통 문서는 부모에게 결과 전달.

## Task 2: 캐시 입력·관측 감독·별도 상태 (Terra/high)

**Files:** Create `src/observation/__init__.py`, `toss_positions.py`, `toss_status.py`, `toss_service.py`; Test `tests/test_toss_observer_positions.py`, `test_toss_observer_status.py`, `test_toss_observer_service.py`.

**Interfaces:**
- Consumes 기존 `Preflight/TossWorker/WorkerCommand/build_app`, `due_slots`, `select_snapshot`; bot 객체를 만들지 않는다.
- Produces `async run_service(*, deployment, settings:dict, claim_start:Callable, stop_event=None, now=None, clock=None, worker_factory=None, positions_factory=None)->int`. 테스트용 주입은 실제 책임 경계를 대체하고 production 승인 우회가 아니다. now 기본 aware UTC, clock monotonic.
- `claim_start(authority)`는 부모 launcher closure가 문서와 authority로 task1 함수에 전달. preflight 이후/worker 이전 정확히 1회 호출. 실패면 worker0.
- Positions client `async fetch()->tuple[str,...]`, `async close()`. 정상 []와 `InputUnavailable` 분리; 실패 원문/response body 출력0.
- Status writer `write(document:dict)` bounded atomic0600/fsync, 원장 ACK 후만 success update. service는 자기 상태만 소유하며 기존 heartbeat registry를 변경하지 않는다.

- [ ] **Step 1: RED 작성/실행.** 구체 시작 테스트:

```python
@pytest.mark.asyncio
async def test_invalid_positions_do_not_become_empty_success(http_fake):
    client = PositionsClient(session_factory=http_fake({'symbol':'087010'}))
    with pytest.raises(InputUnavailable):
        await client.fetch()

@pytest.mark.asyncio
async def test_receipt_failure_prevents_worker(service_fixture):
    settings, deployment, counts = service_fixture
    def reject(authority):
        raise RuntimeError('receipt_failed')
    result = await run_service(deployment=deployment, settings=settings,
                               claim_start=reject, worker_factory=counts.worker)
    assert result != 0
    assert counts.worker_starts == 0
```

- [ ] **Step 2: 최소 구현.** 입력 literal URL·GET/redirect/proxy/cookie/no automatic retry, body/time/cardinality/type 경계. 전체 입력 파싱 후 코드 정렬/dedup, malformed 한 행도 input failure. 모든 후보/시각/가격 비교값은 spec대로 빈/None.
- [ ] **Step 3: 감독/상태 구현.** preflight→receipt→starting status→worker.start, 5분 슬롯 price와 독립 calendar, missed/input failure 처리, 미래/과거 구분, busy/실패 last_success 유지. 만료/OFF/SIGTERM→신규 제출 차단→worker stop/join 확정→상태·exit code. 스레드 종료 불명은 stopping_unconfirmed, 자동 worker 교체0.

```python
# price branch contract (actual errors handled with fixed codes)
holdings = await positions.fetch()
snapshot = select_snapshot(candidates=(), holdings=holdings,
    source_success_at=None, now=now(), policy=authority.plan.document, kis_quotes={})
command = WorkerCommand('prices', slot.slot_id, snapshot)
# on input failure submit WorkerCommand('missed', slot.slot_id, {'kind':'prices'})
# but classify status as input_unavailable, not the worker's idle result.
```

- [ ] **Step 4: GREEN/인수.** real worker fake transport 또는 bounded worker double로 성공/전량실패/빈보유/입력실패/과거슬롯/캘린더/만료/취소/cleanup 불확실. writer symlink/write/fsync 실패, 상태 계수·coverage/latency·0분모·원장 불일치 인수. health에는 release/grant/plan/PID·상태·시각·누적 counts·실측latency·production_eligibleFalse, 비밀/전체 입력 없음.
- [ ] **Step 5: 명시 파일 commit+push·자체 리뷰·RED/GREEN 보고.** test timestamp는 고정, 환경/네트워크 격리.

## Task 3: 릴리스 패키징·설치·정리 timer (Astra/high)

**Files:** Create `scripts/ops/toss_observer/package_release.py`, `install.py`, `retention.py`, `qwq-toss-observer.service`, `qwq-toss-observer-retention.service`, `qwq-toss-observer-retention.timer`, `requirements.lock`; Test `tests/test_toss_observer_install.py`, `test_toss_observer_retention.py`, `test_toss_observer_package.py`.

**Interfaces:** Consumes task1 manifest/config helpers via stdlib importlib by exact file path (build/install only), common JSON contract above. Produces root-owned actual deployment/plan/grant and service files from exact artifact/UID/host/approved dates; deployment.runtime references task2. No actual provisioning by child agent.

- [ ] **Step 1: RED.** temp-root installer dry-run has writes0/useradd0/systemctl0/keys0; malicious staging symlink/path escape and existing protected target overwrite 거부; retention refuses early/live-service/wrongUID/symlink/unknown path.

```python
def test_dry_run_does_not_read_credentials_or_start_service(installer_fixture):
    result = installer_fixture.run(dry_run=True)
    assert result['ready'] is True
    assert installer_fixture.secret_reads == 0
    assert installer_fixture.system_writes == []

def test_retention_does_not_delete_auth_records(retention_fixture):
    run_retention(retention_fixture.config, now=retention_fixture.expired,
                  service_active=False)
    assert retention_fixture.token_record.read_bytes() == b'protected'
```

- [ ] **Step 2: package.** 기존 Python 설치 metadata에서 검증할 aiohttp/전이 의존성 버전을 선택하고 wheel SHA256 lock. 빌더는 명시 wheelhouse로 `--no-index --require-hashes --no-compile --target deps`를 사용, 기존 venv 수정0. 프로젝트 closure만 복사(app/src/data/providers/toss, 빈 package initializers, utils/data_freshness, utils/loop_heartbeat, schedulers/toss_shadow, observation); 테스트/봇 엔트리/키/실상태 포함0. 부모가 공개 wheel 다운로드를 별도로 담당한다.
- [ ] **Step 3: install.** artifact 내용 검증 후 immutable destination 새 생성·root seal, 전용 UID/GID생성, 엄격하게 Toss 2개 필드만 원본env에서 읽어 root0600 EnvironmentFile로 복사(원본 무변경/값 출력0). plan 날짜와 grant max7일/실제 시작/만료·hashes 바인딩. root trust files 새설치, 기존파일 있으면 덮어쓰기 대신 refuse 또는 검증된 동일파일 재사용. 실제 토큰·상태는 읽거나삭제하지 않는다.
- [ ] **Step 4: unit와 retention.** ExecStart `/usr/bin/python3 -I -S /usr/libexec/qwq-toss-observer/launcher.py --deployment /etc/qwq-toss-observer/deployment.json`, Environment=TOSS_API=1/PYTHONDONTWRITEBYTECODE=1, 전용EnvironmentFile, spec hardening/limits·Restart=no. Retention은 explicit approved cohort inode/권한/기한·inactive 확인 뒤 원장만 unlink, 안전 기록 보존·삭제영수증 fsync. default dry-run, 실제 설치와 `--activate` 분리; root 권한은 테스트 fake boundary로만 대체.
- [ ] **Step 5: GREEN.** dry-run→실제 temp-root file layout+document load, `systemd-analyze verify`(비밀 없는 unit), EnvFile quoting/parser/중복키·newline거부, 재설치 경합/권한/파일변조/기존 사용자그룹 거부, retention 안전경계. commit+push와 보고. 실제 sudo/useradd/systemctl 명령 실행금지(부모만 실행).

## Task 4: 통합·독립 리뷰·배포 (부모 통합, Astra/xhigh 리뷰)

**Files:** Update `CHANGELOG.md`, `CLAUDE.md`, `docs/README.md`, `docs/operations/toss-shadow-runtime.md`; Create `docs/reviews/toss-observer-service-2026-09-17.md`, integration tests `tests/test_toss_observer_integration.py` if boundary tests require them. 공통 문서 작성은 부모만 담당한다.

- [ ] **Step 1: 각 task spec+quality 리뷰.** baseline~head review package와 task brief/report로 독립 reviewer를 배치한다. 수정은 해당 구현자에게 반환하고 covering test evidence 후 scoped 재리뷰한다. 격리 분기 commit을 부모 feature에 명시 merge/cherry-pick한다.
- [ ] **Step 2: 통합 인수.** temp-root sealed artifact+가짜HTTP end-to-end, 누락 registry/expiry/receipt/publicURL/invalid positions에서 OAuth/GET0, valid flow에서 durable 결과·별도 status. 기존 돈 경로 테스트 불변, 전체 UTC/KST verify·비밀정보 검사·의존성 폐쇄성 확인. root 작업 checkout은 여전히8ff2f55/PID 동일.
- [ ] **Step 3: 최종 리뷰/문서/커밋·푸시.** Astra/xhigh broad branch 리뷰, 새 결함은 한정 fixwave/재리뷰. S01~S12 증거와 미실행 운영 인수 기록. Required CI exact head 성공 후 PR/main 병합, root 운영 checkout pull금지.
- [ ] **Step 4: 실제 설치 preflight.** 기존 봇 PID/시각·pending/연결/정체·설정7경로 보호지문 확보, 실제 UID/host·클라이언트 일치·approved dates·root paths/권한·디스크/systemd 지원 대조. 비밀 출력 없는 dry-run, 서명 대신root operator anchor를 설치한다. 조건실패시기존봇유지.
- [ ] **Step 5: 새 서비스만 ON.** sealed release와root bootstrap을 설치한 뒤 전용UID `--check`에서발급0을 확인, 이후 unit start1회. 기존서비스restart0, 실제OAuth/GET/원장 관측구분,unknown/revoked발생하면자동재발급0으로중단보고.
- [ ] **Step 6: See·인계.** 자체status/PID/실행SHA/만료·issuer/sender·원장·기존PID/설정지문동일 대조. 장외면idle/다음예정만보고. 3영업일인수·유효가격비교·소비자승격미완을명시하고 새관측상태기록문서만커밋·푸시/PR한다.

## 검증 실행 계약

```bash
observer_test_cache=$(mktemp -d)
env -i PATH=/usr/bin:/bin LANG=C.UTF-8 TZ=Asia/Seoul \
 PYTHONPYCACHEPREFIX="$observer_test_cache" PYTHONDONTWRITEBYTECODE=1 \
 PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 PYTEST_ADDOPTS='-p no:cacheprovider --tb=short -rx' \
 QWQ_DEPLOY_SSH_KEY=/tmp/qwq-offline-no-ssh-key \
 QWQ_VERIFY_PYTHON=/home/ubuntu/projects/qwq-ai-trader/venv/bin/python \
 bash scripts/dev/verify.sh
```

집중 테스트는 같은 env-i에서 `/home/ubuntu/projects/qwq-ai-trader/venv/bin/python -m pytest <task의 테스트파일> -q -p pytest_asyncio.plugin`으로 실행한다. fixture의 application authority/OAuth·원장 쓰기는 합성 temporary tree에만 허용한다. 전체verify는 알려진 기존 xfailed2를 새회귀와구분한다. 운영검증을 로컬verify에 끼워넣지 않는다.
