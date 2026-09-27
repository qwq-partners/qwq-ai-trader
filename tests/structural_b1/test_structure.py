"""Small B1a tests only: no source/native lane or large-node claim."""

import importlib.util
import os
from pathlib import Path
import sys

import pytest


def _load_siblings():
    folder = Path(__file__).parent
    for name in ("subject", "observer", "fixtures", "mutants"):
        module_name = "_qwq_b1_" + name
        spec = importlib.util.spec_from_file_location(module_name, folder / (name + ".py"))
        module = importlib.util.module_from_spec(spec)
        sys.modules[module_name] = module
        spec.loader.exec_module(module)


_load_siblings()
import _qwq_b1_subject as subject
import _qwq_b1_observer as observer
import _qwq_b1_fixtures as fixtures
import _qwq_b1_mutants as mutants


def test_append_requires_actual_chain(monkeypatch):
    """Missing actual append cannot be certified by a phase/completion flag."""
    census = observer.install_census(monkeypatch, subject)
    supervisor = subject.make_supervisor(fixtures.TOKEN)
    operation = supervisor.active
    violations = fixtures.drive(supervisor, operation, fixtures.TOKEN, (fixtures.APPEND,), census)
    assert operation.arena.head is not None, "STRUCTURE_MISSING_CHAIN"
    assert violations == (), violations
    state = observer.snapshot(supervisor, census)
    slots = dict(state.slots)
    assert slots[("arena", "head")] == slots[("arena", "tail")] == 1
    assert slots[(1, "tag")] == "EMPTY"
    assert slots[(1, "prev")] is None and slots[(1, "next")] is None
    assert all(slots[("holders", name)] is None for name in observer.HOLDER_SLOTS)
    assert (state.reached, state.returned, state.live) == (1, 1, (1,))


def _start(monkeypatch, count=0):
    census = observer.install_census(monkeypatch, subject)
    supervisor = subject.make_supervisor(fixtures.TOKEN)
    operation = supervisor.active
    assert fixtures.drive(supervisor, operation, fixtures.TOKEN, (fixtures.APPEND,) * count, census) == ()
    return supervisor, operation, census


def _step_checked(supervisor, operation, census):
    before = observer.snapshot(supervisor, census)
    assert subject.tick(supervisor, operation, fixtures.TOKEN) is None
    after = observer.snapshot(supervisor, census)
    assert observer.check_transition(before, after) == ()
    return after


def _assert_literal(state, compact, phase, action, constructors):
    assert fixtures.compact(state) == compact, "STRUCTURE_LITERAL_SLOTS"
    assert state.phase == phase
    assert state.action == (None if phase == "IDLE" else action)
    assert (state.reached, state.returned, state.generation) == (constructors,) * 3
    assert state.outcome is None and state.fault_id is None
    assert state.issues == ()


def test_allocate_to_new_exact_transition(monkeypatch):
    supervisor, operation, census = _start(monkeypatch)
    before = observer.snapshot(supervisor, census)
    assert (before.live, before.reached, before.returned) == ((), 0, 0)
    assert fixtures.compact(before) == (None, None, None, None, None, None, None, ())
    assert before.phase == "IDLE" and before.action is None
    assert before.outcome is None and before.fault_id is None and before.issues == ()
    assert operation.token is fixtures.TOKEN
    subject.submit(supervisor, operation, fixtures.TOKEN, fixtures.APPEND)
    after = _step_checked(supervisor, operation, census)
    _assert_literal(after, fixtures.APPEND_STATES[0][0], "A1", fixtures.APPEND, 1)


