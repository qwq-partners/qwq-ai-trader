"""실제 private-root 누락을 먼저 판정하는 독립 첫 RED."""

from dataclasses import replace
import importlib.util
import os
from pathlib import Path
import sys

import pytest


def _load_once(name, path):
    path = path.resolve()
    if name in sys.modules:
        module = sys.modules[name]
        if (getattr(module, "__b1b_loader_identity__", None) is not module
                or Path(module.__file__).resolve() != path
                or module.__spec__.name != name
                or Path(module.__spec__.origin).resolve() != path):
            raise RuntimeError("B1B_MODULE_IDENTITY_CONFLICT")
        return module
    spec = importlib.util.spec_from_file_location(name, path)
    if spec is None or spec.loader is None:
        raise RuntimeError("B1B_MODULE_LOAD_FAILED")
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    module.__b1b_loader_identity__ = module
    return module


_HERE = Path(__file__).resolve().parent
shell = _load_once("_qwq_b1b_shell", _HERE.parent / "structural_b1" / "subject.py")
subject = _load_once("_qwq_b1b_subject", _HERE / "subject.py")
observer = _load_once("_qwq_b1b_observer", _HERE / "observer.py")
fixtures = _load_once("_qwq_b1b_fixtures", _HERE / "fixtures.py")
mutants = _load_once("_qwq_b1b_mutants", _HERE / "mutants.py")


def test_scalar_requires_private_root(monkeypatch):
    # 버그 표적: scalar 생성/게시를 생략해 private root가 없는 상태.
    token = (1, 2, 3, 4)
    census = observer.install_census(monkeypatch, subject)
    supervisor = subject.make_supervisor(token)
    operation = supervisor.active
    violations = fixtures.drive(supervisor, operation, token, "scalar7", census)
    assert operation.arena.root is not None, "STRUCTURE_MISSING_PRIVATE_ROOT"
    assert violations == (), violations
    expected = replace(fixtures.expected_states("scalar7")[-1],
                       operation_id=id(operation), token_id=id(token))
    assert observer.check_state(observer.snapshot(supervisor, census), expected) == ()


def _bootstrap(monkeypatch):
    token = (1, 2, 3, 4)
    census = observer.install_census(monkeypatch, subject)
    supervisor = subject.make_supervisor(token)
    return supervisor, supervisor.active, token, census


def _bound(state, operation, token, fault=None):
    return replace(state, operation_id=id(operation), token_id=id(token),
                   fault_id=None if fault is None else id(fault),
                   outcome=state.outcome if fault is None else "DISPOSAL_FAULT")


def _assert_case(monkeypatch, case):
    supervisor, operation, token, census = _bootstrap(monkeypatch)
    violations = fixtures.drive(supervisor, operation, token, case, census)
    assert violations == (), violations
    assert observer.check_state(observer.snapshot(supervisor, census),
                                _bound(fixtures.expected_states(case)[-1], operation, token)) == ()
    return supervisor, operation, token, census


@pytest.mark.parametrize("case", tuple(row[0] for row in fixtures.SCALARS if row[0] != "scalar7"))
def test_scalar_types_and_nonfinite_are_private(monkeypatch, case):
    supervisor, operation, token, census = _assert_case(monkeypatch, case)
    assert operation.outcome is None
    assert observer.retained_live(census) == (1,)
    _assert_rejected(supervisor, operation, token, census, ("scalar", 1))


@pytest.mark.parametrize("case", ("empty_object", "empty_array"))
def test_empty_containers(monkeypatch, case):
    supervisor, operation, token, census = _assert_case(monkeypatch, case)
    assert observer.retained_live(census) == (1,)
    assert census.generation == 2
    assert operation.outcome is None
    _assert_rejected(supervisor, operation, token, census, ("start_array", None))


def test_array_order_and_duplicates(monkeypatch):
    supervisor, operation, token, census = _assert_case(monkeypatch, "array_three")
    assert observer.retained_live(census) == (1, 3, 4, 5, 6, 7, 8)


