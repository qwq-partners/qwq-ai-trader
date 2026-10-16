# 2026-10-16 후속 시간 방어 독립 중요 최종 리뷰

## 판정

**APPROVE — 검토 범위 내 신규 P0/P1/P2 결함 0건.** 소스·시험·문서 논리의 승인이다. 배포·재시작·예약·매수 재개 승인이나 코디네이터의 전체 검증/CI를 대신하는 판정이 아니다.

- 요청 모델/effort: `gpt-6-astra` / `xhigh`.
- 실제 모델 식별자와 유효 effort: 이 리뷰 환경에 독립 실행 metadata가 노출되지 않아 **미검증**.
- 독립 리뷰이며 교차 공급자 리뷰로 주장하지 않는다.
- 정책: `/home/ubuntu/.config/ai-agents/model-routing.md` 전체 읽음.
- 작업 트리: `/home/ubuntu/projects/qwq-ai-trader/.claude/worktrees/codex-recent-work-d9b36c`.
- 기준: `10cf00363bda8410cb00f2c3887a56a6fcd85fef`.
- 검토 HEAD: `700494a0ce469bd5d177efbf0650e79e77b01277` 및 검토 당시 6개 미커밋 문서 변경/새 `docs/research/llm-capture-readout-2026-10-16.md`.
- 저장소 파일 수정 없음. 운영 상태·로그·계좌·캐시·`.env`·서비스·네트워크·유료 API 접근 없음. 이 보고서만 `/tmp`에 기록.

## 확인한 근거

1. **전체 입장 구간 검사** — `src/analytics/entry_observation_runtime.py:420`의 하한만 09:15→09:30으로 강화됐다. 먼저 KST로 변환하며 기존 `now < start < admission_end < end`, 동일 KST 평일/날짜 검사와 결합된다. 따라서 부분 겹침·UTC 표기의 09:30 직전·15:01 직후 종료는 통과하지 않는다. `LLM_VERSION`은 앞선 `validate_window_contract()`가 `fixed_horizon_bid_markout`을 요구하므로 다른 outcome_basis로 새 검사 분기를 우회할 수 없다. 기존 horizon+max-delay 종료 여유는 유지된다.
2. **실제 매매 게이트 보존** — `src/core/cross_validator.py:285`의 non-core KR BUY 09:00~09:29 하드 차단과 09:30 이후 -8점은 수정되지 않았다. `src/core/engine.py:2021`의 실제 검증 호출과 실패 시 반환이 `:2066`의 LLM 분기보다 앞선다. 신규 시험은 진짜 검증기로 09:15/09:29:59.999999 거부 및 09:30의 70→62 통과를 확인하며 패널 입력만 격리한다. 시간 하한 충족을 LLM 실제 발생 보장으로 승격하지 않는다.
3. **과거 해석 계약 보존** — `validate_window_contract`와 봉인 원장 리더의 시간 계약은 변경되지 않았다. 신규 09:15 합성 봉인 원장 시험은 설치 검증을 거치지 않은 기록된 계약을 strict 리더와 `build_capture_readiness`로 읽고, `no_llm_checks`/LLM0/수익성 통과 false 및 study·journal 바이트 불변을 확인한다. 09:00 역사 계약 시험도 유지된다.
4. **옛 profile/시험 매핑의 범위** — `legacy_profile_resource_check`는 복사본의 시간만 옛 profile에 맞춰 자원 준비 술어를 시험한다. 실제 activation 통합 시험에는 원래 미래 안전 runtime projection을 전달해 옛 profile이 이를 거절함을 확인한다. 별도 `test_final_synthetic_artifact_loader_rejects_incompatible_0915_llm_window`는 수정하지 않은 10월16일 installer 산출물을 진짜 `CapturePlan.load`에 넣어 신규 설치 거부와 원장 부재를 확인한다. 날짜 매핑이 실제 구형 계획의 통과를 숨기지 않는다.
5. **설치 체인** — 날짜 고정 installer 자체는 이력으로 보존된다. `scripts/ops/entry_capture_prepare_20261016.py:443`의 실제 정적 검사도 배포된 `CapturePlan.load`를 실행한다. apply는 이 검사를 타이머 enable보다 앞서 수행한다. 새 시간표에 맞는 실제 날짜/SHA의 installer나 예약이 추가됐다는 주장은 없다.
6. **문서 해석** — 새 판독 문서는 실제 후보2개 첫 score 차단과, 실제 trace에는 도달하지 않았지만 코드상 존재하는 09:30 전 LLM 불가능성을 구분한다. 등락률 5% 대입은 단일 조건의 가정 계산으로 제한하고 실제 rejection으로 적지 않는다. 원천 최대 가중45와 최종 점수 상한, 보정 총합과 단일 수급 감점의 효과, 반환 후보 분모와 스크리너 전체 분모를 구분한다. 호가4,995개/후보 교집합0·판단 파일 부재·수익 null·기록 finality unverified도 과장하지 않는다.
7. **수급 의미의 잔여 문제** — `kr_screener.py`의 기관/외국인 양의 순위 입력에서 true가 설정되고 SPDI 및 개인단독매수 절에서 false가 재사용되는 소스를 확인했다. 문서는 순위 미등장/빈 캐시를 관측된 비양수로 취급하지 않으며, 계측 및 단일 shadow 비교를 다음 제안으로 남긴다. 실제 점수 수정은 이번 변경에 없다.

## 직접 수행한 검증

한 번의 한정 실행만 수행했다. 코디네이터가 수행 중인 전체 suite는 중복 실행하지 않았다.

```text
PYTHONDONTWRITEBYTECODE=1 TZ=UTC /home/ubuntu/projects/qwq-ai-trader/venv/bin/python -m pytest -q -p no:cacheprovider tests/test_llm_window_observation.py tests/test_entry_capture_prepare_20261016.py::test_final_synthetic_artifact_loader_rejects_incompatible_0915_llm_window tests/test_entry_capture_activate_20261016.py::test_retired_profile_rejects_future_safe_runtime_projection_without_checkout tests/test_entry_capture_activate_20261016.py::test_real_preparation_checks_hook_actual_resources_and_unconnected_capacity
```

- 결과: **71 passed in 3.10s**, exit 0.
- 테스트 격리: 운영 상태·외부 네트워크 접근 시도 **0건**.
- `git diff --check`: exit 0.
- 검토 종료 시 HEAD는 위 SHA와 일치하며 작업 트리의 수정 목록은 문서만 남아 있음을 확인했다.

## 남는 한계·마감 항목

- 새 문서의 `검증·통합 기록`(검토 당시 73행)은 코디네이터의 전체 verify/비밀정보 검사·리뷰·최종 PR 상태를 실제 결과로 채워야 한다. 진행 중인 정상 마감 항목이며 코드 결함으로 분류하지 않았다.
- 실제 10월16일 수집 원본, 실행 영수증, 역할 감사/월 예산 원장 및 보호 지문은 이 리뷰의 접근 범위 밖이다. 문서의 실측 수치는 코디네이터 제공 근거와의 일관성만 검토했으며 직접 대사했다고 주장하지 않는다.
- 이 검증은 미래 실측 표본·장 상태·주문 가능성·비용 후 순수익을 입증하지 않는다. 새로운 실제 관측은 별도 날짜/SHA/자원 및 전체 경로 확인이 필요하다.