def test_append_three_literal_transitions(monkeypatch):
    supervisor, operation, census = _start(monkeypatch)
    for count, states in enumerate(fixtures.APPEND_STATES, 1):
        subject.submit(supervisor, operation, fixtures.TOKEN, fixtures.APPEND)
        for want, phase in zip(states, fixtures.APPEND_PHASES, strict=True):
            _assert_literal(_step_checked(supervisor, operation, census), want, phase, fixtures.APPEND, count)
        state = observer.snapshot(supervisor, census)
        assert state.forward == tuple(range(1, count + 1))
        assert state.backward == tuple(range(count, 0, -1))
    assert fixtures.drive(supervisor, operation, fixtures.TOKEN, (fixtures.HEAD,) * 3, census) == ()
    assert observer.terminal_live(census) == ()


UNLINK_CASES = (("single", 1, fixtures.HEAD), ("head", 3, fixtures.HEAD),
                ("tail", 3, fixtures.TAIL), ("middle", 3, fixtures.MIDDLE))


@pytest.mark.parametrize("position,count,action", UNLINK_CASES, ids=("single", "head", "tail", "middle"))
def test_unlink_positions_literal_transitions(monkeypatch, position, count, action):
    supervisor, operation, census = _start(monkeypatch, count)
    subject.submit(supervisor, operation, fixtures.TOKEN, action)
    for want, phase in zip(fixtures.UNLINK_STATES[position], fixtures.UNLINK_PHASES, strict=True):
        _assert_literal(_step_checked(supervisor, operation, census), want, phase, action, count)
    assert fixtures.drive(supervisor, operation, fixtures.TOKEN, (fixtures.HEAD,) * (count - 1), census) == ()
    assert observer.terminal_issues(observer.snapshot(supervisor, census)) == ()
    assert observer.terminal_live(census) == ()


class ForeignFault(Exception):
    def __bool__(self):
        raise AssertionError("FOREIGN_BOOL_CALLED")

    def __repr__(self):
        raise AssertionError("FOREIGN_REPR_CALLED")

    def __str__(self):
        raise AssertionError("FOREIGN_STR_CALLED")


FAULT_CASES = (
    ("append1", 0, fixtures.APPEND, fixtures.APPEND_STATES[0], fixtures.APPEND_PHASES),
    ("append2", 1, fixtures.APPEND, fixtures.APPEND_STATES[1], fixtures.APPEND_PHASES),
    ("append3", 2, fixtures.APPEND, fixtures.APPEND_STATES[2], fixtures.APPEND_PHASES),
    *((name, count, action, fixtures.UNLINK_STATES[name], fixtures.UNLINK_PHASES)
      for name, count, action in UNLINK_CASES),
)
FAULT_BOUNDARIES = tuple(
    pytest.param(count, action, states, phases, row, timing, id=f"{name}-{row}-{timing}")
    for name, count, action, states, phases in FAULT_CASES
    for row in range(len(states)) for timing in ("before", "after")
)


@pytest.mark.parametrize("count,action,states,phases,row,timing", FAULT_BOUNDARIES)
def test_foreign_fault_preserves_each_boundary(monkeypatch, count, action, states, phases, row, timing):
    supervisor, operation, census = _start(monkeypatch, count)
    subject.submit(supervisor, operation, fixtures.TOKEN, action)
    for index in range(row):
        _assert_literal(_step_checked(supervisor, operation, census), states[index], phases[index], action,
                        count + (1 if action == fixtures.APPEND else 0))
    before = observer.snapshot(supervisor, census)
    fault = ForeignFault()
    original_step = subject._step

    def fail_at_boundary(op):
        if timing == "before":
            raise fault
        original_step(op)
        raise fault

    monkeypatch.setattr(subject, "_step", fail_at_boundary)
    assert subject.tick(supervisor, operation, fixtures.TOKEN) is None
    assert operation.fault is fault
    after = observer.snapshot(supervisor, census)
    expected = before if timing == "before" else fixtures.expected_transition(before)
    assert dict(after.slots) == dict(expected.slots)
    assert (after.phase, after.action, after.live) == (expected.phase, expected.action, expected.live)
    assert (after.reached, after.returned, after.generation) == (expected.reached, expected.returned, expected.generation)
    assert after.fault_id == id(fault) and after.outcome == "DISPOSAL_FAULT"
    assert after.operation_id == before.operation_id and after.token_id == before.token_id
    for request in ("submit", "tick"):
        with pytest.raises(ValueError):
            if request == "submit":
                subject.submit(supervisor, operation, fixtures.TOKEN, fixtures.APPEND)
            else:
                subject.tick(supervisor, operation, fixtures.TOKEN)
        assert observer.snapshot(supervisor, census) == after
        assert operation.fault is fault
    # FAULT_RETAINED: deliberately no terminal-live/free assertion or frame clearing.


