"""독립 weakref census와 실제 슬롯의 scalar-only 관측 기록."""

from dataclasses import dataclass, fields
import struct
import weakref

from _qwq_b1b_shell import Arena, Cell, Holders, Operation, Supervisor


class ObserverAbort(BaseException):
    """관측 실패는 subject의 Exception 래치와 분리한다."""


@dataclass(frozen=True)
class ExpectedState:
    slots: tuple
    phase: str
    action: tuple | None
    outcome: str | None
    fault_id: int | None
    operation_id: int | None
    token_id: int | None
    reached: int
    returned: int
    generation: int
    live: tuple
    forward: tuple
    backward: tuple
    semantic: tuple
    continuations: tuple
    completed: tuple
    pending_keys: tuple
    construction: tuple
    unlinking: tuple
    issues: tuple


@dataclass(frozen=True)
class Snapshot(ExpectedState):
    """Cell/frame/exception 객체를 저장하지 않는 실제 관측값."""


class Census:
    def __init__(self):
        self.reached = 0
        self.returned = 0
        self.generation = 0
        self.refs = {}

    def record(self, cell):
        if type(cell) is not Cell:
            raise ObserverAbort("INCONCLUSIVE_COVERAGE")
        self.generation += 1
        self.refs[self.generation] = weakref.ref(cell)

    def identify(self, cell):
        if cell is None:
            return None
        for generation, reference in self.refs.items():
            if reference() is cell:
                return generation
        return "UNCENSUSED"


def install_census(monkeypatch, subject_module):
    if getattr(subject_module._new_cell, "_b1b_census_wrapper", False):
        raise ObserverAbort("INCONCLUSIVE_DOUBLE_CENSUS")
    census = Census()
    constructor = subject_module._new_cell

    def observed_new_cell():
        census.reached += 1
        cell = constructor()
        census.returned += 1
        try:
            census.record(cell)
        except Exception:
            raise ObserverAbort("INCONCLUSIVE_OBSERVER") from None
        return cell

    observed_new_cell._b1b_census_wrapper = True
    monkeypatch.setattr(subject_module, "_new_cell", observed_new_cell)
    return census


def retained_live(census):
    return tuple(g for g, reference in census.refs.items() if reference() is not None)


def _atom(value):
    if value is None:
        return ("NULL", None)
    if type(value) is bool:
        return ("BOOL", value)
    if type(value) is int:
        return ("INT", value)
    if type(value) is float:
        return ("FLOAT", struct.pack("!d", value).hex())
    if type(value) is str:
        return ("STR", value)
    raise ObserverAbort("INCONCLUSIVE_OBSERVER")


def _walk(slots, start, edge, issues):
    result = []
    while start is not None:
        if type(start) is not int or (start, "tag") not in slots:
            issues.append("INCONCLUSIVE_COVERAGE")
            break
        if start in result:
            issues.append("STRUCTURE_CYCLE")
            break
        result.append(start)
        start = slots[(start, edge)]
    return tuple(result)


