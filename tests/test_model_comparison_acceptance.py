"""E04 제한 실호출 인수 실행기 — 합성 전송·임시 원장만. 자격/네트워크/CLI 실행 없음.

확인하는 것: prepare 는 호출 없이 고정 범위(1쌍·시도 4·출력 400·USD 0.50·하루 창)의 manifest·승인 요청서·입력 지문만 적는다.
run 은 승인자·같은 날짜·manifest 지문·owned 전송이 맞을 때만 봉투 하나를 보내고, 영수증에 프롬프트·응답 원문이 없으며,
전송은 끝나면 닫히고, 같은 날 두 번째 쌍은 가드가 막는다. 1쌍 결과를 모델 우위로 해석하지 않는다.
"""
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock
from datetime import datetime, timedelta, timezone

import pytest

from src.analytics import model_comparison_acceptance as acc
from src.analytics import model_comparison_wiring as w
from src.utils.llm_budget import MonthlyBudgetLedger
from tests.test_model_comparison_claude import SCRIPTS, FakeAPI, FakeCLI, _ok, reservations
from tests.test_model_comparison_current_flow import CertifiedOpenAI
from tests.test_model_comparison_wiring import controls

SENTINEL = "SENTINELCTX"      # 공통 입력에만 들어가는 표식 — 영수증/승인/manifest 어디에도 없어야 한다
NOW = datetime(2026, 11, 3, 2, 10, tzinfo=timezone.utc)      # 원장 clock(11:10 KST) — 승인 날짜 계산용 고정값


def prep_now():
    """manifest fixed_at 은 runner 의 실시간 now 보다 앞서야 정책이 '가용' 상태가 된다(미래 fixed_at 은 정상 거절)."""
    return datetime.now(acc.KST) - timedelta(seconds=2)


def spec(**over):
    base = {"symbol": "011200", "strategy": "core_holding", "score": 73.0, "market_regime": "neutral",
            "sector": f"운송 {SENTINEL}",
            "indicators": {"per": 16.2, "pbr": 0.9, "close": 23600.0, "ma200": 19050.0, "foreign_net_buy": 312000,
                           "supply_window_days": 5, "foreign_net_buy_observed_days": 5, "indicators_source": "core_screener"}}
    base.update(over)
    return base


def ledger(tmp_path):
    return MonthlyBudgetLedger(tmp_path / "budget.db", 100, clock=lambda: NOW, policy_enabled=True)


def transports(cli=_ok, api=_ok, owned=True):
    return w.ComparisonTransports(openai=CertifiedOpenAI(), claude_cli=FakeCLI(cli), anthropic_api=FakeAPI(api), owned=owned)


def prepared(tmp_path, day=None, **over):
    led = ledger(tmp_path)
    if day is None:
        day = led.today().isoformat()
    summary = acc.prepare_acceptance(controls(), experiment_id="ACC-1", day=day, candidate={"reasoning_effort": "low"},
                                     input_spec=spec(), out_dir=tmp_path / "acc", now=prep_now(), **over)
    return led, day, summary


def files_text(tmp_path):
    return "\n".join(p.read_text(encoding="utf-8") for p in sorted((tmp_path / "acc").glob("*.json")))


def receipt_files(tmp_path):
    return sorted((tmp_path / "acc").glob(acc.RECEIPT_GLOB))


def test_prepare_writes_fixed_scope_without_calls_or_prompt_text(tmp_path):
    led, day, summary = prepared(tmp_path)
    man = json.loads((tmp_path / "acc" / acc.MANIFEST_FILE).read_text())
    pol = man["execution_policy"]
    assert man["enabled"] is True and man["experiment_id"] == "ACC-1" and man["pairs_per_day"] == 1
    assert (pol["pairs_per_day"], pol["daily_attempt_cap"], pol["max_output_tokens"], pol["experiment_usd_ceiling"]) == (1, 4, 400, "0.50")
    assert pol["valid_from"] == pol["valid_until"] == day
    assert set(pol["allowed_models"]) == {"gpt-5-mini", "claude-opus-5-5"}      # 운영 Bull/Bear 요청 모델만
    assert man["current"]["reasoning_effort"] == "minimal" and man["candidate"]["reasoning_effort"] == "low"
    assert man["bear"]["billing_channel"] == "subscription" and man["bear_api_fallback"]["billing_channel"] == "api"
    appr = json.loads((tmp_path / "acc" / acc.APPROVAL_FILE).read_text())
    assert appr["approved_by"] is None and appr["manifest_sha256"] == w.manifest_sha256(man)
    assert len(appr["common_context_sha256"]) == 64 and len(appr["operating_controls_sha256"]) == 64
    assert set(appr["transports"]) == {"openai_chat", "claude_cli", "anthropic_messages"}
    inp = json.loads((tmp_path / "acc" / acc.INPUT_FILE).read_text())
    assert inp["input_spec"]["symbol"] == "011200" and len(inp["common_context_sha256"]) == 64
    # 공통 입력 본문은 파일에 없고(지문만), 반환 요약에도 없다
    assert "<entry_facts>" not in files_text(tmp_path) and "<entry_facts>" not in json.dumps(summary, ensure_ascii=False)
    assert summary["manifest_sha256"] == appr["manifest_sha256"] and summary["common_context_bytes"] > 0
    assert (tmp_path / "acc" / acc.MANIFEST_FILE).stat().st_mode & 0o777 == 0o600