class TupleSubclass(tuple):
    pass


class StringSubclass(str):
    pass


REJECTIONS = (
    "not_tuple", "tuple_subclass", "short", "long", "name_subclass", "unknown", "payload",
    "busy", "empty_head", "empty_tail", "empty_middle", "middle_one", "middle_two",
    "middle_bad_reverse", "owner_missing", "wrong_operation", "wrong_token", "idle_tick", "fault",
)


@pytest.mark.parametrize("case", REJECTIONS)
def test_action_rejections_are_unchanged(monkeypatch, case):
    count = {"middle_one": 1, "middle_two": 2, "middle_bad_reverse": 3}.get(case, 0)
    supervisor, operation, census = _start(monkeypatch, count)
    malformed = {
        "not_tuple": ["append_empty", None], "tuple_subclass": TupleSubclass(fixtures.APPEND),
        "short": ("append_empty",), "long": ("append_empty", None, None),
        "name_subclass": (StringSubclass("append_empty"), None), "unknown": ("finish", None),
        "payload": ("append_empty", 1),
    }
    action = malformed.get(case, fixtures.APPEND)
    if case == "busy":
        subject.submit(supervisor, operation, fixtures.TOKEN, fixtures.APPEND)
    if case.startswith("empty_") or case.startswith("middle_"):
        action = {"empty_head": fixtures.HEAD, "empty_tail": fixtures.TAIL}.get(case, fixtures.MIDDLE)
    if case == "middle_bad_reverse":
        operation.arena.tail.prev = operation.arena.head
    if case == "fault":
        operation.fault = ForeignFault()
        operation.outcome = "DISPOSAL_FAULT"
    before = observer.snapshot(supervisor, census)
    supplied_operation = subject.Operation(fixtures.TOKEN) if case == "wrong_operation" else operation
    supplied_token = tuple([1, 2, 3, 4]) if case == "wrong_token" else fixtures.TOKEN
    if case == "wrong_token":
        assert supplied_token == fixtures.TOKEN and supplied_token is not fixtures.TOKEN
    if case == "owner_missing":
        supervisor.active = None
    try:
        with pytest.raises(ValueError):
            if case == "idle_tick":
                subject.tick(supervisor, supplied_operation, supplied_token)
            else:
                subject.submit(supervisor, supplied_operation, supplied_token, action)
        if case == "owner_missing":
            assert supervisor.active is None
    finally:
        if case == "owner_missing":
            supervisor.active = operation
    assert observer.snapshot(supervisor, census) == before


@pytest.mark.parametrize("token", (None, [1, 2, 3, 4], TupleSubclass((1, 2, 3, 4)),
                                  (1, 2, 3), (1, 2, 3, 5), (True, 2, 3, 4), (1.0, 2, 3, 4)),
                         ids=("none", "list", "subclass", "short", "value", "bool", "float"))
def test_bootstrap_rejects_malformed_tokens(monkeypatch, token):
    census = observer.install_census(monkeypatch, subject)
    with pytest.raises(ValueError):
        subject.make_supervisor(token)
    assert (census.reached, census.returned, observer.terminal_live(census)) == (0, 0, ())