def test_three_unique_object_keys(monkeypatch):
    supervisor, operation, token, census = _assert_case(monkeypatch, "object_three")
    assert observer.retained_live(census) == (1, 3, 4, 5, 6, 7, 8, 9, 10, 11)


def test_nested_end_then_publish(monkeypatch):
    supervisor, operation, token, census = _assert_case(monkeypatch, "nested")
    assert observer.retained_live(census) == (1, 3, 4, 6, 8, 9, 10, 11)


def _assert_rejected(supervisor, operation, token, census, action=None, *, tick=False,
                     supplied_operation=None, supplied_token=None):
    supplied_operation = operation if supplied_operation is None else supplied_operation
    supplied_token = token if supplied_token is None else supplied_token
    original_view = shell.Supervisor(operation)
    foreign_view = shell.Supervisor(supplied_operation)
    before = observer.snapshot(original_view, census)
    foreign_before = observer.snapshot(foreign_view, census)
    active_id = id(supervisor.active)
    with pytest.raises(ValueError):
        if tick:
            subject.tick(supervisor, supplied_operation, supplied_token)
        else:
            subject.submit(supervisor, supplied_operation, supplied_token, action)
    assert observer.snapshot(original_view, census) == before
    assert observer.snapshot(foreign_view, census) == foreign_before
    assert id(supervisor.active) == active_id


def _prefix(supervisor, operation, token, census, case, last_index):
    trace = fixtures.literal_trace(case)
    actions = iter(trace.actions)
    expected = tuple(_bound(state, operation, token) for state in trace.states)
    before = observer.snapshot(supervisor, census)
    assert observer.check_state(before, expected[0]) == ()
    for index in range(1, last_index + 1):
        if trace.kinds[index] == "submit":
            subject.submit(supervisor, operation, token, next(actions))
        else:
            subject.tick(supervisor, operation, token)
        after = observer.snapshot(supervisor, census)
        assert observer.check_transition(before, after, expected[index - 1], expected[index]) == ()
        before = after


@pytest.mark.parametrize("case", ("duplicate_first", "duplicate_middle", "duplicate_last"))
def test_duplicate_hits_fail_closed(monkeypatch, case):
    supervisor, operation, token, census = _bootstrap(monkeypatch)
    trace = fixtures.literal_trace(case)
    last_submit = max(index for index, kind in enumerate(trace.kinds) if kind == "submit")
    _prefix(supervisor, operation, token, census, case, last_submit - 1)
    before = observer.snapshot(supervisor, census)
    subject.submit(supervisor, operation, token, trace.actions[-1])
    assert observer.check_state(observer.snapshot(supervisor, census),
                                _bound(trace.states[last_submit], operation, token)) == ()
    for index in range(last_submit + 1, len(trace.states)):
        subject.tick(supervisor, operation, token)
        assert observer.check_state(observer.snapshot(supervisor, census),
                                    _bound(trace.states[index], operation, token)) == ()
    after = observer.snapshot(supervisor, census)
    assert (after.reached, after.returned, after.generation) == (before.reached, before.returned, before.generation)
    assert after.slots == before.slots
    assert after.outcome == "B1B_UNSUPPORTED_DUPLICATE"
    for action in (("scalar", 7), ("end", None), ("key", "fresh"), ("publish", None)):
        _assert_rejected(supervisor, operation, token, census, action)
    _assert_rejected(supervisor, operation, token, census, tick=True)


ADMISSION_CASES = (
    ("fresh", ("key", "a")), ("fresh", ("publish", None)), ("fresh", ("end", None)),
    ("open_array", ("key", "a")), ("open_array", ("publish", None)),
    ("open_object", ("scalar", 7)), ("open_object", ("start_object", None)),
    ("open_object", ("start_array", None)), ("open_object", ("publish", None)),
    ("pending_key", ("key", "b")), ("pending_key", ("end", None)),
    ("pending_key", ("publish", None)), ("pending_value", ("end", None)),
    ("pending_value", ("key", "b")), ("pending_value", ("scalar", 8)),
    ("pending_value", ("start_object", None)), ("pending_value", ("start_array", None)),
    ("scalar7", ("scalar", 1)), ("scalar7", ("start_array", None)),
    ("scalar7", ("start_object", None)),
)


