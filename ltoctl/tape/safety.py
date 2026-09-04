"""Loaded-tape identity and append precondition checks."""

from __future__ import annotations

from dataclasses import dataclass

from ..catalog.models import TapeRecord
from ..catalog.store import CatalogStore
from ..errors import SafetyError, TapeError
from .header import read_tape_header


@dataclass(frozen=True)
class LoadedTape:
    header: TapeRecord
    catalog: TapeRecord
    next_file_no: int | None = None


class TapeSafetyService:
    """Centralize identity checks so no writer can accidentally bypass them."""

    def __init__(self, store: CatalogStore):
        self.store = store

    def identify_loaded(self, backend, *, require_writable: bool = False) -> LoadedTape:
        status = backend.status()
        if not status.loaded:
            raise SafetyError(status.error or "no tape is loaded")
        if require_writable and not status.writable:
            raise SafetyError("tape is write protected")
        try:
            header = read_tape_header(backend)
        except TapeError as exc:
            raise SafetyError(f"cannot identify loaded tape: {exc}") from exc
        try:
            catalog = self.store.find_tape(header.tape_id)
        except Exception as exc:
            if isinstance(exc, SafetyError):
                raise
            raise SafetyError(f"unknown tape {header.tape_id!r}; initialize/import it before writing") from exc
        if catalog.tape_id != header.tape_id:
            raise SafetyError(f"tape ID mismatch: header={header.tape_id!r}, catalog={catalog.tape_id!r}")
        if catalog.uuid != header.uuid:
            raise SafetyError(
                f"tape UUID mismatch for {header.tape_id}: header={header.uuid}, catalog={catalog.uuid}"
            )
        return LoadedTape(header=header, catalog=catalog)

    def loaded_is_blank(self, backend) -> bool:
        """Return True only when physical EOD is file 0.

        Call this after header identification has already failed.  Seeking EOD
        on a used tape is expensive, so the happy path must keep using file 0.
        An unknown file number is not treated as blank.
        """

        try:
            backend.seek_eod()
            return backend.current_file_no() == 0
        except Exception:
            return False

    def assert_append_ready(self, backend) -> LoadedTape:
        """Verify identity/journal and establish a fresh physical EOD position."""

        loaded = self.identify_loaded(backend, require_writable=True)
        if loaded.catalog.status != "active":
            raise SafetyError(
                f"tape {loaded.catalog.tape_id} is not appendable while catalog status is "
                f"{loaded.catalog.status!r}"
            )
        unresolved = self.store.unresolved_operations(loaded.catalog.uuid)
        if unresolved:
            states = ", ".join(f"{record.operation_uuid}:{record.state}" for record in unresolved)
            raise SafetyError(f"unresolved tape operation(s) block append: {states}")
        try:
            backend.seek_eod()
            next_file_no = backend.current_file_no()
        except Exception as exc:
            if isinstance(exc, TapeError):
                raise SafetyError(f"cannot establish tape EOD: {exc}") from exc
            raise SafetyError(f"cannot establish tape EOD: {exc}") from exc
        if next_file_no is None or next_file_no < 1:
            raise SafetyError(f"backend did not provide a safe physical EOD file number: {next_file_no!r}")
        catalog_file_numbers: list[int] = []
        seen_archive_uuids: set[str] = set()
        for archive_uuid in loaded.catalog.archives:
            if archive_uuid in seen_archive_uuids:
                raise SafetyError(f"catalog references archive UUID more than once: {archive_uuid}")
            seen_archive_uuids.add(archive_uuid)
            try:
                archive = self.store.load_archive(archive_uuid)
            except Exception as exc:
                raise SafetyError(f"catalog archive reference is unreadable: {archive_uuid}: {exc}") from exc
            if archive.tape_uuid != loaded.catalog.uuid or archive.tape_id != loaded.catalog.tape_id:
                raise SafetyError(f"archive {archive.archive_uuid} disagrees with tape identity")
            catalog_file_numbers.append(archive.tape_file_no)
        expected_catalog_files = list(range(1, len(catalog_file_numbers) + 1))
        if sorted(catalog_file_numbers) != expected_catalog_files:
            raise SafetyError(
                "catalog archive physical file references must be contiguous from file 1: "
                f"{catalog_file_numbers!r}"
            )
        expected_next_file_no = len(catalog_file_numbers) + 1
        if next_file_no != expected_next_file_no:
            raise SafetyError(
                f"backend EOD {next_file_no} does not equal catalog expected next file "
                f"{expected_next_file_no}; cataloged files={catalog_file_numbers!r}"
            )
        return LoadedTape(header=loaded.header, catalog=loaded.catalog, next_file_no=next_file_no)


def identify_loaded_tape(store: CatalogStore, backend, *, require_writable: bool = False) -> LoadedTape:
    return TapeSafetyService(store).identify_loaded(backend, require_writable=require_writable)


def assert_append_ready(store: CatalogStore, backend) -> LoadedTape:
    return TapeSafetyService(store).assert_append_ready(backend)
