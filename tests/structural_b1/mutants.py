"""Fixed B1a mutant interface; transition sites are bound in Task 3."""


class ExternalHolder:
    __slots__ = ("value",)

    def __init__(self):
        self.value = None


def install_mutant(monkeypatch, subject_module, name, external_holder):
    if name == "none":
        return
    raise NotImplementedError("TASK3_MUTANT_SITE_PENDING")
