"""Independent weak census and scalar actual-slot oracle for B1a only."""

from dataclasses import dataclass
import weakref

import _qwq_b1_subject as subject


HOLDER_SLOTS = (
    "new", "replacement", "cursor", "unlink", "left", "right",
    "work_head", "cont_head", "lookup", "pending_key",
)
CELL_REFS = ("payload", "prev", "next", "s0", "s1", "s2", "c0", "c1", "c2", "c3")


class ObserverAbort(BaseException):
    """Observer failure is INCONCLUSIVE_OBSERVER, never a kernel fault."""


class Census:
    __slots__ = ("refs", "reached", "returned", "generation")

    def __init__(self):
        self.refs = {}
        self.reached = 0
        self.returned = 0
        self.generation = 0

    def record(self, cell):
        self.generation += 1
        self.refs[self.generation] = weakref.ref(cell)


def install_census(monkeypatch, subject_module):
    census = Census()
    constructor = subject_module._new_cell

    def observed_constructor():
        census.reached += 1
        cell = constructor()
        census.returned += 1
        try:
            census.record(cell)
        except Exception:
            raise ObserverAbort("INCONCLUSIVE_OBSERVER") from None
        return cell

    monkeypatch.setattr(subject_module, "_new_cell", observed_constructor)
    return census


def _reference(value, census):
    if value is None:
        return None
    # Identity comparison against each live weakref survives object-id reuse.
    for generation, reference in census.refs.items():
        if reference() is value:
            return generation
    return ("UNCENSUSED", id(value))


def _scalar(value):
    if value is None or type(value) in (str, int, bool):
        return value
    return ("NONSCALAR", id(value))


@dataclass(frozen=True)
class Snapshot:
    slots: tuple
    phase: object
    action: object
    outcome: object
    fault_id: object
    operation_id: int
    token_id: int
    reached: int
    returned: int
    generation: int
    live: tuple
    forward: tuple
    backward: tuple
    issues: tuple


def _walk(start, rows, direction):
    seen = []
    while start is not None:
        if type(start) is not int or start in seen or start not in rows:
            return tuple(seen), ("STRUCTURE_CHAIN_WALK",)
        seen.append(start)
        start = rows[start][direction]
    return tuple(seen), ()


def snapshot(supervisor, census):
    """Return scalars only. All weakref dereference locals end at return."""
    try:
        op = supervisor.active
        slots = {}
        issues = []
        for name in ("head", "tail", "root"):
            slots[("arena", name)] = _reference(getattr(op.arena, name), census)
        for name in HOLDER_SLOTS:
            slots[("holders", name)] = _reference(getattr(op.holders, name), census)
        rows = {}
        for generation, reference in census.refs.items():
            cell = reference()
            if cell is None:
                continue
            if type(cell) is not subject.Cell:
                issues.append("STRUCTURE_CELL_TYPE")
            slots[(generation, "tag")] = _scalar(cell.tag)
            rows[generation] = {}
            for name in CELL_REFS:
                value = _reference(getattr(cell, name), census)
                slots[(generation, name)] = value
                rows[generation][name] = value
                if name not in ("prev", "next") and value is not None:
                    issues.append("STRUCTURE_NONEMPTY_CELL")
            if slots[(generation, "tag")] != "EMPTY":
                issues.append("STRUCTURE_NONEMPTY_CELL")
        if any(type(value) is tuple and value[0] == "UNCENSUSED" for value in slots.values()):
            issues.append("STRUCTURE_CENSUS_MISSING")
        if slots[("arena", "root")] is not None or any(
            slots[("holders", name)] is not None
            for name in ("replacement", "work_head", "cont_head", "lookup", "pending_key")
        ):
            issues.append("STRUCTURE_UNUSED_SLOT")
        forward, forward_issues = _walk(slots[("arena", "head")], rows, "next")
        backward, backward_issues = _walk(slots[("arena", "tail")], rows, "prev")
        issues.extend(forward_issues + backward_issues)
        action = op.action
        if type(action) is tuple:
            action = tuple(_scalar(value) for value in action)
        else:
            action = _scalar(action)
        return Snapshot(
            tuple(slots.items()), _scalar(op.phase), action, _scalar(op.outcome),
            None if op.fault is None else id(op.fault), id(op), id(op.token),
            census.reached, census.returned, census.generation, tuple(rows),
            forward, backward, tuple(issues),
        )
    except Exception:
        raise ObserverAbort("INCONCLUSIVE_OBSERVER") from None


def check_transition(before, after):
    """Read the independent literal row, never a subject count/completion helper."""
    from _qwq_b1_fixtures import expected_transition

    if before.issues or after.issues:
        return (before.issues + after.issues)[:1]
    expected = expected_transition(before)
    if after.slots != expected.slots:
        # Slot insertion order is observational only, not a graph property.
        if dict(after.slots) != dict(expected.slots):
            return ("STRUCTURE_SLOT_TRANSITION",)
    for field in (
        "phase", "action", "outcome", "fault_id", "operation_id", "token_id",
        "reached", "returned", "generation", "live",
    ):
        if getattr(after, field) != getattr(expected, field):
            return ("STRUCTURE_" + field.upper(),)
    return ()


def terminal_live(census):
    """Call only after driver/wrapper/snapshot frames have returned; no GC."""
    return tuple(generation for generation, reference in census.refs.items() if reference() is not None)


def coverage_verdict(census, *, bypass_witnessed):
    """A caller's actual bypass witness defeats census completeness, even at 0."""
    if bypass_witnessed:
        return "INCONCLUSIVE_COVERAGE"
    # Absence of a witness is not evidence that no bypass exists.
    return "CENSUS_ONLY_NOT_ALLOCATION_QUALIFIED"


def terminal_issues(state):
    slots = dict(state.slots)
    if state.issues:
        return state.issues[:1]
    if state.phase != "IDLE" or state.action is not None or state.fault_id is not None or state.outcome is not None:
        return ("STRUCTURE_TERMINAL_STATE",)
    if any(slots[("holders", name)] is not None for name in HOLDER_SLOTS):
        return ("STRUCTURE_TERMINAL_HOLDER",)
    if state.forward != tuple(reversed(state.backward)) or state.forward != state.live:
        return ("STRUCTURE_CHAIN_MEMBERSHIP",)
    return ()
