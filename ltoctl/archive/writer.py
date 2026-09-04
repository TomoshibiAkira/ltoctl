"""Streaming ordinary-tar archive writer with journaled safety transitions."""

from __future__ import annotations

import hashlib
import io
import json
import os
import tarfile
import tempfile
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import BinaryIO, Callable, Iterable
from uuid import uuid4

from ..catalog.models import ArchiveRecord, OperationRecord
from ..catalog.store import CatalogStore
from ..errors import ArchiveError, CatalogError, SafetyError, ScanError, TapeError
from ..tape.safety import LoadedTape, TapeSafetyService
from .manifest import iter_manifest_file
from .scanner import ArchiveScanner, ScanResult

ARCHIVE_DESCRIPTOR_MEMBER = "__LTOCTL__/archive.json"
ARCHIVE_MANIFEST_MEMBER = "__LTOCTL__/manifest.jsonl"
METADATA_ROOT = "__LTOCTL__"


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


class HashingWriter:
    """A write-only pump that hashes exactly bytes accepted by the backend."""

    def __init__(self, target: BinaryIO):
        self.target = target
        self.hasher = hashlib.sha256()
        self.bytes_written = 0

    def write(self, data: bytes) -> int:
        if not isinstance(data, bytes):
            raise TypeError("tar writer supplied a non-bytes chunk")
        count = self.target.write(data)
        if count is None:
            count = len(data)
        if count != len(data):
            raise OSError(f"short tape write: accepted {count} of {len(data)} bytes")
        self.hasher.update(data)
        self.bytes_written += count
        return count

    def flush(self) -> None:
        flush = getattr(self.target, "flush", None)
        if flush is not None:
            flush()

    def tell(self) -> int:
        return self.bytes_written

    def digest(self) -> str:
        return self.hasher.hexdigest()


@dataclass(frozen=True)
class ArchiveWriteResult:
    archive: ArchiveRecord
    operation_uuid: str
    tar_bytes: int


def _descriptor(record: ArchiveRecord) -> bytes:
    value = record.to_dict()
    # The exact tar stream hash cannot be known until this descriptor and all
    # payload bytes have been written.  It therefore belongs only externally.
    value.pop("tar_stream_sha256", None)
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")


def _add_metadata(archive: tarfile.TarFile, name: str, payload: bytes) -> None:
    member = tarfile.TarInfo(name)
    member.size = len(payload)
    member.mtime = 0
    member.mode = 0o600
    archive.addfile(member, io.BytesIO(payload))


def _source_basename(path: str | os.PathLike[str]) -> str:
    value = Path(path).name or Path(path).anchor.strip("/") or "root"
    return value


