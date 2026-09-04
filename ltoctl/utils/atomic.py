"""Crash-safe, same-directory file replacement helpers."""

from __future__ import annotations

import json
import os
import tempfile
from collections.abc import Iterable
from pathlib import Path
from typing import Any, BinaryIO, TextIO


def _fsync_directory(directory: Path) -> None:
    """Best-effort fsync of a directory after an atomic rename.

    Directory fsync is supported on Linux, which is the target platform.  A
    few filesystems/platforms reject opening a directory; the data file has
    still been fsynced in that case, so leave the portable fallback harmless.
    """

    try:
        fd = os.open(directory, os.O_RDONLY)
    except OSError:
        return
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def _temporary_path(path: Path) -> tuple[int, Path]:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, name = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=path.parent)
    return fd, Path(name)


def atomic_write_bytes(path: str | os.PathLike[str], data: bytes, *, mode: int | None = None) -> Path:
    """Write *data* and replace *path* atomically.

    The temporary file is created next to the destination, flushed and
    fsynced before ``os.replace``.  On failure it is removed and the previous
    destination remains untouched.
    """

    destination = Path(path)
    fd, temporary = _temporary_path(destination)
    try:
        with os.fdopen(fd, "wb") as stream:
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        if mode is not None:
            os.chmod(temporary, mode)
        os.replace(temporary, destination)
        _fsync_directory(destination.parent)
    except BaseException:
        try:
            temporary.unlink(missing_ok=True)
        except OSError:
            pass
        raise
    return destination


def atomic_write_text(
    path: str | os.PathLike[str],
    text: str,
    *,
    encoding: str = "utf-8",
    mode: int | None = None,
) -> Path:
    return atomic_write_bytes(Path(path), text.encode(encoding), mode=mode)


def atomic_write_json(
    path: str | os.PathLike[str],
    value: Any,
    *,
    indent: int = 2,
    encoding: str = "utf-8",
    mode: int | None = None,
) -> Path:
    text = json.dumps(value, ensure_ascii=False, indent=indent, sort_keys=True)
    text += "\n"
    return atomic_write_text(path, text, encoding=encoding, mode=mode)


def atomic_write_stream(
    path: str | os.PathLike[str],
    chunks: Iterable[str | bytes],
    *,
    binary: bool = False,
    encoding: str = "utf-8",
    mode: int | None = None,
) -> Path:
    """Atomically write an iterable without collecting it in memory."""

    destination = Path(path)
    fd, temporary = _temporary_path(destination)
    try:
        if binary:
            with os.fdopen(fd, "wb") as stream:
                for chunk in chunks:
                    if isinstance(chunk, str):
                        chunk = chunk.encode(encoding)
                    stream.write(chunk)
                stream.flush()
                os.fsync(stream.fileno())
        else:
            with os.fdopen(fd, "w", encoding=encoding, newline="") as stream:
                for chunk in chunks:
                    if isinstance(chunk, bytes):
                        chunk = chunk.decode(encoding)
                    stream.write(chunk)
                stream.flush()
                os.fsync(stream.fileno())
        if mode is not None:
            os.chmod(temporary, mode)
        os.replace(temporary, destination)
        _fsync_directory(destination.parent)
    except BaseException:
        try:
            temporary.unlink(missing_ok=True)
        except OSError:
            pass
        raise
    return destination


def open_binary_for_fsync(path: str | os.PathLike[str]) -> BinaryIO:
    """Open a file for callers that need to append and then fsync manually."""

    return open(path, "wb")


def fsync_stream(stream: TextIO | BinaryIO) -> None:
    stream.flush()
    os.fsync(stream.fileno())
