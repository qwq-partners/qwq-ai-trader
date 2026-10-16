#!/usr/bin/python3
"""Install the separate October 19 v2 capture after unconsumed v1 retirement.

Run with /usr/bin/python3 -I -B, --manifest-sha256 and --check or --apply.
Stage the reviewed manifest/payload in install-20261019-v2. The manifest pins
an explicitly supplied already-deployed full SHA, source inventory, configuration,
process, v1 evidence and v2 payload; no branch is resolved dynamically.

The immutable v1 source supplies its tested I/O, payload, capacity and process
guards in a private module. Its hash is checked BEFORE execution. Only fixed v2
identities are rebound; neither the historical source nor its installed bytes
are rewritten. This file must remain beside the pinned v1 source. The retirement
transaction below replaces v1's consumed-October-16/drop-in-removal transaction.
No installation deploys, restarts, edits budget policy or certifies resource safety.
"""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
import fcntl
import hashlib
import json
import os
from pathlib import Path
import stat
import sys
import types

CORE_SHA256 = 'b39e48fdcd7308d818228f7463f8fec2b0183882363aba73246d210f0a3e2057'


def _load_core():
    path = Path(__file__).with_name('entry_capture_prepare_20261019.py')
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    with os.fdopen(fd, 'rb') as stream:
        before = os.fstat(stream.fileno())
        if not stat.S_ISREG(before.st_mode) or before.st_size > 65536:
            raise RuntimeError('preparation_core_invalid')
        data = stream.read(65537)
        after = os.fstat(stream.fileno())
    if ((before.st_size, before.st_mtime_ns, before.st_ctime_ns) !=
            (after.st_size, after.st_mtime_ns, after.st_ctime_ns)
            or hashlib.sha256(data).hexdigest() != CORE_SHA256):
        raise RuntimeError('preparation_core_pin_mismatch')
    module = types.ModuleType('_oct19_v2_private_core')
    module.__file__ = str(path)
    exec(compile(data, str(path), 'exec'), module.__dict__)
    return module


core = _load_core()
OLD_TIMER, OLD_SERVICE = core.ACT_TIMER, core.ACT_SERVICE
OLD_INSTALL, OLD_BACKUP = core.INSTALL, core.BACKUP
OLD_NEW = dict(core.NEW)
OLD_MANIFEST_SHA256 = 'c7aa38ab9ec0d6dcd627551d8baf35c51433a360915bd3d7036ce7601695be96'
OLD_OWNERS = {path: 1000 if key in ('study', 'engine_manifest', 'once') else 0
              for key, path in OLD_NEW.items()}
OLD_OWNERS.update({OLD_INSTALL/'manifest.json': 0})
OLD_OWNERS.update({OLD_INSTALL/'payload'/key: 0 for key in OLD_NEW})
OLD_OWNERS.update({OLD_BACKUP/name: 0 for name in
                  ('attempt.json', 'complete.json', 'index.json',
                   'original-0', 'original-1', 'original-2', 'original-3')})
OLD_ABSENT = {core.STATE/'activation.receipt': 0, core.STATE/'status.json': 0,
              core.ENGINE_STATE/'startup.receipt': 1000,
              core.ENGINE_STATE/'engine.jsonl': 1000, core.DROPIN: 0}

core.PROFILE = '20261019-llm-window-v2'
core.EPOCH = '2026-10-19-engine-llm-window-v2'
core.INSTALL = Path('/var/lib/qwq-entry-capture/install-20261019-v2')
core.BACKUP = Path('/var/backups/qwq-entry-capture-20261019-v2')
core.INPUT = core.INPUT.parent/core.PROFILE
core.ENGINE_STATE = core.ENGINE_STATE.parent/core.PROFILE
core.STATE = core.STATE.parent/core.PROFILE
core.ACT_SERVICE = 'qwq-entry-capture-20261019-v2.service'
core.ACT_TIMER = 'qwq-entry-capture-20261019-v2.timer'
core.NEW = dict(executor=Path('/usr/libexec/qwq-entry-capture/activate-20261019-v2.py'),
    study=core.INPUT/'study.json', engine_manifest=core.INPUT/'manifest.json', once=core.INPUT/'once.json',
    activation=core.CONFIG/(core.PROFILE+'.json'),
    staged_dropin=core.CONFIG/(core.PROFILE+'-entry-capture.conf'),
    activation_service=core.UNITS/core.ACT_SERVICE, activation_timer=core.UNITS/core.ACT_TIMER)
core.OLD = tuple(OLD_OWNERS)
INSTALL_DEADLINE = datetime(2026, 10, 19, 0, 20, tzinfo=timezone.utc)


def require_absent(path, owner):
    """Absence is checked beneath a trusted parent, including dangling symlinks."""
    parent = core.open_dir(path.parent, owner)
    try:
        try:
            os.stat(path.name, dir_fd=parent, follow_symlinks=False)
        except FileNotFoundError:
            return
        raise core.GuardError('retired_v1_attempt_or_dropin_exists')
    finally:
        os.close(parent)


def retirement_guards(host):
    core.require(host.show(OLD_TIMER, 'ActiveState') == 'inactive'
        and host.show(OLD_TIMER, 'UnitFileState') == 'disabled'
        and host.show(OLD_SERVICE, 'ActiveState') == 'inactive', 'retired_v1_not_quiescent')
    for path, owner in OLD_ABSENT.items():
        require_absent(path, owner)


