"""Cross-file catalog consistency checks."""

from __future__ import annotations

from dataclasses import dataclass, field

from ..errors import CatalogError
from .models import ARCHIVE_STATUSES, OPERATION_STATES, TAPE_STATUSES
from .store import CatalogStore


def _manifest_path_key(path: str) -> bytes:
    """Use the scanner's byte-wise UTF-8 lexical order for manifest paths."""

    return path.encode("utf-8", "surrogatepass")


@dataclass
class ValidationReport:
    errors: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not self.errors

    def to_dict(self) -> dict[str, object]:
        return {"ok": self.ok, "errors": list(self.errors), "warnings": list(self.warnings)}


def validate_catalog(store: CatalogStore) -> ValidationReport:
    report = ValidationReport()
    tapes = []
    archives = []
    operations = []
    try:
        tapes = store.list_tapes()
    except CatalogError as exc:
        report.errors.append(f"cannot load tapes: {exc}")
    try:
        archives = store.list_archives()
    except CatalogError as exc:
        report.errors.append(f"cannot load archives: {exc}")
    try:
        # Plan execution is canonical progress, not a best-effort UI cache;
        # loading plans here exercises the strict schema/partition checks.
        store.list_plans()
    except CatalogError as exc:
        report.errors.append(f"cannot load plans: {exc}")
    try:
        operations = store.list_operations()
    except CatalogError as exc:
        report.errors.append(f"cannot load operations: {exc}")

    tape_by_id: dict[str, object] = {}
    tape_by_uuid: dict[str, object] = {}
    for tape in tapes:
        if tape.tape_id in tape_by_id:
            report.errors.append(f"duplicate tape ID: {tape.tape_id}")
        tape_by_id[tape.tape_id] = tape
        if tape.uuid in tape_by_uuid:
            report.errors.append(f"duplicate tape UUID: {tape.uuid}")
        tape_by_uuid[tape.uuid] = tape
        if tape.status not in TAPE_STATUSES:
            report.errors.append(f"invalid tape status {tape.status!r}: {tape.tape_id}")

    archive_by_uuid: dict[str, object] = {}
    names: dict[str, list[str]] = {}
    occupied: dict[str, dict[int, str]] = {}
    for archive in archives:
        if archive.archive_uuid in archive_by_uuid:
            report.errors.append(f"duplicate archive UUID: {archive.archive_uuid}")
        archive_by_uuid[archive.archive_uuid] = archive
        names.setdefault(archive.name, []).append(archive.archive_uuid)
        if archive.status not in ARCHIVE_STATUSES:
            report.errors.append(f"invalid archive status {archive.status!r}: {archive.archive_uuid}")
        if archive.tape_file_no < 1:
            report.errors.append(f"archive {archive.archive_uuid} uses reserved tape header file 0")
        tape = tape_by_id.get(archive.tape_id)
        if tape is None:
            report.errors.append(f"archive {archive.archive_uuid} references missing tape {archive.tape_id}")
        else:
            if tape.uuid != archive.tape_uuid:
                report.errors.append(
                    f"archive {archive.archive_uuid} tape UUID mismatch: {archive.tape_uuid} != {tape.uuid}"
                )
            locations = occupied.setdefault(archive.tape_id, {})
            previous = locations.get(archive.tape_file_no)
            if previous is not None and previous != archive.archive_uuid:
                report.errors.append(
                    f"tape {archive.tape_id} physical file {archive.tape_file_no} collides "
                    f"({previous}, {archive.archive_uuid})"
                )
            locations[archive.tape_file_no] = archive.archive_uuid
            if archive.archive_uuid not in tape.archives:
                report.errors.append(f"tape {tape.tape_id} does not reference archive {archive.archive_uuid}")
        if not store.manifest_path(archive.archive_uuid).is_file():
            report.errors.append(f"archive {archive.archive_uuid} manifest is missing")
        else:
            try:
                # Validate every line while retaining bounded memory.  The
                # aggregate checks catch a manifest that is individually
                # well-formed but no longer describes its catalog record.
                previous_path_key: bytes | None = None
                manifest_files = 0
                manifest_size = 0
                for entry in store.iter_manifest(archive.archive_uuid):
                    if (
                        entry.path.startswith(("/", "\\"))
                        or "\x00" in entry.path
                        or any(part in {"", ".", ".."} for part in entry.path.split("/"))
                    ):
                        report.errors.append(
                            f"archive {archive.archive_uuid} manifest path is not safe/canonical: {entry.path!r}"
                        )
                    path_key = _manifest_path_key(entry.path)
                    if previous_path_key is not None and path_key <= previous_path_key:
                        report.errors.append(
                            f"archive {archive.archive_uuid} manifest paths are not strictly lexical "
                            f"(duplicate or out of order) at {entry.path!r}"
                        )
                    previous_path_key = path_key
                    if entry.type == "other":
                        report.errors.append(
                            f"archive {archive.archive_uuid} manifest contains unsupported special entry "
                            f"{entry.path!r}"
                        )
                    if entry.type == "file":
                        manifest_files += 1
                        manifest_size += entry.size
                    elif entry.size != 0:
                        report.errors.append(
                            f"archive {archive.archive_uuid} non-file manifest entry has non-zero size: {entry.path!r}"
                        )
                    if entry.type == "symlink" and not isinstance(entry.link_target, str):
                        report.errors.append(
                            f"archive {archive.archive_uuid} symlink has no link target: {entry.path!r}"
                        )
                if manifest_files != archive.file_count:
                    report.errors.append(
                        f"archive {archive.archive_uuid} manifest file count {manifest_files} "
                        f"does not match catalog {archive.file_count}"
                    )
                if manifest_size != archive.logical_size_bytes:
                    report.errors.append(
                        f"archive {archive.archive_uuid} manifest logical size {manifest_size} "
                        f"does not match catalog {archive.logical_size_bytes}"
                    )
            except CatalogError as exc:
                report.errors.append(f"archive {archive.archive_uuid} manifest is invalid: {exc}")

    for tape in tapes:
        for archive_uuid in tape.archives:
            archive = archive_by_uuid.get(archive_uuid)
            if archive is None:
                report.errors.append(f"tape {tape.tape_id} references missing archive {archive_uuid}")
            elif archive.tape_id != tape.tape_id or archive.tape_uuid != tape.uuid:
                report.errors.append(f"tape/archive relationship mismatch for {tape.tape_id}/{archive_uuid}")

    for tape_id, locations in occupied.items():
        physical_numbers = sorted(locations)
        expected_numbers = list(range(1, len(physical_numbers) + 1))
        if physical_numbers != expected_numbers:
            report.errors.append(
                f"tape {tape_id} archive file references are not contiguous from file 1: "
                f"{physical_numbers!r}"
            )

    # Operations are journals, not a source of truth, but malformed journals
    # can otherwise keep a damaged tape looking appendable.  Validate their
    # identity and any archive reference without requiring a pre-write tape
    # init operation to already have a catalog tape record.
    for operation in operations:
        if operation.state not in OPERATION_STATES:
            report.errors.append(f"invalid operation state {operation.state!r}: {operation.operation_uuid}")
        expected_file = operation.expected_tape_file_no
        if operation.archive_uuid is None:
            if expected_file != 0:
                report.errors.append(
                    f"tape-init operation {operation.operation_uuid} must target physical file 0"
                )
            continue
        if expected_file is None or expected_file < 1:
            report.errors.append(
                f"archive operation {operation.operation_uuid} has invalid expected file {expected_file!r}"
            )
        archive = archive_by_uuid.get(operation.archive_uuid)
        if archive is None:
            # A prepared/writing operation can legitimately have no external
            # archive yet.  Once catalog commit is claimed, absence is a
            # canonical inconsistency.
            if operation.state == "catalog_committed":
                report.errors.append(
                    f"catalog-committed operation {operation.operation_uuid} references missing archive "
                    f"{operation.archive_uuid}"
                )
            continue
        if archive.tape_id != operation.tape_id or archive.tape_uuid != operation.tape_uuid:
            report.errors.append(
                f"operation/archive relationship mismatch for {operation.operation_uuid}/"
                f"{operation.archive_uuid}"
            )
        if operation.archive_name is not None and operation.archive_name != archive.name:
            report.errors.append(
                f"operation {operation.operation_uuid} archive name disagrees with catalog archive "
                f"{operation.archive_uuid}"
            )
        if operation.state == "catalog_committed":
            tape = tape_by_id.get(operation.tape_id)
            if tape is None or operation.archive_uuid not in tape.archives:
                report.errors.append(
                    f"catalog-committed operation {operation.operation_uuid} archive is not referenced by tape"
                )

    for name, ids in names.items():
        if len(ids) > 1:
            report.warnings.append(f"archive name is ambiguous ({name!r}): {', '.join(ids)}")
    return report


def validate(store: CatalogStore) -> ValidationReport:
    return validate_catalog(store)
