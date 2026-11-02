"""E04 제한 실호출 인수 — 한 쌍(1 pair)·시도 상한 4·출력 400·USD 0.50·하루 승인 창의 전용 경로.

운영 worker(`install_from_env`)·`_raw_clients`·R1 manifest 는 건드리지 않는다. 이 모듈은 두 단계로 나뉜다.

- ``prepare``: 생산자 제어(`operating_role_controls`)에서 현행 manifest 를 동결하고(``enabled=True`` 지만 실행
  파일이 아님), 승인 요청서(``approved_by`` 비어 있음)·입력 지문을 디렉터리에 적는다. 호출 0, 전송 0.
- ``run``: 명시 승인자·같은 KST 날짜·manifest 지문 일치·worker 전용(owned) 전송이 모두 맞을 때만 봉투 하나를
  `assemble_comparison_worker` 로 조립한 worker 에 offer 하고, 끝나면 영수증(프롬프트·응답 원문 없음)을 적고
  자기 소유 전송만 닫는다. 한 쌍의 실패·불명을 새 쌍으로 재시도하지 않는다.

USD 0.50 은 가드의 예약/추정 비용 계약(`experiment_usd_ceiling`)이며 실제 청구액의 절대 보장이 아니다. 구독 CLI
시도는 월 원장을 예약하지 않고 표시 추정비만 남긴다. 1쌍은 경로 인수이지 모델 우위·선정 품질·순수익 증거가 아니다.
"""
from __future__ import annotations

import argparse
from datetime import datetime
import hashlib
import json
from pathlib import Path
import time
from types import SimpleNamespace
import uuid
from zoneinfo import ZoneInfo

from . import model_comparison_input as mci
from .model_comparison_recovery import inspect_pair
from .model_comparison_wiring import (ComparisonActivationApproval, ComparisonTransports, assemble_comparison_worker,
                                      current_profile_manifest, manifest_sha256, required_transports)
from ..core.entry_llm_context import build_entry_context

KST = ZoneInfo("Asia/Seoul")
# 준비된 인수안의 고정 범위 — 바꾸려면 새 승인이 필요하다.
ACCEPTANCE_POLICY = {"daily_attempt_cap": 4, "max_output_tokens": 400, "experiment_usd_ceiling": "0.50",
                     "pairs_per_day": 1, "max_common_context_bytes": 65536, "required_policy_mode": "normal"}
MANIFEST_FILE, APPROVAL_FILE, INPUT_FILE = "manifest.json", "approval.json", "input.json"
RECEIPT_GLOB = "receipt-*.json"          # 실행(decision_id)별 영수증 — 덮어쓰지 않으며, 하나라도 있으면 같은 디렉터리 재실행을 거부한다
# 인수 성공(finished)으로 세지 않는 runner 사유 — 저장 완료와 인수 통과를 구분한다. 신원은 검토 판정이 집행한다
# (인증된 성공 의견이 양쪽에 있어야 approve/reject, 아니면 hold) — 시도 단위 `resolved_model_unverified_or_mismatch` 는
# CLI 시한 초과(응답 없음)에도 붙으므로 판정 차단 사유만 센다.
_FINDING_REASON_MARKERS = ("independent_review_unavailable", "role_deadline_exceeded_after_response", "api_cost_unknown")
_INPUT_KEYS = ("symbol", "strategy", "score", "market_regime", "sector", "indicators")
# 영속 영수증에서 제거하는 키 — 프롬프트·응답 원문·공통 입력 본문은 어떤 경로로도 남기지 않는다.
_SCRUB_KEYS = frozenset({"prompt", "system", "content", "response", "text", "common_context", "raw", "messages",
                         "fallback_prompt_suffix"})
_TERMINAL = ("finished", "incomplete", "blocked", "journal_dropped", "journal_failed", "expired")


def _canon(value) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)


