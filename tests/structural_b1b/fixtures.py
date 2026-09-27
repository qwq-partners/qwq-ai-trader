"""설계 V0–V7 표에서 독립 작성한 첫 scalar7 literal trace."""

from dataclasses import replace

import _qwq_b1b_subject as subject
from _qwq_b1b_observer import ExpectedState, check_state, check_transition, snapshot


_INITIAL_SLOTS = (
    (("arena", "head"), None), (("arena", "root"), None), (("arena", "tail"), None),
    (("holders", "cont_head"), None), (("holders", "cursor"), None),
    (("holders", "left"), None), (("holders", "lookup"), None),
    (("holders", "new"), None), (("holders", "pending_key"), None),
    (("holders", "replacement"), None), (("holders", "right"), None),
    (("holders", "unlink"), None), (("holders", "work_head"), None),
)
_CELL_ONE = (
    ((1, "c0"), None), ((1, "c1"), None), ((1, "c2"), None), ((1, "c3"), None),
    ((1, "next"), None), ((1, "payload"), ("NULL", None)), ((1, "prev"), None),
    ((1, "s0"), None), ((1, "s1"), None), ((1, "s2"), None), ((1, "tag"), "EMPTY"),
)


def _patch(state, changes=(), removed=(), **fields):
    # 실제 graph를 읽지 않고 앞선 expected의 명시 슬롯만 바꾼다.
    slots = dict(state.slots)
    slots.update(changes)
    slots = {key: value for key, value in slots.items() if key[0] not in removed}
    def order(item):
        owner, name = item[0]
        return (0 if owner == "arena" else 1 if owner == "holders" else 2,
                owner if type(owner) is int else 0, name)
    return replace(state, slots=tuple(sorted(slots.items(), key=order)), **fields)


def _first_scalar_states(case_name):
    if case_name != "scalar7":
        raise ValueError("UNKNOWN_LITERAL_CASE")
    initial = ExpectedState(
        slots=_INITIAL_SLOTS, phase="IDLE", action=None, outcome=None,
        fault_id=None, operation_id=None, token_id=None, reached=0, returned=0,
        generation=0, live=(), forward=(), backward=(), semantic=(),
        continuations=(), completed=(), pending_keys=(), construction=(), unlinking=(), issues=(),
    )
    submit = _patch(initial, phase="V0", action=(("STR", "scalar"), ("INT", 7)))
    v0 = _patch(submit, _CELL_ONE + ((("holders", "new"), 1),),
                phase="V1", reached=1, returned=1, generation=1, live=(1,), construction=(1,))
    v1 = _patch(v0, (((1, "prev"), None),), phase="V2")
    v2 = _patch(v1, ((("arena", "head"), 1),), phase="V3", forward=(1,))
    v3 = _patch(v2, ((("arena", "tail"), 1),), phase="V4", backward=(1,))
    v4 = _patch(v3, (((1, "tag"), "INT"),), phase="V5")
    v5 = _patch(v4, (((1, "payload"), ("INT", 7)),), phase="V6")
    v6 = _patch(v5, ((("arena", "root"), 1),), phase="V7", semantic=(1,))
    v7 = _patch(v6, ((("holders", "new"), None),), phase="IDLE", action=None, construction=())
    return initial, submit, v0, v1, v2, v3, v4, v5, v6, v7


SCALARS = (
    ("scalar_null", None, "NULL", ("NULL", None)),
    ("scalar_false", False, "BOOL", ("BOOL", False)),
    ("scalar_true", True, "BOOL", ("BOOL", True)),
    ("scalar_zero", 0, "INT", ("INT", 0)),
    ("scalar_one", 1, "INT", ("INT", 1)),
    ("scalar_negative", -7, "INT", ("INT", -7)),
    ("scalar_float", 1.0, "FLOAT", ("FLOAT", "3ff0000000000000")),
    ("scalar_negative_zero", -0.0, "FLOAT", ("FLOAT", "8000000000000000")),
    ("scalar_empty_string", "", "STR", ("STR", "")),
    ("scalar_spelling", "1.00", "STR", ("STR", "1.00")),
    ("scalar_korean", "한글\\n", "STR", ("STR", "한글\\n")),
    ("scalar_nan", float("nan"), "FLOAT", ("FLOAT", "7ff8000000000000")),
    ("scalar_infinity", float("inf"), "FLOAT", ("FLOAT", "7ff0000000000000")),
    ("scalar_negative_infinity", -float("inf"), "FLOAT", ("FLOAT", "fff0000000000000")),
    ("scalar7", 7, "INT", ("INT", 7)),
)


