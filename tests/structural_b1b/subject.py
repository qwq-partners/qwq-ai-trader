"""B1b private 구성·순서 게시·CONT 인계의 행별 구조 전이."""

import _qwq_b1b_shell as _shell


Cell = _shell.Cell
Arena = _shell.Arena
Holders = _shell.Holders
Operation = _shell.Operation
Supervisor = _shell.Supervisor
make_supervisor = _shell.make_supervisor
_check_identity = _shell._check_identity

Scalar = bool | int | float | str | None
Action = tuple[str, Scalar]


def submit(
    supervisor: Supervisor,
    operation: Operation,
    token: tuple[int, int, int, int],
    action: Action,
) -> None:
    _check_identity(supervisor, operation, token)
    if operation.outcome is not None:
        raise ValueError("OUTCOME_LATCHED")
    if operation.phase != "IDLE" or operation.action is not None:
        raise ValueError("BUSY")
    if (
        type(action) is not tuple
        or len(action) != 2
        or type(action[0]) is not str
    ):
        raise ValueError("INVALID_ACTION")
    name = action[0]
    payload = action[1]
    if name == "scalar":
        if (
            payload is not None
            and type(payload) is not bool
            and type(payload) is not int
            and type(payload) is not float
            and type(payload) is not str
        ):
            raise ValueError("INVALID_ACTION")
    elif name == "key":
        if type(payload) is not str:
            raise ValueError("INVALID_ACTION")
    elif name in ("start_object", "start_array", "publish", "end"):
        if payload is not None:
            raise ValueError("INVALID_ACTION")
    else:
        raise ValueError("INVALID_ACTION")

    h = operation.holders
    a = operation.arena
    if (
        a.root is not None
        or h.new is not None
        or h.replacement is not None
        or h.cursor is not None
        or h.unlink is not None
        or h.left is not None
        or h.right is not None
        or h.work_head is not None
        or h.lookup is not None
        or h.pending_key is not None
    ):
        raise ValueError("INVALID_BOUNDARY")
    c = h.cont_head
    if c is not None and (
        type(c) is not Cell
        or c.tag != "CONT"
        or type(c.c0) is not Cell
        or c.c0.tag not in ("OBJECT", "ARRAY")
        or (c.c3 is not None and (type(c.c3) is not Cell or c.c3.tag != "CONT"))
    ):
        raise ValueError("INVALID_BOUNDARY")

    if name in ("scalar", "start_object", "start_array"):
        if c is None:
            if a.head is not None or a.tail is not None:
                raise ValueError("INVALID_BOUNDARY")
        elif (
            c.c1 is not None
            or (c.c0.tag == "OBJECT" and (type(c.c2) is not Cell or c.c2.tag != "KEY"))
            or (c.c0.tag == "ARRAY" and c.c2 is not None)
        ):
            raise ValueError("INVALID_BOUNDARY")
        phase = "V0" if name == "scalar" else "O0"
    elif name == "key":
        if c is None or c.c0.tag != "OBJECT" or c.c1 is not None or c.c2 is not None:
            raise ValueError("INVALID_BOUNDARY")
        phase = "L0"
    elif name == "publish":
        if (
            c is None
            or type(c.c1) is not Cell
            or c.c1.tag not in ("NULL", "BOOL", "INT", "FLOAT", "STR", "OBJECT", "ARRAY")
            or (c.c0.tag == "OBJECT" and (type(c.c2) is not Cell or c.c2.tag != "KEY"))
            or (c.c0.tag == "ARRAY" and c.c2 is not None)
        ):
            raise ValueError("INVALID_BOUNDARY")
        phase = "P0"
    else:  # end: 현재 CONT가 아니라 부모의 목적지 준비 상태를 확인한다.
        if c is None or c.c1 is not None or c.c2 is not None:
            raise ValueError("INVALID_BOUNDARY")
        p = c.c3
        if p is not None and (
            type(p.c0) is not Cell
            or p.c0.tag not in ("OBJECT", "ARRAY")
            or p.c1 is not None
            or (p.c0.tag == "OBJECT" and (type(p.c2) is not Cell or p.c2.tag != "KEY"))
            or (p.c0.tag == "ARRAY" and p.c2 is not None)
        ):
            raise ValueError("INVALID_BOUNDARY")
        phase = "D0"

    # 모든 사전 검증이 끝난 뒤에만 제어 상태를 게시한다. graph 쓰기는 없다.
    operation.action = action
    operation.phase = phase


def tick(
    supervisor: Supervisor,
    operation: Operation,
    token: tuple[int, int, int, int],
) -> None:
    _check_identity(supervisor, operation, token)
    if operation.outcome is not None:
        raise ValueError("OUTCOME_LATCHED")
    if operation.phase == "IDLE" or operation.action is None:
        raise ValueError("IDLE")
    try:
        _step(operation)
    except Exception as fault:
        operation.fault = fault
        operation.outcome = "DISPOSAL_FAULT"


