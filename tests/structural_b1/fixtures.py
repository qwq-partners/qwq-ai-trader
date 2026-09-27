"""Expectations transcribed independently from the pinned spec section 3."""

from dataclasses import replace
import _qwq_b1_subject as subject
from _qwq_b1_observer import check_transition, snapshot, terminal_issues


TOKEN = (1, 2, 3, 4)
APPEND = ("append_empty", None)
HEAD = ("unlink_head", None)
TAIL = ("unlink_tail", None)
MIDDLE = ("unlink_middle", None)

# Each row is next phase, destination owner/slot, value expression.
# These literals are the specification, not derived from subject source.
ROWS = {
    "A0": ("A1", "holders", "new", "ALLOCATE"),
    "A1": ("A2", "n", "prev", "t"),
    "A2": ("A3", "APPEND_LINK", "next", "n"),
    "A3": ("A4", "arena", "tail", "n"),
    "A4": ("A5", "holders", "cursor", "n"),
    "A5": ("A6", "holders", "new", None),
    "A6": ("IDLE", "holders", "cursor", None),
    "U0": ("U1", "holders", "cursor", "SELECT"),
    "U1": ("U2", "holders", "unlink", "x"),
    "U2": ("U3", "holders", "left", "PREV"),
    "U3": ("U4", "holders", "right", "NEXT"),
    "U4": ("U5", "LEFT_LINK", "next", "R"),
    "U5": ("U6", "RIGHT_LINK", "prev", "L"),
    "U6": ("U7", "x", "prev", None),
    "U7": ("U8", "x", "next", None),
    "U8": ("U9", "holders", "left", None),
    "U9": ("U10", "holders", "right", None),
    "U10": ("U11", "holders", "unlink", None),
    "U11": ("IDLE", "holders", "cursor", None),
}

