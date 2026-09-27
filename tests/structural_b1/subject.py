"""B1a 독립 구조 RED의 입력인 최소 scaffold. 실제 전이는 아직 미구현이다."""

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
    # Task 1 전용: 실제 chain을 만들지 않아 독립 missing-chain RED를 남긴다.
    operation.action = None
    operation.phase = "IDLE"


def _new_cell() -> Cell:
    # Cell 생성 호출은 이 지점 하나이며 scaffold 전이는 호출하지 않는다.
    return Cell()