def _sha(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _scrub(value):
    if isinstance(value, dict):
        return {k: _scrub(v) for k, v in value.items() if k not in _SCRUB_KEYS}
    if isinstance(value, (list, tuple)):
        return [_scrub(v) for v in value]
    return value


def _write(path: Path, payload) -> None:
    path.write_text(_canon(payload), encoding="utf-8")
    path.chmod(0o600)


def common_context_for(input_spec: dict) -> str:
    """같은 입력 사양에서 항상 같은 공통 사실 입력을 만든다(관측 시각 없음·계좌 자료 없음)."""
    missing = [k for k in _INPUT_KEYS if k not in input_spec]
    if missing:
        raise ValueError("input_spec_missing:" + ",".join(missing))
    return build_entry_context(input_spec["symbol"], input_spec["strategy"], input_spec["score"],
                               input_spec["indicators"], input_spec["market_regime"], input_spec["sector"])


def _allowed_models(controls: dict, candidate: dict) -> list[str]:
    models = []
    for role in ("bull", "bear"):
        role_controls = controls.get(role)
        if not isinstance(role_controls, dict):
            raise ValueError("operating_role_controls_invalid:" + role)
        model = role_controls.get("model")
        if not isinstance(model, str) or not model:
            raise ValueError("operating_model_unresolved:" + role)
        models.append(model)
    cand = candidate.get("model")
    if cand is not None:
        if not isinstance(cand, str) or not cand:
            raise ValueError("candidate_model_invalid")
        models.append(cand)
    return sorted(set(models))


def prepare_acceptance(operating_controls: dict, *, experiment_id: str, day: str, candidate: dict, input_spec: dict,
                       out_dir, now: datetime, horizon_seconds: int = 900, max_input_age_seconds: float = 30.0) -> dict:
    """호출 없이 manifest·승인 요청서·입력 지문을 적는다. 반환값에도 프롬프트 본문은 없다."""
    if not isinstance(experiment_id, str) or not experiment_id.strip():
        raise ValueError("experiment_id_required")
    datetime.strptime(day, "%Y-%m-%d")
    if not isinstance(candidate, dict) or len(candidate) != 1:
        raise ValueError("candidate_must_change_exactly_one_variable")
    out = Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)
    out.chmod(0o700)
    policy = dict(ACCEPTANCE_POLICY, allowed_models=_allowed_models(operating_controls, candidate),
                  valid_from=day, valid_until=day)
    manifest = current_profile_manifest(operating_controls, experiment_id=experiment_id, fixed_at=now.isoformat(),
                                        horizon_seconds=horizon_seconds, execution_policy=policy, candidate=candidate,
                                        max_input_age_seconds=max_input_age_seconds, enabled=True)
    digest = manifest_sha256(manifest)
    context = common_context_for(input_spec)
    approval = {"experiment_id": experiment_id, "manifest_sha256": digest, "valid_from": day, "valid_until": day,
                "transports": list(required_transports(manifest)), "approved_by": None, "approved_at": None,
                "scope": ("1 pair · daily_attempt_cap 4 · max_output_tokens 400 · experiment_usd_ceiling USD 0.50 "
                          "(guard reservation/estimate contract, not a billing guarantee)")}
    _write(out / MANIFEST_FILE, manifest)
    _write(out / APPROVAL_FILE, approval)
    _write(out / INPUT_FILE, {"input_spec": {k: input_spec[k] for k in _INPUT_KEYS}, "operating_controls": operating_controls,
                              "common_context_sha256": _sha(context), "common_context_bytes": len(context.encode("utf-8")),
                              "prepared_at": now.isoformat()})
    return {"out_dir": str(out), "experiment_id": experiment_id, "day": day, "manifest_sha256": digest,
            "transports": approval["transports"], "baseline": manifest["current"], "candidate": manifest["candidate"],
            "bear": manifest["bear"], "execution_policy": manifest["execution_policy"],
            "common_context_sha256": _sha(context), "common_context_bytes": len(context.encode("utf-8"))}


def load_prepared(out_dir) -> dict:
    out = Path(out_dir)
    loaded = {name: json.loads((out / name).read_text(encoding="utf-8")) for name in (MANIFEST_FILE, APPROVAL_FILE, INPUT_FILE)}
    if manifest_sha256(loaded[MANIFEST_FILE]) != loaded[APPROVAL_FILE].get("manifest_sha256"):
        raise ValueError("approval_manifest_mismatch")
    context = common_context_for(loaded[INPUT_FILE]["input_spec"])
    if _sha(context) != loaded[INPUT_FILE].get("common_context_sha256"):
        raise ValueError("input_context_changed")
    return {"manifest": loaded[MANIFEST_FILE], "approval": loaded[APPROVAL_FILE], "input": loaded[INPUT_FILE],
            "common_context": context}