# Hand-written states after each tick: head, tail, new, cursor, unlink,
# left, right, and (generation, prev, next) for EVERY census-live cell.
# None-to-None rows intentionally repeat; snapshots cannot count writes.
APPEND_STATES = (
    (
        (None, None, 1, None, None, None, None, ((1, None, None),)),
        (None, None, 1, None, None, None, None, ((1, None, None),)),
        (1, None, 1, None, None, None, None, ((1, None, None),)),
        (1, 1, 1, None, None, None, None, ((1, None, None),)),
        (1, 1, 1, 1, None, None, None, ((1, None, None),)),
        (1, 1, None, 1, None, None, None, ((1, None, None),)),
        (1, 1, None, None, None, None, None, ((1, None, None),)),
    ),
    (
        (1, 1, 2, None, None, None, None, ((1, None, None), (2, None, None))),
        (1, 1, 2, None, None, None, None, ((1, None, None), (2, 1, None))),
        (1, 1, 2, None, None, None, None, ((1, None, 2), (2, 1, None))),
        (1, 2, 2, None, None, None, None, ((1, None, 2), (2, 1, None))),
        (1, 2, 2, 2, None, None, None, ((1, None, 2), (2, 1, None))),
        (1, 2, None, 2, None, None, None, ((1, None, 2), (2, 1, None))),
        (1, 2, None, None, None, None, None, ((1, None, 2), (2, 1, None))),
    ),
    (
        (1, 2, 3, None, None, None, None, ((1, None, 2), (2, 1, None), (3, None, None))),
        (1, 2, 3, None, None, None, None, ((1, None, 2), (2, 1, None), (3, 2, None))),
        (1, 2, 3, None, None, None, None, ((1, None, 2), (2, 1, 3), (3, 2, None))),
        (1, 3, 3, None, None, None, None, ((1, None, 2), (2, 1, 3), (3, 2, None))),
        (1, 3, 3, 3, None, None, None, ((1, None, 2), (2, 1, 3), (3, 2, None))),
        (1, 3, None, 3, None, None, None, ((1, None, 2), (2, 1, 3), (3, 2, None))),
        (1, 3, None, None, None, None, None, ((1, None, 2), (2, 1, 3), (3, 2, None))),
    ),
)
UNLINK_STATES = {
    "single": (
        (1, 1, None, 1, None, None, None, ((1, None, None),)),
        (1, 1, None, 1, 1, None, None, ((1, None, None),)),
        (1, 1, None, 1, 1, None, None, ((1, None, None),)),
        (1, 1, None, 1, 1, None, None, ((1, None, None),)),
        (None, 1, None, 1, 1, None, None, ((1, None, None),)),
        (None, None, None, 1, 1, None, None, ((1, None, None),)),
        (None, None, None, 1, 1, None, None, ((1, None, None),)),
        (None, None, None, 1, 1, None, None, ((1, None, None),)),
        (None, None, None, 1, 1, None, None, ((1, None, None),)),
        (None, None, None, 1, 1, None, None, ((1, None, None),)),
        (None, None, None, 1, None, None, None, ((1, None, None),)),
        (None, None, None, None, None, None, None, ()),
    ),
    "head": (
        (1, 3, None, 1, None, None, None, ((1, None, 2), (2, 1, 3), (3, 2, None))),
        (1, 3, None, 1, 1, None, None, ((1, None, 2), (2, 1, 3), (3, 2, None))),
        (1, 3, None, 1, 1, None, None, ((1, None, 2), (2, 1, 3), (3, 2, None))),
        (1, 3, None, 1, 1, None, 2, ((1, None, 2), (2, 1, 3), (3, 2, None))),
        (2, 3, None, 1, 1, None, 2, ((1, None, 2), (2, 1, 3), (3, 2, None))),
        (2, 3, None, 1, 1, None, 2, ((1, None, 2), (2, None, 3), (3, 2, None))),
        (2, 3, None, 1, 1, None, 2, ((1, None, 2), (2, None, 3), (3, 2, None))),
        (2, 3, None, 1, 1, None, 2, ((1, None, None), (2, None, 3), (3, 2, None))),
        (2, 3, None, 1, 1, None, 2, ((1, None, None), (2, None, 3), (3, 2, None))),
        (2, 3, None, 1, 1, None, None, ((1, None, None), (2, None, 3), (3, 2, None))),
        (2, 3, None, 1, None, None, None, ((1, None, None), (2, None, 3), (3, 2, None))),
        (2, 3, None, None, None, None, None, ((2, None, 3), (3, 2, None))),
    ),
    "tail": (
        (1, 3, None, 3, None, None, None, ((1, None, 2), (2, 1, 3), (3, 2, None))),
        (1, 3, None, 3, 3, None, None, ((1, None, 2), (2, 1, 3), (3, 2, None))),
        (1, 3, None, 3, 3, 2, None, ((1, None, 2), (2, 1, 3), (3, 2, None))),
        (1, 3, None, 3, 3, 2, None, ((1, None, 2), (2, 1, 3), (3, 2, None))),
        (1, 3, None, 3, 3, 2, None, ((1, None, 2), (2, 1, None), (3, 2, None))),
        (1, 2, None, 3, 3, 2, None, ((1, None, 2), (2, 1, None), (3, 2, None))),
        (1, 2, None, 3, 3, 2, None, ((1, None, 2), (2, 1, None), (3, None, None))),
        (1, 2, None, 3, 3, 2, None, ((1, None, 2), (2, 1, None), (3, None, None))),
        (1, 2, None, 3, 3, None, None, ((1, None, 2), (2, 1, None), (3, None, None))),
        (1, 2, None, 3, 3, None, None, ((1, None, 2), (2, 1, None), (3, None, None))),
        (1, 2, None, 3, None, None, None, ((1, None, 2), (2, 1, None), (3, None, None))),
        (1, 2, None, None, None, None, None, ((1, None, 2), (2, 1, None))),
    ),
    "middle": (
        (1, 3, None, 2, None, None, None, ((1, None, 2), (2, 1, 3), (3, 2, None))),
        (1, 3, None, 2, 2, None, None, ((1, None, 2), (2, 1, 3), (3, 2, None))),
        (1, 3, None, 2, 2, 1, None, ((1, None, 2), (2, 1, 3), (3, 2, None))),
        (1, 3, None, 2, 2, 1, 3, ((1, None, 2), (2, 1, 3), (3, 2, None))),
        (1, 3, None, 2, 2, 1, 3, ((1, None, 3), (2, 1, 3), (3, 2, None))),
        (1, 3, None, 2, 2, 1, 3, ((1, None, 3), (2, 1, 3), (3, 1, None))),
        (1, 3, None, 2, 2, 1, 3, ((1, None, 3), (2, None, 3), (3, 1, None))),
        (1, 3, None, 2, 2, 1, 3, ((1, None, 3), (2, None, None), (3, 1, None))),
        (1, 3, None, 2, 2, None, 3, ((1, None, 3), (2, None, None), (3, 1, None))),
        (1, 3, None, 2, 2, None, None, ((1, None, 3), (2, None, None), (3, 1, None))),
        (1, 3, None, 2, None, None, None, ((1, None, 3), (2, None, None), (3, 1, None))),
        (1, 3, None, None, None, None, None, ((1, None, 3), (3, 1, None))),
    ),
}
APPEND_PHASES = ("A1", "A2", "A3", "A4", "A5", "A6", "IDLE")
UNLINK_PHASES = ("U1", "U2", "U3", "U4", "U5", "U6", "U7", "U8", "U9", "U10", "U11", "IDLE")


