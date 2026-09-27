"""Immutable source-proof collection inventories; stdlib only."""

import hashlib
import json


SOURCE_NODES: tuple[str, ...] = (
    "tests/proofs/l3_source/source_lease_cases.py::test_cold_wrong_ticket_and_second_start_submit_nothing",
    "tests/proofs/l3_source/source_lease_cases.py::test_disposal_oracle_counts_transient_head_edges_in_one_step",
    "tests/proofs/l3_source/source_lease_cases.py::test_every_receipt_and_actual_disposal_edge_is_accounted[255-0]",
    "tests/proofs/l3_source/source_lease_cases.py::test_every_receipt_and_actual_disposal_edge_is_accounted[255-1]",
    "tests/proofs/l3_source/source_lease_cases.py::test_every_receipt_and_actual_disposal_edge_is_accounted[255-4097]",
    "tests/proofs/l3_source/source_lease_cases.py::test_every_receipt_and_actual_disposal_edge_is_accounted[255-64]",
    "tests/proofs/l3_source/source_lease_cases.py::test_every_receipt_and_actual_disposal_edge_is_accounted[255-65]",
    "tests/proofs/l3_source/source_lease_cases.py::test_every_receipt_and_actual_disposal_edge_is_accounted[256-0]",
    "tests/proofs/l3_source/source_lease_cases.py::test_every_receipt_and_actual_disposal_edge_is_accounted[256-1]",
    "tests/proofs/l3_source/source_lease_cases.py::test_every_receipt_and_actual_disposal_edge_is_accounted[256-4097]",
    "tests/proofs/l3_source/source_lease_cases.py::test_every_receipt_and_actual_disposal_edge_is_accounted[256-64]",
    "tests/proofs/l3_source/source_lease_cases.py::test_every_receipt_and_actual_disposal_edge_is_accounted[256-65]",
    "tests/proofs/l3_source/source_lease_cases.py::test_every_receipt_and_actual_disposal_edge_is_accounted[31-0]",
    "tests/proofs/l3_source/source_lease_cases.py::test_every_receipt_and_actual_disposal_edge_is_accounted[31-1]",
    "tests/proofs/l3_source/source_lease_cases.py::test_every_receipt_and_actual_disposal_edge_is_accounted[31-4097]",
    "tests/proofs/l3_source/source_lease_cases.py::test_every_receipt_and_actual_disposal_edge_is_accounted[31-64]",
    "tests/proofs/l3_source/source_lease_cases.py::test_every_receipt_and_actual_disposal_edge_is_accounted[31-65]",
    "tests/proofs/l3_source/source_lease_cases.py::test_every_receipt_and_actual_disposal_edge_is_accounted[32-0]",
    "tests/proofs/l3_source/source_lease_cases.py::test_every_receipt_and_actual_disposal_edge_is_accounted[32-1]",
    "tests/proofs/l3_source/source_lease_cases.py::test_every_receipt_and_actual_disposal_edge_is_accounted[32-4097]",
    "tests/proofs/l3_source/source_lease_cases.py::test_every_receipt_and_actual_disposal_edge_is_accounted[32-64]",
    "tests/proofs/l3_source/source_lease_cases.py::test_every_receipt_and_actual_disposal_edge_is_accounted[32-65]",
    "tests/proofs/l3_source/source_lease_cases.py::test_every_receipt_and_actual_disposal_edge_is_accounted[4-0]",
    "tests/proofs/l3_source/source_lease_cases.py::test_every_receipt_and_actual_disposal_edge_is_accounted[4-1]",
    "tests/proofs/l3_source/source_lease_cases.py::test_every_receipt_and_actual_disposal_edge_is_accounted[4-4097]",
    "tests/proofs/l3_source/source_lease_cases.py::test_every_receipt_and_actual_disposal_edge_is_accounted[4-64]",
    "tests/proofs/l3_source/source_lease_cases.py::test_every_receipt_and_actual_disposal_edge_is_accounted[4-65]",
    "tests/proofs/l3_source/source_lease_cases.py::test_every_receipt_and_actual_disposal_edge_is_accounted[5-0]",
    "tests/proofs/l3_source/source_lease_cases.py::test_every_receipt_and_actual_disposal_edge_is_accounted[5-1]",
    "tests/proofs/l3_source/source_lease_cases.py::test_every_receipt_and_actual_disposal_edge_is_accounted[5-4097]",
    "tests/proofs/l3_source/source_lease_cases.py::test_every_receipt_and_actual_disposal_edge_is_accounted[5-64]",
    "tests/proofs/l3_source/source_lease_cases.py::test_every_receipt_and_actual_disposal_edge_is_accounted[5-65]",
    "tests/proofs/l3_source/source_lease_cases.py::test_fault_protocol_kills_false_cleanup_mutations[premature_proof_completion]",
    "tests/proofs/l3_source/source_lease_cases.py::test_fault_protocol_kills_false_cleanup_mutations[skip_proof_registration]",
    "tests/proofs/l3_source/source_lease_cases.py::test_fault_record_oracle_rejects_invalid_identity_or_duplicate_keys[duplicate_key]",
    "tests/proofs/l3_source/source_lease_cases.py::test_fault_record_oracle_rejects_invalid_identity_or_duplicate_keys[wrong_case]",
    "tests/proofs/l3_source/source_lease_cases.py::test_fault_record_oracle_rejects_missing_or_malformed_observation[[]]",
    "tests/proofs/l3_source/source_lease_cases.py::test_fault_record_oracle_rejects_missing_or_malformed_observation[]",
    "tests/proofs/l3_source/source_lease_cases.py::test_fault_record_oracle_rejects_missing_or_malformed_observation[{\"case\":\"source_fault\",\"fault\":true]",
    "tests/proofs/l3_source/source_lease_cases.py::test_fault_record_oracle_rejects_missing_or_malformed_observation[{\"case\":\"source_fault\"}]",
    "tests/proofs/l3_source/source_lease_cases.py::test_fault_record_oracle_rejects_missing_or_malformed_observation[{}]",
    "tests/proofs/l3_source/source_lease_cases.py::test_fault_record_oracle_rejects_wrong_but_valid_mutation_facts[cleanup_completed-premature_proof_completion-True]",
        "tests/proofs/l3_source/source_lease_cases.py::test_fault_record_oracle_rejects_wrong_but_valid_mutation_facts[cleanup_completed-skip_proof_registration-False]",
        "tests/proofs/l3_source/source_lease_cases.py::test_fault_record_oracle_rejects_wrong_but_valid_mutation_facts[exit_drain_observed-premature_proof_completion-True]",
        "tests/proofs/l3_source/source_lease_cases.py::test_fault_record_oracle_rejects_wrong_but_valid_mutation_facts[exit_drain_observed-skip_proof_registration-False]",
        "tests/proofs/l3_source/source_lease_cases.py::test_fault_record_oracle_rejects_wrong_but_valid_mutation_facts[fault-premature_proof_completion-True]",
        "tests/proofs/l3_source/source_lease_cases.py::test_fault_record_oracle_rejects_wrong_but_valid_mutation_facts[fault-skip_proof_registration-False]",
        "tests/proofs/l3_source/source_lease_cases.py::test_fault_record_oracle_rejects_wrong_but_valid_mutation_facts[follower_entered-premature_proof_completion-True]",
        "tests/proofs/l3_source/source_lease_cases.py::test_fault_record_oracle_rejects_wrong_but_valid_mutation_facts[follower_entered-skip_proof_registration-False]",
        "tests/proofs/l3_source/source_lease_cases.py::test_fault_record_oracle_rejects_wrong_but_valid_mutation_facts[follower_queued-premature_proof_completion-True]",
        "tests/proofs/l3_source/source_lease_cases.py::test_fault_record_oracle_rejects_wrong_but_valid_mutation_facts[follower_queued-skip_proof_registration-False]",
        "tests/proofs/l3_source/source_lease_cases.py::test_fault_record_oracle_rejects_wrong_but_valid_mutation_facts[lock_held-premature_proof_completion-True]",
        "tests/proofs/l3_source/source_lease_cases.py::test_fault_record_oracle_rejects_wrong_but_valid_mutation_facts[lock_held-skip_proof_registration-False]",
        "tests/proofs/l3_source/source_lease_cases.py::test_fault_record_oracle_rejects_wrong_but_valid_mutation_facts[post_fault_turn-premature_proof_completion-True]",
        "tests/proofs/l3_source/source_lease_cases.py::test_fault_record_oracle_rejects_wrong_but_valid_mutation_facts[post_fault_turn-skip_proof_registration-False]",
        "tests/proofs/l3_source/source_lease_cases.py::test_fault_record_oracle_rejects_wrong_but_valid_mutation_facts[same_slot-premature_proof_completion-True]",
        "tests/proofs/l3_source/source_lease_cases.py::test_fault_record_oracle_rejects_wrong_but_valid_mutation_facts[same_slot-skip_proof_registration-False]",
        "tests/proofs/l3_source/source_lease_cases.py::test_fault_record_oracle_rejects_wrong_but_valid_mutation_facts[worker_settled-premature_proof_completion-True]",
    "tests/proofs/l3_source/source_lease_cases.py::test_fault_record_oracle_rejects_wrong_but_valid_mutation_facts[worker_settled-skip_proof_registration-False]",
    "tests/proofs/l3_source/source_lease_cases.py::test_fault_record_oracle_requires_exact_boolean_schema[0]",
    "tests/proofs/l3_source/source_lease_cases.py::test_fault_record_oracle_requires_exact_boolean_schema[1]",
    "tests/proofs/l3_source/source_lease_cases.py::test_fault_record_oracle_requires_exact_boolean_schema[None]",
    "tests/proofs/l3_source/source_lease_cases.py::test_fault_record_oracle_requires_exact_boolean_schema[true]",
    "tests/proofs/l3_source/source_lease_cases.py::test_invalid_disposal_budget_has_no_side_effect[0]",
    "tests/proofs/l3_source/source_lease_cases.py::test_invalid_disposal_budget_has_no_side_effect[1]",
    "tests/proofs/l3_source/source_lease_cases.py::test_invalid_disposal_budget_has_no_side_effect[257]",
    "tests/proofs/l3_source/source_lease_cases.py::test_invalid_disposal_budget_has_no_side_effect[2]",
    "tests/proofs/l3_source/source_lease_cases.py::test_invalid_disposal_budget_has_no_side_effect[3]",
    "tests/proofs/l3_source/source_lease_cases.py::test_invalid_disposal_budget_has_no_side_effect[4.0]",
    "tests/proofs/l3_source/source_lease_cases.py::test_invalid_disposal_budget_has_no_side_effect[False]",
    "tests/proofs/l3_source/source_lease_cases.py::test_invalid_disposal_budget_has_no_side_effect[True]",
    "tests/proofs/l3_source/source_lease_cases.py::test_invalid_sql_receipt_is_rejected_without_losing_cleanup[-1]",
    "tests/proofs/l3_source/source_lease_cases.py::test_invalid_sql_receipt_is_rejected_without_losing_cleanup[0]",
    "tests/proofs/l3_source/source_lease_cases.py::test_invalid_sql_receipt_is_rejected_without_losing_cleanup[1.5]",
    "tests/proofs/l3_source/source_lease_cases.py::test_invalid_sql_receipt_is_rejected_without_losing_cleanup[2]",
    "tests/proofs/l3_source/source_lease_cases.py::test_invalid_sql_receipt_is_rejected_without_losing_cleanup[bad]",
    "tests/proofs/l3_source/source_lease_cases.py::test_known_failure_preserves_cause_but_completes_real_cleanup[known_sql-known_sql]",
    "tests/proofs/l3_source/source_lease_cases.py::test_known_failure_preserves_cause_but_completes_real_cleanup[submit-submit_failed]",
    "tests/proofs/l3_source/source_lease_cases.py::test_last_public_handle_does_not_own_source",
    "tests/proofs/l3_source/source_lease_cases.py::test_native_actual_audit_matches_independent_tp_traverse_observation",
    "tests/proofs/l3_source/source_lease_cases.py::test_native_retirement_oracle_kills_retained_resource_mutations[retain_cursor]",
    "tests/proofs/l3_source/source_lease_cases.py::test_native_retirement_oracle_kills_retained_resource_mutations[retain_metadata]",
    "tests/proofs/l3_source/source_lease_cases.py::test_page_head_move_counts_live_edges_separately_from_removed_work[0]",
    "tests/proofs/l3_source/source_lease_cases.py::test_page_head_move_counts_live_edges_separately_from_removed_work[1]",
    "tests/proofs/l3_source/source_lease_cases.py::test_repeated_caller_cancellation_keeps_sql_slot_and_gate[after_checkpoint]",
    "tests/proofs/l3_source/source_lease_cases.py::test_repeated_caller_cancellation_keeps_sql_slot_and_gate[before_driver]",
    "tests/proofs/l3_source/source_lease_cases.py::test_repeated_caller_cancellation_keeps_sql_slot_and_gate[done_uncollected]",
    "tests/proofs/l3_source/source_lease_cases.py::test_repeated_caller_cancellation_keeps_sql_slot_and_gate[page]",
    "tests/proofs/l3_source/source_lease_cases.py::test_repeated_wait_closed_cancellation_preserves_cleanup_proof",
    "tests/proofs/l3_source/source_lease_cases.py::test_source_receipts_and_cancel_cleanup",
    "tests/proofs/l3_source/source_lease_cases.py::test_sql_worker_terminal_retires_all_native_cursors[0]",
    "tests/proofs/l3_source/source_lease_cases.py::test_sql_worker_terminal_retires_all_native_cursors[1]",
    "tests/proofs/l3_source/source_lease_cases.py::test_stale_source_never_validates_and_cleans[connection]",
    "tests/proofs/l3_source/source_lease_cases.py::test_stale_source_never_validates_and_cleans[fault_epoch]",
    "tests/proofs/l3_source/source_lease_cases.py::test_stale_source_never_validates_and_cleans[incarnation]",
    "tests/proofs/l3_source/source_lease_cases.py::test_stale_source_never_validates_and_cleans[publication_epoch]",
    "tests/proofs/l3_source/source_lease_cases.py::test_stale_source_never_validates_and_cleans[revision]",
    "tests/proofs/l3_source/source_lease_cases.py::test_stale_source_never_validates_and_cleans[store]",
    "tests/proofs/l3_source/source_lease_cases.py::test_uncertified_fault_keeps_actual_gate_drain_pending[driver_create]",
    "tests/proofs/l3_source/source_lease_cases.py::test_uncertified_fault_keeps_actual_gate_drain_pending[driver_prestart_cancel]",
    "tests/proofs/l3_source/source_lease_cases.py::test_uncertified_fault_keeps_actual_gate_drain_pending[foreign]",
    "tests/proofs/l3_source/source_lease_cases.py::test_uncertified_fault_keeps_actual_gate_drain_pending[foreign_then_close]",
    "tests/proofs/l3_source/source_lease_cases.py::test_uncertified_fault_keeps_actual_gate_drain_pending[native_close]",
    "tests/proofs/l3_source/source_lease_cases.py::test_uncertified_fault_keeps_actual_gate_drain_pending[native_unknown]",
    "tests/proofs/l3_source/source_lease_cases.py::test_uncertified_fault_keeps_actual_gate_drain_pending[rollback]",
    "tests/proofs/l3_source/source_lease_cases.py::test_wal_writer_between_selects_cannot_mix_snapshot",
    "tests/proofs/l3_source/source_lease_cases.py::test_wrong_native_factory_is_rejected_before_callback_or_sql[row_factory]",
    "tests/proofs/l3_source/source_lease_cases.py::test_wrong_native_factory_is_rejected_before_callback_or_sql[text_factory]",
)