def run_acceptance(*, out_dir, approved_by: str, ledger, transports: ComparisonTransports, now: datetime,
                   today=None, deadline_seconds: float = 90.0, poll_seconds: float = 0.05) -> dict:
    """승인된 디렉터리로 한 봉투를 실행한다. 모든 거절은 전송 생성·호출 전에 일어난다(ValueError)."""
    if not isinstance(approved_by, str) or not approved_by.strip():
        raise ValueError("approval_missing")
    if not isinstance(transports, ComparisonTransports) or transports.owned is not True:
        raise ValueError("owned_transports_required")
    if isinstance(deadline_seconds, bool) or not isinstance(deadline_seconds, (int, float)) or not 0 < deadline_seconds <= 600:
        raise ValueError("deadline_seconds_out_of_range")
    if any(Path(out_dir).glob(RECEIPT_GLOB)):
        # 이미 실행된 승인 디렉터리 — 실패·불명·차단 결과를 같은 승인으로 다시 시도하지 않는다(새 쌍은 새 준비·새 승인).
        raise ValueError("receipt_exists_refusing_rerun")
    prepared = load_prepared(out_dir)
    manifest, request, spec = prepared["manifest"], prepared["approval"], prepared["input"]["input_spec"]
    recorded = request.get("approved_by")
    if recorded not in (None, approved_by):
        raise ValueError("approval_signer_mismatch")
    approval = ComparisonActivationApproval(experiment_id=request["experiment_id"], manifest_sha256=request["manifest_sha256"],
                                            valid_from=request["valid_from"], valid_until=request["valid_until"],
                                            transports=tuple(request["transports"]), approved_by=approved_by)
    out = Path(out_dir)
    decision_id = f"acceptance-{manifest['experiment_id']}-{uuid.uuid4().hex[:12]}"
    receipt = {"task": "E04 limited live acceptance", "experiment_id": manifest["experiment_id"],
               "manifest_sha256": approval.manifest_sha256, "approved_by": approved_by, "decision_id": decision_id,
               "started_at": now.isoformat(), "status": "not_started", "offer": None, "pair": None,
               "ledger_status_after": None, "transports_closed": None, "error": None, "findings": []}
    receipt_path = out / f"receipt-{decision_id}.json"
    worker = None
    try:
        # 조립 거절(승인 창·지문·전송 집합·프로필)은 전송 생성 전에 끝난다. 승인 서명은 조립이 통과한 뒤에만 적는다.
        worker = assemble_comparison_worker(SimpleNamespace(), manifest=manifest, ledger=ledger, transports=transports,
                                            approval=approval, today=today, journal_dir=out / "journal")
        if recorded is None:
            _write(out / APPROVAL_FILE, dict(request, approved_by=approved_by, approved_at=now.isoformat()))
        envelope = mci.build_envelope(
            decision_id=decision_id, signal_id=f"acceptance-{manifest['experiment_id']}", symbol=spec["symbol"],
            strategy=spec["strategy"], score=float(spec["score"]), market_regime=spec["market_regime"],
            common_context=prepared["common_context"], role_controls=prepared["input"]["operating_controls"],
            neutral_sizing=None,  # 독립 실행기에는 엔진 사이징 문맥이 없다 → runner 가 neutral_sizing_missing 사유를 남긴다
            producer=mci.producer_identity(experiment_id=manifest["experiment_id"], config_sha256=approval.manifest_sha256),
            input_available_at=now.isoformat(), handed_off_at=now.isoformat())
        status = worker.offer(envelope)
        receipt["offer"] = status
        if status != "accepted":
            receipt["status"] = "not_offered"
        else:
            limit = time.monotonic() + float(deadline_seconds)
            while time.monotonic() < limit:
                health = worker.health()
                if any(health.get(k, 0) for k in _TERMINAL):
                    break
                time.sleep(poll_seconds)
            else:
                receipt["status"] = "deadline_exceeded"
            health = worker.health()
            receipt["worker_health"] = {k: health.get(k) for k in _TERMINAL + ("offered", "admitted")}
            try:
                info = inspect_pair(worker.runner.guard, worker.runner.pair_id(decision_id))
                receipt["pair"] = _scrub(info)
                if receipt["status"] == "not_started":
                    receipt["status"] = "finished" if info.get("complete") else "incomplete"
            except Exception as error:  # 영수증 조립 실패는 유형만 남긴다
                receipt["pair_inspect_error"] = type(error).__name__
                if receipt["status"] == "not_started":
                    receipt["status"] = "incomplete"
        try:
            receipt["ledger_status_after"] = _scrub(ledger.status())
        except Exception as error:
            receipt["ledger_status_after"] = {"error": type(error).__name__}
    except Exception as error:
        receipt["status"] = "error"
        receipt["error"] = type(error).__name__ + (":" + str(error) if isinstance(error, ValueError) else "")
    finally:
        if worker is not None:
            try:
                receipt["stop"] = _scrub(worker.stop(5))
            except Exception as error:
                receipt["stop"] = {"error": type(error).__name__}
        receipt["transports_closed"] = _closed(transports)
        receipt["findings"] = _acceptance_findings(receipt)
        if receipt["status"] == "finished" and receipt["findings"]:
            receipt["status"] = "completed_with_findings"      # 결과 행은 저장됐지만 인수 통과 조건을 못 채움
        receipt["finished_at"] = datetime.now(KST).isoformat()
        _write(receipt_path, receipt)
    return receipt