@pytest.mark.parametrize("candidate,match", [({}, "exactly_one"), ({"reasoning_effort": "low", "model": "x"}, "exactly_one"),
                                             ({"model": ""}, "candidate_model_invalid")])
def test_prepare_rejects_invalid_candidate(tmp_path, candidate, match):
    with pytest.raises(ValueError, match=match):
        acc.prepare_acceptance(controls(), experiment_id="ACC-1", day="2026-11-03", candidate=candidate,
                               input_spec=spec(), out_dir=tmp_path / "x", now=NOW)


@pytest.mark.parametrize("bad", ["", "E04/acceptance", "a b", "x" * 65, "../x"])
def test_prepare_rejects_unsafe_experiment_id(tmp_path, bad):
    with pytest.raises(ValueError, match="experiment_id_invalid"):
        acc.prepare_acceptance(controls(), experiment_id=bad, day="2026-11-03", candidate={"reasoning_effort": "low"},
                               input_spec=spec(), out_dir=tmp_path / "x", now=NOW)


def test_prepare_rejects_unresolved_operating_model(tmp_path):
    c = controls()
    c["bear"] = dict(c["bear"], model=None)
    with pytest.raises(ValueError, match="operating_model_unresolved:bear"):
        acc.prepare_acceptance(c, experiment_id="ACC-1", day="2026-11-03", candidate={"reasoning_effort": "low"},
                               input_spec=spec(), out_dir=tmp_path / "x", now=NOW)


def _untouched(tr):
    return tr.openai.calls == [] and tr.claude_cli.calls == [] and tr.anthropic_api.calls == [] \
        and not tr.claude_cli._closed and not tr.anthropic_api._closed


def test_run_refuses_before_any_transport_is_touched(tmp_path):
    led, day, _ = prepared(tmp_path)
    tr = transports()
    with pytest.raises(ValueError, match="approval_missing"):
        acc.run_acceptance(out_dir=tmp_path / "acc", approved_by="", ledger=led, transports=tr, now=NOW)
    with pytest.raises(ValueError, match="owned_transports_required"):
        acc.run_acceptance(out_dir=tmp_path / "acc", approved_by="user", ledger=led, transports=transports(owned=False), now=NOW)
    with pytest.raises(ValueError, match="deadline_seconds_out_of_range"):
        acc.run_acceptance(out_dir=tmp_path / "acc", approved_by="user", ledger=led, transports=tr, now=NOW, deadline_seconds=0)
    # manifest 를 승인 뒤에 바꾸면 지문 불일치로 거절
    path = tmp_path / "acc" / acc.MANIFEST_FILE
    man = json.loads(path.read_text()); man["execution_policy"]["daily_attempt_cap"] = 40
    path.write_text(json.dumps(man, ensure_ascii=False))
    with pytest.raises(ValueError, match="approval_manifest_mismatch"):
        acc.run_acceptance(out_dir=tmp_path / "acc", approved_by="user", ledger=led, transports=tr, now=NOW)
    assert _untouched(tr)


def test_swapped_input_file_from_another_preparation_is_refused(tmp_path):
    """같은 제어로 준비한 다른 종목의 input.json 을 통째로 복사해도 승인서의 입력 지문과 달라 거절된다."""
    led, day, _ = prepared(tmp_path)
    acc.prepare_acceptance(controls(), experiment_id="ACC-2", day=day, candidate={"reasoning_effort": "low"},
                           input_spec=spec(symbol="005930", sector="반도체"), out_dir=tmp_path / "other", now=prep_now())
    (tmp_path / "acc" / acc.INPUT_FILE).write_text((tmp_path / "other" / acc.INPUT_FILE).read_text(encoding="utf-8"), encoding="utf-8")
    tr = transports()
    with pytest.raises(ValueError, match="approval_input_mismatch"):
        acc.run_acceptance(out_dir=tmp_path / "acc", approved_by="user", ledger=led, transports=tr, now=datetime.now(acc.KST))
    assert _untouched(tr)