RELATED_NODES: tuple[str, ...] = (
    "tests/test_execution_owner_ticket_gate.py::test_body_and_store_errors_do_not_strand_lock",
    "tests/test_execution_owner_ticket_gate.py::test_cancellation_while_ready_to_acquire_does_not_strand_lock_or_queue",
    "tests/test_execution_owner_ticket_gate.py::test_cancelled_active_ticket_keeps_gate_until_submitted_store_drains",
    "tests/test_execution_owner_ticket_gate.py::test_cancelled_queued_ticket_is_skipped_without_reordering_survivors",
    "tests/test_execution_owner_ticket_gate.py::test_drain_registration_rejects_foreign_loop_and_owner_task",
    "tests/test_execution_owner_ticket_gate.py::test_fifo_orders_writer_producer_writer",
    "tests/test_execution_owner_ticket_gate.py::test_hold_overrun_records_failure_without_cancelling_store",
    "tests/test_execution_owner_ticket_gate.py::test_lock_identity_and_metrics_snapshot_are_immutable_and_bounded",
    "tests/test_execution_owner_ticket_gate.py::test_one_apply_ticket_covers_receive_and_fill_commits",
    "tests/test_execution_owner_ticket_gate.py::test_raising_entry_or_cleanup_clock_releases_and_advances_queue",
    "tests/test_execution_owner_ticket_gate.py::test_repeated_cancellation_during_drain_keeps_gate_owned",
    "tests/test_execution_owner_ticket_gate.py::test_ticket_handle_is_immutable_and_cannot_change_cleanup_attribution",
    "tests/test_execution_policy_registration_boundaries.py::test_receipt_read_error_rolls_back_and_same_connection_recovers[sqlite]",
    "tests/test_execution_policy_registration_boundaries.py::test_receipt_read_error_rolls_back_and_same_connection_recovers[store]",
    "tests/test_execution_policy_registration_boundaries.py::test_receipt_read_error_rolls_back_and_same_connection_recovers[value]",
    "tests/test_execution_policy_registration_boundaries.py::test_receipt_read_is_one_snapshot_and_cancellation_closes_transaction[cancel_restore]",
    "tests/test_execution_policy_registration_boundaries.py::test_receipt_read_is_one_snapshot_and_cancellation_closes_transaction[concurrent_writer]",
    "tests/test_execution_policy_registration_boundaries.py::test_registration_repeated_cancellation_drains_sql_then_requires_restore[False-after_sql]",
    "tests/test_execution_policy_registration_boundaries.py::test_registration_repeated_cancellation_drains_sql_then_requires_restore[False-before_sql]",
    "tests/test_execution_policy_registration_boundaries.py::test_registration_repeated_cancellation_drains_sql_then_requires_restore[True-after_sql]",
    "tests/test_execution_policy_registration_boundaries.py::test_registration_repeated_cancellation_drains_sql_then_requires_restore[True-before_sql]",
    "tests/test_execution_state_store.py::test_caller_mutation_and_nonfinite_json_never_change_stored_snapshot",
    "tests/test_execution_state_store.py::test_commit_dedup_history_does_not_retain_every_full_checkpoint",
    "tests/test_execution_state_store.py::test_conflicting_version_or_commit_id_cannot_overwrite_money",
    "tests/test_execution_state_store.py::test_database_and_sidecars_are_private_and_durable",
    "tests/test_execution_state_store.py::test_existing_invalid_database_is_rejected_not_recreated[corrupt]",
    "tests/test_execution_state_store.py::test_existing_invalid_database_is_rejected_not_recreated[foreign_schema]",
    "tests/test_execution_state_store.py::test_existing_invalid_database_is_rejected_not_recreated[future_schema]",
    "tests/test_execution_state_store.py::test_reopen_and_idempotent_commit_preserve_atomic_checkpoint",
    "tests/test_execution_state_store.py::test_symlink_database_is_rejected_before_target_mutation",
    "tests/test_execution_state_store.py::test_transaction_failure_leaves_checkpoint_and_commit_marker_unchanged",
)


