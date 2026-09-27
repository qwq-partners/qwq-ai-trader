"""독립 literal 판정에 실제 graph/phase 쓰기를 연결하는 B1b 음성 대조군."""


_NAMES = (
    "clear_container_cursor", "push_cont_early", "clear_child_before_destination",
    "pop_cont_with_live_child", "cursor_value_collision", "skip_lookup",
    "double_lookup_advance", "clear_lookup_early", "entry_missing_value",
    "clear_value_before_publish", "entry_wrong_order", "item_wrong_order",
    "key_as_str", "bool_as_int", "negative_zero_lost", "duplicate_admitted",
    "cont_external_alias",
)


def install_mutant(monkeypatch, subject_module, name, external_holder):
    if name not in _NAMES:
        raise ValueError("UNKNOWN_B1B_MUTANT")
    original = subject_module._step

    def mutated_step(operation):
        h = operation.holders
        phase = operation.phase

        if name == "clear_container_cursor" and phase == "O6":
            original(operation)
            # C0의 container 소유자를 CONT 생성 전에 지운다.
            h.cursor = None
        elif name == "push_cont_early" and phase == "C6":
            original(operation)
            # c3를 설정한 같은 행에서 C7의 push까지 앞당긴다.
            h.cont_head = h.new
        elif (
            name == "clear_child_before_destination" and phase == "D0"
            and h.cont_head.c3 is not None
        ):
            # 완료 자식을 목적지에 복사하기 전에 source를 지운다.
            h.cont_head.c0 = None
            original(operation)
        elif (
            name == "pop_cont_with_live_child" and phase == "D0"
            and h.cont_head.c3 is not None
        ):
            original(operation)
            # c0가 아직 완료 자식을 소유한 상태에서 top CONT를 pop한다.
            h.cont_head = h.cont_head.c3
        elif (
            name == "cursor_value_collision" and phase == "D1"
            and h.cont_head.c3 is not None
        ):
            # 제거 대상 CONT 대신 완료 값을 unlink scratch에 넣는다.
            h.cursor = h.cont_head.c0
            operation.phase = "D2"
        elif name == "skip_lookup" and phase == "L0" and h.cont_head.c0.s0 is not None:
            h.lookup = None
            operation.phase = "K0"
        elif (
            name == "double_lookup_advance" and phase == "L1"
            and h.lookup.s0.payload != operation.action[1] and h.lookup.s2 is not None
        ):
            original(operation)
            # 정상 한 ENTRY 이동 뒤 같은 tick에 두 번째 ENTRY도 건너뛴다.
            h.lookup = h.lookup.s2
            operation.phase = "K0" if h.lookup is None else "L1"
        elif name == "clear_lookup_early" and phase == "L0" and h.cont_head.c0.s0 is not None:
            original(operation)
            # L1 비교 전에 lookup만 지워 phase는 L1로 남긴다.
            h.lookup = None
        elif name == "entry_missing_value" and phase == "P6":
            h.new.s1 = None
            operation.phase = "P7"
        elif name == "clear_value_before_publish" and phase == "P6":
            original(operation)
            # P7의 ordered 게시보다 먼저 P9의 source 해제를 수행한다.
            h.cont_head.c1 = None
        elif (
            name == "entry_wrong_order" and phase == "P7"
            and h.cont_head.c0.tag == "OBJECT" and h.cont_head.c0.s0 is not None
        ):
            # 기존 tail 뒤가 아니라 첫 ENTRY 앞에 삽입한다.
            h.new.s2 = h.cont_head.c0.s0
            h.cont_head.c0.s0 = h.new
            operation.phase = "P8"
        elif (
            name == "item_wrong_order" and phase == "P7"
            and h.cont_head.c0.tag == "ARRAY" and h.cont_head.c0.s0 is not None
        ):
            h.new.s1 = h.cont_head.c0.s0
            h.cont_head.c0.s0 = h.new
            operation.phase = "P8"
        elif name == "key_as_str" and phase == "K4":
            h.new.tag = "STR"
            operation.phase = "K5"
        elif name == "bool_as_int" and phase == "V4" and type(operation.action[1]) is bool:
            h.new.tag = "INT"
            operation.phase = "V5"
        elif (
            name == "negative_zero_lost" and phase == "V5"
            and type(operation.action[1]) is float and operation.action[1] == 0.0
        ):
            h.new.payload = 0.0
            operation.phase = "V6"
        elif (
            name == "duplicate_admitted" and phase == "L1"
            and h.lookup.s0.payload == operation.action[1]
        ):
            # 중복 hit를 miss처럼 처리해 KEY 생성 경로를 개방한다.
            h.lookup = None
            operation.phase = "K0"
        elif name == "cont_external_alias" and phase == "D1":
            # 이 경우만 명시한 외부 holder에 실제 Cell을 보존한다.
            external_holder.append(h.cont_head)
            original(operation)
        else:
            original(operation)

    monkeypatch.setattr(subject_module, "_step", mutated_step)
