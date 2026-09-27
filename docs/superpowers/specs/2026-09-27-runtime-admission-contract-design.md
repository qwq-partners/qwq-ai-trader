# Runtime 등록·관측 대조 — 순수 오프라인 계약

2026-09-27 KST. 독립 계획 리뷰 `APPROVE_PLAN_ONLY`를 받은 후속 설계다.
앞 OS 단계 검증·실제 구현 base 확정 전에는 구현하지 않는다.
상위 [source 계약](2026-09-27-required-source-proof-design.md) §6과 읽기 전용
runtime 분석에서 제안한 최소 slice다. source 경계와 OS controller 뒤에 진행한다.

## 의도와 선택

목적은 등록되지 않은 host/version 폴백, 후보의 자기 QUALIFIED 선언, 서로 다른 subject의
관측 재사용을 계약 수준에서 거부하는 것이다. 현재 실제 qualified capsule은0이다.
순수 파일 내용 대조가 native qualification·격리 실행·독립 승인 진위를 입증하지는 않는다.

선택은 **순수 계약 검사+빈 실제 registry**다. 별도 격리 capsule 제작은 recipe/원본/patch/
lock/loader/실행 profile과 추가 실행계획이 필요하므로 이번 범위에 넣지 않는다.
현재 host3.12.3 또는 과거139 passed를 자격으로 재사용하는 대안은 채택하지 않는다.
컨테이너/namespace 실행, 설치, image build/download, 원격 등록/CI/주문/운영 변경0이다.

성공도 `CONTRACT_MATCH`일 뿐 `scope=offline_runtime_contract_only`,
`registry_trust_verified=false`, `native_qualified=false`,
`source_execution_permitted=false`, `production_eligible=false`를 항상 유지한다.
기존 source `setup_module` 차단·직접 child 거부와 v1 receipt/OS controller를 배선·변경하지 않는다.

## 파일과 API

새 구현 `scripts/dev/source_runtime_contract.py`, 새 시험
`tests/dev/test_source_runtime_contract.py`, 빈 등록부
`docs/verification/source-runtime-registry.json`만 추가한다. 관련 문서는 coordinator 소유다.
순수 module은 stdlib JSON/hash/정규식만 사용하고 파일/환경/Git/network/OS/native 호출0이다.

```text
RuntimeContractError(ValueError)
parse_runtime_document(raw: bytes, *, kind: str) -> dict
subject_digest(subject: dict) -> str
evaluate_runtime_contract(subject_raw: bytes, registry_raw: bytes,
    observation_raw: bytes, *, mode: str, binding: dict) -> dict
```

kind는 `subject`, `registry`, `observation`만. 모든 입력 exact bytes/dict/str/int/bool,
UTF8 document≤1MiB, 깊이≤10, duplicate/unknown key·NaN·surrogate·bool-as-int 거부.
1MiB는 세 입력 합계가 아닌 **각 문서별** 상한이며 decode 전에 적용한다. 깊이는 root
container를1로 세고 dict/list의 중첩 container만 증가시킨다. direct dict의 cycle/과도한
중첩·직렬화 오류도 고정 오류로 거부하며 subject_digest도 같은 subject 검증을 먼저 적용한다.
모든 hash는 lowercase hex64, revision은 hex40, 문자열 field별 상한은 아래와 같다.
parser/subject_digest 예외는 `INVALID_RUNTIME_DOCUMENT` 고정 code로 정규화한다.
canonical JSON은 ensure_ascii=False, sort_keys=True, separators=(',',':'), allow_nan=False,
UTF8이다. subject hash는 이 전체 subject document bytes의 SHA256이다.

## Subject v1

exact keys: `schema,platform,runtime,provenance,profile,runtime_files,source_files,roles`.
schema=`qwq.source-runtime-subject/v1`.

