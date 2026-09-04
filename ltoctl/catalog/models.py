"""Serializable catalog and planning models.

The models deliberately stay as plain dataclasses.  ``to_dict`` and
``from_dict`` make the on-disk JSON shape explicit and validate the fields that
are safety-critical (identifiers, statuses and non-negative counters).
"""

from __future__ import annotations

import os
import re
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from typing import Any, ClassVar
from uuid import NAMESPACE_URL, uuid4, uuid5

from ..errors import CatalogValidationError

SCHEMA_VERSION = 1

TAPE_STATUSES = frozenset({"active", "full", "retired", "needs_recovery", "unknown"})
ARCHIVE_STATUSES = frozenset({"active", "obsolete", "unverified", "corrupt"})
OPERATION_STATES = frozenset(
    {"prepared", "writing", "tape_write_finished", "catalog_committed", "aborted", "failed"}
)
PLAN_EXECUTION_STATUSES = frozenset({"planned", "in_progress", "complete"})
PLAN_GROUP_EXECUTION_STATUSES = frozenset({"pending", "in_progress", "complete"})


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _required_string(value: Any, field_name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise CatalogValidationError(f"{field_name} must be a non-empty string")
    return value


def _nonnegative_int(value: Any, field_name: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise CatalogValidationError(f"{field_name} must be a non-negative integer")
    return value


def _normalized_source_path(value: str) -> str:
    """Normalize a source path for plan identity without resolving symlinks."""

    if not isinstance(value, str) or not value.strip():
        raise CatalogValidationError("plan unit path must be a non-empty string")
    return os.path.normcase(os.path.abspath(os.path.normpath(os.path.expanduser(value))))


def _unit_path_identity(unit: "PlanUnit") -> str:
    return _normalized_source_path(unit.path)


def _unit_stable_identity(unit: "PlanUnit") -> str:
    stable_id = unit.unit_id or unit.path
    if not isinstance(stable_id, str) or not stable_id.strip():
        raise CatalogValidationError("plan unit unit_id must be a non-empty string")
    return stable_id if unit.unit_id else _unit_path_identity(unit)


def _unit_signature(unit: "PlanUnit") -> tuple[Any, ...]:
    """Fields that must agree when a unit is repeated in a plan partition."""

    return (
        _unit_path_identity(unit),
        unit.name,
        unit.size_bytes,
        unit.file_count,
        unit.mtime_ns,
        unit.unit_id,
        tuple(_normalized_source_path(path) for path in unit.source_paths),
        unit.snapshot_fingerprint,
        unit.oversized,
    )


def derive_plan_archive_uuid(
    plan_id: str,
    created_at: str,
    group_no: int,
    unit_identity: str,
) -> str:
    """Derive the stable archive identity for one saved-plan unit.

    A plan retry must never infer ownership from a human-readable archive name
    or a matching source snapshot.  This UUID is the durable join key between
    a plan unit and the archive committed by :class:`ArchiveWriter`.
    """

    if not isinstance(plan_id, str) or not plan_id.strip():
        raise CatalogValidationError("plan_id must be a non-empty string")
    if not isinstance(created_at, str) or not created_at.strip():
        raise CatalogValidationError("created_at must be a non-empty string")
    if isinstance(group_no, bool) or not isinstance(group_no, int) or group_no < 1:
        raise CatalogValidationError("group_no must be a positive integer")
    if not isinstance(unit_identity, str) or not unit_identity.strip():
        raise CatalogValidationError("unit identity must be a non-empty string")
    seed = "\x1f".join(("ltoctl-plan-archive-v1", plan_id, created_at, str(group_no), unit_identity))
    return str(uuid5(NAMESPACE_URL, seed))


def derive_plan_archive_name(
    unit_name: str,
    group_no: int,
    position: int,
    all_unit_names: list[str] | tuple[str, ...],
) -> str:
    """Derive the deterministic catalog name used for a plan unit archive."""

    if not isinstance(unit_name, str) or not unit_name.strip():
        raise CatalogValidationError("unit name must be a non-empty string")
    if isinstance(group_no, bool) or not isinstance(group_no, int) or group_no < 1:
        raise CatalogValidationError("group_no must be a positive integer")
    if isinstance(position, bool) or not isinstance(position, int) or position < 1:
        raise CatalogValidationError("unit position must be a positive integer")
    try:
        unit_part = re.sub(r"[^A-Za-z0-9_.-]+", "_", unit_name).strip("._-") or f"unit-{position}"
        same_name = sum(
            (re.sub(r"[^A-Za-z0-9_.-]+", "_", candidate).strip("._-") or "unit") == unit_part
            for candidate in all_unit_names
        )
    except TypeError as exc:
        raise CatalogValidationError("plan unit names must be strings") from exc
    if same_name == 1:
        return unit_part
    return f"{unit_part}-g{group_no}-u{position}"


def _schema(data: dict[str, Any], *, required: bool = True) -> None:
    if not isinstance(data, dict):
        raise CatalogValidationError("catalog record must be a JSON object")
    version = data.get("schema_version")
    if not required and version is None:
        return
    if version != SCHEMA_VERSION:
        raise CatalogValidationError(f"unsupported schema_version {version!r}; expected {SCHEMA_VERSION}")


def _reject_unknown_fields(data: dict[str, Any], allowed: set[str], label: str) -> None:
    unknown = set(data) - allowed
    if unknown:
        raise CatalogValidationError(f"unknown {label} field(s): {sorted(unknown)!r}")


class Serializable:
    schema_version: ClassVar[int] = SCHEMA_VERSION

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class ManifestEntry(Serializable):
    path: str
    size: int
    mtime_ns: int
    type: str
    link_target: str | None = None
    schema_version: int = field(default=SCHEMA_VERSION, init=False)

    def __post_init__(self) -> None:
        _required_string(self.path, "path")
        _nonnegative_int(self.size, "size")
        _nonnegative_int(self.mtime_ns, "mtime_ns")
        if self.type not in {"file", "dir", "symlink", "other"}:
            raise CatalogValidationError(f"unsupported manifest entry type {self.type!r}")
        if self.link_target is not None and not isinstance(self.link_target, str):
            raise CatalogValidationError("manifest link_target must be a string or null")
        if self.type == "symlink" and not self.link_target:
            raise CatalogValidationError("symlink manifest entries require a link_target")
        if self.type != "symlink" and self.link_target is not None:
            raise CatalogValidationError("only symlink manifest entries may have a link_target")

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "ManifestEntry":
        # The requirements' compact JSONL example intentionally omits a
        # repeated schema field on every line.  Accept that representation
        # while canonical JSON records remain strict.
        _schema(data, required=False)
        _reject_unknown_fields(
            data,
            {"schema_version", "path", "size", "mtime_ns", "type", "link_target"},
            "manifest entry",
        )
        return cls(
            path=_required_string(data.get("path"), "path"),
            size=_nonnegative_int(data.get("size"), "size"),
            mtime_ns=_nonnegative_int(data.get("mtime_ns"), "mtime_ns"),
            type=_required_string(data.get("type"), "type"),
            link_target=data.get("link_target"),
        )


@dataclass
class TapeRecord(Serializable):
    tape_id: str
    uuid: str
    media_type: str
    created_at: str = field(default_factory=utc_now)
    nominal_capacity_bytes: int = 0
    recommended_capacity_bytes: int = 0
    status: str = "active"
    archives: list[str] = field(default_factory=list)
    schema_version: int = field(default=SCHEMA_VERSION, init=False)

    def __post_init__(self) -> None:
        _required_string(self.tape_id, "tape_id")
        _required_string(self.uuid, "uuid")
        _required_string(self.media_type, "media_type")
        _required_string(self.created_at, "created_at")
        _nonnegative_int(self.nominal_capacity_bytes, "nominal_capacity_bytes")
        _nonnegative_int(self.recommended_capacity_bytes, "recommended_capacity_bytes")
        if self.status not in TAPE_STATUSES:
            raise CatalogValidationError(f"unsupported tape status {self.status!r}")
        if len(set(self.archives)) != len(self.archives):
            raise CatalogValidationError("tape archives must not contain duplicates")
        for archive_uuid in self.archives:
            _required_string(archive_uuid, "archive UUID")

    @classmethod
    def new(
        cls,
        tape_id: str,
        *,
        media_type: str = "LTO-6",
        nominal_capacity_bytes: int = 2_500_000_000_000,
        recommended_capacity_bytes: int = 2_350_000_000_000,
        tape_uuid: str | None = None,
    ) -> "TapeRecord":
        return cls(
            tape_id=tape_id,
            uuid=tape_uuid or str(uuid4()),
            media_type=media_type,
            nominal_capacity_bytes=nominal_capacity_bytes,
            recommended_capacity_bytes=recommended_capacity_bytes,
        )

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "TapeRecord":
        _schema(data)
        _reject_unknown_fields(
            data,
            {
                "schema_version",
                "tape_id",
                "uuid",
                "media_type",
                "created_at",
                "nominal_capacity_bytes",
                "recommended_capacity_bytes",
                "status",
                "archives",
            },
            "tape",
        )
        archives = data.get("archives", [])
        if not isinstance(archives, list):
            raise CatalogValidationError("tape archives must be a list")
        return cls(
            tape_id=_required_string(data.get("tape_id"), "tape_id"),
            uuid=_required_string(data.get("uuid"), "uuid"),
            media_type=_required_string(data.get("media_type"), "media_type"),
            created_at=_required_string(data.get("created_at"), "created_at"),
            nominal_capacity_bytes=_nonnegative_int(data.get("nominal_capacity_bytes"), "nominal_capacity_bytes"),
            recommended_capacity_bytes=_nonnegative_int(
                data.get("recommended_capacity_bytes"), "recommended_capacity_bytes"
            ),
            status=data.get("status", "active"),
            archives=list(archives),
        )


@dataclass
class ArchiveRecord(Serializable):
    archive_uuid: str
    name: str
    tape_id: str
    tape_uuid: str
    tape_file_no: int
    created_at: str = field(default_factory=utc_now)
    source_paths: list[str] = field(default_factory=list)
    logical_size_bytes: int = 0
    file_count: int = 0
    tar_stream_sha256: str | None = None
    status: str = "active"
    schema_version: int = field(default=SCHEMA_VERSION, init=False)

    def __post_init__(self) -> None:
        _required_string(self.archive_uuid, "archive_uuid")
        _required_string(self.name, "name")
        _required_string(self.tape_id, "tape_id")
        _required_string(self.tape_uuid, "tape_uuid")
        _nonnegative_int(self.tape_file_no, "tape_file_no")
        if self.tape_file_no < 1:
            raise CatalogValidationError("archive tape_file_no must be >= 1; physical file 0 is the tape header")
        _required_string(self.created_at, "created_at")
        _nonnegative_int(self.logical_size_bytes, "logical_size_bytes")
        _nonnegative_int(self.file_count, "file_count")
        if self.status not in ARCHIVE_STATUSES:
            raise CatalogValidationError(f"unsupported archive status {self.status!r}")
        if self.tar_stream_sha256 is not None and (
            not isinstance(self.tar_stream_sha256, str) or not self.tar_stream_sha256.strip()
        ):
            raise CatalogValidationError("tar_stream_sha256 must be a non-empty string or null")
        for source_path in self.source_paths:
            _required_string(source_path, "source path")

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "ArchiveRecord":
        _schema(data)
        _reject_unknown_fields(
            data,
            {
                "schema_version",
                "archive_uuid",
                "name",
                "tape_id",
                "tape_uuid",
                "tape_file_no",
                "created_at",
                "source_paths",
                "logical_size_bytes",
                "file_count",
                "tar_stream_sha256",
                "status",
            },
            "archive",
        )
        source_paths = data.get("source_paths", [])
        if not isinstance(source_paths, list):
            raise CatalogValidationError("archive source_paths must be a list")
        return cls(
            archive_uuid=_required_string(data.get("archive_uuid"), "archive_uuid"),
            name=_required_string(data.get("name"), "name"),
            tape_id=_required_string(data.get("tape_id"), "tape_id"),
            tape_uuid=_required_string(data.get("tape_uuid"), "tape_uuid"),
            tape_file_no=_nonnegative_int(data.get("tape_file_no"), "tape_file_no"),
            created_at=_required_string(data.get("created_at"), "created_at"),
            source_paths=list(source_paths),
            logical_size_bytes=_nonnegative_int(data.get("logical_size_bytes"), "logical_size_bytes"),
            file_count=_nonnegative_int(data.get("file_count"), "file_count"),
            tar_stream_sha256=data.get("tar_stream_sha256"),
            status=data.get("status", "active"),
        )


@dataclass
class PlanUnit(Serializable):
    """An indivisible source unit used by the packer."""

    path: str
    name: str
    size_bytes: int
    file_count: int
    mtime_ns: int
    source_paths: list[str] = field(default_factory=list)
    unit_id: str | None = None
    oversized: bool = False
    snapshot_fingerprint: str | None = None
    schema_version: int = field(default=SCHEMA_VERSION, init=False)

    def __post_init__(self) -> None:
        _required_string(self.path, "unit path")
        _required_string(self.name, "unit name")
        _nonnegative_int(self.size_bytes, "unit size_bytes")
        _nonnegative_int(self.file_count, "unit file_count")
        _nonnegative_int(self.mtime_ns, "unit mtime_ns")
        if not isinstance(self.source_paths, list):
            raise CatalogValidationError("plan unit source_paths must be a list")
        for source_path in self.source_paths:
            _required_string(source_path, "plan unit source path")
        if self.unit_id is None:
            self.unit_id = self.path
        elif not isinstance(self.unit_id, str) or not self.unit_id.strip():
            raise CatalogValidationError("unit_id must be a non-empty string")

    @property
    def size(self) -> int:
        return self.size_bytes

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "PlanUnit":
        _schema(data)
        _reject_unknown_fields(
            data,
            {
                "schema_version",
                "path",
                "name",
                "size_bytes",
                "size",  # legacy Phase-1 spelling
                "file_count",
                "mtime_ns",
                "source_paths",
                "unit_id",
                "oversized",
                "snapshot_fingerprint",
            },
            "plan unit",
        )
        source_paths = data.get("source_paths", [])
        if not isinstance(source_paths, list):
            raise CatalogValidationError("plan unit source_paths must be a list")
        return cls(
            path=_required_string(data.get("path"), "unit path"),
            name=_required_string(data.get("name"), "unit name"),
            size_bytes=_nonnegative_int(data.get("size_bytes", data.get("size")), "unit size_bytes"),
            file_count=_nonnegative_int(data.get("file_count", 0), "unit file_count"),
            mtime_ns=_nonnegative_int(data.get("mtime_ns", 0), "unit mtime_ns"),
            source_paths=list(source_paths),
            unit_id=data.get("unit_id"),
            oversized=bool(data.get("oversized", False)),
            snapshot_fingerprint=data.get("snapshot_fingerprint"),
        )


@dataclass
class PlanGroup(Serializable):
    group_no: int
    units: list[PlanUnit] = field(default_factory=list)
    used_bytes: int = 0
    capacity_bytes: int = 0
    schema_version: int = field(default=SCHEMA_VERSION, init=False)

    def __post_init__(self) -> None:
        _nonnegative_int(self.group_no, "group_no")
        member_sum = sum(unit.size_bytes for unit in self.units)
        if self.used_bytes == 0 and self.units:
            # ``0`` is the convenient constructor default; derive it once
            # when members are supplied.  Any non-zero supplied value must
            # agree exactly with the members so persisted plans cannot lie.
            self.used_bytes = member_sum
        elif self.used_bytes != member_sum:
            raise CatalogValidationError(
                f"plan group used_bytes {self.used_bytes} does not equal member sum {member_sum}"
            )
        self.validate_consistency()

    def validate_consistency(self) -> None:
        """Raise if members, accounting, or capacity no longer agree."""

        member_sum = sum(unit.size_bytes for unit in self.units)
        if self.used_bytes != member_sum:
            raise CatalogValidationError(
                f"plan group used_bytes {self.used_bytes} does not equal member sum {member_sum}"
            )
        _nonnegative_int(self.used_bytes, "used_bytes")
        _nonnegative_int(self.capacity_bytes, "capacity_bytes")
        if self.capacity_bytes and self.used_bytes > self.capacity_bytes:
            raise CatalogValidationError("plan group exceeds capacity")

    @property
    def free_bytes(self) -> int:
        return max(0, self.capacity_bytes - self.used_bytes)

    @property
    def fill_fraction(self) -> float:
        return self.used_bytes / self.capacity_bytes if self.capacity_bytes else 0.0

    def add_unit(self, unit: PlanUnit) -> None:
        """Add a member while preserving the used-bytes invariant."""

        if self.capacity_bytes and self.used_bytes + unit.size_bytes > self.capacity_bytes:
            raise CatalogValidationError("plan group exceeds capacity")
        self.units.append(unit)
        self.used_bytes += unit.size_bytes

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "PlanGroup":
        _schema(data)
        _reject_unknown_fields(
            data,
            {"schema_version", "group_no", "units", "used_bytes", "capacity_bytes"},
            "plan group",
        )
        raw_units = data.get("units", [])
        if not isinstance(raw_units, list):
            raise CatalogValidationError("plan group units must be a list")
        units = [unit if isinstance(unit, PlanUnit) else PlanUnit.from_dict(unit) for unit in raw_units]
        return cls(
            group_no=_nonnegative_int(data.get("group_no"), "group_no"),
            units=units,
            used_bytes=_nonnegative_int(data.get("used_bytes", 0), "used_bytes"),
            capacity_bytes=_nonnegative_int(data.get("capacity_bytes", 0), "capacity_bytes"),
        )


@dataclass
class PlanGroupExecution(Serializable):
    """Durable execution state for one planned tape group.

    ``completed_units`` is keyed by the stable plan-unit identity (normally
    ``PlanUnit.unit_id``) and contains the catalog UUID of the archive that
    committed that unit.  The object is deliberately typed instead of being
    an open-ended JSON dictionary: malformed progress must fail before a
    resumed append can reach a tape drive.
    """

    group_no: int
    status: str = "pending"
    tape_id: str | None = None
    tape_uuid: str | None = None
    completed_units: dict[str, str] = field(default_factory=dict)
    schema_version: int = field(default=SCHEMA_VERSION, init=False)

    def __post_init__(self) -> None:
        _nonnegative_int(self.group_no, "execution group_no")
        if self.group_no < 1:
            raise CatalogValidationError("execution group_no must be >= 1")
        if self.status not in PLAN_GROUP_EXECUTION_STATUSES:
            raise CatalogValidationError(f"unsupported plan group execution status {self.status!r}")
        if self.tape_id is not None:
            _required_string(self.tape_id, "execution tape_id")
        if self.tape_uuid is not None:
            _required_string(self.tape_uuid, "execution tape_uuid")
        if (self.tape_id is None) != (self.tape_uuid is None):
            raise CatalogValidationError("execution tape_id and tape_uuid must be set together")
        if not isinstance(self.completed_units, dict):
            raise CatalogValidationError("completed_units must be an object mapping unit IDs to archive UUIDs")
        for unit_id, archive_uuid in self.completed_units.items():
            _required_string(unit_id, "completed plan unit identity")
            _required_string(archive_uuid, "completed archive UUID")
        if self.status == "pending" and (self.tape_id is not None or self.completed_units):
            raise CatalogValidationError("pending execution group cannot have a tape binding or completed units")
        if self.status != "pending" and self.tape_id is None:
            raise CatalogValidationError("active execution group must have a tape binding")

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "PlanGroupExecution":
        if not isinstance(data, dict):
            raise CatalogValidationError("plan group execution must be an object")
        _schema(data)
        required = {"schema_version", "group_no", "status", "tape_id", "tape_uuid", "completed_units"}
        missing = required - set(data)
        if missing:
            raise CatalogValidationError(f"plan group execution is missing field(s): {sorted(missing)!r}")
        _reject_unknown_fields(
            data,
            {"schema_version", "group_no", "status", "tape_id", "tape_uuid", "completed_units"},
            "plan group execution",
        )
        completed = data.get("completed_units", {})
        if not isinstance(completed, dict):
            raise CatalogValidationError("completed_units must be an object mapping unit IDs to archive UUIDs")
        return cls(
            group_no=_nonnegative_int(data.get("group_no"), "execution group_no"),
            status=_required_string(data.get("status"), "execution group status"),
            tape_id=data.get("tape_id"),
            tape_uuid=data.get("tape_uuid"),
            completed_units=dict(completed),
        )


@dataclass
class PlanExecution(Serializable):
    """Typed, schema-versioned progress for a :class:`PlanRecord`."""

    status: str = "planned"
    groups: list[PlanGroupExecution] = field(default_factory=list)
    schema_version: int = field(default=SCHEMA_VERSION, init=False)

    def __post_init__(self) -> None:
        if self.status not in PLAN_EXECUTION_STATUSES:
            raise CatalogValidationError(f"unsupported plan execution status {self.status!r}")
        if not isinstance(self.groups, list) or not all(
            isinstance(group, PlanGroupExecution) for group in self.groups
        ):
            raise CatalogValidationError("plan execution groups must be PlanGroupExecution objects")
        numbers = [group.group_no for group in self.groups]
        if len(set(numbers)) != len(numbers):
            raise CatalogValidationError("plan execution group numbers must be unique")
        bound_tape_uuids: set[str] = set()
        seen_in_progress = False
        seen_pending = False
        any_progress = False
        for group in self.groups:
            if group.tape_uuid is not None:
                if group.tape_uuid in bound_tape_uuids:
                    raise CatalogValidationError(
                        f"tape UUID is assigned to multiple execution groups: {group.tape_uuid!r}"
                    )
                bound_tape_uuids.add(group.tape_uuid)
            if group.status == "complete":
                if seen_in_progress or seen_pending:
                    raise CatalogValidationError(
                        f"complete execution group {group.group_no} follows an incomplete group"
                    )
                any_progress = True
            elif group.status == "in_progress":
                if seen_in_progress or seen_pending:
                    raise CatalogValidationError(
                        f"in-progress execution group {group.group_no} follows an incomplete group"
                    )
                seen_in_progress = True
                any_progress = True
            elif group.status == "pending":
                seen_pending = True
            else:
                raise CatalogValidationError(f"unsupported plan group execution status {group.status!r}")
        if self.status == "planned" and any_progress:
            raise CatalogValidationError("planned execution cannot contain group progress")
        if self.status == "in_progress" and not any_progress:
            raise CatalogValidationError("in_progress execution must contain group progress")
        if self.status == "complete" and any(group.status != "complete" for group in self.groups):
            raise CatalogValidationError("complete plan execution requires every group to be complete")

    @classmethod
    def new(cls, groups: list[PlanGroup]) -> "PlanExecution":
        return cls(groups=[PlanGroupExecution(group_no=group.group_no) for group in groups])

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "PlanExecution":
        if not isinstance(data, dict):
            raise CatalogValidationError("plan execution must be an object")
        _schema(data)
        _reject_unknown_fields(data, {"schema_version", "status", "groups"}, "plan execution")
        raw_groups = data.get("groups")
        if not isinstance(raw_groups, list):
            raise CatalogValidationError("plan execution groups must be a list")
        return cls(
            status=_required_string(data.get("status"), "plan execution status"),
            groups=[
                group if isinstance(group, PlanGroupExecution) else PlanGroupExecution.from_dict(group)
                for group in raw_groups
            ],
        )


@dataclass
class PlanRecord(Serializable):
    plan_id: str
    created_at: str
    media_type: str
    nominal_capacity_bytes: int
    recommended_capacity_bytes: int
    usable_capacity_bytes: int
    packing_algorithm: str
    source_paths: list[str]
    units: list[PlanUnit]
    groups: list[PlanGroup]
    oversized_units: list[PlanUnit] = field(default_factory=list)
    unit_depth: int | None = None
    # ``None`` is accepted by constructors for backwards compatibility with
    # plans written before execution tracking existed.  __post_init__ turns
    # it into a typed, schema-versioned planned execution immediately.
    execution: PlanExecution | None = None
    schema_version: int = field(default=SCHEMA_VERSION, init=False)

    def __post_init__(self) -> None:
        _required_string(self.plan_id, "plan_id")
        _required_string(self.created_at, "created_at")
        _required_string(self.media_type, "media_type")
        _required_string(self.packing_algorithm, "packing_algorithm")
        _nonnegative_int(self.nominal_capacity_bytes, "nominal_capacity_bytes")
        _nonnegative_int(self.recommended_capacity_bytes, "recommended_capacity_bytes")
        _nonnegative_int(self.usable_capacity_bytes, "usable_capacity_bytes")
        if not isinstance(self.source_paths, list):
            raise CatalogValidationError("plan source_paths must be a list")
        for source_path in self.source_paths:
            _required_string(source_path, "plan source path")
        if self.unit_depth is not None:
            _nonnegative_int(self.unit_depth, "unit_depth")
        # Validate the partition before deriving default execution groups so
        # malformed direct constructors produce the same typed catalog error
        # as malformed JSON round-trips.
        self.validate_partition()
        if self.execution is None:
            self.execution = PlanExecution.new(self.groups)
        elif isinstance(self.execution, dict):
            # Empty execution objects are the legacy Phase-1 representation.
            # Non-empty objects must carry the explicit execution schema.
            self.execution = PlanExecution.new(self.groups) if not self.execution else PlanExecution.from_dict(self.execution)
        elif not isinstance(self.execution, PlanExecution):
            raise CatalogValidationError("plan execution must be a PlanExecution object")
        self.validate_execution()

    @property
    def tape_count(self) -> int:
        return len(self.groups)

    @property
    def total_source_size_bytes(self) -> int:
        return sum(unit.size_bytes for unit in self.units)

    @property
    def total_unused_capacity_bytes(self) -> int:
        return sum(group.free_bytes for group in self.groups)

    def validate_partition(self) -> None:
        """Validate that groups form a lossless partition of plan units.

        This is intentionally stricter than checking aggregate byte counts.
        A corrupt plan must not be allowed to silently omit, duplicate, or
        replace a source unit when it is later applied.
        """

        for unit in self.units:
            if not isinstance(unit, PlanUnit):
                raise CatalogValidationError("plan top-level units must be PlanUnit objects")
        for group in self.groups:
            if not isinstance(group, PlanGroup):
                raise CatalogValidationError("plan groups must be PlanGroup objects")
        for unit in self.oversized_units:
            if not isinstance(unit, PlanUnit):
                raise CatalogValidationError("plan oversized_units must be PlanUnit objects")

        top_by_stable: dict[str, PlanUnit] = {}
        top_by_path: dict[str, PlanUnit] = {}
        for unit in self.units:
            stable_id = _unit_stable_identity(unit)
            path_id = _unit_path_identity(unit)
            if stable_id in top_by_stable:
                raise CatalogValidationError(f"duplicate top-level plan unit identity: {stable_id!r}")
            if path_id in top_by_path:
                raise CatalogValidationError(f"duplicate top-level plan source path: {unit.path!r}")
            top_by_stable[stable_id] = unit
            top_by_path[path_id] = unit

        oversized_by_stable: dict[str, PlanUnit] = {}
        for unit in self.oversized_units:
            stable_id = _unit_stable_identity(unit)
            if stable_id in oversized_by_stable:
                raise CatalogValidationError(f"duplicate oversized plan unit identity: {stable_id!r}")
            top = top_by_stable.get(stable_id)
            if top is None:
                raise CatalogValidationError(f"oversized unit is not present in top-level units: {unit.path}")
            if _unit_signature(unit) != _unit_signature(top):
                raise CatalogValidationError(f"oversized unit does not match top-level snapshot: {unit.path}")
            if top.oversized != unit.oversized:
                raise CatalogValidationError(f"oversized classification disagrees with top-level unit: {unit.path}")
            if not unit.oversized or unit.size_bytes <= self.usable_capacity_bytes:
                raise CatalogValidationError(
                    f"oversized unit must be marked oversized and exceed usable capacity: {unit.path}"
                )
            oversized_by_stable[stable_id] = unit

        expected_group_numbers = list(range(1, len(self.groups) + 1))
        actual_group_numbers = [group.group_no for group in self.groups]
        if actual_group_numbers != expected_group_numbers:
            raise CatalogValidationError(
                f"plan group numbers must be unique and consecutive from 1: {actual_group_numbers!r}"
            )

        seen_group_units: dict[str, int] = {}
        for group in self.groups:
            if group.capacity_bytes != self.usable_capacity_bytes:
                raise CatalogValidationError(
                    f"group {group.group_no} capacity {group.capacity_bytes} "
                    f"does not equal plan usable capacity {self.usable_capacity_bytes}"
                )
            group.validate_consistency()
            for grouped_unit in group.units:
                stable_id = _unit_stable_identity(grouped_unit)
                top = top_by_stable.get(stable_id)
                if top is None:
                    raise CatalogValidationError(
                        f"group {group.group_no} contains unit absent from top-level units: {grouped_unit.path}"
                    )
                if _unit_signature(grouped_unit) != _unit_signature(top):
                    raise CatalogValidationError(
                        f"group {group.group_no} unit snapshot does not match top-level unit: {grouped_unit.path}"
                    )
                if grouped_unit.oversized or stable_id in oversized_by_stable:
                    raise CatalogValidationError(
                        f"oversized unit appears in a regular group: {grouped_unit.path}"
                    )
                if stable_id in seen_group_units:
                    previous = seen_group_units[stable_id]
                    raise CatalogValidationError(
                        f"plan unit appears in multiple groups ({previous}, {group.group_no}): {grouped_unit.path}"
                    )
                seen_group_units[stable_id] = group.group_no

        for stable_id, unit in top_by_stable.items():
            if stable_id in oversized_by_stable:
                if stable_id in seen_group_units:
                    raise CatalogValidationError(f"oversized unit is also present in a regular group: {unit.path}")
                continue
            if unit.size_bytes > self.usable_capacity_bytes:
                raise CatalogValidationError(
                    f"unit exceeds usable capacity but is not listed as oversized: {unit.path}"
                )
            if seen_group_units.get(stable_id) is None:
                raise CatalogValidationError(f"non-oversized unit is missing from plan groups: {unit.path}")

    def validate_execution(self) -> None:
        """Validate durable execution progress against this plan partition."""

        if not isinstance(self.execution, PlanExecution):
            raise CatalogValidationError("plan execution must be a PlanExecution object")
        if self.execution.status not in PLAN_EXECUTION_STATUSES:
            raise CatalogValidationError(f"unsupported plan execution status {self.execution.status!r}")
        if not isinstance(self.execution.groups, list) or not all(
            isinstance(group, PlanGroupExecution) for group in self.execution.groups
        ):
            raise CatalogValidationError("plan execution groups must be PlanGroupExecution objects")
        for execution_group in self.execution.groups:
            # Dataclasses remain mutable for the executor's atomic progress
            # updates.  Re-run field-level validation before persistence so a
            # caller cannot mutate a valid object into malformed JSON.
            execution_group.__post_init__()
        expected_group_numbers = list(range(1, len(self.groups) + 1))
        actual_group_numbers = [group.group_no for group in self.execution.groups]
        if actual_group_numbers != expected_group_numbers:
            raise CatalogValidationError(
                "plan execution groups must exactly match plan group numbers: "
                f"{actual_group_numbers!r} != {expected_group_numbers!r}"
            )

        plan_groups = {group.group_no: group for group in self.groups}
        completed_ids: set[str] = set()
        completed_archives: set[str] = set()
        any_progress = False
        active_groups: list[int] = []
        bound_tape_uuids: set[str] = set()
        for execution_group in self.execution.groups:
            group = plan_groups[execution_group.group_no]
            if execution_group.tape_uuid is not None:
                if execution_group.tape_uuid in bound_tape_uuids:
                    raise CatalogValidationError(
                        f"tape UUID is assigned to multiple execution groups: {execution_group.tape_uuid!r}"
                    )
                bound_tape_uuids.add(execution_group.tape_uuid)
            expected_ids = {_unit_stable_identity(unit) for unit in group.units}
            actual_ids = set(execution_group.completed_units)
            unknown_ids = actual_ids - expected_ids
            if unknown_ids:
                raise CatalogValidationError(
                    f"execution group {group.group_no} contains unknown completed unit(s): "
                    f"{sorted(unknown_ids)!r}"
                )
            duplicate_ids = actual_ids & completed_ids
            if duplicate_ids:
                raise CatalogValidationError(
                    f"completed plan unit appears in multiple execution groups: {sorted(duplicate_ids)!r}"
                )
            completed_ids.update(actual_ids)
            for archive_uuid in execution_group.completed_units.values():
                if archive_uuid in completed_archives:
                    raise CatalogValidationError(
                        f"archive UUID is assigned to multiple completed plan units: {archive_uuid!r}"
                    )
                completed_archives.add(archive_uuid)

            if execution_group.status == "pending":
                if execution_group.tape_id is not None or actual_ids:
                    raise CatalogValidationError(
                        f"pending execution group {group.group_no} has a binding or completed units"
                    )
            elif execution_group.status == "in_progress":
                any_progress = True
                active_groups.append(group.group_no)
                if actual_ids == expected_ids:
                    raise CatalogValidationError(
                        f"execution group {group.group_no} has all units but is not complete"
                    )
            elif execution_group.status == "complete":
                any_progress = True
                if actual_ids != expected_ids:
                    missing = sorted(expected_ids - actual_ids)
                    raise CatalogValidationError(
                        f"complete execution group {group.group_no} is missing unit(s): {missing!r}"
                    )

        # Group execution is sequential: the only legal state sequence is
        # ``complete* + in_progress? + pending*``.  In particular, a pending
        # group may not be followed by an active/complete group merely because
        # the JSON happened to contain otherwise valid individual records.
        if len(active_groups) > 1:
            raise CatalogValidationError("more than one plan group is in progress")
        seen_in_progress = False
        seen_pending = False
        for execution_group in self.execution.groups:
            if execution_group.status == "complete":
                if seen_in_progress or seen_pending:
                    raise CatalogValidationError(
                        f"complete execution group {execution_group.group_no} follows an incomplete group"
                    )
            elif execution_group.status == "in_progress":
                if seen_in_progress or seen_pending:
                    raise CatalogValidationError(
                        f"in-progress execution group {execution_group.group_no} follows an incomplete group"
                    )
                seen_in_progress = True
            else:  # pending
                seen_pending = True

        if self.execution.status == "planned" and any_progress:
            raise CatalogValidationError("planned execution cannot contain group progress")
        if self.execution.status == "in_progress":
            if not any_progress:
                raise CatalogValidationError("in_progress execution must contain group progress")
            if all(group.status == "complete" for group in self.execution.groups):
                raise CatalogValidationError("all groups are complete but plan execution is not complete")
        if self.execution.status == "complete":
            if not all(group.status == "complete" for group in self.execution.groups):
                raise CatalogValidationError("complete plan execution requires every group to be complete")
            if completed_ids != {
                _unit_stable_identity(unit) for group in self.groups for unit in group.units
            }:
                raise CatalogValidationError("complete plan execution does not cover every group unit")

    def to_dict(self) -> dict[str, Any]:
        # Revalidate before every persistence operation as callers can mutate
        # the lists after construction.
        self.validate_partition()
        self.validate_execution()
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "PlanRecord":
        _schema(data)
        _reject_unknown_fields(
            data,
            {
                "schema_version",
                "plan_id",
                "created_at",
                "media_type",
                "nominal_capacity_bytes",
                "recommended_capacity_bytes",
                "usable_capacity_bytes",
                "packing_algorithm",
                "source_paths",
                "units",
                "groups",
                "oversized_units",
                "unit_depth",
                "execution",
            },
            "plan",
        )
        raw_units = data.get("units", [])
        raw_groups = data.get("groups", [])
        raw_oversized = data.get("oversized_units", [])
        if not all(isinstance(value, list) for value in (raw_units, raw_groups, raw_oversized)):
            raise CatalogValidationError("plan units, groups, and oversized_units must be lists")
        source_paths = data.get("source_paths", [])
        if not isinstance(source_paths, list):
            raise CatalogValidationError("plan source_paths must be a list")
        return cls(
            plan_id=_required_string(data.get("plan_id"), "plan_id"),
            created_at=_required_string(data.get("created_at"), "created_at"),
            media_type=_required_string(data.get("media_type"), "media_type"),
            nominal_capacity_bytes=_nonnegative_int(
                data.get("nominal_capacity_bytes"), "nominal_capacity_bytes"
            ),
            recommended_capacity_bytes=_nonnegative_int(
                data.get("recommended_capacity_bytes"), "recommended_capacity_bytes"
            ),
            usable_capacity_bytes=_nonnegative_int(data.get("usable_capacity_bytes"), "usable_capacity_bytes"),
            packing_algorithm=_required_string(data.get("packing_algorithm"), "packing_algorithm"),
            source_paths=list(source_paths),
            units=[unit if isinstance(unit, PlanUnit) else PlanUnit.from_dict(unit) for unit in raw_units],
            groups=[group if isinstance(group, PlanGroup) else PlanGroup.from_dict(group) for group in raw_groups],
            oversized_units=[
                unit if isinstance(unit, PlanUnit) else PlanUnit.from_dict(unit) for unit in raw_oversized
            ],
            unit_depth=data.get("unit_depth"),
            execution=data.get("execution"),
        )


@dataclass
class OperationRecord(Serializable):
    operation_uuid: str
    state: str
    tape_id: str
    tape_uuid: str
    archive_uuid: str | None = None
    archive_name: str | None = None
    expected_tape_file_no: int | None = None
    created_at: str = field(default_factory=utc_now)
    updated_at: str = field(default_factory=utc_now)
    error: str | None = None
    schema_version: int = field(default=SCHEMA_VERSION, init=False)

    def __post_init__(self) -> None:
        _required_string(self.operation_uuid, "operation_uuid")
        _required_string(self.tape_id, "tape_id")
        _required_string(self.tape_uuid, "tape_uuid")
        _required_string(self.created_at, "created_at")
        _required_string(self.updated_at, "updated_at")
        if self.state not in OPERATION_STATES:
            raise CatalogValidationError(f"unsupported operation state {self.state!r}")
        if self.expected_tape_file_no is not None:
            _nonnegative_int(self.expected_tape_file_no, "expected_tape_file_no")
        if self.archive_uuid is not None:
            _required_string(self.archive_uuid, "archive_uuid")
        if self.archive_name is not None:
            _required_string(self.archive_name, "archive_name")
        if self.error is not None and not isinstance(self.error, str):
            raise CatalogValidationError("operation error must be a string or null")

    @classmethod
    def new(
        cls,
        tape_id: str,
        tape_uuid: str,
        *,
        archive_uuid: str | None = None,
        archive_name: str | None = None,
        expected_tape_file_no: int | None = None,
    ) -> "OperationRecord":
        return cls(
            operation_uuid=str(uuid4()),
            state="prepared",
            tape_id=tape_id,
            tape_uuid=tape_uuid,
            archive_uuid=archive_uuid,
            archive_name=archive_name,
            expected_tape_file_no=expected_tape_file_no,
        )

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "OperationRecord":
        _schema(data)
        _reject_unknown_fields(
            data,
            {
                "schema_version",
                "operation_uuid",
                "state",
                "tape_id",
                "tape_uuid",
                "archive_uuid",
                "archive_name",
                "expected_tape_file_no",
                "created_at",
                "updated_at",
                "error",
            },
            "operation",
        )
        return cls(
            operation_uuid=_required_string(data.get("operation_uuid"), "operation_uuid"),
            state=_required_string(data.get("state"), "state"),
            tape_id=_required_string(data.get("tape_id"), "tape_id"),
            tape_uuid=_required_string(data.get("tape_uuid"), "tape_uuid"),
            archive_uuid=data.get("archive_uuid"),
            archive_name=data.get("archive_name"),
            expected_tape_file_no=data.get("expected_tape_file_no"),
            created_at=_required_string(data.get("created_at"), "created_at"),
            updated_at=_required_string(data.get("updated_at"), "updated_at"),
            error=data.get("error"),
        )
