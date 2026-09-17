"""Private atomic status snapshots. Failure is fatal, never a success heartbeat."""
from __future__ import annotations

import json
import os
from pathlib import Path
import secrets
import stat


class StatusError(Exception):
    def __init__(self):
        super().__init__('status_unavailable')


class StatusWriter:
    def __init__(self, path, *, max_bytes=16384):
        self.path = Path(path)
        if not self.path.is_absolute() or '..' in self.path.parts or max_bytes != 16384:
            raise StatusError()
        self.max_bytes = max_bytes

    def write(self, document: dict):
        descriptors = []
        temp_name = None
        try:
            if type(document) is not dict:
                raise StatusError()
            raw = json.dumps(document, sort_keys=True, separators=(',', ':'),
                             ensure_ascii=True, allow_nan=False).encode() + b'\n'
            if len(raw) > self.max_bytes:
                raise StatusError()
            flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC
            parent = os.open('/', flags)
            descriptors.append((parent, None))
            for index, part in enumerate(self.path.parts[1:-1]):
                child = os.open(part, flags, dir_fd=parent)
                descriptors.append((child, part))
                info = os.fstat(child)
                if info.st_uid not in {0, os.getuid()}:
                    raise StatusError()
                last = index == len(self.path.parts[1:-1]) - 1
                if last:
                    if info.st_uid != os.getuid() or stat.S_IMODE(info.st_mode) != 0o700:
                        raise StatusError()
                elif info.st_mode & 0o022 and not (info.st_uid == 0 and info.st_mode & stat.S_ISVTX):
                    raise StatusError()
                parent = child

            def check_paths():
                for index, (fd, part) in enumerate(descriptors[1:], 1):
                    info = os.fstat(fd)
                    linked = os.stat(part, dir_fd=descriptors[index - 1][0], follow_symlinks=False)
                    if not stat.S_ISDIR(linked.st_mode) or (info.st_dev, info.st_ino) != (linked.st_dev, linked.st_ino):
                        raise StatusError()
                try:
                    target = os.stat(self.path.name, dir_fd=parent, follow_symlinks=False)
                except FileNotFoundError:
                    return
                if (not stat.S_ISREG(target.st_mode) or target.st_nlink != 1
                        or target.st_uid != os.getuid() or stat.S_IMODE(target.st_mode) != 0o600):
                    raise StatusError()

            check_paths()
            temp_name = '.status-' + secrets.token_hex(12)
            fd = os.open(temp_name, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_CLOEXEC,
                         0o600, dir_fd=parent)
            try:
                remaining = memoryview(raw)
                while remaining:
                    written = os.write(fd, remaining)
                    if written <= 0:
                        raise StatusError()
                    remaining = remaining[written:]
                os.fsync(fd)
            finally:
                os.close(fd)
            check_paths()
            os.replace(temp_name, self.path.name, src_dir_fd=parent, dst_dir_fd=parent)
            temp_name = None
            os.fsync(parent)
            check_paths()
        except Exception:
            raise StatusError() from None
        finally:
            if temp_name is not None and descriptors:
                try:
                    os.unlink(temp_name, dir_fd=descriptors[-1][0])
                except OSError:
                    pass
            for fd, _ in reversed(descriptors):
                os.close(fd)
