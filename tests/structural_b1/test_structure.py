"""Small B1a tests only: no source/native lane or large-node claim."""

import importlib.util
import os
from pathlib import Path
import sys

import pytest


def _load_siblings():
    folder = Path(__file__).parent
    for name in ("subject", "observer", "fixtures", "mutants"):
        module_name = "_qwq_b1_" + name
        spec = importlib.util.spec_from_file_location(module_name, folder / (name + ".py"))
        module = importlib.util.module_from_spec(spec)
        sys.modules[module_name] = module
        spec.loader.exec_module(module)


_load_siblings()
import _qwq_b1_subject as subject
import _qwq_b1_observer as observer
import _qwq_b1_fixtures as fixtures
import _qwq_b1_mutants as mutants


def test_append_requires_actual_chain(monkeypatch):
    """Missing actual append cannot be certified by a phase/completion flag."""
    census = observer.install_census(monkeypatch, subject)
    supervisor = subject.make_supervisor(fixtures.TOKEN)
    operation = supervisor.active
    violations = fixtures.drive(supervisor, operation, fixtures.TOKEN, (fixtures.APPEND,), census)
    assert operation.arena.head is not None, "STRUCTURE_MISSING_CHAIN"
    assert violations == (), violations
    state = observer.snapshot(supervisor, census)
    slots = dict(state.slots)
    assert slots[("arena", "head")] == slots[("arena", "tail")] == 1
    assert slots[(1, "tag")] == "EMPTY"
    assert slots[(1, "prev")] is None and slots[(1, "next")] is None
    assert all(slots[("holders", name)] is None for name in observer.HOLDER_SLOTS)
    assert (state.reached, state.returned, state.live) == (1, 1, (1,))