@pytest.mark.parametrize("case,action", ADMISSION_CASES)
def test_open_parent_admission_is_unchanged(monkeypatch, case, action):
    supervisor, operation, token, census = _assert_case(monkeypatch, case)
    _assert_rejected(supervisor, operation, token, census, action)
    _assert_rejected(supervisor, operation, token, census, tick=True)


def test_busy_admission_is_unchanged(monkeypatch):
    supervisor, operation, token, census = _bootstrap(monkeypatch)
    subject.submit(supervisor, operation, token, ("scalar", 7))
    _assert_rejected(supervisor, operation, token, census, ("scalar", 8))


def test_array_pending_child_cannot_end_or_overwrite(monkeypatch):
    supervisor, operation, token, census = _bootstrap(monkeypatch)
    # array_three의 두 번째 동작 종료 literal 경계: 게시 전 c1=3.
    trace = fixtures.literal_trace("array_three")
    boundary = tuple(i for i, kind in enumerate(trace.kinds) if kind == "submit")[2] - 1
    _prefix(supervisor, operation, token, census, "array_three", boundary)
    for action in (("end", None), ("scalar", 1), ("start_object", None), ("start_array", None)):
        _assert_rejected(supervisor, operation, token, census, action)


class _Hostile:
    def __eq__(self, other):
        raise AssertionError("USER_EQUALITY_CALLED")

    def __repr__(self):
        raise AssertionError("USER_REPR_CALLED")


class _IntSubclass(int):
    pass


class _FloatSubclass(float):
    pass


class _StrSubclass(str):
    pass


class _TupleSubclass(tuple):
    pass


INVALID_ACTIONS = (
    None, (), ("scalar",), ("scalar", 1, 2), ["scalar", 1],
    (None, None), ("unknown", None), (_Hostile(), None),
    ("scalar", []), ("scalar", {}), ("scalar", ()), ("scalar", b"x"),
    ("scalar", object()), ("scalar", _Hostile()), ("scalar", _IntSubclass(1)),
    ("scalar", _FloatSubclass(1)), ("scalar", _StrSubclass("x")),
    (_StrSubclass("scalar"), 1), _TupleSubclass(("scalar", 1)),
    ("start_object", 1), ("start_array", False), ("publish", "x"), ("end", _Hostile()),
    ("key", None), ("key", 7), ("key", _StrSubclass("x")), ("key", _Hostile()),
)


@pytest.mark.parametrize("action_index", range(len(INVALID_ACTIONS)))
def test_invalid_input_and_identity_are_constructor_zero(monkeypatch, action_index):
    # 잘못된 payload만 거부 근거가 되도록 나머지 admission 조건은 충족시킨다.
    case = "open_object" if action_index >= 23 else "pending_value" if action_index == 21 else "open_array" if action_index == 22 else "fresh"
    supervisor, operation, token, census = _assert_case(monkeypatch, case)
    counts = (census.reached, census.returned, census.generation)
    _assert_rejected(supervisor, operation, token, census, INVALID_ACTIONS[action_index])
    assert (census.reached, census.returned, census.generation) == counts


@pytest.mark.parametrize("mode", ("equal_token", "hostile_token", "wrong_operation", "missing_active", "foreign_active"))
@pytest.mark.parametrize("is_tick", (False, True))
def test_identity_checks_original_and_foreign_state(monkeypatch, mode, is_tick):
    supervisor, operation, token, census = _assert_case(monkeypatch, "pending_value")
    foreign = shell.Operation(token)
    if is_tick:
        subject.submit(supervisor, operation, token, ("publish", None))
        subject.submit(shell.Supervisor(foreign), foreign, token, ("scalar", 7))
    supplied_operation = foreign if mode == "wrong_operation" else operation
    supplied_token = tuple([1, 2, 3, 4]) if mode == "equal_token" else _Hostile() if mode == "hostile_token" else token
    if mode == "equal_token":
        assert supplied_token == token and supplied_token is not token
    if mode == "missing_active":
        supervisor.active = None
    elif mode == "foreign_active":
        supervisor.active = foreign
    original_before = observer.snapshot(shell.Supervisor(operation), census)
    foreign_before = observer.snapshot(shell.Supervisor(foreign), census)
    action = ("scalar", 7) if mode == "wrong_operation" else ("publish", None)
    _assert_rejected(supervisor, operation, token, census, action, tick=is_tick,
                     supplied_operation=supplied_operation, supplied_token=supplied_token)
    assert observer.snapshot(shell.Supervisor(operation), census) == original_before
    assert observer.snapshot(shell.Supervisor(foreign), census) == foreign_before


