"""Filesystem-backed canonical catalog store."""

from __future__ import annotations

import json
import os
from collections.abc import Iterable, Iterator
from pathlib import Path
from typing import Any, TypeVar

from ..errors import CatalogError
from ..utils.atomic import atomic_write_json, atomic_write_stream
from .models import (
    ArchiveRecord,
    ManifestEntry,
    OperationRecord,
    PlanRecord,
    TapeRecord,
    derive_plan_archive_name,
    derive_plan_archive_uuid,
)

T = TypeVar("T")


def default_catalog_root() -> Path:
    configured = os.environ.get("LTOCTL_CATALOG")
    if configured:
        return Path(configured).expanduser()
    return Path.home() / ".local" / "share" / "ltoctl"


class CatalogStore:
    """Read and atomically update canonical JSON/JSONL files.

    ``root`` is the directory containing ``tapes/``, ``archives/`` and the
    other catalog subdirectories.  No database or hidden state is used.
    """

    SUBDIRECTORIES = ("tapes", "archives", "manifests", "plans", "operations", "index")

    def __init__(self, root: str | os.PathLike[str] | None = None, *, create: bool = True):
        self.root = Path(root).expanduser() if root is not None else default_catalog_root()
        if create:
            self.ensure_layout()

    def ensure_layout(self) -> None:
        self.root.mkdir(parents=True, exist_ok=True)
        for name in self.SUBDIRECTORIES:
            (self.root / name).mkdir(parents=True, exist_ok=True)

    @staticmethod
    def _component(value: str) -> str:
        if not isinstance(value, str) or not value or value in {".", ".."}:
            raise CatalogError(f"invalid catalog identifier {value!r}")
        if Path(value).name != value or "/" in value or "\\" in value or "\x00" in value:
            raise CatalogError(f"catalog identifier must be a simple filename: {value!r}")
        return value

    def _json_path(self, kind: str, identifier: str) -> Path:
        if kind not in {"tapes", "archives", "plans", "operations"}:
            raise CatalogError(f"unsupported catalog kind {kind!r}")
        return self.root / kind / f"{self._component(identifier)}.json"

    @staticmethod
    def _load_json(path: Path) -> dict[str, Any]:
        try:
            with path.open("r", encoding="utf-8") as stream:
                value = json.load(stream)
        except FileNotFoundError as exc:
            raise CatalogError(f"missing catalog file: {path}") from exc
        except (OSError, json.JSONDecodeError) as exc:
            raise CatalogError(f"cannot read catalog file {path}: {exc}") from exc
        if not isinstance(value, dict):
            raise CatalogError(f"catalog file must contain a JSON object: {path}")
        return value

    def _save_record(self, kind: str, identifier: str, record: Any) -> Path:
        path = self._json_path(kind, identifier)
        atomic_write_json(path, record.to_dict())
        return path

    def save_tape(self, record: TapeRecord) -> Path:
        return self._save_record("tapes", record.tape_id, record)

    def load_tape(self, tape_id: str) -> TapeRecord:
        path = self._json_path("tapes", tape_id)
        record = TapeRecord.from_dict(self._load_json(path))
        if record.tape_id != tape_id:
            raise CatalogError(
                f"tapes record identity {record.tape_id!r} does not match filename {path.name!r}"
            )
        return record

    def iter_tapes(self) -> Iterator[TapeRecord]:
        yield from self._iter_records("tapes", TapeRecord.from_dict)

    def list_tapes(self) -> list[TapeRecord]:
        return list(self.iter_tapes())

    def find_tape(self, reference: str) -> TapeRecord:
        try:
            return self.load_tape(reference)
        except CatalogError:
            matches = [record for record in self.iter_tapes() if record.uuid == reference]
            if len(matches) == 1:
                return matches[0]
            if len(matches) > 1:
                raise CatalogError(f"multiple tapes match UUID {reference!r}")
            raise CatalogError(f"unknown tape {reference!r}")

    def save_archive(self, record: ArchiveRecord) -> Path:
        return self._save_record("archives", record.archive_uuid, record)

    def load_archive(self, archive_uuid: str) -> ArchiveRecord:
        path = self._json_path("archives", archive_uuid)
        record = ArchiveRecord.from_dict(self._load_json(path))
        if record.archive_uuid != archive_uuid:
            raise CatalogError(
                f"archives record identity {record.archive_uuid!r} does not match filename {path.name!r}"
            )
        return record

    def iter_archives(self) -> Iterator[ArchiveRecord]:
        yield from self._iter_records("archives", ArchiveRecord.from_dict)

    def list_archives(self) -> list[ArchiveRecord]:
        return list(self.iter_archives())

    def find_archive(self, reference: str) -> ArchiveRecord:
        try:
            return self.load_archive(reference)
        except CatalogError:
            matches = [record for record in self.iter_archives() if record.name == reference]
            if len(matches) == 1:
                return matches[0]
            if len(matches) > 1:
                raise CatalogError(f"archive name is ambiguous: {reference!r}; use its UUID")
            raise CatalogError(f"unknown archive {reference!r}")

    def save_plan(self, record: PlanRecord) -> Path:
        self._validate_plan_execution_archives(record)
        return self._save_record("plans", record.plan_id, record)

    def load_plan(self, plan_id: str) -> PlanRecord:
        path = self._json_path("plans", plan_id)
        record = PlanRecord.from_dict(self._load_json(path))
        if record.plan_id != plan_id:
            raise CatalogError(
                f"plans record identity {record.plan_id!r} does not match filename {path.name!r}"
            )
        self._validate_plan_execution_archives(record)
        return record

    def iter_plans(self) -> Iterator[PlanRecord]:
        for record in self._iter_records("plans", PlanRecord.from_dict):
            self._validate_plan_execution_archives(record)
            yield record

    def list_plans(self) -> list[PlanRecord]:
        return list(self.iter_plans())

    def _validate_plan_execution_archives(self, record: PlanRecord) -> None:
        """Validate completed execution references against canonical archives."""

        record.validate_partition()
        record.validate_execution()
        execution_groups = {group.group_no: group for group in record.execution.groups}
        all_unit_names = tuple(unit.name for unit in record.units)
        tape_cache: dict[str, TapeRecord] = {}
        for group in record.groups:
            execution = execution_groups[group.group_no]
            for position, unit in enumerate(group.units, 1):
                unit_id = unit.unit_id or unit.path
                archive_uuid = execution.completed_units.get(unit_id)
                if archive_uuid is None:
                    continue
                expected_uuid = derive_plan_archive_uuid(
                    record.plan_id,
                    record.created_at,
                    group.group_no,
                    unit_id,
                )
                if archive_uuid != expected_uuid:
                    raise CatalogError(
                        f"plan {record.plan_id!r} completed unit {unit.path!r} references "
                        f"non-deterministic archive UUID {archive_uuid!r}; expected {expected_uuid!r}"
                    )
                try:
                    archive = self.load_archive(archive_uuid)
                except CatalogError as exc:
                    raise CatalogError(
                        f"plan {record.plan_id!r} completed unit {unit.path!r} references "
                        f"missing archive {archive_uuid!r}"
                    ) from exc
                if archive.archive_uuid != archive_uuid:
                    raise CatalogError(
                        f"plan {record.plan_id!r} completed archive path {archive_uuid!r} "
                        f"contains mismatched UUID {archive.archive_uuid!r}"
                    )
                expected_name = derive_plan_archive_name(
                    unit.name,
                    group.group_no,
                    position,
                    all_unit_names,
                )
                if archive.name != expected_name:
                    raise CatalogError(
                        f"plan {record.plan_id!r} completed archive {archive_uuid!r} name disagrees: "
                        f"{archive.name!r} != {expected_name!r}"
                    )
                # A completed plan records historical ownership.  Later
                # verification or retention workflows may legitimately mark
                # that physical archive obsolete/unverified/corrupt; the
                # execution reference must remain loadable in every legal
                # ArchiveRecord status.
                if not isinstance(archive.tar_stream_sha256, str) or not archive.tar_stream_sha256.strip():
                    raise CatalogError(
                        f"plan {record.plan_id!r} completed archive {archive_uuid!r} has no stream hash"
                    )
                if execution.tape_id != archive.tape_id or execution.tape_uuid != archive.tape_uuid:
                    raise CatalogError(
                        f"plan {record.plan_id!r} completed archive {archive_uuid!r} is on the wrong tape"
                    )
                if archive.source_paths != (list(unit.source_paths) or [unit.path]):
                    raise CatalogError(
                        f"plan {record.plan_id!r} completed archive {archive_uuid!r} source paths disagree"
                    )
                if archive.logical_size_bytes != unit.size_bytes or archive.file_count != unit.file_count:
                    raise CatalogError(
                        f"plan {record.plan_id!r} completed archive {archive_uuid!r} snapshot disagrees"
                    )
                try:
                    tape = tape_cache.setdefault(execution.tape_id, self.load_tape(execution.tape_id))
                except CatalogError as exc:
                    raise CatalogError(
                        f"plan {record.plan_id!r} completed archive {archive_uuid!r} references "
                        f"unreadable tape {execution.tape_id!r}"
                    ) from exc
                if tape.uuid != execution.tape_uuid:
                    raise CatalogError(
                        f"plan {record.plan_id!r} group {group.group_no} tape UUID disagrees with catalog"
                    )
                occurrences = [
                    index for index, value in enumerate(tape.archives) if value == archive_uuid
                ]
                if len(occurrences) != 1:
                    raise CatalogError(
                        f"plan {record.plan_id!r} completed archive {archive_uuid!r} lacks "
                        "exactly one reverse tape reference"
                    )
                expected_file_no = occurrences[0] + 1
                if archive.tape_file_no != expected_file_no:
                    raise CatalogError(
                        f"plan {record.plan_id!r} completed archive {archive_uuid!r} physical file "
                        f"{archive.tape_file_no} disagrees with tape location {expected_file_no}"
                    )

    def save_operation(self, record: OperationRecord) -> Path:
        return self._save_record("operations", record.operation_uuid, record)

    def load_operation(self, operation_uuid: str) -> OperationRecord:
        path = self._json_path("operations", operation_uuid)
        record = OperationRecord.from_dict(self._load_json(path))
        if record.operation_uuid != operation_uuid:
            raise CatalogError(
                f"operations record identity {record.operation_uuid!r} does not match filename {path.name!r}"
            )
        return record

    def iter_operations(self) -> Iterator[OperationRecord]:
        yield from self._iter_records("operations", OperationRecord.from_dict)

    def list_operations(self) -> list[OperationRecord]:
        return list(self.iter_operations())

    def unresolved_operations(self, tape_uuid: str | None = None) -> list[OperationRecord]:
        # Failed/writing operations remain blockers because a physical file
        # may have reached the medium.  ``aborted`` is reserved for a
        # persisted prepared operation that reconcile proves never crossed the
        # write boundary (EOD is still the expected next file).
        resolved = {"catalog_committed", "aborted"}
        records = (record for record in self.iter_operations() if record.state not in resolved)
        if tape_uuid is not None:
            records = (record for record in records if record.tape_uuid == tape_uuid)
        return list(records)

    def manifest_path(self, archive_uuid: str) -> Path:
        return self.root / "manifests" / f"{self._component(archive_uuid)}.jsonl"

    def save_manifest(self, archive_uuid: str, entries: Iterable[ManifestEntry | dict[str, Any]]) -> Path:
        """Write a manifest atomically while keeping memory bounded."""

        def lines() -> Iterator[str]:
            for entry in entries:
                if isinstance(entry, ManifestEntry):
                    value = entry.to_dict()
                else:
                    value = ManifestEntry.from_dict(dict(entry)).to_dict()
                # JSONL manifests use the compact, human-readable fields from
                # the requirements.  A schema field is accepted on read but
                # omitted to avoid repeating it tens of millions of times.
                value.pop("schema_version", None)
                yield json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")) + "\n"

        return atomic_write_stream(self.manifest_path(archive_uuid), lines())

    def iter_manifest(self, archive_uuid: str) -> Iterator[ManifestEntry]:
        path = self.manifest_path(archive_uuid)
        try:
            stream = path.open("r", encoding="utf-8")
        except FileNotFoundError as exc:
            raise CatalogError(f"missing manifest: {path}") from exc
        try:
            for line_no, line in enumerate(stream, 1):
                if not line.strip():
                    continue
                try:
                    data = json.loads(line)
                    if not isinstance(data, dict):
                        raise ValueError("line is not an object")
                    yield ManifestEntry.from_dict(data)
                except (json.JSONDecodeError, ValueError, CatalogError) as exc:
                    raise CatalogError(f"invalid manifest {path}:{line_no}: {exc}") from exc
        finally:
            stream.close()

    def _iter_records(self, kind: str, parser: Any) -> Iterator[Any]:
        directory = self.root / kind
        if not directory.exists():
            return
        for path in sorted(directory.glob("*.json")):
            try:
                record = parser(self._load_json(path))
                # Canonical filenames are part of the recovery contract.  A
                # record copied under a different filename must not become a
                # second, ambiguous identity during validation or lookup.
                expected_identifier = {
                    "tapes": getattr(record, "tape_id", None),
                    "archives": getattr(record, "archive_uuid", None),
                    "plans": getattr(record, "plan_id", None),
                    "operations": getattr(record, "operation_uuid", None),
                }.get(kind)
                if expected_identifier != path.stem:
                    raise CatalogError(
                        f"{kind} record identity {expected_identifier!r} does not match filename {path.name!r}"
                    )
                yield record
            except CatalogError:
                raise
            except (TypeError, ValueError, KeyError) as exc:
                raise CatalogError(f"invalid {kind} record {path}: {exc}") from exc