class _LiteralTrace:
    """고정 표의 expected 패치만 확장하며 실제 subject 상태는 읽지 않는다."""

    def __init__(self):
        self.states = [_first_scalar_states("scalar7")[0]]
        self.actions = []
        self.kinds = ["initial"]

    def row(self, phase, changes=(), **fields):
        if phase == "IDLE":
            fields["action"] = None
        self.states.append(_patch(self.states[-1], changes, phase=phase, **fields))
        self.kinds.append("tick")

    def submit(self, name, payload, atom, phase):
        self.actions.append((name, payload))
        self.states.append(_patch(self.states[-1], phase=phase, action=(("STR", name), atom)))
        self.kinds.append("submit")

    def allocate(self, prefix, generation, chain, cursor=None):
        # chain/generation/cursor는 아래 사례의 literal 인수이다.
        cells = tuple(((generation, slot), value) for (_, slot), value in _CELL_ONE)
        construction = (generation,) if cursor is None else (generation, cursor)
        self.row(prefix + "1", cells + ((("holders", "new"), generation),),
                 reached=generation, returned=generation, generation=generation,
                 live=self.states[-1].live + (generation,), construction=construction)
        tail = chain[-1] if chain else None
        self.row(prefix + "2", (((generation, "prev"), tail),))
        destination = (tail, "next") if chain else ("arena", "head")
        self.row(prefix + "3", ((destination, generation),), forward=chain + (generation,))
        self.row(prefix + "4", ((("arena", "tail"), generation),), backward=tuple(reversed(chain + (generation,))))

    def scalar(self, value, tag, atom, generation, chain, cont, semantic):
        self.submit("scalar", value, atom, "V0")
        self.allocate("V", generation, chain)
        self.row("V5", (((generation, "tag"), tag),))
        self.row("V6", (((generation, "payload"), atom),))
        destination = ("arena", "root") if cont is None else (cont, "c1")
        self.row("V7", ((destination, generation),), semantic=semantic,
                 completed=() if cont is None else (generation,))
        self.row("IDLE", ((("holders", "new"), None),), construction=())

    def start(self, tag, container, cont, chain, previous, stack, semantic):
        self.submit("start_object" if tag == "OBJECT" else "start_array", None, ("NULL", None), "O0")
        self.allocate("O", container, chain)
        self.row("O5", (((container, "tag"), tag),))
        self.row("O6", ((("holders", "cursor"), container),))
        self.row("C0", ((("holders", "new"), None),))
        self.allocate("C", cont, chain + (container,), cursor=container)
        self.row("C5", (((cont, "tag"), "CONT"),))
        self.row("C6", (((cont, "c0"), container),))
        self.row("C7", (((cont, "c3"), previous),))
        self.row("C8", ((("holders", "cont_head"), cont),), continuations=stack, semantic=semantic)
        self.row("C9", ((("holders", "cursor"), None),), construction=(cont,))
        self.row("IDLE", ((("holders", "new"), None),), construction=())

    def key(self, key, generation, chain, cont, entries, semantic, pending, hit=False):
        self.submit("key", key, ("STR", key), "L0")
        self.row("L1" if entries else "K0", ((("holders", "lookup"), entries[0] if entries else None),))
        for index, entry in enumerate(entries):
            last = index + 1 == len(entries)
            if last and hit:
                self.row("IDLE", ((("holders", "lookup"), None),), outcome="B1B_UNSUPPORTED_DUPLICATE")
                return
            self.row("K0" if last else "L1", ((("holders", "lookup"), None if last else entries[index + 1]),))
        self.allocate("K", generation, chain)
        self.row("K5", (((generation, "tag"), "KEY"),))
        self.row("K6", (((generation, "payload"), ("STR", key)),))
        self.row("K7", (((cont, "c2"), generation),), semantic=semantic, pending_keys=pending)
        self.row("IDLE", ((("holders", "new"), None),), construction=())

    def publish(self, generation, chain, cont, container, value, key, previous,
                semantic_published, semantic_done, pending_done):
        self.submit("publish", None, ("NULL", None), "P0")
        self.allocate("P", generation, chain)
        is_object = key is not None
        self.row("P5", (((generation, "tag"), "ENTRY" if is_object else "ITEM"),))
        self.row("P6" if is_object else "P7", (((generation, "s0"), key if is_object else value),))
        if is_object:
            self.row("P7", (((generation, "s1"), value),))
        destination = (container, "s0") if previous is None else (previous, "s2" if is_object else "s1")
        self.row("P8", ((destination, generation),), semantic=semantic_published)
        self.row("P9", (((container, "s1"), generation),))
        self.row("P10" if is_object else "P11", (((cont, "c1"), None),), completed=(), semantic=semantic_done)
        if is_object:
            self.row("P11", (((cont, "c2"), None),), pending_keys=pending_done)
        self.row("IDLE", ((("holders", "new"), None),), construction=())

    def end(self, container, cont, parent, chain, left, right, stack,
            semantic_destination, semantic_popped, pending, survivors):
        self.submit("end", None, ("NULL", None), "D0")
        destination = ("arena", "root") if parent is None else (parent, "c1")
        self.row("D1", ((destination, container),), semantic=semantic_destination,
                 completed=() if parent is None else (container,))
        self.row("D2", ((("holders", "cursor"), cont),), unlinking=(cont,))
        self.row("D3", (((cont, "c0"), None),), semantic=semantic_popped)
        self.row("D4", ((("holders", "cont_head"), parent),), continuations=stack, pending_keys=pending)
        self.row("D5", (((cont, "c3"), None),))
        self.row("U1", (((cont, "tag"), "EMPTY"),))
        self.row("U2", ((("holders", "unlink"), cont),))
        self.row("U3", ((("holders", "left"), left),))
        self.row("U4", ((("holders", "right"), right),))
        self.row("U5", (((left, "next"), right),), forward=survivors)
        destination = ("arena", "tail") if right is None else (right, "prev")
        self.row("U6", ((destination, left),), backward=tuple(reversed(survivors)))
        self.row("U7", (((cont, "prev"), None),))
        self.row("U8", (((cont, "next"), None),))
        self.row("U9", ((("holders", "left"), None),))
        self.row("U10", ((("holders", "right"), None),))
        self.row("U11", ((("holders", "unlink"), None),))
        self.row("IDLE", ((("holders", "cursor"), None),), removed=(cont,), live=survivors, unlinking=())


