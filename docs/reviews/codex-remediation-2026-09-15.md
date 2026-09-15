# 2026-09-15 Codex 교차 리뷰 후속 수정

기준: `1aba7d7d54281b19767fd875aa52445446502876`. 사용자 승인 범위는 리뷰 결함 8건의 구현·오프라인 검증이다. 최종 독립 리뷰는 Claude가 수행한다. 운영 체크아웃·배포·재시작·주문·설정·원격 저장소는 변경하지 않는다.

## 작업 원장

| 리뷰 ID | 결함 / 인수 조건 | 담당 | 상태 |
|---|---|---|---|
| R1 | 장외 동기화 수면이 거래일 08:00을 넘지 않음 | gpt-6-astra/high, 격리 워크트리 | 구현·회귀 통과 |
| R2 | 고정·활성 트레일링 손절 갭 관통은 시가 체결, 불가능한 손절가 체결 금지 | gpt-6-astra/high, 격리 워크트리 | 구현·회귀 통과 |
| R3 | 과거 A/B의 모든 신선도·만료 판정에 판단 시각 사용 | gpt-6-astra/high + gpt-5.6-terra/high 시계 API | 구현·회귀 통과 |
| R4 | MCP None/오류/파싱 실패는 획득 실패, 실패 캐시 금지 | gpt-5.6-terra/high, 격리 워크트리 | 구현·회귀 통과 |
| R5 | 정상 중립 DART 응답도 fetched=True | gpt-5.6-terra/high | 구현·회귀 통과 |
| R6 | 위험 경고 뒤에도 검증 통과 +10은 v2 매력에서 제거 | gpt-5.6-terra/high | 구현·회귀 통과 |
| R7 | 유효 전문가 4명 미만·커버리지 미상은 예측 평가 기권 | 주 에이전트 | 구현·회귀 통과 |
| R8 | CF가 불변 심의 원장을 읽어 오전 BUY를 보존, 종목·일자당 1표본 | 주 에이전트 | 구현·회귀 통과 |

작업 방식: 사용자 지정 병렬 구현(동일 파일 동시 편집 금지), TDD의 RED→GREEN 증거, 통합 자가 점검·전체 테스트·비밀정보 검사. 구현자는 최종 독립 리뷰어를 겸하지 않으며 Claude 리뷰 전에는 승인/배포 완료로 표시하지 않는다.

## 통합 자가 점검에서 보완한 경계

- UTC와 naive KST의 단순 tzinfo 교체로 나이가 9시간 어긋나는 경계 → 실제 순간 비교. None/비유한 보고서 나이는 JSON null로 저장.
- MCP 바깥 모양만 정상인 중첩 오류·None·비유한 숫자 → 소비자가 해석 가능한 자료인지 확인, 정상 0/명시 빈 목록은 보존.
- 후보일과 계획일 불일치·미래 관측 → 기권/제외. 특히 항목 status만 제외하면 미래 자료를 반영한 보고서 score가 남으므로 보고서 전체를 제외하며, 별도 정상 보고서는 사용한다.
- CF 실제 `TradingTeam._save` 경로로 10:30 BUY → 14:00 HOLD 저장 후 수집 검증. 기존 HOLD→BUY 재분류의 가격·r5 보존, 신규 체결 증거/미상으로 제외 시 저장, 중복 심의 ID와 표본 분모 검증.

## 검증

- 기준 테스트는 796 passed / 2 xfailed. 개별 RED 예: R1 7 failed/13 passed, R7/R8 16 failed/2 passed, 표준 JSON 직렬화 1 failed, 미래 점수 혼합 2 failed를 확인 후 수정.
- 신규 회귀 파일 4개: **106 passed**, 운영 상태·외부 네트워크 접근 시도 **0건**.
- 최종 통합 `verify.sh`: **902 passed / 2 xfailed / 기존 pykrx 경고 1건**, 23.24초. Python 문법 검사·비밀정보 의심 패턴 검사 통과, 운영 상태·외부 네트워크 접근 시도 **0건**.
- 기존 xfail 2건은 `test_live_backtest_parity.py`의 손절 발동 기준(가격 하락률 vs 수수료 포함 순손익률)과 익절 접촉(일봉 고가 vs 실시간 현재가) 차이. 이번 수정의 새 실패가 아니며 해당 parity/승격 제한은 그대로다.
- 검증 후 운영 루트는 `main` / `1aba7d7d54281b19767fd875aa52445446502876`, staged/unstaged 변경 없음으로 확인했다. 실제 서비스 상태·운영 데이터는 조회하지 않았다.