@pytest.mark.parametrize("token_index", range(11))
def test_invalid_bootstrap_is_constructor_zero(monkeypatch, token_index):
    census = observer.install_census(monkeypatch, subject)
    invalid = (None, [1, 2, 3, 4], (1, 2, 3), (1, 2, 3, 4, 5),
               (True, 2, 3, 4), (1, 2, 3, 5), (1, 2, 3, _Hostile()), _TupleSubclass((1, 2, 3, 4)),
               (1.0, 2, 3, 4), (1, 2, 3, _IntSubclass(4)), _Hostile())
    with pytest.raises(ValueError):
        subject.make_supervisor(invalid[token_index])
    assert census.reached == census.returned == census.generation == 0


class _ForeignFault(Exception):
    def __str__(self):
        raise AssertionError("FOREIGN_FAULT_FORMATTED")

    def __repr__(self):
        raise AssertionError("FOREIGN_FAULT_FORMATTED")


@pytest.mark.parametrize("case,row_index,side", fixtures.fault_boundaries())
def test_fault_before_and_after_each_reachable_row(monkeypatch, case, row_index, side):
    supervisor, operation, token, census = _bootstrap(monkeypatch)
    _prefix(supervisor, operation, token, census, case, row_index - 1)
    fault = _ForeignFault("FOREIGN_FAULT")
    original = subject._step

    def fail_at_boundary(current):
        if side == "before":
            raise fault
        original(current)
        raise fault

    monkeypatch.setattr(subject, "_step", fail_at_boundary)
    subject.tick(supervisor, operation, token)
    expected_index = row_index - 1 if side == "before" else row_index
    expected = _bound(fixtures.expected_states(case)[expected_index], operation, token, fault)
    assert operation.fault is fault
    assert observer.check_state(observer.snapshot(supervisor, census), expected) == ()
    _assert_rejected(supervisor, operation, token, census, ("scalar", 1))
    _assert_rejected(supervisor, operation, token, census, tick=True)


@pytest.mark.parametrize("site", ("census_record", "snapshot"))
def test_observer_abort_is_inconclusive(monkeypatch, site):
    supervisor, operation, token, census = _bootstrap(monkeypatch)

    def fail(*args):
        raise RuntimeError("OBSERVER_FAILURE")

    if site == "census_record":
        monkeypatch.setattr(census, "record", fail)
        subject.submit(supervisor, operation, token, ("scalar", 7))
        with pytest.raises(observer.ObserverAbort, match="INCONCLUSIVE_OBSERVER"):
            subject.tick(supervisor, operation, token)
        assert census.reached == census.returned == 1
    else:
        monkeypatch.setattr(observer, "_snapshot", fail)
        with pytest.raises(observer.ObserverAbort, match="INCONCLUSIVE_OBSERVER"):
            observer.snapshot(supervisor, census)
    assert operation.fault is None
    assert operation.outcome is None


def test_uncensused_reference_is_not_qualified(monkeypatch):
    supervisor, operation, token, census = _bootstrap(monkeypatch)
    operation.arena.root = shell.Cell()
    actual = observer.snapshot(supervisor, census)
    expected = _bound(fixtures.expected_states("fresh")[0], operation, token)
    assert observer.check_state(actual, expected) == ("INCONCLUSIVE_COVERAGE",)
    assert "INCONCLUSIVE_COVERAGE" in actual.issues


