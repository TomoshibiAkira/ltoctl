"""Idempotent recovery of interrupted tape writes."""

from __future__ import annotations

import tempfile
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

from ..catalog.models import ArchiveRecord, OperationRecord, TapeRecord
from ..catalog.store import CatalogStore
from ..errors import ArchiveError, CatalogError, ReconcileError, SafetyError, TapeError
from ..tape.header import open_backend_file, read_tape_header
from .reader import ArchiveInspection, inspect_archive_stream


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


@dataclass
class ReconcileResult:
    reconciled_operations: list[str] = field(default_factory=list)
    needs_recovery: bool = False
    messages: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not self.needs_recovery

    def to_dict(self) -> dict[str, object]:
        return {
            "ok": self.ok,
            "reconciled_operations": list(self.reconciled_operations),
            "needs_recovery": self.needs_recovery,
            "messages": list(self.messages),
        }


def _save_operation(store: CatalogStore, operation: OperationRecord, *, error: str | None = None) -> None:
    operation.error = error
    operation.updated_at = _now()
    store.save_operation(operation)


def _mark_tape_recovery(store: CatalogStore, tape_id: str, result: ReconcileResult, message: str) -> None:
    result.needs_recovery = True
    result.messages.append(message)
    try:
        tape = store.find_tape(tape_id)
    except CatalogError:
        return
    if tape.status != "needs_recovery":
        tape.status = "needs_recovery"
        store.save_tape(tape)


def _catalog_layout(store: CatalogStore, tape: TapeRecord) -> list[int]:
    """Validate catalog references and return their physical file numbers."""

    numbers: list[int] = []
    seen_uuids: set[str] = set()
    for archive_uuid in tape.archives:
        if archive_uuid in seen_uuids:
            raise ReconcileError(f"catalog references archive UUID more than once: {archive_uuid}")
        seen_uuids.add(archive_uuid)
        try:
            archive = store.load_archive(archive_uuid)
        except CatalogError as exc:
            raise ReconcileError(f"catalog archive reference is unreadable: {archive_uuid}: {exc}") from exc
        if archive.tape_id != tape.tape_id or archive.tape_uuid != tape.uuid:
            raise ReconcileError(f"archive {archive_uuid} disagrees with tape identity")
        numbers.append(archive.tape_file_no)
    expected = list(range(1, len(numbers) + 1))
    if sorted(numbers) != expected:
        raise ReconcileError(f"catalog archive physical files are not contiguous from file 1: {numbers!r}")
    return numbers


def _physical_eod(backend) -> int:
    try:
        backend.seek_eod()
        value = backend.current_file_no()
    except Exception as exc:
        raise ReconcileError(f"cannot establish physical EOD: {exc}") from exc
    if value is None or value < 1:
        raise ReconcileError(f"backend returned unsafe physical EOD {value!r}")
    return value


def _commit_init(store: CatalogStore, operation: OperationRecord, header: TapeRecord) -> None:
    try:
        existing = store.find_tape(header.tape_id)
    except CatalogError:
        existing = None
    if existing is not None and existing.uuid != header.uuid:
        raise ReconcileError("catalog tape ID exists with a different UUID")
    store.save_tape(existing or header)
    operation.state = "catalog_committed"
    _save_operation(store, operation)


def _commit_archive(
    store: CatalogStore,
    operation: OperationRecord,
    inspection: ArchiveInspection,
) -> None:
    embedded = inspection.archive
    if operation.archive_uuid != embedded.archive_uuid:
        raise ReconcileError("archive descriptor UUID does not match operation journal")
    if operation.tape_id != embedded.tape_id or operation.tape_uuid != embedded.tape_uuid:
        raise ReconcileError("archive descriptor tape identity does not match operation journal")
    if operation.expected_tape_file_no != embedded.tape_file_no:
        raise ReconcileError("archive descriptor physical file number does not match operation journal")
    archive = ArchiveRecord(
        archive_uuid=embedded.archive_uuid,
        name=embedded.name,
        tape_id=embedded.tape_id,
        tape_uuid=embedded.tape_uuid,
        tape_file_no=embedded.tape_file_no,
        created_at=embedded.created_at,
        source_paths=embedded.source_paths,
        logical_size_bytes=embedded.logical_size_bytes,
        file_count=embedded.file_count,
        tar_stream_sha256=inspection.sha256,
        status=embedded.status,
    )
    try:
        existing = store.load_archive(archive.archive_uuid)
    except CatalogError:
        existing = None
    if existing is not None:
        if existing.tar_stream_sha256 not in {None, inspection.sha256}:
            raise ReconcileError("existing external archive record hash conflicts with tape stream")
        for field in ("tape_id", "tape_uuid", "tape_file_no", "name", "file_count", "logical_size_bytes"):
            if getattr(existing, field) != getattr(archive, field):
                raise ReconcileError(f"existing archive record field {field!r} conflicts with tape descriptor")
    store.save_manifest(archive.archive_uuid, inspection.iter_manifest())
    store.save_archive(archive)
    tape = store.find_tape(archive.tape_id)
    if tape.uuid != archive.tape_uuid:
        raise ReconcileError("catalog tape UUID conflicts with archive descriptor")
    if archive.archive_uuid not in tape.archives:
        tape.archives.append(archive.archive_uuid)
        store.save_tape(tape)
    operation.state = "catalog_committed"
    _save_operation(store, operation)


