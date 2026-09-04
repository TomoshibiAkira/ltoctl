"""Tape initialization and operation-journal state transitions."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone

from ..catalog.models import OperationRecord, TapeRecord
from ..catalog.store import CatalogStore
from ..errors import CatalogError, SafetyError, TapeError
from ..planner.scanner import MEDIA_PROFILES
from .header import build_tape_header, read_tape_header


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _media_profile(media: str) -> tuple[str, int, int]:
    key = media.lower().replace("-", "")
    try:
        return MEDIA_PROFILES[key]
    except KeyError as exc:
        raise TapeError(f"unsupported media type {media!r}") from exc


def _set_operation(store: CatalogStore, operation: OperationRecord, state: str, *, error: str | None = None) -> None:
    operation.state = state
    operation.updated_at = _now()
    operation.error = error
    store.save_operation(operation)


@dataclass(frozen=True)
class TapeInitResult:
    tape: TapeRecord
    operation_uuid: str


class TapeService:
    def __init__(self, store: CatalogStore, backend):
        self.store = store
        self.backend = backend

    def initialize(
        self,
        tape_id: str,
        *,
        media: str = "lto6",
        confirm: bool = False,
        tape_uuid: str | None = None,
    ) -> TapeInitResult:
        """Initialize a blank loaded tape with a self-describing file 0.

        ``confirm`` is intentionally required by the service as well as the
        CLI.  A caller embedding ltoctl cannot accidentally turn this into a
        silent destructive operation.
        """

        if not confirm:
            raise SafetyError("tape initialization is destructive; explicit confirmation is required")
        status = self.backend.status()
        if not status.loaded:
            raise SafetyError(status.error or "no tape is loaded")
        if not status.writable:
            raise SafetyError("tape is write protected")
        try:
            self.store._component(tape_id)
        except CatalogError as exc:
            raise SafetyError(f"invalid tape ID {tape_id!r}") from exc
        try:
            existing = self.store.find_tape(tape_id)
        except CatalogError:
            if (self.store.root / "tapes" / f"{tape_id}.json").exists():
                raise SafetyError(f"catalog tape record is malformed: {tape_id}")
            existing = None
        if existing is not None:
            raise SafetyError(f"tape ID already exists in catalog: {tape_id}")
        media_type, nominal, recommended = _media_profile(media)
        tape = TapeRecord.new(
            tape_id,
            media_type=media_type,
            nominal_capacity_bytes=nominal,
            recommended_capacity_bytes=recommended,
            tape_uuid=tape_uuid,
        )
        # A fresh physical medium must have no existing file before we write
        # file 0.  The check is semantic and does not issue an erase command.
        try:
            self.backend.seek_eod()
            existing_file_no = self.backend.current_file_no()
        except Exception as exc:
            raise SafetyError(f"cannot establish blank-tape EOD: {exc}") from exc
        if existing_file_no != 0:
            raise SafetyError(
                f"tape is not blank (physical EOD is file {existing_file_no}); refusing initialization"
            )
        self.backend.rewind()

        operation = OperationRecord.new(
            tape.tape_id,
            tape.uuid,
            expected_tape_file_no=0,
        )
        self.store.save_operation(operation)
        try:
            _set_operation(self.store, operation, "writing")
            stream = self.backend.write_tape_file()
            stream.write(build_tape_header(tape))
            # Mock keeps the write buffer open until finish; a Linux backend
            # owns flushing/closing in finish_tape_file.
            self.backend.finish_tape_file()
            _set_operation(self.store, operation, "tape_write_finished")

            self.backend.seek_file(0)
            readback = read_tape_header(self.backend)
            if readback.tape_id != tape.tape_id or readback.uuid != tape.uuid:
                raise SafetyError("tape header readback identity does not match initialization record")
            self.store.save_tape(tape)
            _set_operation(self.store, operation, "catalog_committed")
            return TapeInitResult(tape=tape, operation_uuid=operation.operation_uuid)
        except BaseException as exc:
            # Once a complete file 0 exists, keep tape_write_finished so
            # reconcile can safely commit the missing external catalog.  Any
            # earlier/ambiguous point is failed and remains an append blocker.
            if operation.state != "tape_write_finished":
                try:
                    _set_operation(self.store, operation, "failed", error=str(exc))
                except BaseException:
                    pass
            else:
                try:
                    operation.error = str(exc)
                    operation.updated_at = _now()
                    self.store.save_operation(operation)
                except BaseException:
                    pass
            raise


def init_tape(
    store: CatalogStore,
    backend,
    tape_id: str,
    *,
    media: str = "lto6",
    confirm: bool = False,
    tape_uuid: str | None = None,
) -> TapeInitResult:
    return TapeService(store, backend).initialize(
        tape_id,
        media=media,
        confirm=confirm,
        tape_uuid=tape_uuid,
    )