def _acceptance_findings(receipt: dict) -> list[str]:
    """저장 완료(`pair.complete`)와 인수 통과를 구분한다. 어느 하나라도 있으면 `finished` 가 아니다."""
    findings = []
    pair = receipt.get("pair")
    if receipt.get("offer") == "accepted":
        if not isinstance(pair, dict) or pair.get("complete") is not True:
            findings.append("pair_incomplete")
        row = pair.get("result") if isinstance(pair, dict) else None
        # baseline(current)·candidate 두 arm 모두 판정이 서야 한다 — 후보 Bull 만 판정 불가여도 인수 통과가 아니다.
        for arm in ("current", "candidate"):
            branch = row.get(arm) if isinstance(row, dict) else None
            verdict = branch.get("verdict") if isinstance(branch, dict) else None
            if verdict not in ("approve", "reject"):
                findings.append(arm + "_verdict_hold_or_missing")
            reasons = branch.get("reasons") if isinstance(branch, dict) else None
            for reason in reasons if isinstance(reasons, list) else []:
                if isinstance(reason, str) and any(marker in reason for marker in _FINDING_REASON_MARKERS):
                    findings.append(arm + ":reason:" + reason)
        status = receipt.get("ledger_status_after")
        unresolved = status.get("unresolved_count") if isinstance(status, dict) else None
        if unresolved != 0:
            findings.append("ledger_unresolved:" + str(unresolved))
    stop = receipt.get("stop")
    if receipt.get("offer") is not None and not (isinstance(stop, dict) and stop.get("drained") is True and stop.get("journal_drained") is True):
        findings.append("worker_stop_not_drained")
    closed = receipt.get("transports_closed")
    if not (isinstance(closed, dict) and closed and all(v is True for v in closed.values())):
        findings.append("transport_not_closed")
    return findings


def _closed(transports) -> dict:
    """소유 전송의 정리 상태 — Claude CLI/API 는 `_closed`, OpenAI 는 aiohttp 세션이 없거나 닫혔는지로 본다."""
    closed = {name: bool(getattr(client, "_closed", False)) for name, client in
              (("claude_cli", transports.claude_cli), ("anthropic_api", transports.anthropic_api)) if client is not None}
    session = getattr(transports.openai, "_session", None)
    closed["openai"] = session is None or bool(getattr(session, "closed", False))
    return closed


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="E04 limited live acceptance (prepare | run)")
    sub = parser.add_subparsers(dest="mode", required=True)
    prep = sub.add_parser("prepare")
    prep.add_argument("--experiment-id", required=True)
    prep.add_argument("--day", required=True, help="KST YYYY-MM-DD (valid_from = valid_until)")
    prep.add_argument("--candidate", required=True, help='JSON with exactly one Bull variable, e.g. {"reasoning_effort":"low"}')
    prep.add_argument("--input-json", required=True, help="JSON file: symbol/strategy/score/market_regime/sector/indicators")
    prep.add_argument("--out", required=True)
    run = sub.add_parser("run")
    run.add_argument("--dir", required=True)
    run.add_argument("--approved-by", required=True)
    run.add_argument("--deadline-seconds", type=float, default=90.0)
    args = parser.parse_args(argv)

    from ..core.cross_validator import SINGLE_FALLBACK_TIMEOUT
    from ..utils.llm import LLMConfig, LLMManager
    manager = LLMManager(LLMConfig.from_env())            # 제어·공유 월 원장만 쓴다. 호출 없음.
    now = datetime.now(KST)
    if args.mode == "prepare":
        controls = mci.operating_role_controls(manager, fallback_prompt_suffix="", fallback_timeout=SINGLE_FALLBACK_TIMEOUT)
        spec = json.loads(Path(args.input_json).read_text(encoding="utf-8"))
        summary = prepare_acceptance(controls, experiment_id=args.experiment_id, day=args.day,
                                     candidate=json.loads(args.candidate), input_spec=spec, out_dir=args.out, now=now)
        print(_canon(summary))
        return 0
    if manager.monthly_budget is None:
        print(_canon({"status": "error", "error": "shared_monthly_ledger_unavailable"}))
        return 2
    from ..utils.claude_transport import AnthropicAPIClient, ClaudeCLIClient
    from ..utils.llm import OpenAIClient
    transports = ComparisonTransports(openai=OpenAIClient(manager.config), claude_cli=ClaudeCLIClient(manager.config),
                                      anthropic_api=AnthropicAPIClient(manager.config), owned=True)
    receipt = run_acceptance(out_dir=args.dir, approved_by=args.approved_by, ledger=manager.monthly_budget,
                             transports=transports, now=now, deadline_seconds=args.deadline_seconds)
    print(_canon({k: receipt.get(k) for k in ("status", "offer", "decision_id", "error", "transports_closed")}))
    return 0 if receipt.get("status") == "finished" else 1
