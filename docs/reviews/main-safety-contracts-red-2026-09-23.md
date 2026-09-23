# Main 안전 계약 수용 테스트 — INTENTIONALLY RED / 배포 불가

## 범위와 모델

- 작업 N2-A: 제품을 합치기 전에 현재 main의 안전성 수용 테스트를 개발선에 그대로 고정한다. 이번 산출물은 **의도된 RED 테스트 commit이며 deployable이 아니다**.
- 기준 개발 HEAD: `78a253b797b160da746ea7d4548edeccffc40dfb`.
- 읽기 전용 원본: main `afa6e1e2240e79eb6964611babc466c55abcf783`.
- 작업 브랜치: `feature/main-safety-contracts-20260923`.
- 요청 모델/effort: `gpt-6-astra / high`. 응답 측 actual-model/effective-effort 필드는 노출되지 않아 실제 값은 **미검증**이다. fallback·worker fan-out 없음.
- 역할은 기존 main 테스트의 정확한 이식과 RED 근거 확보다. 이전 KIS 관측 구현의 자기 리뷰가 아니며 제품 코드 리뷰/수정/통합/배포 승인을 주장하지 않는다.
- 변경은 새 테스트 5파일과 이 보고서뿐이다. src/config/기존 테스트/CHANGELOG/CLAUDE/다른 문서 변경 0. 단언 완화·skip·xfail 추가 0. 실 API/주문/.env/운영 캐시·로그/SSH/배포/재시작/설정/push 0. 전체 suite는 금지 범위여서 실행하지 않았다.

## Plan → Do: 원본 동일성

각 파일을 지정 SHA의 `git show`로 읽고 apply_patch로 신규 생성했다. `git hash-object`와 원본 `git ls-tree` blob이 모두 일치한다. 원본에 있던 주석과 문서의 “수정” 표현은 그 main 구현의 역사이며, 이 개발선에서 기능 구현을 완료했다는 뜻이 아니다.

| 새 테스트 파일 | 원본·복사본 공통 blob |
| --- | --- |
| tests/test_kis_tr_switch.py | c7230d8cde0aec17990d341dd97b8c8273f3f37d |
| tests/test_kis_pagination_protocol.py | e79330e6e268a1e8755a0ff930df71fc1f1a92bd |
| tests/test_engine_stale_pending_fixes.py | 1d92f0d32b470f75f9ff6ecd2f4488c4ee81c97a |
| tests/test_exit_exempt_sell_guard.py | 4936177ebc6f935e897b2effd2efabfaa65cbe33 |
| tests/test_stale_sell_cancel_failure.py | dedaf455a3ec889a3f039d81067ee1b9671bd737 |

## See: 실제 검증

모든 pytest 명령의 공통 실행 접두부:

```text
env -i PATH=/usr/bin:/bin LANG=C.UTF-8 TZ=UTC PYTHONDONTWRITEBYTECODE=1 PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 QWQ_DEPLOY_SSH_KEY=/tmp/qwq-offline-no-ssh-key /home/ubuntu/projects/qwq-ai-trader/venv/bin/python -m pytest
```

실행한 인자와 종료값:

1. 기존 baseline:
   `tests/test_engine_legacy_stale_eviction_characterization.py tests/test_engine_legacy_session_characterization.py -q -p no:cacheprovider --tb=short --show-capture=no`
   → **31 passed / 4.53초 / exit0 / 격리 위반0**.
2. main 5파일 기본 실행:
   `tests/test_kis_tr_switch.py tests/test_kis_pagination_protocol.py tests/test_engine_stale_pending_fixes.py tests/test_exit_exempt_sell_guard.py tests/test_stale_sell_cancel_failure.py -q -p no:cacheprovider --tb=short --show-capture=no`
   → **수집 ERROR1 / 4.37초 / exit2 / 격리0**. `test_stale_sell_cancel_failure.py:28`이 `src.core.engine._SellKeep`를 import할 수 없어 전체 실행이 테스트 본문 전에 중단됐다. 따라서 이 실행은 행동 RED가 아니라 **수집 단계 계약 부재**다.
3. 동일한 main 5파일에 `--continue-on-collection-errors`를 추가한 진단:
   → **77 failed / 18 passed / 수집 ERROR1 / 12.13초 / exit1 / 격리0**.
   수집 불가능한 파일은 그대로 ERROR이며, 나머지 파일만 실행해 근거를 더 얻었다. 이 옵션은 오류를 통과나 xfail로 바꾸지 않는다.
4. baseline 2파일의 `--collect-only -q -p no:cacheprovider --tb=short --show-capture=no`:
   → **31 tests collected / 2.98초 / exit0 / 격리0**. stale/eviction **26개**, session **5개**를 확인했다.