def inventory_digest(nodes: tuple[str, ...]) -> str:
    """Return the specified canonical JSON SHA-256 for an inventory tuple."""
    encoded = json.dumps(
        nodes, ensure_ascii=False, separators=(",", ":"), allow_nan=False
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def validate_inventory(actual: list[str], *, lane: str) -> None:
    """Require an unordered, duplicate-free exact inventory for one lane."""
    if lane not in {"source", "related"}:
        raise ValueError("source_proof_inventory_invalid_lane")
    if type(actual) is not list:
        raise ValueError("source_proof_inventory_invalid_actual")
    if len(actual) > 20_000:
        raise ValueError("source_proof_inventory_too_many_nodes")
    for node in actual:
        if type(node) is not str:
            raise ValueError("source_proof_inventory_invalid_node")
        try:
            if len(node.encode("utf-8")) > 2048:
                raise ValueError("source_proof_inventory_node_too_long")
        except UnicodeEncodeError as exc:
            raise ValueError("source_proof_inventory_invalid_node") from exc
    if len(set(actual)) != len(actual):
        raise ValueError("source_proof_inventory_duplicate")
    expected = SOURCE_NODES if lane == "source" else RELATED_NODES
    unexpected = set(actual) - set(expected)
    if unexpected:
        raise ValueError("source_proof_inventory_unexpected")
    if set(actual) != set(expected):
        raise ValueError("source_proof_inventory_missing")
