#!/usr/bin/python3
"""Fixed October 19 v4 capture after preserving unconsumed v1, v2 and v3.

Use /usr/bin/python3 -I -B with --manifest-sha256 and --check or --apply.
The immutable, SHA-pinned v3 wrapper loads its pinned v2 transaction and v1
primitives. All three files must remain beside this wrapper. Each import gets
a private module;
only that private transaction's fixed identities and retirement hooks change.
No historical installed/source bytes are rewritten or old timers rearmed.
The reviewed manifest binds an already deployed full SHA/PID/configuration.
"""
from __future__ import annotations

import hashlib
import os
from pathlib import Path
import stat
import types

TRANSACTION_SHA256 = '3f1b48f03aea88bc0fe04de9c71f70b45bf225b4d7937b940f3a91110f696a21'


def _load_transaction():
    path = Path(__file__).with_name('entry_capture_prepare_20261019_v3.py')
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
    module = types.ModuleType('_oct19_v4_private_predecessor')
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
         manifest_sha256='170d956b810862442ef74b60cf000f57a15a3be6bea4f3d663128e2cd5cab915'),
)
# Preserve v3's complete backup of v1/v2, its permanent markers and inputs.
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

core.PROFILE = '20261019-llm-window-v4'
core.EPOCH = '2026-10-19-engine-llm-window-v4'
core.INSTALL = Path('/var/lib/qwq-entry-capture/install-20261019-v4')
core.BACKUP = Path('/var/backups/qwq-entry-capture-20261019-v4')
core.INPUT = core.INPUT.parent/core.PROFILE
core.ENGINE_STATE = core.ENGINE_STATE.parent/core.PROFILE
core.STATE = core.STATE.parent/core.PROFILE
core.ACT_SERVICE = 'qwq-entry-capture-20261019-v4.service'
core.ACT_TIMER = 'qwq-entry-capture-20261019-v4.timer'
core.NEW = dict(executor=Path('/usr/libexec/qwq-entry-capture/activate-20261019-v4.py'),
    study=core.INPUT/'study.json', engine_manifest=core.INPUT/'manifest.json', once=core.INPUT/'once.json',
    activation=core.CONFIG/(core.PROFILE+'.json'),
    staged_dropin=core.CONFIG/(core.PROFILE+'-entry-capture.conf'),
    activation_service=core.UNITS/core.ACT_SERVICE, activation_timer=core.UNITS/core.ACT_TIMER)
core.OLD = tuple(OLD_OWNERS)
INSTALL_DEADLINE = transaction.INSTALL_DEADLINE


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