파일별 실행 결과:

| main 테스트 | 실행된 실패 | 통과 | 미실행/구분 |
| --- | ---: | ---: | --- |
| test_kis_tr_switch.py | 35 | 3 | 모두 전환 계약 미도입으로 실패 |
| test_kis_pagination_protocol.py | 7 | 3 | 계약 미도입1 + 행동 단언6 |
| test_engine_stale_pending_fixes.py | 20 | 10 | 계약 미도입6 + 행동 단언13 + 하네스 분기1 |
| test_exit_exempt_sell_guard.py | 15 | 2 | 행동 단언15 |
| test_stale_sell_cancel_failure.py | 실행 전 중단 | 0 | _SellKeep import ERROR; 이 파일의 행동 검증 결과는 없음 |

77개 실패를 원인별로 구분한다:

- **미도입된 인터페이스/계약 42건:** `_TR_NEW` 부재28건(TR 파일27 + 페이징1), `_tr_id` 부재8건(환경변수 subprocess가 비정상 종료), `get_exchange_open_orders(symbol=…)` 인자 미지원5건, `release_kept_stale_buy` 메서드 부재1건.
- **실제 행동 단언 차이 34건:** 다음 페이지 `tr_cont: N`·외부계좌 마지막 페이지 종료6건, stale BUY/SELL·예약·fallback 상태13건, 면제 SELL/코어 경로15건.
- **하네스의 예상 밖 분기 1건:** `test_same_symbol_buy_in_the_same_call_is_blocked_after_zero_cancel_release`에서 차단될 예정이던 BUY가 sizing 분기로 들어가 `SimpleNamespace.total_equity` 부재로 종료했다. 의도한 차단이 없다는 경로 증거지만, 이 결과 자체로 실제 주문 중복 발생까지 단정하지 않는다. 하네스를 보강하거나 제품을 바꾸지 않았다.

보조 검사:

- `git diff --check`와 새 5파일 `python -m py_compile`는 순차 명령에서 통과했다(PYTHONPYCACHEPREFIX=/tmp/qwq-main-safety-contracts-pycache).
- 뒤이어 새 5파일에 verify.sh와 동일한 private-key/AWS/GitHub 비밀 패턴을 파일명만 출력하도록 검사했다. rg **exit1=매치 없음**이다. 복합 명령의 최종 exit1은 테스트/문법 실패가 아니라 이 no-match 상태다.
- 외부 네트워크·운영 상태 접근은 테스트 격리 장치가 모든 실행에서 0건으로 확인했다.

## 기존 특성화26개와의 관계

기존 `test_engine_legacy_stale_eviction_characterization.py`는 스스로 “옳은 동작의 정의가 아니다”라고 명시한 당시 legacy 대조군이다. **26개 전체가 폐기 대상이라는 뜻은 아니다.** 정규장 fallback, 수량, 상한, 축출 보호 등 보존할 계약도 포함한다. 실제 main 행동을 통합하는 coordinator는 다음 직접 충돌을 명시적으로 처분해야 한다.

| 개발선에서 현재 통과하는 구 기대 | main 수용 기대 |
| --- | --- |
| 동시호가 stale SELL을 취소하고 재주문하지 않음 | 유효한 보유 포지션의 원 지정가 SELL 유지; 먼저 취소하지 않음 |
| exit_exempt 종목도 stale fallback에서 시장가 SELL | 면제 종목 SELL·시장가 fallback 차단, 취소 생존 상태에 맞는 pending 처리 |
| stale BUY 취소0건을 소멸로 읽어 pending/예약 해제 | 브로커 추적·거래소 생존/판단불가 계약에 따라 보수적으로 유지·확인 |

위 충돌은 각각 새 main 테스트의 실제 행동 단언 실패로도 관측됐다. 기존 특성화나 새 main 테스트를 이번 작업에서 수정하지 않았다. session 5개는 기존 세션 경계 대조군이며 전부 통과했다. 분할 SELL 취소 실패 보호의 main 계약은 이번 브랜치에서 `_SellKeep` import 단계에 막혀 행동 검증하지 못했다.

## 후속과 제한

이 commit은 수용 계약을 먼저 고정하기 위한 **INTENTIONALLY RED / 배포 불가** 산출물이다. 이후 제품 통합은 coordinator가 독립 작업에서 수행한다. 수집 오류 해소 뒤 분할 SELL 파일까지 실행하고, 구 legacy 단언의 각 충돌을 처분한 다음, 관련 검증·coordinator 전체 suite·독립 critical 리뷰가 필요하다. 이번 테스트 결과를 개발선의 운영 승격이나 기존 설치 차단 해제로 해석하지 않는다.