def test_terminal_after_frames_return(monkeypatch):
    supervisor, operation, census = _start(monkeypatch)
    assert fixtures.drive(supervisor, operation, fixtures.TOKEN,
                          (fixtures.APPEND, fixtures.APPEND, fixtures.APPEND,
                           fixtures.MIDDLE, fixtures.HEAD, fixtures.HEAD), census) == ()
    assert observer.terminal_live(census) == ()
    state = observer.snapshot(supervisor, census)
    assert state.forward == state.backward == state.live == ()
    assert observer.terminal_issues(state) == ()
    # Another allocation must use a new generation even if Python reuses an id.
    assert fixtures.drive(supervisor, operation, fixtures.TOKEN, (fixtures.APPEND,), census) == ()
    assert observer.snapshot(supervisor, census).live == (4,)
    assert fixtures.drive(supervisor, operation, fixtures.TOKEN, (fixtures.HEAD,), census) == ()
    assert observer.terminal_live(census) == ()


@pytest.mark.parametrize("site", ("census", "snapshot"))
def test_observer_failure_is_inconclusive(monkeypatch, site):
    supervisor, operation, census = _start(monkeypatch)
    subject.submit(supervisor, operation, fixtures.TOKEN, fixtures.APPEND)
    before = observer.snapshot(supervisor, census)

    def fail(*args):
        raise RuntimeError("OBSERVER_INTERNAL_FAILURE")

    if site == "census":
        monkeypatch.setattr(observer.Census, "record", fail)
        with pytest.raises(observer.ObserverAbort, match="INCONCLUSIVE_OBSERVER"):
            subject.tick(supervisor, operation, fixtures.TOKEN)
        assert census.reached == census.returned == 1
        assert operation.phase == "A0" and operation.holders.new is None
    else:
        with monkeypatch.context() as patch:
            patch.setattr(observer, "_reference", fail)
            with pytest.raises(observer.ObserverAbort, match="INCONCLUSIVE_OBSERVER"):
                observer.snapshot(supervisor, census)
        assert observer.snapshot(supervisor, census) == before
    assert supervisor.active is operation and operation.token is fixtures.TOKEN
    assert operation.fault is None and operation.outcome is None


@pytest.mark.parametrize("visibility", ("visible", "hidden"))
def test_allocator_bypass_is_not_qualified(monkeypatch, visibility):
    supervisor, operation, census = _start(monkeypatch)
    holder = mutants.ExternalHolder()
    if visibility == "visible":
        # Bypass the observed constructor, but store the actual uncensused cell.
        monkeypatch.setattr(subject, "_new_cell", subject.Cell)
        subject.submit(supervisor, operation, fixtures.TOKEN, fixtures.APPEND)
        subject.tick(supervisor, operation, fixtures.TOKEN)
        state = observer.snapshot(supervisor, census)
        assert "STRUCTURE_CENSUS_MISSING" in state.issues
        assert dict(state.slots)[("holders", "new")][0] == "UNCENSUSED"
    else:
        holder.value = subject.Cell()
        assert fixtures.drive(supervisor, operation, fixtures.TOKEN,
                              (fixtures.APPEND, fixtures.HEAD), census) == ()
        assert observer.terminal_live(census) == ()
        assert type(holder.value) is subject.Cell
        # A witnessed hidden allocation remains invisible to this census.
        assert observer.coverage_verdict(census, bypass_witnessed=True) == "INCONCLUSIVE_COVERAGE"


RAW_NAMES = (
    "allocate_and_link", "skip_chain", "clear_new_before_cursor", "unlink_single", "unlink_head",
    "unlink_tail", "unlink_middle", "clear_unlink_early", "clear_cursor_early",
    "hidden_leaf", "exception_alias", "closure_alias",
)