def _step(operation: Operation) -> None:
    # 각 분기는 한 graph 슬롯만 쓴다. phase/action/outcome은 별도 제어 상태다.
    h = operation.holders
    a = operation.arena
    phase = operation.phase

    # Q0–Q3의 다섯 역할만 공유한다. A/U0나 외부 명령열은 실행하지 않는다.
    if phase in ("V0", "O0", "C0", "K0", "P0"):
        h.new = _new_cell()
        operation.phase = phase[0] + "1"
    elif phase in ("V1", "O1", "C1", "K1", "P1"):
        h.new.prev = a.tail
        operation.phase = phase[0] + "2"
    elif phase in ("V2", "O2", "C2", "K2", "P2"):
        if a.tail is None:
            a.head = h.new
        else:
            a.tail.next = h.new
        operation.phase = phase[0] + "3"
    elif phase in ("V3", "O3", "C3", "K3", "P3"):
        a.tail = h.new
        operation.phase = phase[0] + "4"

    elif phase == "V4":
        payload = operation.action[1]
        if payload is None:
            tag = "NULL"
        elif type(payload) is bool:
            tag = "BOOL"
        elif type(payload) is int:
            tag = "INT"
        elif type(payload) is float:
            tag = "FLOAT"
        else:
            tag = "STR"
        h.new.tag = tag
        operation.phase = "V5"
    elif phase == "V5":
        h.new.payload = operation.action[1]
        operation.phase = "V6"
    elif phase == "V6":
        if h.cont_head is None:
            a.root = h.new
        else:
            h.cont_head.c1 = h.new
        operation.phase = "V7"
    elif phase == "V7":
        h.new = None
        operation.action = None
        operation.phase = "IDLE"

    elif phase == "O4":
        h.new.tag = "OBJECT" if operation.action[0] == "start_object" else "ARRAY"
        operation.phase = "O5"
    elif phase == "O5":
        h.cursor = h.new
        operation.phase = "O6"
    elif phase == "O6":
        h.new = None
        operation.phase = "C0"
    elif phase == "C4":
        h.new.tag = "CONT"
        operation.phase = "C5"
    elif phase == "C5":
        h.new.c0 = h.cursor
        operation.phase = "C6"
    elif phase == "C6":
        h.new.c3 = h.cont_head
        operation.phase = "C7"
    elif phase == "C7":
        h.cont_head = h.new
        operation.phase = "C8"
    elif phase == "C8":
        h.cursor = None
        operation.phase = "C9"
    elif phase == "C9":
        h.new = None
        operation.action = None
        operation.phase = "IDLE"

    elif phase == "L0":
        h.lookup = h.cont_head.c0.s0
        operation.phase = "K0" if h.lookup is None else "L1"
    elif phase == "L1":
        # exact str 한 쌍만 비교하고 KEY 생성 전에 중복을 래치한다.
        if h.lookup.s0.payload == operation.action[1]:
            h.lookup = None
            operation.outcome = "B1B_UNSUPPORTED_DUPLICATE"
            operation.action = None
            operation.phase = "IDLE"
        else:
            h.lookup = h.lookup.s2
            operation.phase = "K0" if h.lookup is None else "L1"
    elif phase == "K4":
        h.new.tag = "KEY"
        operation.phase = "K5"
    elif phase == "K5":
        h.new.payload = operation.action[1]
        operation.phase = "K6"
    elif phase == "K6":
        h.cont_head.c2 = h.new
        operation.phase = "K7"
    elif phase == "K7":
        h.new = None
        operation.action = None
        operation.phase = "IDLE"

    elif phase == "P4":
        h.new.tag = "ENTRY" if h.cont_head.c0.tag == "OBJECT" else "ITEM"
        operation.phase = "P5"
    elif phase == "P5":
        if h.cont_head.c0.tag == "OBJECT":
            h.new.s0 = h.cont_head.c2
            operation.phase = "P6"
        else:
            h.new.s0 = h.cont_head.c1
            operation.phase = "P7"
    elif phase == "P6":
        h.new.s1 = h.cont_head.c1
        operation.phase = "P7"
    elif phase == "P7":
        if h.cont_head.c0.s0 is None:
            h.cont_head.c0.s0 = h.new
        elif h.cont_head.c0.tag == "OBJECT":
            h.cont_head.c0.s1.s2 = h.new
        else:
            h.cont_head.c0.s1.s1 = h.new
        operation.phase = "P8"
    elif phase == "P8":
        # s1은 실제 strong tail index다. P7 뒤의 이 한 행에서만 따라잡는다.
        h.cont_head.c0.s1 = h.new
        operation.phase = "P9"
    elif phase == "P9":
        h.cont_head.c1 = None
        operation.phase = "P10" if h.cont_head.c0.tag == "OBJECT" else "P11"
    elif phase == "P10":
        h.cont_head.c2 = None
        operation.phase = "P11"
    elif phase == "P11":
        h.new = None
        operation.action = None
        operation.phase = "IDLE"

    elif phase == "D0":
        # 완료 자식을 먼저 최종 목적지에 둔다. cursor는 제거할 CONT 전용이다.
        if h.cont_head.c3 is None:
            a.root = h.cont_head.c0
        else:
            h.cont_head.c3.c1 = h.cont_head.c0
        operation.phase = "D1"
    elif phase == "D1":
        h.cursor = h.cont_head
        operation.phase = "D2"
    elif phase == "D2":
        h.cursor.c0 = None
        operation.phase = "D3"
    elif phase == "D3":
        h.cont_head = h.cursor.c3
        operation.phase = "D4"
    elif phase == "D4":
        h.cursor.c3 = None
        operation.phase = "D5"
    elif phase == "D5":
        h.cursor.tag = "EMPTY"
        operation.phase = "U1"
    elif phase in ("U1", "U2", "U3", "U4", "U5", "U6", "U7", "U8", "U9", "U10", "U11"):
        _shell._step(operation)
    else:
        raise ValueError("INVALID_PHASE")


def _new_cell() -> Cell:
    # 유일한 Cell 생성 지점이며 Q0의 new 인계에서만 호출한다.
    return Cell()
