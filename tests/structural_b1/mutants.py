"""B1a 실제 슬롯 변이: source58e2123의 고정 전이 행에 결속한다."""


STEP_NAMES = (
    "allocate_and_link", "skip_chain", "clear_new_before_cursor", "unlink_single",
    "unlink_head", "unlink_tail", "unlink_middle", "clear_unlink_early", "clear_cursor_early",
    "double_write_restore",
)
IDENTITY_NAMES = ("owner_missing", "wrong_operation", "wrong_token")
ALIAS_NAMES = ("hidden_leaf", "exception_alias", "closure_alias")


class ExternalHolder:
    __slots__ = ("value",)

    def __init__(self):
        self.value = None


def install_mutant(monkeypatch, subject_module, name, external_holder):
    if name == "none":
        return
    if name in STEP_NAMES:
        original_step = subject_module._step

        def changed_step(operation):
            phase = operation.phase
            if name == "allocate_and_link" and phase == "A0":
                original_step(operation)  # source:162, 생성/new 인계는 그대로.
                operation.arena.head = operation.holders.new  # A2:169를 조기 실행.
                return
            if name == "skip_chain" and phase == "A2":
                # source:169/171 실제 link 대입만 누락하고 다음 phase는 유지.
                operation.phase = "A3"
                return
            if name == "clear_new_before_cursor" and phase == "A4":
                operation.holders.new = None  # source:180을177의 인계보다 먼저.
                original_step(operation)
                return
            if (
                name == "unlink_single" and phase == "U5"
                and operation.action[0] == "unlink_head"
                and operation.holders.left is None and operation.holders.right is None
            ):
                # source:213의 마지막 tail anchor 해제를 실제로 생략한다.
                operation.phase = "U6"
                return
            if (
                name == "unlink_head" and phase == "U4"
                and operation.action[0] == "unlink_head"
                and operation.holders.left is None and operation.holders.right is not None
            ):
                # source:207의 head=right 대입 누락, 원 head가 남는다.
                operation.phase = "U5"
                return
            if (
                name == "unlink_tail" and phase == "U5"
                and operation.action[0] == "unlink_tail"
                and operation.holders.left is not None and operation.holders.right is None
            ):
                # source:213의 tail=left 대입 누락, 원 tail이 남는다.
                operation.phase = "U6"
                return
            if (
                name == "unlink_middle" and phase == "U4"
                and operation.action[0] == "unlink_middle"
                and operation.holders.left is not None and operation.holders.right is not None
            ):
                # source:205의 left.next=right 대입 누락, 가운데 cell link 잔존.
                operation.phase = "U5"
                return
            if name == "clear_unlink_early" and phase == "U9":
                original_step(operation)
                operation.holders.unlink = None  # source:228을 U10보다 한 행 먼저.
                return
            if name == "clear_cursor_early" and phase == "U10":
                original_step(operation)
                operation.holders.cursor = None  # source:232를 U11보다 한 행 먼저.
                return
            if name == "double_write_restore" and phase == "A1":
                original_step(operation)  # source:165의 정상 prev=tail 대입.
                # 실제 추가 graph 쓰기2, 순변화0. Snapshot kill로 세지 않는다.
                operation.holders.new.prev = operation.holders.new
                operation.holders.new.prev = operation.arena.tail
                return
            original_step(operation)

        monkeypatch.setattr(subject_module, "_step", changed_step)
        return
    if name in IDENTITY_NAMES:
        original_submit, original_tick = subject_module.submit, subject_module.tick

        def wrong_arguments(supervisor, operation, token):
            if name == "owner_missing":
                supervisor = subject_module.Supervisor(None)
            elif name == "wrong_operation":
                operation = subject_module.Operation(token)
            else:
                token = tuple([1, 2, 3, 4])
            return supervisor, operation, token

        def submit(supervisor, operation, token, action):
            return original_submit(*wrong_arguments(supervisor, operation, token), action)

        def tick(supervisor, operation, token):
            return original_tick(*wrong_arguments(supervisor, operation, token))

        monkeypatch.setattr(subject_module, "submit", submit)
        monkeypatch.setattr(subject_module, "tick", tick)
        return
    if name in ALIAS_NAMES:
        original_constructor = subject_module._new_cell

        def retain_alias():
            cell = original_constructor()
            if name == "hidden_leaf":
                external_holder.value = cell
            elif name == "exception_alias":
                external_holder.value = Exception(cell)
            else:
                external_holder.value = lambda: cell
            return cell

        monkeypatch.setattr(subject_module, "_new_cell", retain_alias)
        return
    raise ValueError("HARNESS_UNKNOWN_MUTANT")
