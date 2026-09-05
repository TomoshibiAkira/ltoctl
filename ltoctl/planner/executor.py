"""Safe orchestration for executing a saved archive plan.

Plan execution deliberately stays above :class:`ArchiveWriter`: every unit is
still appended through the normal tape identity, EOD, journal and catalog
commit path.  This module only persists which already-committed archive owns a
plan unit and which tape is assigned to each group.
"""

from __future__ import annotations

import inspect
from collections.abc import Callable
from dataclasses import dataclass, field

from ..archive.writer import ArchiveWriter
from ..catalog.models import (
    ArchiveRecord,
    PlanGroup,
    PlanGroupExecution,
    PlanRecord,
    PlanUnit,
    derive_plan_archive_name,
    derive_plan_archive_uuid,
)
from ..catalog.store import CatalogStore
from ..errors import CatalogError, PlannerError, SafetyError, TapeError
from ..tape.safety import TapeSafetyService
from .scanner import SourceChange, rescan_plan


class PlanSourceDriftError(PlannerError):
    """Raised when a plan's source snapshot is no longer exact."""

    def __init__(self, changes: list[SourceChange]):
        self.changes = list(changes)
        summary = "; ".join(f"{change.path}: {change.reason}" for change in self.changes)
        super().__init__(f"plan source snapshot changed; refusing execution: {summary}")

    def to_dict(self) -> dict[str, object]:
        return {"ok": False, "error": str(self), "source_changes": [change.to_dict() for change in self.changes]}


@dataclass
class PlanApplyResult:
    """Machine-readable outcome of one plan-apply invocation."""

    plan_id: str
    status: str
    group_no: int | None = None
    bound_tape_id: str | None = None
    bound_tape_uuid: str | None = None
    initialized_tape_id: str | None = None
    applied_units: list[str] = field(default_factory=list)
    skipped_units: list[str] = field(default_factory=list)
    recovered_units: list[str] = field(default_factory=list)
    oversized_units: list[str] = field(default_factory=list)
    source_changes: list[SourceChange] = field(default_factory=list)

    def to_dict(self) -> dict[str, object]:
        return {
            "ok": not self.source_changes,
            "plan_id": self.plan_id,
            "status": self.status,
            "group_no": self.group_no,
            "bound_tape_id": self.bound_tape_id,
            "bound_tape_uuid": self.bound_tape_uuid,
            "initialized_tape_id": self.initialized_tape_id,
            "applied_units": list(self.applied_units),
            "skipped_units": list(self.skipped_units),
            "recovered_units": list(self.recovered_units),
            "oversized_units": list(self.oversized_units),
            "source_changes": [change.to_dict() for change in self.source_changes],
        }


BeforeGroup = Callable[..., object | None]
OnBlankTape = Callable[..., str | None]
OnApplyProgress = Callable[[str, dict[str, object]], None]


def _unit_key(unit: PlanUnit) -> str:
    return unit.unit_id or unit.path