def test_run_outside_the_approved_day_is_fail_closed_receipt(tmp_path):
    led, day, _ = prepared(tmp_path, day="2026-01-02")       # 승인 창이 오늘이 아님
    tr = transports()
    receipt = acc.run_acceptance(out_dir=tmp_path / "acc", approved_by="user", ledger=led, transports=tr, now=NOW)
    assert receipt["status"] == "error" and receipt["error"].startswith("ValueError:approval_expired")
    assert receipt["offer"] is None and _untouched(tr)
    # 조립이 거절되면 승인 서명도 적지 않는다
    assert json.loads((tmp_path / "acc" / acc.APPROVAL_FILE).read_text())["approved_by"] is None


def test_run_sends_one_pair_and_leaves_a_scrubbed_receipt(tmp_path):
    led, day, _ = prepared(tmp_path)
    tr = transports(cli=SCRIPTS["timeout"])                   # CLI 허용 실패 → 남은 시한으로 API 1회
    now = datetime.now(acc.KST)
    receipt = acc.run_acceptance(out_dir=tmp_path / "acc", approved_by="user", ledger=led, transports=tr, now=now,
                                 deadline_seconds=30)
    assert receipt["status"] == "finished" and receipt["offer"] == "accepted" and receipt["findings"] == []
    assert receipt["worker_health"]["finished"] == 1
    assert receipt["stop"]["drained"] is True and receipt["stop"]["journal_drained"] is True
    assert len(tr.openai.calls) == 2 and len(tr.claude_cli.calls) == 1 and len(tr.anthropic_api.calls) == 1
    assert len(reservations(led)) == 3 and led.status()["unresolved_count"] == 0      # bull 2 + API 1, 구독 0
    pair = receipt["pair"]
    assert pair["complete"] is True and len(pair["attempts"]) == 4
    row = pair["result"]
    assert row["current"]["review_policy"] == "entry-independent-review-v1" and row["subscription_attempts"] == 1
    assert "neutral_sizing_missing" in row["current"]["reasons"]      # 독립 실행기: 엔진 사이징 문맥 없음
    bear = [a for a in row["current"]["attempts"] if a["role"] == "bear"]
    assert [a["billing_channel"] for a in bear] == ["subscription", "api"]
    # 원문 비유출: 공통 입력 표식(입력 사양 파일 input.json 에만 설계상 존재)·프롬프트 블록·응답 본문 키가
    # 영수증/승인/manifest 어디에도 없고, input.json 에도 조립된 프롬프트 블록은 없다(사양+지문만)
    acc_dir = tmp_path / "acc"
    [receipt_file] = receipt_files(tmp_path)
    assert receipt_file.name == f"receipt-{receipt['decision_id']}.json"
    scrubbed = json.dumps(receipt, ensure_ascii=False) + receipt_file.read_text(encoding="utf-8") + "".join(
        (acc_dir / n).read_text(encoding="utf-8") for n in (acc.APPROVAL_FILE, acc.MANIFEST_FILE))
    assert SENTINEL not in scrubbed and "<entry_facts>" not in scrubbed and '"prompt"' not in scrubbed and '"content"' not in scrubbed
    assert "<entry_facts>" not in (acc_dir / acc.INPUT_FILE).read_text(encoding="utf-8")
    # 전송은 닫혔고 승인 서명이 기록됐다
    assert receipt["transports_closed"] == {"claude_cli": True, "anthropic_api": True, "openai": True}
    appr = json.loads((tmp_path / "acc" / acc.APPROVAL_FILE).read_text())
    assert appr["approved_by"] == "user" and appr["approved_at"] == now.isoformat()
    assert json.loads(receipt_file.read_text())["status"] == "finished"
    # 같은 승인 디렉터리 재실행은 영수증이 있으면 전송을 만지기 전에 거부한다 — 첫 영수증은 덮어쓰이지 않는다
    tr2 = transports(cli=SCRIPTS["timeout"])
    with pytest.raises(ValueError, match="receipt_exists_refusing_rerun"):
        acc.run_acceptance(out_dir=tmp_path / "acc", approved_by="user", ledger=led, transports=tr2, now=datetime.now(acc.KST))
    assert _untouched(tr2) and receipt_files(tmp_path) == [receipt_file]
    assert json.loads(receipt_file.read_text())["decision_id"] == receipt["decision_id"]


