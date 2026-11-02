"""Linux file primitives for pinned directories and atomic inode history.

Path.resolve()/Path.replace() cannot protect a check/replace window against WebDAV
writers. Keep descriptor-relative operations here; domain code decides the scope.
"""

from __future__ import annotations

import ctypes
import errno
import os
import stat

from contextlib import contextmanager
from pathlib import Path
from typing import Iterator


class FileChangedError(OSError):
    """A regular file changed while it was being read."""


class AtomicRenameUnavailable(OSError):
    """The platform/filesystem cannot exchange or move files atomically."""


def child_directory(parent: int, name: str, *, create: bool = False) -> int:
    if create:
        try:
            os.mkdir(name, mode=0o700, dir_fd=parent)
        except FileExistsError:
            pass
    return os.open(name, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=parent)


def open_directory(path: Path, *, create: bool = False) -> int:
    if not path.is_absolute() or ".." in path.parts:
        raise ValueError("Directory must be an absolute path without '..'")
    descriptor = os.open(path.anchor, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        for part in path.parts[1:]:
            child = child_directory(descriptor, part, create=create)
            os.close(descriptor)
            descriptor = child
        return descriptor
    except BaseException:
        os.close(descriptor)
        raise


@contextmanager
def parent_directory(root: int, parts: tuple[str, ...]) -> Iterator[tuple[int, str]]:
    descriptor = os.dup(root)
    try:
        for part in parts[:-1]:
            child = child_directory(descriptor, part)
            os.close(descriptor)
            descriptor = child
        yield descriptor, parts[-1]
    finally:
        os.close(descriptor)


def read_regular(parent: int, name: str, max_bytes: int) -> tuple[bytes, os.stat_result]:
    descriptor = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=parent)
    try:
        before = os.fstat(descriptor)
        if not stat.S_ISREG(before.st_mode) or before.st_nlink != 1 or before.st_size > max_bytes:
            raise ValueError("Expected a regular, non-linked file within the size limit")
        chunks, remaining = [], max_bytes + 1
        while remaining:
            chunk = os.read(descriptor, min(remaining, 16_384))
            if not chunk:
                break
            chunks.append(chunk)
            remaining -= len(chunk)
        raw = b"".join(chunks)
        after = os.fstat(descriptor)
        if (before.st_mtime_ns, before.st_ctime_ns, before.st_size) != (after.st_mtime_ns, after.st_ctime_ns, after.st_size):
            raise FileChangedError("File is changing; retry after synchronization")
        if len(raw) > max_bytes:
            raise ValueError("File exceeds the size limit")
        return raw, after
    finally:
        os.close(descriptor)


def write_new(parent: int, name: str, raw: bytes) -> None:
    descriptor = os.open(name, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600, dir_fd=parent)
    with os.fdopen(descriptor, "wb") as stream:
        stream.write(raw)
        stream.flush()
        os.fsync(stream.fileno())
    os.fsync(parent)


class AtomicRenamer:
    """Exchange retains the displaced inode, even if an external writer holds it."""

    def __init__(self) -> None:
        try:
            self._rename = ctypes.CDLL(None, use_errno=True).renameat2
            self._rename.argtypes = [ctypes.c_int, ctypes.c_char_p, ctypes.c_int, ctypes.c_char_p, ctypes.c_uint]
            self._rename.restype = ctypes.c_int
        except AttributeError as exc:
            raise AtomicRenameUnavailable("Linux renameat2 is required") from exc

    def move(self, source: tuple[int, str], target: tuple[int, str], *, exchange: bool = False) -> None:
        source_fd, source_name = source
        target_fd, target_name = target
        if self._rename(source_fd, os.fsencode(source_name), target_fd, os.fsencode(target_name), 2 if exchange else 1) != 0:
            number = ctypes.get_errno()
            error = AtomicRenameUnavailable if number in (errno.ENOSYS, errno.EINVAL, errno.EOPNOTSUPP, errno.EXDEV) else OSError
            raise error(number, os.strerror(number))
        os.fsync(source_fd)
        if target_fd != source_fd:
            os.fsync(target_fd)