class PlanExecutor:
    """Execute or resume a persisted :class:`PlanRecord` safely."""

    def __init__(self, store: CatalogStore, backend, *, safety: TapeSafetyService | None = None):
        self.store = store
        self.backend = backend
        self.safety = safety or TapeSafetyService(store)

    def _load(self, plan: PlanRecord | str) -> PlanRecord:
        return plan if isinstance(plan, PlanRecord) else self.store.load_plan(plan)

    @staticmethod
    def _archive_name(plan: PlanRecord, group: PlanGroup, position: int, unit: PlanUnit) -> str:
        # Keep the natural unit name for the common case (the guided plan UI
        # presents these as archive names).  If a plan contains duplicate
        # names, add deterministic group/position disambiguation so a
        # plan-save failure can still recover the exact committed archive.
        return derive_plan_archive_name(
            unit.name,
            group.group_no,
            position,
            tuple(candidate.name for candidate in plan.units),
        )

    @staticmethod
    def _archive_uuid(plan: PlanRecord, group: PlanGroup, unit: PlanUnit) -> str:
        return derive_plan_archive_uuid(
            plan.plan_id,
            plan.created_at,
            group.group_no,
            _unit_key(unit),
        )

    @staticmethod
    def _sources(unit: PlanUnit) -> list[str]:
        return list(unit.source_paths) or [unit.path]

    @staticmethod
    def _invoke_before_group(callback: BeforeGroup, group: PlanGroup, execution: PlanGroupExecution) -> object | None:
        """Call callbacks with either ``(group, execution)``, ``(group)`` or ``()``.

        The two-argument form exposes the persisted binding for UI code while
        the one-argument form keeps the service pleasant to use in tests and
        small integrations.  Signature inspection avoids swallowing a
        callback's own TypeError.
        """

        try:
            signature = inspect.signature(callback)
        except (TypeError, ValueError):
            return callback(group)
        positional = [
            parameter
            for parameter in signature.parameters.values()
            if parameter.kind
            in {inspect.Parameter.POSITIONAL_ONLY, inspect.Parameter.POSITIONAL_OR_KEYWORD}
        ]
        has_varargs = any(
            parameter.kind == inspect.Parameter.VAR_POSITIONAL
            for parameter in signature.parameters.values()
        )
        first_name = positional[0].name.casefold() if positional else ""
        first_value: object = (
            group.group_no
            if first_name in {"group_no", "group_number", "groupnum", "number", "no"}
            else group
        )
        if has_varargs or len(positional) >= 2:
            return callback(first_value, execution)
        if positional:
            return callback(first_value)
        return callback()

    @staticmethod
    def _progress(
        on_progress: OnApplyProgress | None,
        event: str,
        **details: object,
    ) -> None:
        if on_progress is None:
            return
        on_progress(event, details)

    def _candidate_archive(
        self,
        archive_uuid: str,
        name: str,
        unit: PlanUnit,
        tape_id: str,
        tape_uuid: str,
    ) -> ArchiveRecord | None:
        """Recover one exact deterministic archive UUID, never by metadata."""

        path = self.store.root / "archives" / f"{archive_uuid}.json"
        try:
            archive = self.store.load_archive(archive_uuid)
        except CatalogError as exc:
            if path.exists():
                raise PlannerError(
                    f"deterministic archive record {archive_uuid!r} is unreadable; refusing recovery"
                ) from exc
            return None
        return self._validate_archive_record(
            archive,
            archive_uuid=archive_uuid,
            name=name,
            unit=unit,
            tape_id=tape_id,
            tape_uuid=tape_uuid,
        )

    def _load_exact_archive(self, archive_uuid: str) -> ArchiveRecord | None:
        """Load an exact archive UUID, distinguishing missing from corrupt."""

        path = self.store.root / "archives" / f"{archive_uuid}.json"
        try:
            return self.store.load_archive(archive_uuid)
        except CatalogError as exc:
            if path.exists():
                raise PlannerError(
                    f"deterministic archive record {archive_uuid!r} is unreadable; refusing recovery"
                ) from exc
            return None

    def _validate_archive_reverse_relation(
        self,
        archive: ArchiveRecord,
        *,
        tape_id: str,
        tape_uuid: str,
    ) -> None:
        if archive.tape_id != tape_id or archive.tape_uuid != tape_uuid:
            raise PlannerError(
                f"archive {archive.archive_uuid!r} is bound to the wrong tape "
                f"{archive.tape_id}/{archive.tape_uuid}"
            )
        try:
            tape = self.store.load_tape(tape_id)
        except CatalogError as exc:
            raise PlannerError(f"archive {archive.archive_uuid!r} references unreadable tape {tape_id!r}") from exc
        if tape.uuid != tape_uuid:
            raise PlannerError(f"archive {archive.archive_uuid!r} tape UUID disagrees with catalog tape")
        occurrences = [index for index, value in enumerate(tape.archives) if value == archive.archive_uuid]
        if len(occurrences) != 1:
            raise PlannerError(
                f"archive {archive.archive_uuid!r} must have exactly one reverse tape reference"
            )
        expected_file_no = occurrences[0] + 1
        if archive.tape_file_no != expected_file_no:
            raise PlannerError(
                f"archive {archive.archive_uuid!r} physical file {archive.tape_file_no} "
                f"does not match tape catalog location {expected_file_no}"
            )

    def _validate_archive_record(
        self,
        archive: ArchiveRecord,
        *,
        archive_uuid: str,
        name: str,
        unit: PlanUnit,
        tape_id: str,
        tape_uuid: str,
        require_active: bool = True,
    ) -> ArchiveRecord:
        if archive.archive_uuid != archive_uuid:
            raise PlannerError("archive record UUID does not match deterministic plan identity")
        if archive.name != name:
            raise PlannerError(
                f"archive {archive_uuid!r} name disagrees with plan unit: "
                f"{archive.name!r} != {name!r}"
            )
        if require_active and archive.status != "active":
            raise PlannerError(
                f"deterministic archive {archive_uuid!r} has non-active status {archive.status!r}"
            )
        if not isinstance(archive.tar_stream_sha256, str) or not archive.tar_stream_sha256.strip():
            raise PlannerError(f"deterministic archive {archive_uuid!r} has no committed stream hash")
        if archive.source_paths != self._sources(unit):
            raise PlannerError(f"archive {archive_uuid!r} source paths disagree for {unit.path!r}")
        if archive.logical_size_bytes != unit.size_bytes or archive.file_count != unit.file_count:
            raise PlannerError(f"archive {archive_uuid!r} snapshot disagrees for {unit.path!r}")
        self._validate_archive_reverse_relation(archive, tape_id=tape_id, tape_uuid=tape_uuid)
        return archive

    def _preflight_group_capacity(
        self,
        plan: PlanRecord,
        group: PlanGroup,
        execution: PlanGroupExecution,
        tape_id: str,
        tape_uuid: str,
    ) -> None:
        """Check the complete pending group before its first physical append."""

        try:
            loaded = self.safety.assert_append_ready(self.backend)
        except SafetyError:
            raise
        if loaded.catalog.tape_id != tape_id or loaded.catalog.uuid != tape_uuid:
            raise SafetyError("loaded tape identity changed during group capacity preflight")

        used_bytes = 0
        for position, archive_uuid in enumerate(loaded.catalog.archives, 1):
            try:
                archive = self.store.load_archive(archive_uuid)
            except CatalogError as exc:
                raise SafetyError(
                    f"cannot read physical archive {archive_uuid!r} during group capacity preflight"
                ) from exc
            if archive.tape_id != tape_id or archive.tape_uuid != tape_uuid:
                raise SafetyError(f"archive {archive_uuid!r} disagrees with loaded tape identity")
            if archive.tape_file_no != position:
                raise SafetyError(
                    f"archive {archive_uuid!r} physical file {archive.tape_file_no} "
                    f"does not match catalog position {position}"
                )
            # Obsolete, unverified and corrupt records still occupy physical
            # tape bytes.  Capacity is therefore based on every cataloged
            # physical archive, not just active records.
            used_bytes += archive.logical_size_bytes

        pending_bytes = 0
        for position, unit in enumerate(group.units, 1):
            if _unit_key(unit) in execution.completed_units:
                continue
            # A previous invocation may have committed this exact archive and
            # failed only while saving plan progress.  Treat that physical
            # archive as already complete for capacity purposes; the append
            # loop below will recover it by the same deterministic UUID.
            expected_uuid = self._archive_uuid(plan, group, unit)
            existing = self._load_exact_archive(expected_uuid)
            if existing is not None:
                self._validate_archive_record(
                    existing,
                    archive_uuid=expected_uuid,
                    name=self._archive_name(plan, group, position, unit),
                    unit=unit,
                    tape_id=tape_id,
                    tape_uuid=tape_uuid,
                )
                continue
            pending_bytes += unit.size_bytes
        budget = loaded.catalog.recommended_capacity_bytes
        if budget > 0 and used_bytes + pending_bytes > budget:
            raise SafetyError(
                f"plan group {group.group_no} exceeds tape budget before writing: "
                f"used={used_bytes}, pending={pending_bytes}, budget={budget}"
            )

    def _validate_completed_archive(
        self,
        archive_uuid: str,
        expected_uuid: str,
        name: str,
        unit: PlanUnit,
        tape_id: str,
        tape_uuid: str,
    ) -> ArchiveRecord:
        if archive_uuid != expected_uuid:
            raise PlannerError(
                f"completed archive for {unit.path!r} does not use its deterministic UUID "
                f"{expected_uuid!r}"
            )
        try:
            archive = self.store.load_archive(archive_uuid)
        except CatalogError as exc:
            raise PlannerError(
                f"plan completed unit {unit.path!r} references missing archive {archive_uuid!r}"
            ) from exc
        return self._validate_archive_record(
            archive,
            archive_uuid=archive_uuid,
            name=name,
            unit=unit,
            tape_id=tape_id,
            tape_uuid=tape_uuid,
            require_active=False,
        )

    def _blank_init_tape_id(
        self,
        group: PlanGroup,
        execution: PlanGroupExecution,
        *,
        init_tape_id: str | None,
        confirm_init: bool,
        on_blank_tape: OnBlankTape | None,
    ) -> str:
        if on_blank_tape is not None:
            chosen = on_blank_tape(group, execution)
            if not isinstance(chosen, str) or not chosen.strip():
                raise SafetyError("blank tape initialization declined")
            return chosen.strip()
        if not init_tape_id or not init_tape_id.strip():
            raise SafetyError(
                "loaded tape is blank; initialize it during apply with --init-tape TAPE-ID --yes, "
                "or run: ltoctl tape init TAPE-ID --yes"
            )
        if not confirm_init:
            raise SafetyError("blank tape initialization requires explicit confirmation")
        return init_tape_id.strip()

    def _bind_group(
        self,
        plan: PlanRecord,
        group: PlanGroup,
        execution: PlanGroupExecution,
        *,
        before_group: BeforeGroup | None,
        remap: bool,
        confirm_remap: bool,
        init_tape_id: str | None = None,
        confirm_init: bool = False,
        on_blank_tape: OnBlankTape | None = None,
        on_progress: OnApplyProgress | None = None,
    ) -> tuple[str, str, str | None]:
        if remap:
            if not confirm_remap:
                raise PlannerError("--remap-group requires explicit confirmation")
            if execution.completed_units:
                raise PlannerError(
                    f"cannot remap group {group.group_no} after any unit has completed"
                )
            execution.status = "pending"
            execution.tape_id = None
            execution.tape_uuid = None

        if before_group is not None:
            replacement = self._invoke_before_group(before_group, group, execution)
            if replacement is not None:
                self.backend = replacement

        initialized_tape_id: str | None = None
        try:
            loaded = self.safety.identify_loaded(self.backend, require_writable=True)
        except SafetyError as identity_error:
            # Header read failures are wrapped from TapeError.  Other identity
            # problems (write-protect, unknown cataloged tape, UUID mismatch)
            # must not be treated as a blank cartridge.
            if not isinstance(identity_error.__cause__, TapeError):
                raise
            blank = self.safety.loaded_is_blank(self.backend)
            if execution.tape_uuid is not None:
                if blank:
                    raise SafetyError(
                        f"loaded tape is blank; group {group.group_no} is bound to "
                        f"{execution.tape_id}. Load that labeled cartridge, or remap."
                    ) from identity_error
                raise
            if not blank:
                raise
            tape_id = self._blank_init_tape_id(
                group,
                execution,
                init_tape_id=init_tape_id,
                confirm_init=confirm_init,
                on_blank_tape=on_blank_tape,
            )
            from ..tape.service import init_tape

            init_tape(self.store, self.backend, tape_id, media=plan.media_type, confirm=True)
            initialized_tape_id = tape_id
            loaded = self.safety.identify_loaded(self.backend, require_writable=True)
            self._progress(
                on_progress,
                "initialized",
                tape_id=loaded.catalog.tape_id,
                tape_uuid=loaded.catalog.uuid,
            )
        if loaded.catalog.status != "active":
            raise SafetyError(
                f"tape {loaded.catalog.tape_id} is not active: {loaded.catalog.status!r}"
            )
        current_id = loaded.catalog.tape_id
        current_uuid = loaded.catalog.uuid

        if execution.tape_uuid is not None:
            if execution.tape_id != current_id or execution.tape_uuid != current_uuid:
                raise SafetyError(
                    f"loaded tape {current_id}/{current_uuid} does not match group {group.group_no} "
                    f"binding {execution.tape_id}/{execution.tape_uuid}; use explicit remap"
                )
        else:
            for other in plan.execution.groups:
                if other.group_no == execution.group_no:
                    continue
                if other.tape_uuid == current_uuid:
                    raise SafetyError(
                        f"tape {current_id}/{current_uuid} is already bound to another plan group"
                    )
            execution.tape_id = current_id
            execution.tape_uuid = current_uuid

        execution.status = "in_progress"
        plan.execution.status = "in_progress"
        # Persist the binding before the first physical append.  If this save
        # fails there is no journal/tape write to reconcile, and retrying is
        # safe.
        self.store.save_plan(plan)
        return current_id, current_uuid, initialized_tape_id

    def _eject_if_another_group_remains(
        self,
        plan: PlanRecord,
        group: PlanGroup,
        tape_id: str,
        on_progress: OnApplyProgress | None,
    ) -> None:
        """Unload after a group when a later group still needs a different tape."""

        if not any(
            other.group_no > group.group_no and other.status != "complete"
            for other in plan.execution.groups
        ):
            return
        try:
            self.backend.eject()
        except TapeError:
            raise
        except Exception as exc:
            raise TapeError(
                f"cannot eject tape after plan group {group.group_no}: {exc}"
            ) from exc
        self._progress(on_progress, "ejected", group_no=group.group_no, tape_id=tape_id)

    def _group_targets(self, plan: PlanRecord, group_no: int | None) -> list[PlanGroup]:
        by_no = {group.group_no: group for group in plan.groups}
        if group_no is not None:
            if group_no not in by_no:
                raise PlannerError(f"unknown plan group {group_no}")
            target = by_no[group_no]
            for group in plan.groups:
                if group.group_no >= group_no:
                    break
                execution = next(item for item in plan.execution.groups if item.group_no == group.group_no)
                if execution.status != "complete":
                    raise PlannerError(
                        f"cannot execute group {group_no} before group {group.group_no} is complete"
                    )
            return [target]
        return [
            group
            for group in plan.groups
            if next(item for item in plan.execution.groups if item.group_no == group.group_no).status != "complete"
        ]

    def apply(
        self,
        plan: PlanRecord | str,
        *,
        group_no: int | None = None,
        before_group: BeforeGroup | None = None,
        remap_group: int | None = None,
        confirm_remap: bool = False,
        confirm: bool | None = None,
        init_tape_id: str | None = None,
        confirm_init: bool = False,
        on_blank_tape: OnBlankTape | None = None,
        on_progress: OnApplyProgress | None = None,
    ) -> PlanApplyResult:
        if confirm is not None:
            confirm_remap = confirm
        record = self._load(plan)
        # Validate canonical completed references even when ``plan`` was
        # supplied as an in-memory PlanRecord rather than loaded through the
        # store.  This keeps complete no-op queries from trusting a forged
        # execution map.
        self.store._validate_plan_execution_archives(record)
        if not record.groups:
            raise PlannerError(
                "cannot apply a plan with no packed groups; oversized units must be planned separately"
            )
        deferred = [_unit_key(unit) for unit in record.oversized_units]
        if remap_group is not None:
            if group_no is not None and group_no != remap_group:
                raise PlannerError("--remap-group must name the applied --group")
            group_no = remap_group
            if not confirm_remap:
                raise PlannerError("--remap-group requires explicit confirmation")

        execution_by_no = {item.group_no: item for item in record.execution.groups}
        if remap_group is not None:
            remap_execution = execution_by_no.get(remap_group)
            if remap_execution is None:
                raise PlannerError(f"unknown plan group {remap_group}")
            if remap_execution.completed_units:
                raise PlannerError(
                    f"cannot remap group {remap_group} after any unit has completed"
                )

        # Completion is a durable catalog fact.  Once the execution record is
        # complete, or an explicitly requested group is complete, do not scan
        # source paths again: a successful archive is allowed to outlive (and
        # therefore be safely queried after deletion of) its source tree.
        if remap_group is None:
            if record.execution.status == "complete":
                skipped: list[str] = []
                if group_no is not None:
                    requested_group = next(
                        (group for group in record.groups if group.group_no == group_no),
                        None,
                    )
                    if requested_group is None:
                        raise PlannerError(f"unknown plan group {group_no}")
                    skipped = [_unit_key(unit) for unit in requested_group.units]
                return PlanApplyResult(
                    record.plan_id,
                    record.execution.status,
                    group_no=group_no,
                    skipped_units=skipped,
                    oversized_units=deferred,
                )
            if group_no is not None:
                requested_execution = execution_by_no.get(group_no)
                if requested_execution is None:
                    raise PlannerError(f"unknown plan group {group_no}")
                if requested_execution.status == "complete":
                    requested_group = next(group for group in record.groups if group.group_no == group_no)
                    return PlanApplyResult(
                        record.plan_id,
                        record.execution.status,
                        group_no=group_no,
                        skipped_units=[_unit_key(unit) for unit in requested_group.units],
                        oversized_units=deferred,
                    )

        changes = rescan_plan(record, packed_only=True)
        if changes:
            raise PlanSourceDriftError(changes)

        targets = self._group_targets(record, group_no)
        if not targets:
            return PlanApplyResult(
                record.plan_id,
                record.execution.status,
                group_no=group_no,
                oversized_units=deferred,
            )

        result = PlanApplyResult(
            record.plan_id,
            record.execution.status,
            group_no=group_no,
            oversized_units=deferred,
        )
        for group in targets:
            execution = execution_by_no[group.group_no]
            if remap_group == group.group_no and execution.completed_units:
                raise PlannerError(
                    f"cannot remap group {group.group_no} after any unit has completed"
                )
            if remap_group == group.group_no:
                if execution.tape_uuid and self.store.unresolved_operations(execution.tape_uuid):
                    raise PlannerError(
                        f"cannot remap group {group.group_no} while its previous tape has unresolved operations"
                    )
                for position, unit in enumerate(group.units, 1):
                    expected_uuid = self._archive_uuid(record, group, unit)
                    existing = self._load_exact_archive(expected_uuid)
                    if existing is not None:
                        archive_name = self._archive_name(record, group, position, unit)
                        self._validate_archive_record(
                            existing,
                            archive_uuid=expected_uuid,
                            name=archive_name,
                            unit=unit,
                            tape_id=existing.tape_id,
                            tape_uuid=existing.tape_uuid,
                        )
                        raise PlannerError(
                            f"cannot remap group {group.group_no}: deterministic archive {expected_uuid!r} "
                            f"already exists for {unit.path!r}"
                        )
            if execution.status == "complete":
                # An explicitly requested already-complete group is a
                # successful no-op.  In particular, do not rebind it or
                # transiently change its status while checking progress.
                result.skipped_units.extend(_unit_key(unit) for unit in group.units)
                continue
            remap = remap_group == group.group_no
            tape_id, tape_uuid, initialized_tape_id = self._bind_group(
                record,
                group,
                execution,
                before_group=before_group,
                remap=remap,
                confirm_remap=confirm_remap,
                init_tape_id=init_tape_id,
                confirm_init=confirm_init,
                on_blank_tape=on_blank_tape,
                on_progress=on_progress,
            )
            result.bound_tape_id = tape_id
            result.bound_tape_uuid = tape_uuid
            if initialized_tape_id:
                result.initialized_tape_id = initialized_tape_id
            # This preflight accounts for the whole group before any append,
            # including all already committed physical archives on the tape.
            self._preflight_group_capacity(record, group, execution, tape_id, tape_uuid)
            self._progress(
                on_progress,
                "start_writing",
                group_no=group.group_no,
                tape_id=tape_id,
            )
            for position, unit in enumerate(group.units, 1):
                key = _unit_key(unit)
                archive_name = self._archive_name(record, group, position, unit)
                expected_uuid = self._archive_uuid(record, group, unit)
                if key in execution.completed_units:
                    self._validate_completed_archive(
                        execution.completed_units[key],
                        expected_uuid,
                        archive_name,
                        unit,
                        tape_id,
                        tape_uuid,
                    )
                    result.skipped_units.append(key)
                    continue

                # If the prior invocation committed the tape/catalog append
                # but failed while saving the plan JSON, recover that archive
                # by identity instead of appending the same unit again.
                archive = self._candidate_archive(
                    expected_uuid,
                    archive_name,
                    unit,
                    tape_id,
                    tape_uuid,
                )
                if archive is not None:
                    result.recovered_units.append(key)
                else:
                    archive = ArchiveWriter(self.store, self.backend, safety=self.safety).add(
                        self._sources(unit),
                        name=archive_name,
                        archive_uuid=expected_uuid,
                    )
                    result.applied_units.append(key)
                execution.completed_units[key] = archive.archive_uuid
                if len(execution.completed_units) == len(group.units):
                    execution.status = "complete"
                record.execution.status = (
                    "complete"
                    if all(item.status == "complete" for item in record.execution.groups)
                    else "in_progress"
                )
                # ArchiveWriter returns only after catalog_committed.  This
                # atomic plan save is therefore the first durable progress
                # marker for the unit.
                self.store.save_plan(record)

            execution.status = "complete"
            record.execution.status = (
                "complete"
                if all(item.status == "complete" for item in record.execution.groups)
                else "in_progress"
            )
            self.store.save_plan(record)
            self._eject_if_another_group_remains(record, group, tape_id, on_progress)

        result.status = record.execution.status
        return result


def apply_plan(
    store: CatalogStore,
    backend,
    plan: PlanRecord | str,
    *,
    group_no: int | None = None,
    before_group: BeforeGroup | None = None,
    remap_group: int | None = None,
    confirm_remap: bool = False,
    confirm: bool | None = None,
    init_tape_id: str | None = None,
    confirm_init: bool = False,
    on_blank_tape: OnBlankTape | None = None,
    on_progress: OnApplyProgress | None = None,
) -> PlanApplyResult:
    return PlanExecutor(store, backend).apply(
        plan,
        group_no=group_no,
        before_group=before_group,
        remap_group=remap_group,
        confirm_remap=confirm_remap,
        confirm=confirm,
        init_tape_id=init_tape_id,
        confirm_init=confirm_init,
        on_blank_tape=on_blank_tape,
        on_progress=on_progress,
    )