def test_signer_mismatch_is_refused_before_assembly(tmp_path):
    led, day, _ = prepared(tmp_path)
    appr = tmp_path / "acc" / acc.APPROVAL_FILE
    appr.write_text(json.dumps(dict(json.loads(appr.read_text()), approved_by="user"), ensure_ascii=False))
    tr = transports()
    with pytest.raises(ValueError, match="approval_signer_mismatch"):
        acc.run_acceptance(out_dir=tmp_path / "acc", approved_by="someone-else", ledger=led, transports=tr, now=datetime.now(acc.KST))
    assert _untouched(tr)


def test_stored_pair_with_unverified_identity_is_not_an_acceptance_pass(tmp_path):
    """결과 행이 저장돼도(complete) 신원 미검증 의견은 hold → finished 가 아니라 completed_with_findings."""
    led, day, _ = prepared(tmp_path)
    tr = transports(cli=SCRIPTS["timeout"], api=SCRIPTS["identity_mismatch"])
    receipt = acc.run_acceptance(out_dir=tmp_path / "acc", approved_by="user", ledger=led, transports=tr,
                                 now=datetime.now(acc.KST), deadline_seconds=30)
    assert receipt["offer"] == "accepted" and receipt["pair"]["complete"] is True
    assert receipt["status"] == "completed_with_findings"
    assert "current_verdict_hold_or_missing" in receipt["findings"] and "candidate_verdict_hold_or_missing" in receipt["findings"]
    # 신원 불일치 응답은 독립 검토 불가 판정·불명 API 비용(unknown 예약)으로 드러난다 — 모두 인수 중단 조건이다
    assert "current:reason:independent_review_unavailable" in receipt["findings"]
    assert "current:reason:api_cost_unknown" in receipt["findings"]
    assert any(f.startswith("ledger_unresolved_attempt:") and f.endswith(":unknown") for f in receipt["findings"])
    assert receipt["transports_closed"] == {"claude_cli": True, "anthropic_api": True, "openai": True}


class CandidateBrokenOpenAI(CertifiedOpenAI):
    """기준 Bull(minimal)은 정상, 후보 Bull(low)만 판정 불가(빈 본문) — 후보 arm 만 hold 가 된다."""
    async def complete(self, prompt, system='', model=None, **kw):
        response = await super().complete(prompt, system, model, **kw)
        if kw.get("reasoning_effort") == "low":
            response.content = ""
        return response


def test_candidate_only_failure_is_not_an_acceptance_pass(tmp_path):
    led, day, _ = prepared(tmp_path)
    tr = w.ComparisonTransports(openai=CandidateBrokenOpenAI(), claude_cli=FakeCLI(_ok), anthropic_api=FakeAPI(_ok), owned=True)
    receipt = acc.run_acceptance(out_dir=tmp_path / "acc", approved_by="user", ledger=led, transports=tr,
                                 now=datetime.now(acc.KST), deadline_seconds=30)
    assert receipt["pair"]["complete"] is True and receipt["pair"]["result"]["current"]["verdict"] in ("approve", "reject")
    assert receipt["status"] == "completed_with_findings"
    assert "candidate_verdict_hold_or_missing" in receipt["findings"] and "current_verdict_hold_or_missing" not in receipt["findings"]


def test_open_anthropic_session_after_stop_is_a_finding(tmp_path):
    led, day, _ = prepared(tmp_path)
    tr = transports(cli=SCRIPTS["timeout"])
    tr.anthropic_api._session = SimpleNamespace(closed=False, close=AsyncMock(side_effect=RuntimeError("stuck")))
    receipt = acc.run_acceptance(out_dir=tmp_path / "acc", approved_by="user", ledger=led, transports=tr,
                                 now=datetime.now(acc.KST), deadline_seconds=30)
    assert receipt["transports_closed"]["anthropic_api"] is False and tr.anthropic_api._closed
    assert receipt["status"] == "completed_with_findings" and "transport_not_closed" in receipt["findings"]