- platform: `image_sha256,os_id,architecture`.
  os_id는 `[a-z0-9][a-z0-9._-]{0,63}`, architecture=`x86_64` 한 profile만 지원한다.
  image_sha256은 플랫폼별 artifact digest라는 선언이며 tag/index fallback은 없다.
- runtime: `implementation,version,soabi,build_sha256`.
  implementation=`cpython`, version=`3.12.3`, soabi=`cpython-312-x86_64-linux-gnu`.
  실제 source helper의 한정 조건이지 버전만으로 자격을 부여하는 규칙이 아니다.
- provenance: exact hash keys `cpython_source,cpython_patches,sqlite_source,sqlite_patches,
  build_recipe,dependency_lock`. patch 없음도 검토된 빈 patch manifest의 실제 hash가 필요하다.
- profile: exact keys `id,kernel_policy_sha256,runtime_readonly,source_readonly,
  network_enabled,home_mounted,credentials_mounted,private_tmp`.
  id=`linux-ro-network-off/v1`; readonly2/private_tmp=true,
  network/home/credentials=false만 지원한다. hash는 승인할 kernel/ABI policy의 내용 지문이다.
  이것은 선언 검증이며 host namespace/cgroup 생성이나 보안 상태의 실제 증명이 아니다.
- runtime_files/source_files: exact dict의 상대 POSIX path→hex64, 각각1..4096개.
  path는 UTF8≤512bytes, component1..128bytes, absolute/빈 component/./../NUL/역슬래시 거부.
  case-sensitive이며 canonical path 문자열만 받는다. 값은 실제 파일 내용 hash의 선언이다.
  파일 system에 접근하거나 symlink/loader를 실제 검사했다고 주장하지 않는다.
- roles: exact keys 아래13개, 각 값은 해당 file map의 정확한 key를 참조한다.
  runtime role5=`interpreter,libpython,sqlite_extension,sqlite_library,loader`는 runtime_files,
  source role8=`helper,cases,guard,controller,bootstrap,inventory,oracle,dependency_manifest`는 source_files.
  서로 다른 role의 동일 path 참조는 허용한다(예: helper와 oracle). file map의 중복 key는 거부한다.
  source manifest는 store/gate/recovery_projection 및 transitive imports/관련3시험/plugin을 포함한
  전체 subject 내용 목록을 외부 독립 검토로 확정해야 한다. 위 최소 role 수가 closure 완전성의 증명은 아니다.

subject에는 registry 상태·approval/evidence 문서·실행 run SHA를 넣지 않는다.
후작성 보고서 때문에 subject hash가 자기참조하지 않으며, 실행 코드 bytes 변경은 hash를 바꾼다.
docs-only run SHA 변경이 같은 subject를 재자격 처리할 근거는 아니다.

## Registry v1과 외부 binding

exact keys `schema,entries`, schema=`qwq.source-runtime-registry/v1`.
entries는 exact list0..256, entry exact keys
`subject_sha256,state,profile_sha256,bootstrap_plan_sha256,evidence_sha256,review_sha256`.
state는 `UNKNOWN,BOOTSTRAP_ALLOWED,QUALIFIED,RETIRED`만, subject hash 중복은 거부한다.
profile hash는 subject.profile의 canonical JSON hash다.
bootstrap_plan hash는 BOOTSTRAP_ALLOWED/QUALIFIED에서 필수, UNKNOWN/RETIRED에서는 null 또는 hash.
evidence/review는 QUALIFIED에서 모두 필수hash, 나머지는 null 또는 hash(퇴역 이력 보존 가능).
실제 신규 registry는 정확히 `{"schema":"qwq.source-runtime-registry/v1","entries":[]}`다.
합성 QUALIFIED fixture를 이 파일에 추가하거나 registry를 함수가 자동 갱신하지 않는다.

