"""Bounded, offline-only CLI for deciding four verification receipts."""

from __future__ import annotations

import argparse
import json
import os
import stat
import sys

from scripts.dev.verification_contract import EvidenceError, evaluate_bundle, parse_document


_MAX_DOCUMENT_BYTES = 32 * 1024 * 1024
_READ_CHUNK_BYTES = 1024 * 1024
_DECISION_SCHEMA = "qwq.verification-decision/v1"


class _ArgumentError(ValueError):
    """An argument error whose text is never printed to callers."""


class _InputError(ValueError):
    """A bounded input failure represented only by its fixed public code."""

    def __init__(self, code: str):
        super().__init__(code)
        self.code = code


class _Parser(argparse.ArgumentParser):
    def error(self, _message: str) -> None:
        raise _ArgumentError("ARGUMENT_ERROR")


def _rejected(*errors: str) -> dict:
    return {
        "schema": _DECISION_SCHEMA,
        "status": "REJECTED",
        "errors": sorted(set(errors)),
        "scope": "offline_evidence_only",
        "production_eligible": False,
    }


def _emit(decision: dict) -> None:
    sys.stdout.write(json.dumps(decision, ensure_ascii=False, sort_keys=True, separators=(",", ":")) + "\n")


def _count_option(arguments: list[str], option: str) -> int:
    return sum(argument == option or argument.startswith(option + "=") for argument in arguments)


def _parse_args(arguments: list[str]):
    parser = _Parser(add_help=False, allow_abbrev=False)
    parser.add_argument("--expected", required=True)
    parser.add_argument("--receipt", action="append", default=[])
    parsed = parser.parse_args(arguments)
    if _count_option(arguments, "--expected") != 1 or len(parsed.receipt) != 4:
        raise _ArgumentError("ARGUMENT_ERROR")
    return parsed


def _read_regular_file(path: str) -> bytes:
    flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NONBLOCK", 0)
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    try:
        descriptor = os.open(path, flags)
    except (OSError, TypeError, ValueError):
        raise _InputError("INPUT_OPEN_ERROR") from None
    try:
        try:
            metadata = os.fstat(descriptor)
        except OSError:
            raise _InputError("INPUT_STAT_ERROR") from None
        if not stat.S_ISREG(metadata.st_mode):
            raise _InputError("INPUT_NOT_REGULAR")
        if metadata.st_size > _MAX_DOCUMENT_BYTES:
            raise _InputError("INPUT_TOO_LARGE")
        raw = bytearray()
        while len(raw) <= _MAX_DOCUMENT_BYTES:
            try:
                chunk = os.read(descriptor, min(_READ_CHUNK_BYTES, _MAX_DOCUMENT_BYTES + 1 - len(raw)))
            except OSError:
                raise _InputError("INPUT_READ_ERROR") from None
            if not chunk:
                break
            raw.extend(chunk)
        if len(raw) > _MAX_DOCUMENT_BYTES:
            raise _InputError("INPUT_TOO_LARGE")
        return bytes(raw)
    finally:
        try:
            os.close(descriptor)
        except OSError:
            pass


def _parse_input(path: str, *, kind: str) -> dict:
    raw = _read_regular_file(path)
    try:
        return parse_document(raw, kind=kind)
    except EvidenceError:
        raise _InputError("INPUT_DOCUMENT_REJECTED") from None


def main(argv: list[str] | None = None) -> int:
    arguments = list(sys.argv[1:] if argv is None else argv)
    try:
        parsed = _parse_args(arguments)
    except (_ArgumentError, SystemExit):
        _emit(_rejected("ARGUMENT_ERROR"))
        return 2
    try:
        expected = _parse_input(parsed.expected, kind="expectation")
        receipts = [_parse_input(path, kind="receipt") for path in parsed.receipt]
    except _InputError as exc:
        _emit(_rejected(exc.code))
        return 1 if exc.code == "INPUT_DOCUMENT_REJECTED" else 2
    decision = evaluate_bundle(receipts, expected)
    _emit(decision)
    return 0 if decision["status"] == "EVIDENCE_CONSISTENT" else 1


if __name__ == "__main__":
    raise SystemExit(main())
