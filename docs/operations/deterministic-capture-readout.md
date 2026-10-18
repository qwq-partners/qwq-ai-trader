# 고정 관측의 단발 오프라인 판독

> **2026-10-18 운영 갱신:** [직접 KIS 계측 배포·v4 예약/판독](selection-capture-2026-10-19.md#10월18일-직접-kis-계측-배포와-v4-교체). 고정 `a0bc9c94`·PID2208722·NRestarts0·복구ready·미체결0. 운영 전체7290검사와 독립 중요 리뷰 통과, 매수 중지·월100·unknown1/normal·기존 설정/자료 보존.10월19일09:25 v4 예약과10:05~10:20 판독 입력 사전검사 완료. 실수집·판독은 아직이며 자동 판독은 미예약이다. 후속 문서 커밋은 재배포하지 않는다. 아래 미배포/v3 표시는 당시 이력이다.

> **10월19일 v3 판독 준비:** [입력 지문·고정 소스·실행 순서](selection-capture-2026-10-19.md#10월17일-후속--판독-입력-고정). 기존 실행기를 그대로 사용하며 `--check`만 통과했다. 시작 허용 창은10:05~10:20 KST이고 자동 판독은 등록하지 않았다. 아래10월16일 JSON은 형식 예시다.

`scripts/ops/run_capture_readout.py`는 이미 승인된 관측 파일을 고정 manifest로 한 번 읽는 도구다. 네트워크, AI 호출, 계좌 조회, 주문, 운영 설정 변경, 서비스 관리, 예약 설치는 하지 않는다. 설치/예약은 별도 검토 대상이며 이 문서 자체가 설치 완료 근거는 아니다.

## 입력과 고정 소스

manifest는 아래 키만 허용한다. 예상 연구 SHA와 실행 소스 SHA는 필수다. 상대경로, 중복 JSON 키, 추가 명령/인수, naive 시각을 거부한다. 시작 허용 창은 같은 UTC offset/날짜의 최대 한 시간이며 양 끝 시각을 포함한다. CLI에 시각 덮어쓰기 옵션은 없다. 실제 aware UTC 시계로 최초 검사와 lock 획득 후 검사를 수행한다.

```json
{
  "version": "deterministic-capture-readout-v1",
  "study": "/home/ubuntu/.local/share/qwq-entry-observation/20261016-llm-window-v1/study.json",
  "study_sha256": "39957565351aaab0ef4c4ce7f8461206118cb0af09719536a0bb7f0686d22024",
  "ledger": "/home/ubuntu/.local/state/qwq-entry-observation/20261016-llm-window-v1/engine.jsonl",
  "decisions": "/home/ubuntu/.cache/ai_trader/llm_decisions/decisions-2026-10.jsonl",
  "output_dir": "/home/ubuntu/.local/state/qwq-research/capture-readout-20261016",
  "not_before": "2026-10-16T09:45:00+09:00",
  "not_after": "2026-10-16T10:00:00+09:00",
  "source_sha256": "REPLACE_WITH_REVIEWED_SOURCE_FINGERPRINT"
}
```

예시 시간은 판독 **시작** 허용 창이다. 실제 관측 종료/봉인보다 뒤에 잡아야 한다. `output_dir`는 소스 트리 밖의 새 전용 경로이며 부모 디렉터리가 미리 존재해야 한다. manifest와 원본 입력은 출력 디렉터리 안에 둘 수 없다. 각 신규 관측은 별도 날짜/연구/manifest/출력 경로를 사용한다.

소스 준비자는 검토된 파일 전용 소스 사본을 고정하고 다음 명령으로 fingerprint를 구한다. 아래 `REVIEWED_SOURCE`는 실제 검토된 사본 절대경로로 교체한다.

```bash
/home/ubuntu/projects/qwq-ai-trader/venv/bin/python \
  /REVIEWED_SOURCE/scripts/ops/run_capture_readout.py --source-fingerprint
```

fingerprint는 이 러너, 고정된 네 CLI, 존재하는 `scripts/__init__.py`와 `scripts/ops/__init__.py`, 전체 `src/**/*.py`의 상대경로/내용 해시를 결합한다. Git 메타데이터, `.env`, 캐시, 운영 로그는 탐색하거나 읽지 않는다. 소스 사본에는 검토되지 않은 변경이나 동시에 쓰는 작업자가 없어야 한다. fingerprint는 승인·서명·보안 샌드박스를 대신하지 않는다. Python 실행 파일/설치된 표준 라이브러리는 신뢰된 런타임 전제이며 fingerprint에 포함되지 않는다.

manifest 생성 예시(이 코드는 예약하지 않는다):

```python
import json
import os
import subprocess
from pathlib import Path

python = '/home/ubuntu/projects/qwq-ai-trader/venv/bin/python'
source = Path('/REVIEWED_SOURCE')
manifest = json.loads(Path('/REVIEWED_MANIFEST_TEMPLATE.json').read_text())
manifest['source_sha256'] = subprocess.check_output(
    [python, str(source / 'scripts/ops/run_capture_readout.py'), '--source-fingerprint'],
    text=True,
).strip()
# 목적 경로는 사전에 검토한다. 기존 manifest를 덮어쓰지 않는다.
fd = os.open('/REVIEWED_MANIFEST.json', os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
with os.fdopen(fd, 'w') as handle:
    json.dump(manifest, handle, ensure_ascii=False, indent=2)
    handle.write('\n')
    handle.flush()
    os.fsync(handle.fileno())
```

## 사전 점검과 실행

```bash
/home/ubuntu/projects/qwq-ai-trader/venv/bin/python \
  /REVIEWED_SOURCE/scripts/ops/run_capture_readout.py \
  --manifest /REVIEWED_MANIFEST.json --check
```

`--check`는 manifest 형식/고정 소스/기존 입력 경로·크기/존재하는 연구 파일의 SHA를 확인한다. 아직 생성되지 않은 연구·원장·판단 파일과 그 부모 경로는 허용한다. 미래 파일의 내용·봉인·완결성을 미리 검증했다는 뜻은 아니다. once와 출력 파일을 만들지 않는다. 존재하는 심볼릭 링크 입력/출력은 거부한다.

실제 허용 시각에 다음 명령을 호출한다. 시각을 넘기거나 다른 날짜에 호출하면 attempt를 소비하지 않는다. 예상 연구 해시 불일치, 미생성 원장, 분석 실패는 시도 영수증을 남기고 자동 재시도하지 않는다.

```bash
/home/ubuntu/projects/qwq-ai-trader/venv/bin/python \
  /REVIEWED_SOURCE/scripts/ops/run_capture_readout.py \
  --manifest /REVIEWED_MANIFEST.json
```

## 실행 계약과 출력

1. `flock` 비차단 lock을 잡는다. 출력 디렉터리는 현재 사용자 소유 `0700`, 새 파일은 `0600`이다. 기존 lock의 종류·소유자·권한·hardlink 수를 검사한다. 경로 구성요소를 `O_NOFOLLOW`로 열고 새 파일은 `O_EXCL`로 생성하여 기존 파일과 symlink를 덮어쓰지 않는다.
2. `attempted.json`을 배타적으로 만들고 파일/디렉터리를 fsync한 뒤 입력 복사나 하위 명령을 시작한다. 비어 있거나 부분적으로 쓰인 attempt도 소비된 것으로 취급한다. `SIGKILL` 뒤 summary가 없어도 재실행하지 않는다. lock/attempt를 자동 삭제하거나 고치지 않는다.
3. manifest, 연구(최대 4 MiB), 원장(최대 256 MiB)을 출력 디렉터리에 보존한다. 연구/원장은 복사 전후 inode·크기·수정 정보를 비교한다. 연구 SHA는 예상값과 일치해야 한다.
4. 월 판단 파일은 open 시점 크기(최대 256 MiB)의 **고정 prefix**만 복사한다. 같은 prefix를 다시 읽어 SHA를 비교하고 append된 꼬리는 제외한다. 부분 JSONL/진행 중 행은 기존 판독기에서 차단된다. 파일이 없으면 원본이나 빈 대체본을 만들지 않는다. snapshot identity/bytes/SHA를 summary에 기록한다. 이 방식도 판단 기록의 최종 완결성을 증명하지 않으므로 `decision_recording_finality=unverified`를 유지한다.
5. 아래 네 고정 명령을 순차 호출한다. manifest로 명령/추가 인수를 지정할 수 없고 `shell=False`다. 모든 명령에 동일한 실제 시작 시각, 연구/원장 사본, 256 MiB 입력 상한을 전달한다. 세션 검토 파일은 자동 생성/전달하지 않는다.

| 결과 파일 | 기존 분석 명령 |
|---|---|
| `readiness.json` | `report_capture_readiness.py` |
| `gate-trace.json` | `report_entry_gate_trace.py` |
| `frame-diagnostics.json` | `report_kis_frame_diagnostics.py` |
| `evaluation-unreviewed.json` | `prepare_entry_evaluation.py` |

하위 명령은 비밀정보 없는 allowlist 환경과 전용 빈 HOME/TMPDIR, 출력 디렉터리를 작업 경로로 사용한다. Python `-I -B`와 별도 비어 있는 bytecode 경로로 상위 환경의 Python 설정·사용자 패키지·기존 `.pyc`를 배제한다. 명령별 wall time 90초, CPU 60초, 주소공간 2 GiB, stdout/stderr 각각 32 MiB, 열린 파일 64개를 제한하고 core dump를 금지한다. timeout이면 해당 프로세스 그룹을 종료한다. stdout/stderr는 메모리에 누적하지 않고 전용 파일에 직접 저장한다. 유효 JSON인지 확인할 때는 출력 상한 안에서 읽는다.

하위 명령 실패 후에도 나머지 진단은 실행하여 누락 근거를 남긴다. 매 명령 직전과 전체 종료 후 소스 fingerprint를 다시 대조한다. 원장의 hash/seal/lifecycle/completeness 검증은 기존 엄격한 CLI 판독기를 재사용한다.

`summary.json`은 명령별 실제 returncode, timeout, 유효 JSON 여부, argv, 입력 snapshot 메타데이터를 담는다. 네 명령의 성공 코드만으로 완결성을 판정하지 않는다. readiness의 `journal.verified=true`, `complete=true`, `dropped_records=0`, `technical_ready=true`도 필요하다.

| 상태 | CLI 종료 코드 | 의미 |
|---|---:|---|
| `preflight_ok` | 0 | 사전 점검 완료, 미래 내용/봉인 미확인 |
| `diagnostics_complete` | 0 | 네 판독과 원장 무결성 gate 완료; 개별 증거/검토는 여전히 필요 |
| `blocked` | 2 | 입력 부족/손상/불완전/하위 분석 실패; attempt 보존 |
| `already_attempted`, `busy` | 3 | 이전/진행 중 시도, 추가 실행 없음 |
| `time_rejected` | 4 | 허용 시작 창 밖, attempt 미소비 |
| `invalid_manifest` | 5 | 형식/소스/경로/출력 문제; 출력 오류가 시도 뒤 발생하면 attempt는 보존 |

## 해석과 남은 검토

`diagnostics_complete`는 명령 수행과 원장 완결성의 이름이며 가격 비교 가능·전략 통과·주문 허가가 아니다. 판단 파일 결측/빈 파일, 미결합 신호, frame/연결 공백, 세션 검토 과제는 개별 보고서에서 확인한다. `no_llm_checks`/`no_comparable_pairs`는 수익 0이 아니다.

결과는 항상 `human_review_required=true`, `decision_recording_finality=unverified`, `profitability_pass=false`, `production_eligible=false`, `account_return=null`이다. 실제 `signal`, `order_ready`, 주문량/손절/자본 snapshot, 최초 호가/900초 후 호가, 정확한 호가 ID/해시에 대한 외부 근거 기반 세션 검토는 이 러너가 만들지 않는다. 판독 완료 뒤 사람은 이 실제 공백부터 검토해야 한다.

영수증/manifest/출력은 개인 연구 자료이며 Git에 넣지 않는다. 기존 attempt를 삭제해 재시도하지 않는다. 실패 원인 확인 뒤 별도 승인된 새 판독 계획이 필요한지 결정한다. 예약 설치, 봉인 전 운영 소스 변경, 매수 재개, 유료 비교, 배포/재시작은 이 도구 범위 밖이다.