@pytest.mark.parametrize("corruption", ("semantic_cycle", "allocation_cycle", "undeclared_alias", "tail_index"))
def test_observer_rejects_cycles_and_undeclared_aliases(monkeypatch, corruption):
    supervisor, operation, token, census = _assert_case(monkeypatch, "array_three")
    if corruption == "semantic_cycle":
        operation.arena.root.s0.s1 = operation.arena.root.s0
    elif corruption == "allocation_cycle":
        operation.arena.tail.next = operation.arena.head
    elif corruption == "undeclared_alias":
        operation.arena.root.s0.s1.s0 = operation.arena.root.s0.s0
    else:
        operation.arena.root.s1 = operation.arena.root.s0
    actual = observer.snapshot(supervisor, census)
    expected = _bound(fixtures.expected_states("array_three")[-1], operation, token)
    violation = observer.check_state(actual, expected)
    assert violation and violation[0].startswith("STRUCTURE_"), violation


def test_snapshot_preserves_bool_int_float_distinctions(monkeypatch):
    supervisor, operation, token, census = _assert_case(monkeypatch, "scalar_true")
    expected = _bound(fixtures.expected_states("scalar_true")[-1], operation, token)
    operation.arena.root.payload = 1
    assert observer.check_state(observer.snapshot(supervisor, census), expected) == ("STRUCTURE_SLOTS",)
    operation.arena.root.payload = 1.0
    assert observer.check_state(observer.snapshot(supervisor, census), expected) == ("STRUCTURE_SLOTS",)


def test_snapshot_rejects_reference_subclasses(monkeypatch):
    supervisor, operation, token, census = _bootstrap(monkeypatch)

    class CellSubclass(shell.Cell):
        pass

    operation.arena.root = CellSubclass()
    actual = observer.snapshot(supervisor, census)
    expected = _bound(fixtures.expected_states("fresh")[0], operation, token)
    assert observer.check_state(actual, expected) == ("STRUCTURE_REFERENCE_TYPE",)


def test_cont_live_after_frames(monkeypatch):
    with monkeypatch.context() as normal:
        supervisor, operation, token, census = _assert_case(normal, "empty_object")
        assert observer.retained_live(census) == (1,)
    external_holder = []
    supervisor, operation, token, census = _bootstrap(monkeypatch)
    original = subject._step

    def retain_cont(current):
        if current.phase == "D1":
            external_holder.append(current.holders.cont_head)
        original(current)

    monkeypatch.setattr(subject, "_step", retain_cont)
    violations = fixtures.drive(supervisor, operation, token, "empty_object", census)
    assert violations == ("STRUCTURE_SLOTS",)
    assert observer.retained_live(census) == (1, 2)
    assert operation.arena.root is not None
    external_holder.clear()
    assert observer.retained_live(census) == (1,)


def test_snapshot_cannot_prove_single_physical_write(monkeypatch):
    original = subject._step
    writes = []

    def double_write_restore(current):
        if current.phase == "V5":
            current.holders.new.payload = 99
            current.holders.new.payload = None
            writes.extend(("temporary", "restore"))
        original(current)

    monkeypatch.setattr(subject, "_step", double_write_restore)
    _assert_case(monkeypatch, "scalar7")
    assert writes == ["temporary", "restore"]


@pytest.mark.parametrize("name,case", fixtures.RAW_CASES)
def test_actual_mutants_rejected(monkeypatch, name, case):
    with monkeypatch.context() as normal:
        _assert_case(normal, case)
    external_holder = []
    mutants.install_mutant(monkeypatch, subject, name, external_holder)
    supervisor, operation, token, census = _bootstrap(monkeypatch)
    violations = fixtures.drive(supervisor, operation, token, case, census)
    assert violations and violations[0].startswith("STRUCTURE_"), violations


def test_raw_mutant_probe(monkeypatch):
    name = os.environ.get("QWQ_B1B_RAW_MUTANT", "none")
    mapping = dict(fixtures.RAW_CASES)
    if name != "none" and name not in mapping:
        raise RuntimeError("UNKNOWN_RAW_MUTANT")
    external_holder = []
    if name != "none":
        mutants.install_mutant(monkeypatch, subject, name, external_holder)
    case = "nested" if name == "none" else mapping[name]
    _assert_case(monkeypatch, case)