def _reconcile_operation(
    store: CatalogStore,
    backend,
    operation: OperationRecord,
    header: TapeRecord,
) -> None:
    if operation.tape_id != header.tape_id or operation.tape_uuid != header.uuid:
        raise SafetyError(
            f"loaded tape {header.tape_id}/{header.uuid} does not match operation "
            f"{operation.tape_id}/{operation.tape_uuid}"
        )
    try:
        catalog_tape = store.find_tape(header.tape_id)
    except CatalogError:
        catalog_tape = None
    # A tape-init operation is the only operation that can legitimately have
    # no catalog tape yet.  It must account for exactly one physical file.
    if operation.archive_uuid is None:
        if operation.expected_tape_file_no != 0:
            raise ReconcileError("tape-init operation does not target physical file 0")
        if catalog_tape is not None and catalog_tape.archives:
            raise ReconcileError("tape-init journal conflicts with already cataloged archive files")
        if _physical_eod(backend) != 1:
            raise ReconcileError("tape init cannot be proven: physical EOD is not immediately after file 0")
        _commit_init(store, operation, header)
        return

    if catalog_tape is None:
        raise ReconcileError("archive operation has no catalog tape record")
    numbers = _catalog_layout(store, catalog_tape)
    expected = operation.expected_tape_file_no
    if expected is None or expected < 1:
        raise ReconcileError("archive operation has no safe expected physical file number")
    if operation.state == "prepared":
        # A prepared archive operation is persisted before any tape stream is
        # opened.  If EOD is still exactly the expected next file, reconcile
        # can prove that no physical bytes were appended and terminate it.
        # The same EOD for a writing/tape_write_finished/failed operation is
        # intentionally *not* safe to infer: those states may have crossed an
        # unobservable device boundary.
        if expected != len(numbers) + 1:
            raise ReconcileError(
                f"prepared operation expected file {expected}, but catalog next file is {len(numbers) + 1}"
            )
        if _physical_eod(backend) != expected:
            raise ReconcileError(
                f"prepared operation cannot be aborted: physical EOD is not expected file {expected}"
            )
        operation.state = "aborted"
        _save_operation(store, operation)
        return
    # If the archive is already in the reverse tape relation, the journal
    # likely stopped after part of the catalog commit.  Otherwise it must be
    # precisely the next physical file.  In both cases unexplained physical
    # files beyond EOD are a hard recovery blocker.
    if expected in numbers:
        physical_eod = _physical_eod(backend)
        if physical_eod != len(numbers) + 1:
            raise ReconcileError(
                f"physical EOD {physical_eod} is beyond catalog expected next file {len(numbers) + 1}; "
                "an un-cataloged physical file is present"
            )
    else:
        if expected != len(numbers) + 1:
            raise ReconcileError(
                f"operation expected file {expected}, but catalog next file is {len(numbers) + 1}"
            )
        physical_eod = _physical_eod(backend)
        if physical_eod != expected + 1:
            raise ReconcileError(
                f"archive operation cannot be proven: physical EOD {physical_eod} is not immediately after "
                f"expected file {expected}"
            )
    backend.seek_file(expected)
    stream = open_backend_file(backend)
    inspection: ArchiveInspection | None = None
    try:
        with tempfile.TemporaryDirectory(prefix="ltoctl-reconcile-") as temporary:
            inspection = inspect_archive_stream(stream, manifest_path=Path(temporary) / "manifest.jsonl")
            _commit_archive(store, operation, inspection)
    finally:
        try:
            stream.close()
        except OSError:
            pass
        if inspection is not None:
            inspection.close()


def reconcile_tape(
    store: CatalogStore,
    backend,
    *,
    operation_uuid: str | None = None,
) -> ReconcileResult:
    """Reconcile all unresolved operations for the loaded tape idempotently."""

    result = ReconcileResult()
    try:
        status = backend.status()
        if not status.loaded:
            raise SafetyError(status.error or "no tape is loaded")
        header = read_tape_header(backend)
    except (TapeError, SafetyError) as exc:
        if operation_uuid is not None:
            try:
                operation = store.load_operation(operation_uuid)
            except CatalogError as load_exc:
                raise ReconcileError(f"unknown operation: {operation_uuid}") from load_exc
            _mark_tape_recovery(store, operation.tape_id, result, f"cannot read tape header: {exc}")
            _save_operation(store, operation, error=str(exc))
            return result
        raise ReconcileError(f"cannot read tape header: {exc}") from exc

    if operation_uuid is not None:
        operation = store.load_operation(operation_uuid)
        if operation.state in {"catalog_committed", "aborted"}:
            return result
        operations = [operation]
    else:
        operations = list(store.unresolved_operations(header.uuid))

    for operation in operations:
        try:
            _reconcile_operation(store, backend, operation, header)
            result.reconciled_operations.append(operation.operation_uuid)
        except (ArchiveError, TapeError, CatalogError, ReconcileError, SafetyError, OSError, ValueError) as exc:
            _mark_tape_recovery(
                store,
                operation.tape_id,
                result,
                f"operation {operation.operation_uuid} cannot be proven complete: {exc}",
            )
            try:
                _save_operation(store, operation, error=str(exc))
            except CatalogError:
                pass
    return result


def reconcile(store: CatalogStore, backend, *, operation_uuid: str | None = None) -> ReconcileResult:
    return reconcile_tape(store, backend, operation_uuid=operation_uuid)