def test_open_openai_session_after_stop_is_a_finding(tmp_path):
    led, day, _ = prepared(tmp_path)
    tr = transports(cli=_ok)
    tr.openai._session = SimpleNamespace(closed=False, close=AsyncMock(side_effect=RuntimeError("stuck")))   # worker 가 닫지 못하는 세션
    receipt = acc.run_acceptance(out_dir=tmp_path / "acc", approved_by="user", ledger=led, transports=tr,
                                 now=datetime.now(acc.KST), deadline_seconds=30)
    assert receipt["transports_closed"]["openai"] is False
    assert receipt["status"] == "completed_with_findings" and "transport_not_closed" in receipt["findings"]


def test_unclosed_transport_or_undrained_worker_is_a_finding(tmp_path, monkeypatch):
    led, day, _ = prepared(tmp_path)
    monkeypatch.setattr(acc, "_closed", lambda transports: {"claude_cli": True, "anthropic_api": False, "openai": True})
    receipt = acc.run_acceptance(out_dir=tmp_path / "acc", approved_by="user", ledger=led, transports=transports(cli=_ok),
                                 now=datetime.now(acc.KST), deadline_seconds=30)
    assert receipt["status"] == "completed_with_findings" and "transport_not_closed" in receipt["findings"]


def test_cli_success_needs_no_api_and_reserves_only_bull(tmp_path):
    led, day, _ = prepared(tmp_path)
    tr = transports(cli=_ok)
    receipt = acc.run_acceptance(out_dir=tmp_path / "acc", approved_by="user", ledger=led, transports=tr,
                                 now=datetime.now(acc.KST), deadline_seconds=30)
    assert receipt["status"] == "finished"
    assert len(tr.claude_cli.calls) == 1 and len(tr.anthropic_api.calls) == 0 and len(reservations(led)) == 2
    assert receipt["pair"]["result"]["subscription_attempts"] == 1


def test_prepare_refuses_to_overwrite_an_existing_preparation(tmp_path):
    led, day, _ = prepared(tmp_path)
    before = {n: (tmp_path / "acc" / n).read_text(encoding="utf-8") for n in (acc.MANIFEST_FILE, acc.APPROVAL_FILE, acc.INPUT_FILE)}
    with pytest.raises(ValueError, match="prepare_dir_not_empty"):
        acc.prepare_acceptance(controls(), experiment_id="ACC-9", day=day, candidate={"reasoning_effort": "low"},
                               input_spec=spec(symbol="005930"), out_dir=tmp_path / "acc", now=prep_now())
    assert {n: (tmp_path / "acc" / n).read_text(encoding="utf-8") for n in before} == before


def test_completion_during_stop_is_recorded_with_deadline_flag(tmp_path):
    """폴링 시한이 먼저 끝나도 stop 대기 중 완료된 결과·정산이 최종 영수증에 반영되고 시한 초과는 따로 남는다."""
    led, day, _ = prepared(tmp_path)
    tr = transports(cli=_ok)
    receipt = acc.run_acceptance(out_dir=tmp_path / "acc", approved_by="user", ledger=led, transports=tr,
                                 now=datetime.now(acc.KST), deadline_seconds=0.001, poll_seconds=0.001)
    assert receipt["deadline_exceeded"] is True and "deadline_exceeded" in receipt["findings"]
    assert receipt["pair"]["complete"] is True and receipt["ledger_status_after"]["unresolved_count"] == 0
    assert receipt["status"] == "completed_with_findings" and receipt["stop"]["drained"] is True


def test_future_fixed_at_is_refused_before_any_call(tmp_path):
    led = ledger(tmp_path)
    acc.prepare_acceptance(controls(), experiment_id="ACC-1", day=led.today().isoformat(), candidate={"reasoning_effort": "low"},
                           input_spec=spec(), out_dir=tmp_path / "acc", now=datetime.now(acc.KST) + timedelta(hours=1))
    tr = transports()
    receipt = acc.run_acceptance(out_dir=tmp_path / "acc", approved_by="user", ledger=led, transports=tr,
                                 now=datetime.now(acc.KST), deadline_seconds=30)
    assert receipt["offer"] == "accepted" and receipt["status"] == "incomplete"
    assert _untouched_calls(tr) and tr.claude_cli._closed and tr.anthropic_api._closed


def _untouched_calls(tr):
    return tr.openai.calls == [] and tr.claude_cli.calls == [] and tr.anthropic_api.calls == []