## 부록 A — 실행 중 실패한 모든 node

아래는 계속수집 진단의 77개 실패 목록이다. 수집 ERROR는 별도로 `tests/test_stale_sell_cancel_failure.py`다.

```text
tests/test_kis_tr_switch.py::test_tr_id_snapshot_for_every_path[False-expected0]
tests/test_kis_tr_switch.py::test_tr_id_snapshot_for_every_path[True-expected1]
tests/test_kis_tr_switch.py::test_legacy_order_body_is_byte_identical
tests/test_kis_tr_switch.py::test_legacy_cancel_and_modify_bodies_are_byte_identical
tests/test_kis_tr_switch.py::test_legacy_daily_query_keeps_excg_all - ...
tests/test_kis_tr_switch.py::test_legacy_cancelable_query_body_unchanged
tests/test_kis_tr_switch.py::test_new_order_body_adds_excg_and_cndt_pric
tests/test_kis_tr_switch.py::test_new_cancel_and_modify_bodies_add_excg
tests/test_kis_tr_switch.py::test_new_order_body_hashkey_covers_the_added_keys
tests/test_kis_tr_switch.py::test_new_cancelable_falls_back_to_psbl_qty
tests/test_kis_tr_switch.py::test_new_cancelable_prefers_rmn_qty_when_present
tests/test_kis_tr_switch.py::test_new_cancelable_blank_rmn_qty_still_reads_psbl_qty
tests/test_kis_tr_switch.py::test_new_cancelable_blank_both_quantities_is_undecidable
tests/test_kis_tr_switch.py::test_new_cancelable_without_any_qty_key_is_undecidable
tests/test_kis_tr_switch.py::test_new_cancelable_negative_qty_is_undecidable
tests/test_kis_tr_switch.py::test_legacy_cancelable_negative_qty_keeps_current_behaviour
tests/test_kis_tr_switch.py::test_legacy_cancelable_missing_rmn_qty_keeps_current_behaviour
tests/test_kis_tr_switch.py::test_check_fills_reads_the_same_response_keys[False]
tests/test_kis_tr_switch.py::test_check_fills_reads_the_same_response_keys[True]
tests/test_kis_tr_switch.py::test_order_tr_and_body_by_session[False-regular-TTTC0802U-False]
tests/test_kis_tr_switch.py::test_order_tr_and_body_by_session[False-pre_market-TTTC0802U-False]
tests/test_kis_tr_switch.py::test_order_tr_and_body_by_session[False-next_market-TTTC0802U-False]
tests/test_kis_tr_switch.py::test_order_tr_and_body_by_session[True-regular-TTTC0012U-True]
tests/test_kis_tr_switch.py::test_order_tr_and_body_by_session[True-pre_market-TTTC0802U-False]
tests/test_kis_tr_switch.py::test_order_tr_and_body_by_session[True-next_market-TTTC0802U-False]
tests/test_kis_tr_switch.py::test_order_and_modify_posts_are_pinned_to_no_retry[False]
tests/test_kis_tr_switch.py::test_order_and_modify_posts_are_pinned_to_no_retry[True]
tests/test_kis_tr_switch.py::test_kis_tr_set_env_parsing[None-TTTC0802U]
tests/test_kis_tr_switch.py::test_kis_tr_set_env_parsing[-TTTC0802U]
tests/test_kis_tr_switch.py::test_kis_tr_set_env_parsing[legacy-TTTC0802U]
tests/test_kis_tr_switch.py::test_kis_tr_set_env_parsing[NEW-TTTC0802U]
tests/test_kis_tr_switch.py::test_kis_tr_set_env_parsing[New-TTTC0802U]
tests/test_kis_tr_switch.py::test_kis_tr_set_env_parsing[1-TTTC0802U]
tests/test_kis_tr_switch.py::test_kis_tr_set_env_parsing[true-TTTC0802U]
tests/test_kis_tr_switch.py::test_kis_tr_set_env_parsing[new-TTTC0012U]
tests/test_kis_pagination_protocol.py::test_daily_fills_single_page_sends_no_tr_cont_header
tests/test_kis_pagination_protocol.py::test_positions_three_pages_send_n_from_the_second
tests/test_kis_pagination_protocol.py::test_positions_for_account_three_pages_send_n_from_the_second
tests/test_kis_pagination_protocol.py::test_daily_fills_three_pages_send_n_from_the_second
tests/test_kis_pagination_protocol.py::test_retry_of_the_same_page_keeps_the_same_tr_cont
tests/test_kis_pagination_protocol.py::test_positions_for_account_stops_on_header_d_even_with_filled_ctx
tests/test_kis_pagination_protocol.py::test_positions_for_account_two_pages_still_merge
tests/test_engine_stale_pending_fixes.py::test_closing_auction_keeps_limit_sell_on_exchange
tests/test_engine_stale_pending_fixes.py::test_zero_cancel_with_tracked_order_keeps_reservation[submitted]
tests/test_engine_stale_pending_fixes.py::test_zero_cancel_with_tracked_order_keeps_reservation[partial]
tests/test_engine_stale_pending_fixes.py::test_zero_cancel_retry_keeps_while_alive_on_exchange[거래소 생존 → 연속 판단불가 횟수 초기화]
tests/test_engine_stale_pending_fixes.py::test_zero_cancel_retry_keeps_while_alive_on_exchange[조회 실패(판단 불가) → 횟수 +1]
tests/test_engine_stale_pending_fixes.py::test_zero_cancel_retry_releases_when_gone_from_exchange
tests/test_engine_stale_pending_fixes.py::test_zero_cancel_without_tracked_order_still_releases
tests/test_engine_stale_pending_fixes.py::test_confirmed_alive_order_is_never_force_released
tests/test_engine_stale_pending_fixes.py::test_keep_is_bounded_even_if_exchange_query_keeps_failing
tests/test_engine_stale_pending_fixes.py::test_open_orders_error_keeps_pending
tests/test_engine_stale_pending_fixes.py::test_new_pending_starts_with_zero_fallback_count
tests/test_engine_stale_pending_fixes.py::test_open_order_query_three_states_only_for_the_asked_symbol[005930-F-None]
tests/test_engine_stale_pending_fixes.py::test_open_order_query_three_states_only_for_the_asked_symbol[005930-M-None]
tests/test_engine_stale_pending_fixes.py::test_open_order_query_three_states_only_for_the_asked_symbol[005930-D-expected2]
tests/test_engine_stale_pending_fixes.py::test_open_order_query_three_states_only_for_the_asked_symbol[005930--expected3]
tests/test_engine_stale_pending_fixes.py::test_open_order_query_three_states_only_for_the_asked_symbol[000660-F-expected4]
tests/test_engine_stale_pending_fixes.py::test_partial_fill_of_a_kept_stale_buy_unblocks_exits[취소 실패로 유지 중 → 즉시 해제]
tests/test_engine_stale_pending_fixes.py::test_same_symbol_buy_in_the_same_call_is_blocked_after_zero_cancel_release
tests/test_engine_stale_pending_fixes.py::test_synced_position_of_a_kept_stale_buy_can_exit_without_any_signal[유지 중이던 BUY → 해제 후 손절 발행]
tests/test_engine_stale_pending_fixes.py::test_release_kept_stale_buy_leaves_other_pendings_alone
tests/test_exit_exempt_sell_guard.py::test_engine_blocks_exempt_sell_from_any_source[core_early_alert-meta0]
tests/test_exit_exempt_sell_guard.py::test_engine_blocks_exempt_sell_from_any_source[core_stale_auto_sell-meta1]
tests/test_exit_exempt_sell_guard.py::test_engine_blocks_exempt_sell_from_any_source[core_rebalance-meta2]
tests/test_exit_exempt_sell_guard.py::test_engine_blocks_exempt_sell_from_any_source[core_trim-meta3]
tests/test_exit_exempt_sell_guard.py::test_engine_blocks_exempt_sell_from_any_source[gap_and_go-meta4]
tests/test_exit_exempt_sell_guard.py::test_engine_still_sells_non_exempt_and_follows_live_set
tests/test_exit_exempt_sell_guard.py::test_stale_sell_fallback_never_resubmits_exempt_symbol
tests/test_exit_exempt_sell_guard.py::test_stale_exempt_sell_keeps_pending_when_cancel_fails
tests/test_exit_exempt_sell_guard.py::test_stale_fallback_aborts_when_exemption_lands_during_cancel
tests/test_exit_exempt_sell_guard.py::test_on_order_rechecks_exemption_before_submit
tests/test_exit_exempt_sell_guard.py::test_scheduler_cleanup_keeps_exempt_pending_while_sell_may_be_live
tests/test_exit_exempt_sell_guard.py::test_gap_strategy_sell_on_exempt_manual_position_never_becomes_order
tests/test_exit_exempt_sell_guard.py::test_core_early_alert_skips_exempt_core_position
tests/test_exit_exempt_sell_guard.py::test_core_rebalance_fallback_stop_skips_exempt
tests/test_exit_exempt_sell_guard.py::test_core_rebalance_replace_skips_exempt_and_does_not_stall_buys
```