def old_inputs(manifest):
    originals = {}
    for path, owner in OLD_OWNERS.items():
        data = core.trusted_read(path, owner=owner)
        core.require(core.sha(data) == manifest['old_files'][str(path)], 'retired_v1_pin_mismatch')
        originals[path] = data
    pinned = originals[OLD_INSTALL/'manifest.json']
    core.require(core.sha(pinned) == OLD_MANIFEST_SHA256, 'retired_v1_manifest_changed')
    old_manifest = core.document(pinned)
    for key, path in OLD_NEW.items():
        core.require(core.sha(originals[path]) == old_manifest['new_files'][key]
            and originals[path] == originals[OLD_INSTALL/'payload'/key], 'retired_v1_payload_changed')
    return originals


def runtime_guards(manifest, host):
    core.require(datetime.now(timezone.utc) < INSTALL_DEADLINE, 'installation_deadline_passed')
    retirement_guards(host)
    # v1's retired=True branch requires the shared bot drop-in to remain absent.
    core.runtime_guards(manifest, host, retired=True)


def prepare(expected, *, apply=False, host=None):
    core.require(datetime.now(timezone.utc) < INSTALL_DEADLINE, 'installation_deadline_passed')
    host = host or core.Host()
    manifest = core.load_manifest(expected)
    core.require(not core.BACKUP.exists() and not core.BACKUP.is_symlink(), 'preparation_already_attempted')
    originals = old_inputs(manifest)
    runtime_guards(manifest, host)
    core.check_destinations(host)
    core.check_disk_capacity(host)
    files = {key: core.trusted_read(core.INSTALL/'payload'/key) for key in core.NEW}
    core.validate_payload(manifest, files)
    if not apply:
        return dict(checked=True, applied=False, reason='payload_validated_pending_runtime_validation',
                    runtime_semantics_verified=False, memory_safety_verified=False)
    lock = os.open(core.LOCK, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    try:
        info = os.fstat(lock)
        core.require(stat.S_ISREG(info.st_mode) and info.st_uid == 1000 and info.st_nlink == 1
            and stat.S_IMODE(info.st_mode) == 0o644, 'unsafe_deployment_lock')
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        runtime_guards(manifest, host)
        originals = old_inputs(manifest)
        core.check_destinations(host)
        core.check_disk_capacity(host)
        # The exclusive directory is the permanent attempt marker even if its receipt fails.
        core.mkdir_new(core.BACKUP)
        try:
            core.write_new(core.BACKUP/'attempt.json', core.raw(dict(status='attempt_consumed', manifest_sha256=expected)))
            for index, data in enumerate(originals.values()):
                core.write_new(core.BACKUP/f'original-{index}', data)
            core.write_new(core.BACKUP/'index.json', core.raw({str(path): dict(file=f'original-{index}', sha256=core.sha(data))
                for index, (path, data) in enumerate(originals.items())}))
            for path, owner in ((core.INPUT, 1000), (core.ENGINE_STATE, 1000), (core.STATE, 0)):
                core.mkdir_new(path, owner)
            for key, path in core.NEW.items():
                owner = 1000 if key in ('study', 'engine_manifest', 'once') else 0
                mode = 0o644 if key in ('executor', 'activation_service', 'activation_timer') else 0o600
                core.write_new(path, files[key], mode, owner)
            host.static()
            runtime_guards(manifest, host)
            for key, path in core.NEW.items():
                core.require(core.trusted_read(path, owner=1000 if key in ('study', 'engine_manifest', 'once') else 0)
                    == files[key], 'installed_input_changed')
            old_inputs(manifest)
            host.run(['/usr/bin/systemctl', 'daemon-reload'])
            runtime_guards(manifest, host)
            core.verify_loaded_units(host)
            core.require(host.show(core.ACT_TIMER, 'Unit') == core.ACT_SERVICE, 'new_timer_target_mismatch')
            host.static()
            runtime_guards(manifest, host)
            core.check_disk_capacity(host)
            old_inputs(manifest)
            host.run(['/usr/bin/systemctl', 'enable', '--now', core.ACT_TIMER])
            runtime_guards(manifest, host)
            old_inputs(manifest)
            core.require(host.show(core.ACT_TIMER, 'ActiveState') == 'active', 'timer_not_armed')
            core.verify_next_elapse(host.show(core.ACT_TIMER, 'NextElapseUSecRealtime'))
            core.write_new(core.BACKUP/'complete.json', core.raw(dict(status='complete', manifest_sha256=expected)))
        except BaseException:
            disarmed = False
            try:
                host.run(['/usr/bin/systemctl', 'disable', '--now', core.ACT_TIMER])
                disarmed = (host.show(core.ACT_TIMER, 'ActiveState') == 'inactive'
                            and host.show(core.ACT_TIMER, 'UnitFileState') == 'disabled')
            except BaseException:
                pass
            try:
                core.write_new(core.BACKUP/'failed.json', core.raw(dict(status='operator_review_required', new_timer_disarmed=disarmed)))
            except BaseException:
                pass
            raise core.GuardError('preparation_partial_disarmed' if disarmed else 'preparation_disarm_failed') from None
    finally:
        os.close(lock)
    return dict(checked=True, applied=True, reason='future_timer_armed', bot_restarted=False,
                toss_started=False, runtime_semantics_verified=True, memory_safety_verified=False)


def main(argv=None):
    os.umask(0o077)
    sys.dont_write_bytecode = True
    parser = argparse.ArgumentParser(description=__doc__)
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument('--check', action='store_true')
    group.add_argument('--apply', action='store_true')
    parser.add_argument('--manifest-sha256', required=True)
    args = parser.parse_args(argv)
    try:
        core.require(os.geteuid() == 0 and sys.flags.isolated and not sys.flags.optimize,
                     'root_isolated_nonoptimized_required')
        result = prepare(args.manifest_sha256, apply=args.apply)
    except Exception as exc:
        print(json.dumps(dict(ok=False, reason=str(exc) if isinstance(exc, core.GuardError)
                              else 'preparation_io_or_validation_failed')))
        return 1
    print(json.dumps(result))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
