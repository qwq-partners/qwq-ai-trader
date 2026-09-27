"""Fixed B1a mutants; pending transition sites require Task 3 source binding."""


PENDING_SITES = (
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
    if name in PENDING_SITES:
        raise NotImplementedError("TASK3_MUTANT_SITE_PENDING:" + name)
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