def _object_three(trace):
    trace.start("OBJECT", 1, 2, (), None, (2,), (1,))
    trace.key("b", 3, (1, 2), 2, (), (1, 3), (3,))
    trace.scalar(True, "BOOL", ("BOOL", True), 4, (1, 2, 3), 2, (1, 4, 3))
    trace.publish(5, (1, 2, 3, 4), 2, 1, 4, 3, None, (1, 5, 3, 4), (1, 5, 3, 4), ())
    trace.key("a", 6, (1, 2, 3, 4, 5), 2, (5,), (1, 5, 3, 4, 6), (6,))
    trace.scalar(7, "INT", ("INT", 7), 7, (1, 2, 3, 4, 5, 6), 2, (1, 5, 3, 4, 7, 6))
    trace.publish(8, (1, 2, 3, 4, 5, 6, 7), 2, 1, 7, 6, 5,
                  (1, 5, 3, 4, 8, 6, 7), (1, 5, 3, 4, 8, 6, 7), ())
    trace.key("c", 9, (1, 2, 3, 4, 5, 6, 7, 8), 2, (5, 8), (1, 5, 3, 4, 8, 6, 7, 9), (9,))
    trace.scalar("1.00", "STR", ("STR", "1.00"), 10, (1, 2, 3, 4, 5, 6, 7, 8, 9), 2,
                 (1, 5, 3, 4, 8, 6, 7, 10, 9))
    trace.publish(11, (1, 2, 3, 4, 5, 6, 7, 8, 9, 10), 2, 1, 10, 9, 8,
                  (1, 5, 3, 4, 8, 6, 7, 11, 9, 10), (1, 5, 3, 4, 8, 6, 7, 11, 9, 10), ())


