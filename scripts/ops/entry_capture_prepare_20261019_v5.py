#!/usr/bin/python3
"""Fixed October 19 v5 capture after preserving unconsumed v1, v2, v3 and v4.

Use /usr/bin/python3 -I -B with --manifest-sha256 and --check or --apply.
The immutable, SHA-pinned v4 wrapper loads its pinned v3/v2/v1 predecessors.
All four predecessor files must remain beside this wrapper. Each import gets
a private module. Only its identities, retirement hooks and the explicit v4
study-version adapter change.
No historical installed/source bytes are rewritten or old timers rearmed.
The reviewed manifest binds an already deployed full SHA/PID/configuration.
"""
from __future__ import annotations

from copy import deepcopy
import hashlib
import os
from pathlib import Path
import stat
import types

TRANSACTION_SHA256 = '36ef03bb057c1a5c2d637ad68a982d8d0eac6f4e52f790232e6707f87c5c0efa'


def _load_transaction():
    path = Path(__file__).with_name('entry_capture_prepare_20261019_v4.py')
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    with os.fdopen(fd, 'rb') as stream:
        before = os.fstat(stream.fileno())
        if not stat.S_ISREG(before.st_mode) or before.st_size > 65536:
            raise RuntimeError('preparation_transaction_invalid')
        data = stream.read(65537)
        after = os.fstat(stream.fileno())
    if ((before.st_size, before.st_mtime_ns, before.st_ctime_ns) !=
            (after.st_size, after.st_mtime_ns, after.st_ctime_ns)
            or hashlib.sha256(data).hexdigest() != TRANSACTION_SHA256):
        raise RuntimeError('preparation_transaction_pin_mismatch')
    module = types.ModuleType('_oct19_v5_private_predecessor')
    module.__file__ = str(path)
    exec(compile(data, str(path), 'exec'), module.__dict__)
    return module


predecessor = _load_transaction()
# prepare/runtime_guards resolve their hooks in the private v2 transaction.
# Patch that exact module below, leaving every historical import untouched.
transaction = predecessor.transaction
core = predecessor.core
PREDECESSORS = (*predecessor.PREDECESSORS,
    dict(timer=core.ACT_TIMER, service=core.ACT_SERVICE,
         install=core.INSTALL, files=dict(core.NEW),
         manifest_sha256='397450db7d56699b1c13d4122e1eb053ad4f4cbb5a98d1db80a6732d1fdcb63b'),
)
# Preserve v4's complete backup of v1/v2/v3, permanent markers and inputs.
OLD_OWNERS = dict(predecessor.OLD_OWNERS)
OLD_OWNERS.update({path: 1000 if key in ('study', 'engine_manifest', 'once') else 0
                   for key, path in core.NEW.items()})
OLD_OWNERS[core.INSTALL/'manifest.json'] = 0
OLD_OWNERS.update({core.INSTALL/'payload'/key: 0 for key in core.NEW})
OLD_OWNERS.update({core.BACKUP/name: 0 for name in
                  ('attempt.json', 'complete.json', 'index.json')})
OLD_OWNERS.update({core.BACKUP/f'original-{i}': 0 for i in range(len(predecessor.OLD_OWNERS))})
OLD_ABSENT = dict(predecessor.OLD_ABSENT)
OLD_ABSENT.update({core.STATE/'activation.receipt': 0, core.STATE/'status.json': 0,
                   core.ENGINE_STATE/'startup.receipt': 1000, core.ENGINE_STATE/'engine.jsonl': 1000})

core.PROFILE = '20261019-llm-window-v5'
core.EPOCH = '2026-10-19-engine-llm-window-v5'
core.INSTALL = Path('/var/lib/qwq-entry-capture/install-20261019-v5')
core.BACKUP = Path('/var/backups/qwq-entry-capture-20261019-v5')
core.INPUT = core.INPUT.parent/core.PROFILE
core.ENGINE_STATE = core.ENGINE_STATE.parent/core.PROFILE
core.STATE = core.STATE.parent/core.PROFILE
core.ACT_SERVICE = 'qwq-entry-capture-20261019-v5.service'
core.ACT_TIMER = 'qwq-entry-capture-20261019-v5.timer'
core.NEW = dict(executor=Path('/usr/libexec/qwq-entry-capture/activate-20261019-v5.py'),
    study=core.INPUT/'study.json', engine_manifest=core.INPUT/'manifest.json', once=core.INPUT/'once.json',
    activation=core.CONFIG/(core.PROFILE+'.json'),
    staged_dropin=core.CONFIG/(core.PROFILE+'-entry-capture.conf'),
    activation_service=core.UNITS/core.ACT_SERVICE, activation_timer=core.UNITS/core.ACT_TIMER)
core.OLD = tuple(OLD_OWNERS)
INSTALL_DEADLINE = transaction.INSTALL_DEADLINE


_historical_validate_study = core.validate_study


def validate_study(study, head):
    """실제 v4 입력은 보존하고 공통 경계만 과거 검증기에 투영한다."""
    core.require(type(study) is dict and type(study.get('capture')) is dict,
                 'study_contract_mismatch')
    selection = study['capture'].get('selection_basis')
    core.require(type(selection) is dict and selection.get('version') == 'selection-basis-v4',
                 'selection_contract_invalid')
    projected = deepcopy(study)
    projected['capture']['selection_basis']['version'] = 'selection-basis-v3'
    _historical_validate_study(projected, head)


# 설치 bytes/hash와 Host.static의 실제 CapturePlan 입력은 변환하지 않는다.
core.validate_study = validate_study


def require_absent(path, owner):
    parent = core.open_dir(path.parent, owner)
    try:
        try:
            os.stat(path.name, dir_fd=parent, follow_symlinks=False)
        except FileNotFoundError:
            return
        raise core.GuardError('predecessor_attempt_or_dropin_exists')
    finally:
        os.close(parent)


def retirement_guards(host):
    for previous in PREDECESSORS:
        core.require(host.show(previous['timer'], 'ActiveState') == 'inactive'
            and host.show(previous['timer'], 'UnitFileState') == 'disabled'
            and host.show(previous['service'], 'ActiveState') == 'inactive',
            'predecessor_not_quiescent')
    for path, owner in OLD_ABSENT.items():
        require_absent(path, owner)


def old_inputs(manifest):
    originals = {}
    for path, owner in OLD_OWNERS.items():
        data = core.trusted_read(path, owner=owner)
        core.require(core.sha(data) == manifest['old_files'][str(path)], 'predecessor_pin_mismatch')
        originals[path] = data
    for previous in PREDECESSORS:
        pinned = originals[previous['install']/'manifest.json']
        core.require(core.sha(pinned) == previous['manifest_sha256'], 'predecessor_manifest_changed')
        old_manifest = core.document(pinned)
        for key, path in previous['files'].items():
            core.require(core.sha(originals[path]) == old_manifest['new_files'][key]
                and originals[path] == originals[previous['install']/'payload'/key],
                'predecessor_payload_changed')
    return originals


# The pinned transaction invokes these module-local hooks on every preflight,
# lock acquisition, reload and arm boundary; its backup/disarm logic is unchanged.
transaction.__doc__ = __doc__
transaction.retirement_guards = retirement_guards
transaction.old_inputs = old_inputs
runtime_guards = transaction.runtime_guards
prepare = transaction.prepare
main = transaction.main


if __name__ == '__main__':
    raise SystemExit(main())