def compact(state):
    slots = dict(state.slots)
    return (
        slots[("arena", "head")], slots[("arena", "tail")],
        *(slots[("holders", name)] for name in ("new", "cursor", "unlink", "left", "right")),
        tuple((g, slots[(g, "prev")], slots[(g, "next")]) for g in state.live),
    )


def expected_transition(before):
    slots = dict(before.slots)
    phase, owner, slot, value = ROWS[before.phase]
    symbols = {
        "n": slots[("holders", "new")], "t": slots[("arena", "tail")],
        "x": slots[("holders", "cursor")], "L": slots[("holders", "left")],
        "R": slots[("holders", "right")],
    }
    live = before.live
    reached, returned, generation = before.reached, before.returned, before.generation
    if value == "ALLOCATE":
        generation += 1
        reached += 1
        returned += 1
        value = generation
        live += (generation,)
        slots[(generation, "tag")] = "EMPTY"
        for ref in ("payload", "prev", "next", "s0", "s1", "s2", "c0", "c1", "c2", "c3"):
            slots[(generation, ref)] = None
    elif value == "SELECT":
        value = {
            "unlink_head": slots[("arena", "head")],
            "unlink_tail": slots[("arena", "tail")],
            "unlink_middle": slots.get((slots[("arena", "head")], "next")),
        }[before.action[0]]
    elif value in ("PREV", "NEXT"):
        value = slots[(symbols["x"], value.lower())]
    elif value is not None:
        value = symbols[value]
    if owner == "APPEND_LINK":
        owner, slot = ("arena", "head") if symbols["t"] is None else (symbols["t"], "next")
    elif owner == "LEFT_LINK":
        owner, slot = ("arena", "head") if symbols["L"] is None else (symbols["L"], "next")
    elif owner == "RIGHT_LINK":
        owner, slot = ("arena", "tail") if symbols["R"] is None else (symbols["R"], "prev")
    elif owner in ("n", "x"):
        owner = symbols[owner]
    slots[(owner, slot)] = value
    if before.phase == "U11":
        dead = symbols["x"]
        live = tuple(g for g in live if g != dead)
        slots = {key: value for key, value in slots.items() if key[0] != dead}
    return replace(
        before, slots=tuple(slots.items()), phase=phase,
        action=None if phase == "IDLE" else before.action,
        reached=reached, returned=returned, generation=generation, live=live,
    )


def drive(supervisor, operation, token, actions, census):
    ticks = 0
    for action in actions:
        before = snapshot(supervisor, census)
        subject.submit(supervisor, operation, token, action)
        after = snapshot(supervisor, census)
        expected_phase = "A0" if action == APPEND else "U0"
        if after != replace(before, phase=expected_phase, action=action):
            return ("STRUCTURE_SUBMIT_MUTATION",)
        while operation.phase != "IDLE":
            if ticks >= 1000:
                return ("INCONCLUSIVE_TICK_LIMIT",)
            before = after
            subject.tick(supervisor, operation, token)
            ticks += 1
            after = snapshot(supervisor, census)
            violations = check_transition(before, after)
            if violations:
                return violations
        violations = terminal_issues(after)
        if violations:
            return violations
    return ()