def literal_trace(case_name):
    trace = _LiteralTrace()
    for name, value, tag, atom in SCALARS:
        if case_name == name:
            trace.scalar(value, tag, atom, 1, (), None, (1,))
            return trace
    if case_name in ("empty_object", "empty_array", "open_object", "open_array", "pending_key", "pending_value"):
        tag = "ARRAY" if case_name in ("empty_array", "open_array") else "OBJECT"
        trace.start(tag, 1, 2, (), None, (2,), (1,))
        if case_name in ("pending_key", "pending_value"):
            trace.key("a", 3, (1, 2), 2, (), (1, 3), (3,))
            if case_name == "pending_value":
                trace.scalar(7, "INT", ("INT", 7), 4, (1, 2, 3), 2, (1, 4, 3))
        if case_name.startswith("empty_"):
            trace.end(1, 2, None, (1, 2), 1, None, (), (1,), (1,), (), (1,))
        return trace
    if case_name == "array_three":
        trace.start("ARRAY", 1, 2, (), None, (2,), (1,))
        trace.scalar(True, "BOOL", ("BOOL", True), 3, (1, 2), 2, (1, 3))
        trace.publish(4, (1, 2, 3), 2, 1, 3, None, None, (1, 4, 3), (1, 4, 3), ())
        trace.scalar(True, "BOOL", ("BOOL", True), 5, (1, 2, 3, 4), 2, (1, 4, 3, 5))
        trace.publish(6, (1, 2, 3, 4, 5), 2, 1, 5, None, 4, (1, 4, 3, 6, 5), (1, 4, 3, 6, 5), ())
        trace.scalar(-0.0, "FLOAT", ("FLOAT", "8000000000000000"), 7, (1, 2, 3, 4, 5, 6), 2, (1, 4, 3, 6, 5, 7))
        trace.publish(8, (1, 2, 3, 4, 5, 6, 7), 2, 1, 7, None, 6,
                      (1, 4, 3, 6, 5, 8, 7), (1, 4, 3, 6, 5, 8, 7), ())
        trace.end(1, 2, None, (1, 2, 3, 4, 5, 6, 7, 8), 1, 3, (),
                  (1, 4, 3, 6, 5, 8, 7), (1, 4, 3, 6, 5, 8, 7), (), (1, 3, 4, 5, 6, 7, 8))
        return trace
    if case_name in ("object_three", "open_object_three", "duplicate_first", "duplicate_middle", "duplicate_last"):
        _object_three(trace)
        if case_name.startswith("duplicate_"):
            key, entries = {"duplicate_first": ("b", (5,)), "duplicate_middle": ("a", (5, 8)),
                            "duplicate_last": ("c", (5, 8, 11))}[case_name]
            trace.key(key, None, (), 2, entries, (), (), hit=True)
        elif case_name == "object_three":
            trace.end(1, 2, None, (1, 2, 3, 4, 5, 6, 7, 8, 9, 10, 11), 1, 3, (),
                      (1, 5, 3, 4, 8, 6, 7, 11, 9, 10), (1, 5, 3, 4, 8, 6, 7, 11, 9, 10), (),
                      (1, 3, 4, 5, 6, 7, 8, 9, 10, 11))
        return trace
    if case_name == "nested":
        trace.start("OBJECT", 1, 2, (), None, (2,), (1,))
        trace.key("a", 3, (1, 2), 2, (), (1, 3), (3,))
        trace.start("ARRAY", 4, 5, (1, 2, 3), 2, (5, 2), (4, 1, 3))
        trace.start("OBJECT", 6, 7, (1, 2, 3, 4, 5), 5, (7, 5, 2), (6, 4, 1, 3))
        trace.end(6, 7, 5, (1, 2, 3, 4, 5, 6, 7), 6, None, (5, 2),
                  (6, 4, 1, 3), (4, 6, 1, 3), (3,), (1, 2, 3, 4, 5, 6))
        trace.publish(8, (1, 2, 3, 4, 5, 6), 5, 4, 6, None, None,
                      (4, 8, 6, 1, 3), (4, 8, 6, 1, 3), (3,))
        trace.scalar(7, "INT", ("INT", 7), 9, (1, 2, 3, 4, 5, 6, 8), 5, (4, 8, 6, 9, 1, 3))
        trace.publish(10, (1, 2, 3, 4, 5, 6, 8, 9), 5, 4, 9, None, 8,
                      (4, 8, 6, 10, 9, 1, 3), (4, 8, 6, 10, 9, 1, 3), (3,))
        trace.end(4, 5, 2, (1, 2, 3, 4, 5, 6, 8, 9, 10), 4, 6, (2,),
                  (4, 8, 6, 10, 9, 1, 3), (1, 4, 8, 6, 10, 9, 3), (3,), (1, 2, 3, 4, 6, 8, 9, 10))
        trace.publish(11, (1, 2, 3, 4, 6, 8, 9, 10), 2, 1, 4, 3, None,
                      (1, 11, 3, 4, 8, 6, 10, 9), (1, 11, 3, 4, 8, 6, 10, 9), ())
        trace.end(1, 2, None, (1, 2, 3, 4, 6, 8, 9, 10, 11), 1, 3, (),
                  (1, 11, 3, 4, 8, 6, 10, 9), (1, 11, 3, 4, 8, 6, 10, 9), (), (1, 3, 4, 6, 8, 9, 10, 11))
        return trace
    if case_name == "fresh":
        return trace
    raise ValueError("UNKNOWN_LITERAL_CASE")