class ArchiveWriter:
    """Create one physical tape-file archive after all safety checks pass."""

    def __init__(self, store: CatalogStore, backend, *, safety: TapeSafetyService | None = None):
        self.store = store
        self.backend = backend
        self.safety = safety or TapeSafetyService(store)

    def _check_capacity(self, tape, logical_size: int) -> None:
        if tape.recommended_capacity_bytes <= 0:
            return
        used = 0
        for archive_uuid in tape.archives:
            archive = self.store.load_archive(archive_uuid)
            # Physical tape space is append-only.  Obsolete/unverified/
            # corrupt catalog status does not reclaim bytes already written.
            used += archive.logical_size_bytes
        if used + logical_size > tape.recommended_capacity_bytes:
            raise SafetyError(
                f"archive logical size {logical_size} exceeds tape budget: "
                f"used={used}, budget={tape.recommended_capacity_bytes}"
            )

    @staticmethod
    def _validate_name(name: str) -> None:
        if not isinstance(name, str) or not name.strip():
            raise ArchiveError("archive name must be non-empty")
        if name in {".", "..", METADATA_ROOT} or "/" in name or "\\" in name or "\x00" in name:
            raise ArchiveError(f"archive name is invalid or reserved: {name!r}")

    def add(
        self,
        source_paths: Iterable[str | os.PathLike[str]],
        *,
        name: str,
        archive_uuid: str | None = None,
        before_write: Callable[[], None] | None = None,
    ) -> ArchiveRecord:
        paths = [str(path) for path in source_paths]
        self._validate_name(name)
        if not paths:
            raise ArchiveError("at least one source path is required")
        if any(existing.name == name for existing in self.store.iter_archives()):
            raise ArchiveError(f"archive name already exists: {name!r}")

        archive_uuid = archive_uuid or str(uuid4())
        try:
            self.store._component(archive_uuid)
        except CatalogError as exc:
            raise ArchiveError(f"invalid archive UUID {archive_uuid!r}") from exc
        try:
            self.store.load_archive(archive_uuid)
        except CatalogError:
            # The store currently reports a missing archive and malformed
            # archive JSON through the same domain exception.  A direct path
            # check distinguishes the safe missing case without swallowing a
            # corrupted existing record.
            archive_path = self.store.root / "archives" / f"{archive_uuid}.json"
            if archive_path.exists():
                raise ArchiveError(f"cannot reserve archive UUID {archive_uuid!r}: existing record is invalid")
        else:
            raise ArchiveError(f"archive UUID already exists: {archive_uuid!r}")
        with tempfile.TemporaryDirectory(prefix="ltoctl-manifest-") as temporary:
            manifest_path = Path(temporary) / "manifest.jsonl"
            scan = ArchiveScanner().scan(paths, manifest_path=manifest_path)
            loaded = self.safety.assert_append_ready(self.backend)
            self._check_capacity(loaded.catalog, scan.logical_size_bytes)
            created_at = _now()
            archive = ArchiveRecord(
                archive_uuid=archive_uuid,
                name=name,
                tape_id=loaded.catalog.tape_id,
                tape_uuid=loaded.catalog.uuid,
                tape_file_no=loaded.next_file_no or 0,
                created_at=created_at,
                source_paths=paths,
                logical_size_bytes=scan.logical_size_bytes,
                file_count=scan.file_count,
                tar_stream_sha256=None,
                status="active",
            )
            # Revalidate immediately before journal creation.  This check is
            # deliberately pre-write and pre-journal: a known source change
            # cannot leave a failed operation that later makes a healthy tape
            # look like it needs recovery.
            if before_write is not None:
                before_write()
            try:
                current = ArchiveScanner().scan(paths)
            except ScanError as exc:
                raise ArchiveError(f"source changed or became unreadable before journal creation: {exc}") from exc
            if (
                current.logical_size_bytes != scan.logical_size_bytes
                or current.file_count != scan.file_count
                or current.snapshot_fingerprint != scan.snapshot_fingerprint
            ):
                raise ArchiveError(
                    "source changed before journal creation; refusing to write (regenerate the archive)"
                )
            operation = OperationRecord.new(
                loaded.catalog.tape_id,
                loaded.catalog.uuid,
                archive_uuid=archive.archive_uuid,
                archive_name=archive.name,
                expected_tape_file_no=archive.tape_file_no,
            )
            self.store.save_operation(operation)
            tar_bytes = 0
            try:
                operation.state = "writing"
                operation.updated_at = _now()
                self.store.save_operation(operation)
                tape_stream = self.backend.write_tape_file()
                pump = HashingWriter(tape_stream)
                with tarfile.open(
                    fileobj=pump,
                    mode="w|",
                    format=tarfile.PAX_FORMAT,
                    # The scanner records hard-linked paths as independent
                    # regular files.  Dereferencing keeps the resulting tar
                    # ordinary and makes each manifest entry self-contained.
                    dereference=True,
                ) as tar:
                    _add_metadata(tar, ARCHIVE_DESCRIPTOR_MEMBER, _descriptor(archive))
                    manifest_size = manifest_path.stat().st_size
                    manifest_info = tarfile.TarInfo(ARCHIVE_MANIFEST_MEMBER)
                    manifest_info.size = manifest_size
                    manifest_info.mtime = 0
                    manifest_info.mode = 0o600
                    with manifest_path.open("rb") as manifest_stream:
                        tar.addfile(manifest_info, manifest_stream)
                    # Emit payload in exactly the scanner's lexical manifest
                    # order.  This makes the stream self-auditing: reconcile
                    # can compare every TarInfo with the preceding manifest
                    # entry without materializing either side.
                    roots = {_source_basename(path): Path(path) for path in paths}
                    for entry in iter_manifest_file(manifest_path):
                        root, separator, suffix = entry.path.partition("/")
                        source_root = roots.get(root)
                        if source_root is None:
                            raise ArchiveError(f"manifest path has unknown source root: {entry.path!r}")
                        payload_path = source_root / suffix if separator else source_root
                        # Dereference regular files so hard-linked paths are
                        # independent ordinary payload files, but preserve
                        # symlinks exactly as the scanner recorded them.
                        tar.dereference = entry.type != "symlink"
                        tar.add(payload_path, arcname=entry.path, recursive=False)
                # Detect source churn during tar generation before emitting a
                # physical file boundary.  The backend remains in its
                # ambiguous write state and the journal blocks future writes.
                after = ArchiveScanner().scan(paths)
                if (
                    after.logical_size_bytes != scan.logical_size_bytes
                    or after.file_count != scan.file_count
                    or after.snapshot_fingerprint != scan.snapshot_fingerprint
                ):
                    raise ArchiveError("source changed while archive was being generated; tape write is unresolved")
                tar_bytes = pump.bytes_written
                archive.tar_stream_sha256 = pump.digest()
                self.backend.finish_tape_file()
                operation.state = "tape_write_finished"
                operation.updated_at = _now()
                self.store.save_operation(operation)

                # Canonical files are installed only after the physical file
                # is complete.  If any one commit fails, leave
                # tape_write_finished for reconcile rather than rewriting tape.
                self.store.save_manifest(archive.archive_uuid, iter_manifest_file(manifest_path))
                self.store.save_archive(archive)
                updated_tape = self.store.load_tape(loaded.catalog.tape_id)
                if updated_tape.uuid != loaded.catalog.uuid:
                    raise SafetyError("catalog tape UUID changed during archive commit")
                if archive.archive_uuid not in updated_tape.archives:
                    updated_tape.archives.append(archive.archive_uuid)
                self.store.save_tape(updated_tape)
                operation.state = "catalog_committed"
                operation.updated_at = _now()
                operation.error = None
                self.store.save_operation(operation)
                return archive
            except BaseException as exc:
                if operation.state != "catalog_committed":
                    try:
                        if operation.state == "tape_write_finished":
                            operation.error = str(exc)
                            operation.updated_at = _now()
                            self.store.save_operation(operation)
                        elif operation.state in {"prepared", "writing"}:
                            operation.state = "failed"
                            operation.error = str(exc)
                            operation.updated_at = _now()
                            self.store.save_operation(operation)
                    except BaseException:
                        pass
                raise

    # A small compatibility spelling for callers that describe the operation
    # as a physical write rather than an archive append.
    write = add


def add_archive(
    store: CatalogStore,
    backend,
    source_paths: Iterable[str | os.PathLike[str]],
    *,
    name: str,
    archive_uuid: str | None = None,
    before_write: Callable[[], None] | None = None,
) -> ArchiveRecord:
    return ArchiveWriter(store, backend).add(
        source_paths,
        name=name,
        archive_uuid=archive_uuid,
        before_write=before_write,
    )