## 부록 B — 기존 baseline31개

아래 앞 26개가 기존 stale/eviction 기대, 뒤 5개가 session 기대다. 모두 현재 개발 기준선에서 통과했다.

```text
tests/test_engine_legacy_stale_eviction_characterization.py::test_a_stale_limit_sell_is_cancelled_then_resubmitted_at_market
tests/test_engine_legacy_stale_eviction_characterization.py::test_the_sell_fallback_threshold_is_ninety_seconds[89-False]
tests/test_engine_legacy_stale_eviction_characterization.py::test_the_sell_fallback_threshold_is_ninety_seconds[90-True]
tests/test_engine_legacy_stale_eviction_characterization.py::test_the_fallback_quantity_is_the_pending_quantity_not_the_whole_position[30-30]
tests/test_engine_legacy_stale_eviction_characterization.py::test_the_fallback_quantity_is_the_pending_quantity_not_the_whole_position[None-100]
tests/test_engine_legacy_stale_eviction_characterization.py::test_the_third_fallback_only_clears_the_pending_and_leaves_the_limit_order
tests/test_engine_legacy_stale_eviction_characterization.py::test_in_the_closing_auction_the_cancel_is_sent_and_nothing_is_reordered
tests/test_engine_legacy_stale_eviction_characterization.py::test_a_rejected_fallback_counts_up_and_gives_up_at_the_limit[0-False]
tests/test_engine_legacy_stale_eviction_characterization.py::test_a_rejected_fallback_counts_up_and_gives_up_at_the_limit[1-True]
tests/test_engine_legacy_stale_eviction_characterization.py::test_a_submit_exception_clears_the_pending_without_knowing_it_was_accepted
tests/test_engine_legacy_stale_eviction_characterization.py::test_a_cancel_exception_skips_the_reorder_and_keeps_the_pending
tests/test_engine_legacy_stale_eviction_characterization.py::test_an_exit_exempt_symbol_is_still_market_sold_by_the_fallback_loop
tests/test_engine_legacy_stale_eviction_characterization.py::test_nothing_is_swept_while_no_signal_arrives
tests/test_engine_legacy_stale_eviction_characterization.py::test_a_stale_buy_is_released_whether_the_cancel_matched_anything_or_not[0]
tests/test_engine_legacy_stale_eviction_characterization.py::test_a_stale_buy_is_released_whether_the_cancel_matched_anything_or_not[3]
tests/test_engine_legacy_stale_eviction_characterization.py::test_a_cancel_exception_keeps_the_stale_buy_pending
tests/test_engine_legacy_stale_eviction_characterization.py::test_without_a_usable_cancel_method_the_stale_buy_is_released_anyway[absent]
tests/test_engine_legacy_stale_eviction_characterization.py::test_without_a_usable_cancel_method_the_stale_buy_is_released_anyway[without_cancel]
tests/test_engine_legacy_stale_eviction_characterization.py::test_a_full_book_emits_a_replacement_sell_for_the_weakest_position
tests/test_engine_legacy_stale_eviction_characterization.py::test_the_protected_positions_are_never_evicted[core]
tests/test_engine_legacy_stale_eviction_characterization.py::test_the_protected_positions_are_never_evicted[pending]
tests/test_engine_legacy_stale_eviction_characterization.py::test_the_protected_positions_are_never_evicted[exit_exempt]
tests/test_engine_legacy_stale_eviction_characterization.py::test_the_protected_positions_are_never_evicted[winner]
tests/test_engine_legacy_stale_eviction_characterization.py::test_the_protected_positions_are_never_evicted[cooldown]
tests/test_engine_legacy_stale_eviction_characterization.py::test_an_edge_below_five_points_skips_the_replacement
tests/test_engine_legacy_stale_eviction_characterization.py::test_two_high_score_buys_in_one_batch_evict_two_different_victims
tests/test_engine_legacy_session_characterization.py::test_the_legacy_session_table_uses_the_same_boundaries_as_the_owner
tests/test_engine_legacy_session_characterization.py::test_legacy_accepts_a_limit_protection_sell_in_the_closing_auction
tests/test_engine_legacy_session_characterization.py::test_legacy_refuses_the_same_sessions_the_owner_cannot_build[closing-market-동시호가]
tests/test_engine_legacy_session_characterization.py::test_legacy_refuses_the_same_sessions_the_owner_cannot_build[break-limit-휴장]
tests/test_engine_legacy_session_characterization.py::test_legacy_refuses_the_same_sessions_the_owner_cannot_build[closed-limit-장 마감]
```