def expected_states(case_name):
    if case_name == "scalar7":
        return _first_scalar_states(case_name)
    return tuple(literal_trace(case_name).states)


def case_actions(case_name):
    return tuple(literal_trace(case_name).actions)


FAULT_CASES = tuple(row[0] for row in SCALARS) + (
    "empty_object", "empty_array", "array_three", "object_three", "nested",
    "duplicate_first", "duplicate_middle", "duplicate_last",
)


def fault_boundaries():
    return tuple((case, index, side) for case in FAULT_CASES
                 for index, kind in enumerate(literal_trace(case).kinds) if kind == "tick"
                 for side in ("before", "after"))


RAW_CASES = (
    ("clear_container_cursor", "nested"), ("push_cont_early", "nested"),
    ("clear_child_before_destination", "nested"), ("pop_cont_with_live_child", "nested"),
    ("cursor_value_collision", "nested"), ("skip_lookup", "object_three"),
    ("double_lookup_advance", "object_three"), ("clear_lookup_early", "object_three"),
    ("entry_missing_value", "object_three"), ("clear_value_before_publish", "object_three"),
    ("entry_wrong_order", "object_three"), ("item_wrong_order", "array_three"),
    ("key_as_str", "object_three"), ("bool_as_int", "scalar_true"),
    ("negative_zero_lost", "scalar_negative_zero"), ("duplicate_admitted", "duplicate_middle"),
    ("cont_external_alias", "empty_object"),
)


def drive(supervisor, operation, token, case_name, census):
    expected = tuple(replace(state, operation_id=id(operation), token_id=id(token))
                     for state in expected_states(case_name))
    before = snapshot(supervisor, census)
    violation = check_state(before, expected[0])
    if violation:
        return violation
    index = 1
    ticks = 0
    for action in case_actions(case_name):
        subject.submit(supervisor, operation, token, action)
        after = snapshot(supervisor, census)
        violation = check_transition(before, after, expected[index - 1], expected[index])
        if violation:
            return violation
        before = after
        index += 1
        while expected[index - 1].phase != "IDLE":
            if ticks >= 1000:
                return ("INCONCLUSIVE_TICK_CAP",)
            subject.tick(supervisor, operation, token)
            ticks += 1
            after = snapshot(supervisor, census)
            violation = check_transition(before, after, expected[index - 1], expected[index])
            if violation:
                return violation
            before = after
            index += 1
    if index != len(expected):
        return ("STRUCTURE_TRACE_LENGTH",)
    return ()
