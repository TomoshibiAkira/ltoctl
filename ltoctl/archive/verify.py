"""Exact streaming tar-stream verification for archives and tapes."""

from __future__ import annotations

import tempfile
from dataclasses import dataclass
from pathlib import Path

from ..catalog.models import ArchiveRecord
from ..catalog.store import CatalogStore
from ..errors import ArchiveError, CatalogError, VerificationError
from ..tape.header import open_backend_file
from ..tape.safety import TapeSafetyService
from .reader import inspect_archive_stream, resolve_archive


@dataclass(frozen=True)
class VerificationResult:
    archive_uuid: str
    archive_name: str
    tape_id: str
    tape_file_no: int
    ok: bool
    expected_sha256: str | None
    actual_sha256: str | None
    tar_bytes: int = 0
    error: str | None = None

    def to_dict(self) -> dict[str, object]:
        return {
            "archive_uuid": self.archive_uuid,
            "archive_name": self.archive_name,
            "tape_id": self.tape_id,
            "tape_file_no": self.tape_file_no,
            "ok": self.ok,
            "expected_sha256": self.expected_sha256,
            "actual_sha256": self.actual_sha256,
            "tar_bytes": self.tar_bytes,
            "error": self.error,
        }


def verify_archive(
    store: CatalogStore,
    backend,
    reference: str | ArchiveRecord,
) -> VerificationResult:
    record = reference if isinstance(reference, ArchiveRecord) else resolve_archive(store, reference)
    loaded = TapeSafetyService(store).identify_loaded(backend)
    if loaded.catalog.tape_id != record.tape_id or loaded.catalog.uuid != record.tape_uuid:
        raise VerificationError("loaded tape does not match archive catalog identity")
    backend.seek_file(record.tape_file_no)
    stream = open_backend_file(backend)
    inspection = None
    try:
        with tempfile.TemporaryDirectory(prefix="ltoctl-verify-") as temporary:
            try:
                inspection = inspect_archive_stream(stream, manifest_path=Path(temporary) / "manifest.jsonl")
            except ArchiveError as exc:
                return VerificationResult(
                    archive_uuid=record.archive_uuid,
                    archive_name=record.name,
                    tape_id=record.tape_id,
                    tape_file_no=record.tape_file_no,
                    ok=False,
                    expected_sha256=record.tar_stream_sha256,
                    actual_sha256=None,
                    error=str(exc),
                )
            errors: list[str] = []
            if record.tar_stream_sha256 is None:
                errors.append("catalog archive has no tar_stream_sha256")
            elif inspection.sha256 != record.tar_stream_sha256:
                errors.append("tar stream hash mismatch")
            descriptor = inspection.archive
            if descriptor.archive_uuid != record.archive_uuid:
                errors.append("embedded archive UUID mismatch")
            if descriptor.tape_id != record.tape_id or descriptor.tape_uuid != record.tape_uuid:
                errors.append("embedded tape identity mismatch")
            if descriptor.tape_file_no != record.tape_file_no:
                errors.append("embedded physical tape file number mismatch")
            if descriptor.logical_size_bytes != record.logical_size_bytes:
                errors.append("embedded logical size mismatch")
            if descriptor.file_count != record.file_count:
                errors.append("embedded file count mismatch")
            return VerificationResult(
                archive_uuid=record.archive_uuid,
                archive_name=record.name,
                tape_id=record.tape_id,
                tape_file_no=record.tape_file_no,
                ok=not errors,
                expected_sha256=record.tar_stream_sha256,
                actual_sha256=inspection.sha256,
                tar_bytes=inspection.tar_bytes,
                error="; ".join(errors) if errors else None,
            )
    finally:
        try:
            stream.close()
        except OSError:
            pass
        if inspection is not None:
            inspection.close()


def verify_tape(store: CatalogStore, backend, tape_reference: str) -> list[VerificationResult]:
    tape = store.find_tape(tape_reference)
    loaded = TapeSafetyService(store).identify_loaded(backend)
    if loaded.catalog.tape_id != tape.tape_id or loaded.catalog.uuid != tape.uuid:
        raise VerificationError("loaded tape does not match requested tape")
    try:
        backend.seek_eod()
        eod = backend.current_file_no()
    except Exception as exc:
        raise VerificationError(f"cannot establish tape EOD: {exc}") from exc
    expected_eod = len(tape.archives) + 1
    if eod != expected_eod:
        raise VerificationError(
            f"physical EOD {eod} does not match catalog expected next file {expected_eod}; "
            "there may be an un-cataloged physical file"
        )
    archives = []
    seen_files: set[int] = set()
    for archive_uuid in tape.archives:
        try:
            archive = store.load_archive(archive_uuid)
        except CatalogError as exc:
            raise VerificationError(f"tape references unreadable archive {archive_uuid}: {exc}") from exc
        if archive.tape_file_no in seen_files:
            raise VerificationError(f"duplicate catalog physical file {archive.tape_file_no}")
        seen_files.add(archive.tape_file_no)
        if archive.status == "active":
            archives.append(archive)
    if sorted(seen_files) != list(range(1, len(tape.archives) + 1)):
        raise VerificationError("catalog archive file references are not contiguous from file 1")
    archives.sort(key=lambda archive: archive.tape_file_no)
    return [verify_archive(store, backend, archive) for archive in archives]


def verify_archive_record(store: CatalogStore, backend, record: ArchiveRecord) -> VerificationResult:
    return verify_archive(store, backend, record)
