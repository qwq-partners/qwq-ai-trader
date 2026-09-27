"""B1a EMPTY cell 생성·chain·holder·unlink의 행별 원시 전이."""

Action = tuple[str, None]


class Cell:
    __slots__ = (
        "tag", "payload", "prev", "next", "s0", "s1", "s2",
        "c0", "c1", "c2", "c3", "__weakref__",
    )

    def __init__(self) -> None:
        self.tag = "EMPTY"
        self.payload = None
        self.prev = None
        self.next = None
        self.s0 = None
        self.s1 = None
        self.s2 = None
        self.c0 = None
        self.c1 = None
        self.c2 = None
        self.c3 = None


class Arena:
    __slots__ = ("head", "tail", "root")

    def __init__(self) -> None:
        self.head = None
        self.tail = None
        self.root = None


class Holders:
    __slots__ = (
        "new", "replacement", "cursor", "unlink", "left", "right",
        "work_head", "cont_head", "lookup", "pending_key",
    )

    def __init__(self) -> None:
        self.new = None
        self.replacement = None
        self.cursor = None
        self.unlink = None
        self.left = None
        self.right = None
        self.work_head = None
        self.cont_head = None
        self.lookup = None
        self.pending_key = None


class Operation:
    __slots__ = ("token", "arena", "holders", "phase", "action", "fault", "outcome")

    def __init__(self, token: tuple[int, int, int, int]) -> None:
        self.token = token
        self.arena = Arena()
        self.holders = Holders()
        self.phase = "IDLE"
        self.action = None
        self.fault = None
        self.outcome = None


class Supervisor:
    __slots__ = ("active",)

    def __init__(self, operation: Operation) -> None:
        self.active = operation


def make_supervisor(token: tuple[int, int, int, int]) -> Supervisor:
    if (
        type(token) is not tuple
        or len(token) != 4
        or type(token[0]) is not int
        or type(token[1]) is not int
        or type(token[2]) is not int
        or type(token[3]) is not int
        or token != (1, 2, 3, 4)
    ):
        raise ValueError("INVALID_TOKEN")
    return Supervisor(Operation(token))


def _check_identity(
    supervisor: Supervisor,
    operation: Operation,
    token: tuple[int, int, int, int],
) -> None:
    if (
        type(supervisor) is not Supervisor
        or type(operation) is not Operation
        or supervisor.active is not operation
        or operation.token is not token
    ):
        raise ValueError("INVALID_IDENTITY")
    if operation.fault is not None:
        raise ValueError("FAULT_LATCHED")


def submit(
    supervisor: Supervisor,
    operation: Operation,
    token: tuple[int, int, int, int],
    action: Action,
) -> None:
    _check_identity(supervisor, operation, token)
    if operation.phase != "IDLE" or operation.action is not None:
        raise ValueError("BUSY")
    if (
        type(action) is not tuple
        or len(action) != 2
        or type(action[0]) is not str
        or action[1] is not None
        or action[0] not in (
            "append_empty", "unlink_head", "unlink_tail", "unlink_middle",
        )
    ):
        raise ValueError("INVALID_ACTION")
    if action[0] != "append_empty":
        if operation.arena.head is None or operation.arena.tail is None:
            raise ValueError("EMPTY_CHAIN")
        if action[0] == "unlink_middle" and (
            type(operation.arena.head) is not Cell
            or type(operation.arena.tail) is not Cell
            or type(operation.arena.head.next) is not Cell
            or operation.arena.head is operation.arena.tail
            or operation.arena.head.next is operation.arena.head
            or operation.arena.head.next is operation.arena.tail
            or operation.arena.head.prev is not None
            or operation.arena.tail.next is not None
            or operation.arena.head.next.next is not operation.arena.tail
            or operation.arena.head.next.prev is not operation.arena.head
            or operation.arena.tail.prev is not operation.arena.head.next
        ):
            raise ValueError("INVALID_MIDDLE")
    operation.action = action
    operation.phase = "A0" if action[0] == "append_empty" else "U0"


def tick(
    supervisor: Supervisor,
    operation: Operation,
    token: tuple[int, int, int, int],
) -> None:
    _check_identity(supervisor, operation, token)
    if operation.phase == "IDLE" or operation.action is None:
        raise ValueError("IDLE")
    try:
        _step(operation)
    except Exception as fault:
        operation.fault = fault
        operation.outcome = "DISPOSAL_FAULT"


def _step(operation: Operation) -> None:
    # 각 분기는 표의 한 행이다. phase/action은 graph 슬롯과 별개다.
    if operation.phase == "A0":
        operation.holders.new = _new_cell()
        operation.phase = "A1"
    elif operation.phase == "A1":
        operation.holders.new.prev = operation.arena.tail
        operation.phase = "A2"
    elif operation.phase == "A2":
        if operation.arena.tail is None:
            operation.arena.head = operation.holders.new
        else:
            operation.arena.tail.next = operation.holders.new
        operation.phase = "A3"
    elif operation.phase == "A3":
        operation.arena.tail = operation.holders.new
        operation.phase = "A4"
    elif operation.phase == "A4":
        operation.holders.cursor = operation.holders.new
        operation.phase = "A5"
    elif operation.phase == "A5":
        operation.holders.new = None
        operation.phase = "A6"
    elif operation.phase == "A6":
        operation.holders.cursor = None
        operation.action = None
        operation.phase = "IDLE"
    elif operation.phase == "U0":
        if operation.action[0] == "unlink_head":
            operation.holders.cursor = operation.arena.head
        elif operation.action[0] == "unlink_tail":
            operation.holders.cursor = operation.arena.tail
        else:
            operation.holders.cursor = operation.arena.head.next
        operation.phase = "U1"
    elif operation.phase == "U1":
        operation.holders.unlink = operation.holders.cursor
        operation.phase = "U2"
    elif operation.phase == "U2":
        operation.holders.left = operation.holders.cursor.prev
        operation.phase = "U3"
    elif operation.phase == "U3":
        operation.holders.right = operation.holders.cursor.next
        operation.phase = "U4"
    elif operation.phase == "U4":
        if operation.holders.left is not None:
            operation.holders.left.next = operation.holders.right
        else:
            operation.arena.head = operation.holders.right
        operation.phase = "U5"
    elif operation.phase == "U5":
        if operation.holders.right is not None:
            operation.holders.right.prev = operation.holders.left
        else:
            operation.arena.tail = operation.holders.left
        operation.phase = "U6"
    elif operation.phase == "U6":
        operation.holders.cursor.prev = None
        operation.phase = "U7"
    elif operation.phase == "U7":
        operation.holders.cursor.next = None
        operation.phase = "U8"
    elif operation.phase == "U8":
        operation.holders.left = None
        operation.phase = "U9"
    elif operation.phase == "U9":
        operation.holders.right = None
        operation.phase = "U10"
    elif operation.phase == "U10":
        operation.holders.unlink = None
        operation.phase = "U11"
    elif operation.phase == "U11":
        # 옛 링크와 이웃/unlink holder를 비운 뒤 마지막 cell 참조를 해제한다.
        operation.holders.cursor = None
        operation.action = None
        operation.phase = "IDLE"
    else:
        raise ValueError("INVALID_PHASE")


def _new_cell() -> Cell:
    # 유일한 Cell 생성 지점이며 A0의 new holder 인계에서만 호출한다.
    return Cell()