def test_preexisting_unknown_reservations_are_baseline_not_a_finding(tmp_path):
    """과거 월 unknown(운영 원장의 2026-10 336건 같은)은 월 필터 없이 unresolved_count 에 들어간다 — 이 실행이 늘리지 않으면 통과."""
    led = MonthlyBudgetLedger(tmp_path / "budget.db", 100, clock=lambda: NOW, policy_enabled=True,
                              missing_cost_policy="log_and_continue")
    token = led.reserve("gpt-5-mini", "seed", "", 10, task="seed", role="seed")
    led.settle(token, outcome="unknown")
    assert led.status()["unresolved_count"] == 1
    acc.prepare_acceptance(controls(), experiment_id="ACC-1", day=led.today().isoformat(), candidate={"reasoning_effort": "low"},
                           input_spec=spec(), out_dir=tmp_path / "acc", now=prep_now())
    tr = transports(cli=SCRIPTS["timeout"])
    receipt = acc.run_acceptance(out_dir=tmp_path / "acc", approved_by="user", ledger=led, transports=tr,
                                 now=datetime.now(acc.KST), deadline_seconds=30)
    assert receipt["status"] == "finished" and receipt["findings"] == []
    assert receipt["ledger_status_before"]["unresolved_count"] == 1 and receipt["ledger_status_after"]["unresolved_count"] == 1
    assert receipt["ledger_unresolved_delta"] == 0
    tokens = [a["reservation_token"] for a in receipt["pair"]["attempts"] if a["reservation_token"] is not None]
    assert len(tokens) == 3 and all(a["state"] == "known" for a in receipt["pair"]["attempts"] if a["reservation_token"] is not None)


def test_ledger_criterion_follows_this_pairs_reservations_not_shared_counts():
    """다른 호출의 unknown 추가(전체 +1)나 과거 unknown 대사(전체 -1)는 인수 결과를 바꾸지 않고, 이 쌍의 토큰이 unknown 이면 finding 이다."""
    known = [{"attempt_id": "p:0", "state": "known", "reservation_token": "t0"},
             {"attempt_id": "p:1", "state": "known_reconciled", "reservation_token": "t1"},
             {"attempt_id": "p:2", "state": "pending", "reservation_token": None}]      # 구독 시도: 예약 없음 → 원장 기준 밖
    def receipt(attempts, before, after):
        return {"offer": "accepted",
                "pair": {"complete": True, "attempts": attempts,
                         "result": {"current": {"verdict": "approve", "reasons": []}, "candidate": {"verdict": "reject", "reasons": []}}},
                "ledger_status_before": {"unresolved_count": before}, "ledger_status_after": {"unresolved_count": after},
                "stop": {"drained": True, "journal_drained": True}, "transports_closed": {"openai": True}}
    assert acc._acceptance_findings(receipt(known, 336, 337)) == []        # 동시 운영 호출의 unknown
    assert acc._acceptance_findings(receipt(known, 336, 335)) == []        # 과거 unknown 대사
    assert acc._unresolved_delta(receipt(known, 336, 337)) == 1 and acc._unresolved_delta(receipt(known, None, 1)) is None
    bad = known + [{"attempt_id": "p:3", "state": "unknown", "reservation_token": "t3"}]
    assert acc._acceptance_findings(receipt(bad, 5, 5)) == ["ledger_unresolved_attempt:p:3:unknown"]
    stuck = known + [{"attempt_id": "p:4", "state": "pending", "reservation_token": "t4"}]
    assert acc._acceptance_findings(receipt(stuck, 5, 5)) == ["ledger_unresolved_attempt:p:4:pending"]


def test_operating_llm_config_opens_shared_ledger_settings_or_refuses(tmp_path):
    """CLI 는 from_env(월 예산 None → 원장 없음) 대신 운영 YAML 설정으로 월 예산·원장 경로·정책을 읽는다."""
    raw = {"llm": {"monthly_budget": 100.0, "budget_state_path": str(tmp_path / "ledger.sqlite3"), "budget_policy_enabled": True,
                   "missing_cost_policy": "log_and_continue", "openai_model_light": "gpt-5-mini"}}
    cfg = acc.load_operating_llm_config(raw, dotenv=False)
    assert cfg.monthly_budget_usd == 100.0 and cfg.budget_state_path == str(tmp_path / "ledger.sqlite3")
    assert cfg.budget_policy_enabled is True and cfg.missing_cost_policy == "log_and_continue"
    assert cfg.openai_model_light == "gpt-5-mini" and cfg.claude_model == "claude-opus-5-5"
    for broken in ({}, {"llm": {}}, {"llm": {"monthly_budget": 100.0}}, {"llm": {"monthly_budget": 100.0, "budget_state_path": " "}}):
        with pytest.raises(ValueError, match="operating_llm_budget_config_missing"):
            acc.load_operating_llm_config(broken, dotenv=False)