최종 재현 명령(반드시 아래 격리 워크트리에서, 운영 .env 로드 금지):

```bash
cd /home/ubuntu/projects/qwq-ai-trader/.claude/worktrees/codex-review-fixes-20260915
env -i PATH=/usr/bin:/bin LANG=C.UTF-8 TZ=Asia/Seoul \
  PYTHONDONTWRITEBYTECODE=1 PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 \
  PYTEST_ADDOPTS='-p no:cacheprovider --tb=short -rx' \
  QWQ_DEPLOY_SSH_KEY=/tmp/qwq-offline-no-ssh-key \
  QWQ_VERIFY_PYTHON=/home/ubuntu/projects/qwq-ai-trader/venv/bin/python \
  bash scripts/dev/verify.sh
```

`QWQ_DEPLOY_SSH_KEY`는 env-i 상태에서 배포 스크립트의 **가짜 SSH 테스트**가 HOME 기본값을 확장하지 않게 하는 비운영 경로다. 실제 SSH/배포는 실행하지 않는다. pytest 격리 가드가 외부 네트워크·운영 상태 접근을 차단한다.

## 남는 한계와 승인 경계

- 합성 테스트는 투자 성능 증명이 아니다. 과거 A/B 성과를 다시 실행/덮어쓰기하지 않았다. 위험 사이징·팀 판단의 운영 승격과 canary는 포함하지 않는다.
- date-only 스냅샷의 자정은 재현 가능한 **가정**이지 실제 판단 시각이 아니다. 명시 시각이 없는 실데이터는 성능 증거로 사용하기 전에 보강해야 한다. 일중 고가/저가 순서·stale 청산 parity 한계도 그대로다.
- validation_pass_bonus 없는 구보고서는 기존 risk_clear 추정 폴백이라 과거 점수 분해를 복원할 수 없다. `TEAM_CONVICTION` 등 설정 변경 없음; 재활성화는 별도 검증 대상이다.
- CF는 종목·일자별 종가 기준 연구다. 일중 각 심의의 독립 성과가 아니다. 기존 불변 원장이 없던 날짜의 이미 덮어쓴 BUY는 복원 불가. load_day의 부분 손상 행 건너뛰기 계약 및 체결 콜백 예외→False 레거시 동작은 남아 있으며 별도 보강 후보다. 원장·운영 상태 파일을 이번 작업에서 재작성하지 않았다.
- 최종 독립 리뷰 **미실시**(사용자 지시대로 Claude 담당). 로컬 feature 브랜치와 워크트리를 보존하며 main merge/push/PR/배포는 하지 않는다.

## Claude 인계 프롬프트

```text
Codex가 구현한 R1~R8 수정본을 읽기 전용 독립 리뷰해줘.
워크트리: /home/ubuntu/projects/qwq-ai-trader/.claude/worktrees/codex-review-fixes-20260915
브랜치: feature/codex-review-fixes-20260915
비교 범위: 1aba7d7d54281b19767fd875aa52445446502876..HEAD
먼저 docs/reviews/codex-remediation-2026-09-15.md와 diff를 읽고,
각 결함의 재현→수정→실제 소비 경로 연결을 확인해줘.
우선순위: 08:00 동기화 루프 경계, fixed/trailing 갭 체결, 판단 시계와 미래 점수 누수,
MCP 실패 캐시·DART fetched, validation_pass_bonus, 전문가 기권 분모,
불변 심의 원장→CF 재분류·멱등·체결 증거·저장.
신규 테스트가 구현을 그대로 따라 쓴 것인지도 검토하고 위 안전 검증 명령으로 재실행해줘.
P0/P1/P2를 파일·라인·재현 입력·영향과 함께 보고하고 독립 리뷰 전제를 유지해줘.
승인 전 수정/머지/푸시/배포/재시작/SSH/주문/설정 변경은 하지 마.
외부 API·운영 자격증명·캐시를 사용하지 마.
```