binding exact keys `expected_revision,observed_revision,expected_sha256`.
두 revision은 일치해야 하고 actual registry_raw bytes hash가 expected_sha256과 일치해야 한다.
registry 안에는 자기 commit revision/hash를 넣지 않는다. 기대 revision/bytes의 신뢰·현재성은
외부 신뢰 계층 책임이며 이 모듈은 caller가 제공한 값의 일치만 검사한다.
후보가 expected도 같이 조작할 수 있는 호출은 보호 경계가 아니다. required job은 별도 승인된
base/revision 선택과 gate-file 독립 검토·원격 보호를 입증하기 전 사용하지 않는다.

## Observation v1과 판정

observation exact keys `schema,subject_sha256,profile_sha256,runtime_files,source_files,provenance`.
schema=`qwq.source-runtime-observation/v1`; map/provenance 형식은 subject와 같다.
이 입력은 외부 관측자가 별도로 제공해야 한다. subject를 복사하는 관측 생성기/기본값은 만들지 않는다.
모든 file/path/hash set과 provenance가 subject와 exact equality여야 한다.
observation 자체가 실제 loaded closure/불변 runtime/security 상태를 관측했다는 인증은 아니다.

mode exact str `bootstrap` 또는 `required`만. BOOTSTRAP_ALLOWED는 bootstrap에만,
QUALIFIED는 required에만 match한다. qualified의 bootstrap 재실행도 새 명시 처분 없이 허용하지 않는다.
UNKNOWN/RETIRED/등록 없음은 `UNSUPPORTED`, malformed/binding/내용 mismatch는 `REJECTED`다.
다른 문제가 함께 있으면 REJECTED가 우선이며 match되는 다른 entry로 자동 대체하지 않는다.
입력이 모두 valid일 때만 status/declared_state를 사용하며 malformed 시 declared_state=null이다.

decision exact keys `schema,status,errors,mode,declared_state,scope,registry_trust_verified,
native_qualified,source_execution_permitted,production_eligible`.
schema=`qwq.source-runtime-decision/v1`, status=`CONTRACT_MATCH|UNSUPPORTED|REJECTED`.
잘못된 mode는 출력 mode=null이고 고정 `INVALID_MODE` 오류다. errors는 정렬된 unique code list다.
오류 vocabulary는 `INVALID_SUBJECT,INVALID_REGISTRY,INVALID_OBSERVATION,INVALID_MODE,
INVALID_BINDING,REGISTRY_BINDING_MISMATCH,SUBJECT_UNREGISTERED,STATE_UNSUPPORTED,
MODE_STATE_MISMATCH,PROFILE_MISMATCH,SUBJECT_MISMATCH,RUNTIME_FILES_MISMATCH,
SOURCE_FILES_MISMATCH,PROVENANCE_MISMATCH`다. raw path/입력/exception을 출력하지 않는다.
subject_digest는 승인 여부와 무관한 식별 함수이고 상태 전이 함수는 제공하지 않는다.

## 인수와 남은 조건

독립 literal 합성 fixture로 정상 bootstrap/required 일치와 빈 실제 registry의 UNSUPPORTED를
RED부터 고정한다. host/native/SQLite/source import/subprocess 없이 순수 시험만 수행한다.
같은 version의 interpreter/libpython/SQLite/loader 변경, cases/guard/controller 변경,
profile 변경, 다른 registry revision/bytes, UNKNOWN/퇴역/중복/자기선언, missing role,
map 추가/누락/중복/path traversal·UTF8 boundary·크기/깊이/타입·mode mismatch를 거부한다.
approval report hash만 변경해 subject digest가 바뀌지 않으며 source 내용 hash 변화는 바뀐다.
성공 fixture도 execution/native/production/trust=false를 요구한다.

실제 capsule 제작·원본/patch/build/lock·실제 native loader closure·격리 관측·독립 native 리뷰·
선행 신뢰 등록 revision·현재 run oracle·OS 결과·필수 CI 보호는 이 순수 slice로 해소되지 않는다.
없는 증거를 합성해 채우거나 version/hash 문자열만으로 source 차단을 해제하지 않는다.