def _snapshot(supervisor, census):
    if type(supervisor) is not Supervisor or type(supervisor.active) is not Operation:
        raise ObserverAbort("INCONCLUSIVE_SHELL_TYPE")
    operation = supervisor.active
    if type(operation.arena) is not Arena or type(operation.holders) is not Holders:
        raise ObserverAbort("INCONCLUSIVE_SHELL_TYPE")
    issues = []
    slots = {}

    def reference(value):
        if value is not None and type(value) is not Cell:
            issues.append("STRUCTURE_REFERENCE_TYPE")
            return "INVALID_REFERENCE"
        generation = census.identify(value)
        if generation == "UNCENSUSED":
            issues.append("INCONCLUSIVE_COVERAGE")
        return generation

    for name in ("head", "root", "tail"):
        slots[("arena", name)] = reference(getattr(operation.arena, name))
    for name in (
        "cont_head", "cursor", "left", "lookup", "new", "pending_key",
        "replacement", "right", "unlink", "work_head",
    ):
        slots[("holders", name)] = reference(getattr(operation.holders, name))
    for generation, weak in census.refs.items():
        cell = weak()
        if cell is None:
            continue
        if type(cell) is not Cell:
            raise ObserverAbort("INCONCLUSIVE_COVERAGE")
        if type(cell.tag) is not str:
            issues.append("STRUCTURE_TAG_TYPE")
            slots[(generation, "tag")] = "INVALID_TAG"
        else:
            slots[(generation, "tag")] = cell.tag
        slots[(generation, "payload")] = _atom(cell.payload)
        for name in ("prev", "next", "s0", "s1", "s2", "c0", "c1", "c2", "c3"):
            slots[(generation, name)] = reference(getattr(cell, name))
    # Cell 지역 참조는 이 함수 반환 뒤 외부 trace에 남지 않는다.
    forward = _walk(slots, slots[("arena", "head")], "next", issues)
    backward = _walk(slots, slots[("arena", "tail")], "prev", issues)
    continuations = _walk(slots, slots[("holders", "cont_head")], "c3", issues)
    semantic = []

    def visit(generation, ancestors=()):
        if generation is None:
            return
        if generation in ancestors:
            issues.append("STRUCTURE_SEMANTIC_CYCLE")
            return
        if generation in semantic:
            # 게시/완료 인계의 중복 경로 허용은 check_state의 전체 슬롯 literal이 판정한다.
            return
        if type(generation) is not int or (generation, "tag") not in slots:
            issues.append("INCONCLUSIVE_COVERAGE")
            return
        semantic.append(generation)
        tag = slots[(generation, "tag")]
        edges = {"OBJECT": ("s0",), "ARRAY": ("s0",),
                 "ENTRY": ("s0", "s1", "s2"), "ITEM": ("s0", "s1")}.get(tag, ())
        for edge in edges:
            visit(slots[(generation, edge)], ancestors + (generation,))

    visit(slots[("arena", "root")])
    for generation in continuations:
        for edge in ("c0", "c1", "c2"):
            visit(slots[(generation, edge)])
    completed = tuple(slots[(g, "c1")] for g in continuations if slots[(g, "c1")] is not None)
    pending_keys = tuple(slots[(g, "c2")] for g in continuations if slots[(g, "c2")] is not None)
    construction = []
    new = slots[("holders", "new")]
    if new is not None:
        construction.append(new)
    cursor = slots[("holders", "cursor")]
    if cursor is not None and slots.get((cursor, "tag")) in ("OBJECT", "ARRAY"):
        if cursor not in construction:
            construction.append(cursor)
    unlinking = () if cursor is None or cursor in construction else (cursor,)

    def ordering(item):
        owner, name = item[0]
        return (0 if owner == "arena" else 1 if owner == "holders" else 2,
                owner if type(owner) is int else 0, name)

    action = operation.action
    if type(operation.phase) is not str or (operation.outcome is not None and type(operation.outcome) is not str):
        raise ObserverAbort("INCONCLUSIVE_CONTROL_TYPE")
    if action is not None and (type(action) is not tuple or len(action) != 2 or type(action[0]) is not str):
        raise ObserverAbort("INCONCLUSIVE_ACTION_TYPE")
    return Snapshot(
        slots=tuple(sorted(slots.items(), key=ordering)), phase=operation.phase,
        action=None if action is None else (_atom(action[0]), _atom(action[1])),
        outcome=operation.outcome, fault_id=None if operation.fault is None else id(operation.fault),
        operation_id=id(operation), token_id=id(operation.token), reached=census.reached,
        returned=census.returned, generation=census.generation, live=retained_live(census),
        forward=forward, backward=backward, semantic=tuple(semantic), continuations=continuations,
        completed=completed, pending_keys=pending_keys, construction=tuple(construction),
        unlinking=unlinking, issues=tuple(issues),
    )


def snapshot(supervisor, census):
    try:
        return _snapshot(supervisor, census)
    except Exception:
        raise ObserverAbort("INCONCLUSIVE_OBSERVER") from None


def check_state(actual, expected):
    if actual.issues:
        return (actual.issues[0],)
    for field in fields(ExpectedState):
        # 전체 슬롯 비교가 각 행의 선언 alias, tail index 및 부분 초기화까지 고정한다.
        if getattr(actual, field.name) != getattr(expected, field.name):
            return ("STRUCTURE_" + field.name.upper(),)
    return ()


def check_transition(before, after, expected_before, expected_after):
    return check_state(before, expected_before) or check_state(after, expected_after)
