"""Bounded-memory source tree scanner and manifest generation."""

from __future__ import annotations

import os
import hashlib
import posixpath
import stat as stat_module
from collections.abc import Iterable, Iterator
from dataclasses import dataclass
from pathlib import Path

from ..catalog.models import ManifestEntry
from ..errors import ScanError
from ..utils.atomic import atomic_write_stream
from .manifest import manifest_line

RESERVED_ROOT = "__LTOCTL__"


@dataclass
class ScanResult:
    source_paths: list[str]
    logical_size_bytes: int
    file_count: int
    entry_count: int
    manifest_path: Path | None = None
    snapshot_fingerprint: str = ""

    @property
    def total_size_bytes(self) -> int:
        return self.logical_size_bytes

    @property
    def size(self) -> int:
        return self.logical_size_bytes

    @property
    def files(self) -> int:
        return self.file_count


@dataclass(frozen=True)
class _Source:
    path: Path
    display_path: str
    root_name: str


def _absolute_path(value: str | os.PathLike[str]) -> Path:
    path = Path(value).expanduser()
    if not path.is_absolute():
        path = Path.cwd() / path
    return path


def _prepare_sources(paths: Iterable[str | os.PathLike[str]]) -> list[_Source]:
    sources: list[_Source] = []
    seen_roots: set[str] = set()
    for raw in paths:
        path = _absolute_path(raw)
        try:
            stat = path.lstat()
        except FileNotFoundError as exc:
            raise ScanError(f"source does not exist: {path}") from exc
        except OSError as exc:
            raise ScanError(f"cannot inspect source {path}: {exc}") from exc
        # ``name`` is empty for an accidental root path; use a stable label so
        # it cannot create an empty tar member.
        root_name = path.name or path.anchor.strip("/") or "root"
        if root_name == RESERVED_ROOT:
            raise ScanError(f"source basename {RESERVED_ROOT!r} is reserved for archive metadata")
        if root_name in seen_roots:
            raise ScanError(f"source basenames must be unique; duplicate {root_name!r}")
        seen_roots.add(root_name)
        sources.append(_Source(path=path, display_path=str(path), root_name=root_name))
    if not sources:
        raise ScanError("at least one source path is required")
    # The caller's source order is retained in the result/catalog metadata;
    # the scanner sorts a separate view below so manifest order is stable even
    # when multiple roots are supplied in a different CLI order.
    return sources


def _entry_for(path: Path, relative: str, stat: os.stat_result) -> ManifestEntry:
    mode = stat.st_mode
    if os.path.islink(path):
        try:
            target = os.readlink(path)
        except OSError as exc:
            raise ScanError(f"cannot read symlink {path}: {exc}") from exc
        if target.startswith("/") or target.startswith("\\"):
            raise ScanError(f"symlink target escapes archive root: {path} -> {target}")
        resolved_target = posixpath.normpath(posixpath.join(posixpath.dirname(relative), target))
        if resolved_target == ".." or resolved_target.startswith("../"):
            raise ScanError(f"symlink target escapes archive root: {path} -> {target}")
        # A tar symlink has no payload bytes.  ``lstat().st_size`` is the
        # length of the link target on most Unix filesystems, not archived
        # logical data, so keep the manifest accounting aligned with TarInfo.
        return ManifestEntry(path=relative, size=0, mtime_ns=stat.st_mtime_ns, type="symlink", link_target=target)
    if os.path.isdir(path):
        return ManifestEntry(path=relative, size=0, mtime_ns=stat.st_mtime_ns, type="dir")
    if os.path.isfile(path):
        return ManifestEntry(path=relative, size=stat.st_size, mtime_ns=stat.st_mtime_ns, type="file")
    # Device nodes, FIFOs and sockets cannot be represented as safe ordinary
    # tar payloads.  Refuse them at scan time instead of generating an archive
    # that verify accepts but restore cannot safely materialize.
    if not (
        stat_module.S_ISREG(stat.st_mode)
        or stat_module.S_ISDIR(stat.st_mode)
        or stat_module.S_ISLNK(stat.st_mode)
    ):
        raise ScanError(f"unsupported special file type: {path}")
    raise ScanError(f"cannot classify source path: {path}")


def _walk_source(source: _Source) -> Iterator[ManifestEntry]:
    """Yield one source in stable lexical preorder using an explicit stack.

    A previous breadth-first implementation emitted every child in a
    directory before descending, which made ``root/z`` appear before
    ``root/x``.  Pushing children in reverse and processing one node at a time
    gives the normal dictionary order ``root``, ``root/x``, ``root/z``, ...
    while retaining bounded memory.
    """

    # Stack entries are paths and their already-normalized manifest names.
    # os.scandir keeps memory bounded by a single directory's entries plus the
    # directory depth, not the complete tree.
    stack: list[tuple[Path, str]] = [(source.path, source.root_name)]
    while stack:
        directory, prefix = stack.pop()
        try:
            stat = directory.lstat()
        except OSError as exc:
            raise ScanError(f"cannot inspect source {directory}: {exc}") from exc
        entry = _entry_for(directory, prefix, stat)
        yield entry
        if entry.type != "dir":
            continue
        try:
            entries = list(os.scandir(directory))
        except OSError as exc:
            raise ScanError(f"cannot scan directory {directory}: {exc}") from exc
        entries.sort(key=lambda item: item.name.encode("utf-8"), reverse=True)
        for directory_entry in entries:
            child_path = Path(directory_entry.path)
            child_name = f"{prefix}/{directory_entry.name}"
            try:
                # lstat here only detects a deletion/race before queuing; the
                # authoritative stat occurs when the item is popped.
                child_path.lstat()
            except OSError as exc:
                raise ScanError(f"cannot stat {child_path}: {exc}") from exc
            stack.append((child_path, child_name))


class ArchiveScanner:
    """Scan source paths and optionally atomically generate a JSONL manifest."""

    def scan(
        self,
        source_paths: Iterable[str | os.PathLike[str]],
        *,
        manifest_path: str | os.PathLike[str] | None = None,
    ) -> ScanResult:
        sources = _prepare_sources(source_paths)
        total_size = 0
        file_count = 0
        entry_count = 0
        snapshot_hasher = hashlib.sha256()

        ordered_sources = sorted(sources, key=lambda source: source.root_name.encode("utf-8"))

        def entries() -> Iterator[str]:
            nonlocal total_size, file_count, entry_count
            for source in ordered_sources:
                for entry in _walk_source(source):
                    entry_count += 1
                    if entry.type == "file":
                        total_size += entry.size
                        file_count += 1
                    line = manifest_line(entry)
                    snapshot_hasher.update(line.encode("utf-8"))
                    yield line

        destination = Path(manifest_path) if manifest_path is not None else None
        if destination is not None:
            atomic_write_stream(destination, entries())
        else:
            for _ in entries():
                pass
        return ScanResult(
            source_paths=[source.display_path for source in sources],
            logical_size_bytes=total_size,
            file_count=file_count,
            entry_count=entry_count,
            manifest_path=destination,
            snapshot_fingerprint=snapshot_hasher.hexdigest(),
        )


def scan_sources(
    source_paths: Iterable[str | os.PathLike[str]],
    *,
    manifest_path: str | os.PathLike[str] | None = None,
) -> ScanResult:
    return ArchiveScanner().scan(source_paths, manifest_path=manifest_path)