def _raw_actions(name):
    if name in ("none", "allocate_and_link", "skip_chain", "clear_new_before_cursor"):
        return (fixtures.APPEND,)
    if name in ("unlink_single", "hidden_leaf", "exception_alias", "closure_alias"):
        return (fixtures.APPEND, fixtures.HEAD)
    if name == "unlink_head":
        return (fixtures.APPEND,) * 3 + (fixtures.HEAD,) * 3
    if name == "unlink_tail":
        return (fixtures.APPEND,) * 3 + (fixtures.TAIL, fixtures.HEAD, fixtures.HEAD)
    if name in ("unlink_middle", "clear_unlink_early", "clear_cursor_early"):
        return (fixtures.APPEND,) * 3 + (fixtures.MIDDLE, fixtures.HEAD, fixtures.HEAD)
    raise ValueError("HARNESS_UNKNOWN_RAW_MUTANT")


def _probe(monkeypatch, name, actions):
    supervisor, operation, census = _start(monkeypatch)
    external_holder = mutants.ExternalHolder()
    mutants.install_mutant(monkeypatch, subject, name, external_holder)
    violations = fixtures.drive(supervisor, operation, fixtures.TOKEN, actions, census)
    # The external holder stays alive across this terminal measurement. No Cell
    # local from driver/constructor/snapshot survives its returned frame.
    if name in mutants.ALIAS_NAMES:
        assert observer.terminal_live(census) == (), "STRUCTURE_TERMINAL_LIVE"
    assert violations == (), violations
    if actions[-1] != fixtures.APPEND:
        assert observer.terminal_live(census) == (), "STRUCTURE_TERMINAL_LIVE"
    state = observer.snapshot(supervisor, census)
    assert observer.terminal_issues(state) == ()
    if actions[-1] == fixtures.APPEND:
        assert fixtures.compact(state) == fixtures.APPEND_STATES[0][-1]
    else:
        assert fixtures.compact(state) == (None, None, None, None, None, None, None, ())


@pytest.mark.parametrize("name", RAW_NAMES)
def test_actual_mutants_rejected(monkeypatch, name):
    actions = _raw_actions(name)
    with monkeypatch.context() as normal:
        _probe(normal, "none", actions)
    with monkeypatch.context() as changed:
        with pytest.raises(AssertionError, match="STRUCTURE_"):
            _probe(changed, name, actions)


@pytest.mark.parametrize("name", mutants.IDENTITY_NAMES)
@pytest.mark.parametrize("request", ("submit", "tick"))
def test_identity_mutants_rejected_before_allocation(monkeypatch, name, request):
    supervisor, operation, census = _start(monkeypatch)
    if request == "tick":
        subject.submit(supervisor, operation, fixtures.TOKEN, fixtures.APPEND)
    before = observer.snapshot(supervisor, census)
    mutants.install_mutant(monkeypatch, subject, name, mutants.ExternalHolder())
    with pytest.raises(ValueError, match="INVALID_IDENTITY"):
        if request == "submit":
            subject.submit(supervisor, operation, fixtures.TOKEN, fixtures.APPEND)
        else:
            subject.tick(supervisor, operation, fixtures.TOKEN)
    assert observer.snapshot(supervisor, census) == before
    assert census.reached == census.returned == 0


def test_net_delta_does_not_prove_write_count(monkeypatch):
    supervisor, operation, census = _start(monkeypatch)
    holder = mutants.ExternalHolder()
    mutants.install_mutant(monkeypatch, subject, "double_write_restore", holder)
    # This surviving runtime comparison is a limitation, NOT a structural kill.
    assert fixtures.drive(supervisor, operation, fixtures.TOKEN,
                          (fixtures.APPEND, fixtures.APPEND, fixtures.HEAD, fixtures.HEAD), census) == ()
    assert observer.terminal_live(census) == ()


def test_raw_mutant_probe(monkeypatch):
    name = os.environ.get("QWQ_B1_RAW_MUTANT", "none")
    if name not in ("none", *RAW_NAMES):
        raise ValueError("HARNESS_UNKNOWN_RAW_MUTANT")
    _probe(monkeypatch, name, _raw_actions(name))
