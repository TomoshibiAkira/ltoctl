"""Source-unit expansion and saved-plan snapshot handling."""

from __future__ import annotations

import os
from collections.abc import Iterable, Iterator
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from ..archive.scanner import ArchiveScanner
from ..catalog.models import PlanRecord, PlanUnit
from ..errors import PlannerError, ScanError
from ..utils.units import parse_bytes
from .packing import pack_units_ffd

MEDIA_PROFILES: dict[str, tuple[str, int, int]] = {
    "lto5": ("LTO-5", 1_500_000_000_000, 1_400_000_000_000),
    "lto6": ("LTO-6", 2_500_000_000_000, 2_350_000_000_000),
    "lto7": ("LTO-7", 6_000_000_000_000, 5_700_000_000_000),
    "lto8": ("LTO-8", 12_000_000_000_000, 11_400_000_000_000),
    "lto9": ("LTO-9", 18_000_000_000_000, 17_100_000_000_000),
}


@dataclass(frozen=True)
class SourceChange:
    unit_id: str
    path: str
    planned_size_bytes: int
    current_size_bytes: int | None
    planned_file_count: int
    current_file_count: int | None
    planned_mtime_ns: int
    current_mtime_ns: int | None
    reason: str
    planned_snapshot_fingerprint: str | None = None
    current_snapshot_fingerprint: str | None = None

    def to_dict(self) -> dict[str, object]:
        return {
            "unit_id": self.unit_id,
            "path": self.path,
            "planned_size_bytes": self.planned_size_bytes,
            "current_size_bytes": self.current_size_bytes,
            "planned_file_count": self.planned_file_count,
            "current_file_count": self.current_file_count,
            "planned_mtime_ns": self.planned_mtime_ns,
            "current_mtime_ns": self.current_mtime_ns,
            "reason": self.reason,
            "planned_snapshot_fingerprint": self.planned_snapshot_fingerprint,
            "current_snapshot_fingerprint": self.current_snapshot_fingerprint,
        }


def _absolute(value: str | os.PathLike[str]) -> Path:
    path = Path(value).expanduser()
    return path if path.is_absolute() else Path.cwd() / path


def _children_at_depth(root: Path, depth: int) -> list[Path]:
    if depth < 0:
        raise PlannerError("unit depth cannot be negative")
    if depth == 0:
        return [root]
    if not root.exists() and not root.is_symlink():
        raise ScanError(f"source does not exist: {root}")
    frontier = [root]
    for _ in range(depth):
        next_frontier: list[Path] = []
        for parent in frontier:
            if not parent.is_dir() or parent.is_symlink():
                # A file/leaf cannot be expanded further.  Retain it as the
                # indivisible unit rather than failing on ``iterdir``.
                next_frontier.append(parent)
                continue
            try:
                children = sorted(parent.iterdir(), key=lambda child: child.name.encode("utf-8"))
            except OSError as exc:
                raise ScanError(f"cannot expand {parent}: {exc}") from exc
            for child in children:
                # Do not recurse through a symlink even when it points at a
                # directory; doing so would make a plan non-reproducible.
                next_frontier.append(child)
        frontier = next_frontier
    return frontier


def expand_units(source_paths: Iterable[str | os.PathLike[str]], unit_depth: int | None = None) -> list[Path]:
    paths = [_absolute(value) for value in source_paths]
    if not paths:
        raise PlannerError("at least one source path is required")
    if unit_depth is None:
        units = paths
    else:
        units = []
        for root in paths:
            units.extend(_children_at_depth(root, unit_depth))
    seen: set[str] = set()
    result: list[Path] = []
    for path in units:
        key = str(path)
        if key in seen:
            raise PlannerError(f"source path appears in more than one archive unit: {path}")
        seen.add(key)
        result.append(path)
    if not result:
        raise PlannerError("unit expansion produced no source paths")
    return result


def _make_unit(path: Path) -> PlanUnit:
    result = ArchiveScanner().scan([path])
    try:
        stat = path.lstat()
    except OSError as exc:
        raise ScanError(f"cannot inspect source {path}: {exc}") from exc
    return PlanUnit(
        path=str(path),
        name=path.name or path.anchor.strip("/") or "root",
        size_bytes=result.logical_size_bytes,
        file_count=result.file_count,
        mtime_ns=stat.st_mtime_ns,
        source_paths=[str(path)],
        unit_id=str(path),
        snapshot_fingerprint=result.snapshot_fingerprint,
    )


def _profile(media: str) -> tuple[str, int, int]:
    key = media.lower().replace("-", "")
    try:
        return MEDIA_PROFILES[key]
    except KeyError as exc:
        raise PlannerError(f"unsupported media type {media!r}; known media: {', '.join(MEDIA_PROFILES)}") from exc


def plan_sources(
    source_paths: Iterable[str | os.PathLike[str]],
    *,
    media: str = "lto6",
    capacity: str | int | None = None,
    unit_depth: int | None = None,
    plan_id: str | None = None,
) -> PlanRecord:
    source_values = list(source_paths)
    media_type, nominal, recommended = _profile(media)
    if capacity is not None:
        recommended = parse_bytes(capacity)
        if recommended <= 0:
            raise PlannerError("capacity must be positive")
    units = [_make_unit(path) for path in expand_units(source_values, unit_depth)]
    groups, oversized = pack_units_ffd(units, recommended)
    return PlanRecord(
        plan_id=plan_id or f"plan-{datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')}",
        created_at=datetime.now(timezone.utc).isoformat(),
        media_type=media_type,
        nominal_capacity_bytes=nominal,
        recommended_capacity_bytes=recommended,
        usable_capacity_bytes=recommended,
        packing_algorithm="first_fit_decreasing",
        source_paths=[str(_absolute(value)) for value in source_values],
        units=units,
        groups=groups,
        oversized_units=oversized,
        unit_depth=unit_depth,
    )


def rescan_plan(plan: PlanRecord, *, packed_only: bool = False) -> list[SourceChange]:
    """Rescan planned units and report snapshot differences.

    Oversized units are not packed.  ``packed_only`` skips them so source
    drift on those trees does not block writing the groups that already fit.
    """

    changes: list[SourceChange] = []
    units = [unit for unit in plan.units if not unit.oversized] if packed_only else list(plan.units)
    for unit in units:
        try:
            current = _make_unit(Path(unit.path))
        except (ScanError, OSError) as exc:
            changes.append(
                SourceChange(
                    unit_id=unit.unit_id or unit.path,
                    path=unit.path,
                    planned_size_bytes=unit.size_bytes,
                    current_size_bytes=None,
                    planned_file_count=unit.file_count,
                    current_file_count=None,
                    planned_mtime_ns=unit.mtime_ns,
                    current_mtime_ns=None,
                    reason=str(exc),
                    planned_snapshot_fingerprint=unit.snapshot_fingerprint,
                )
            )
            continue
        reasons: list[str] = []
        if current.size_bytes != unit.size_bytes:
            reasons.append("size changed")
        if current.file_count != unit.file_count:
            reasons.append("file count changed")
        if current.mtime_ns != unit.mtime_ns:
            reasons.append("mtime changed")
        if unit.snapshot_fingerprint and current.snapshot_fingerprint != unit.snapshot_fingerprint:
            reasons.append("metadata snapshot changed")
        if reasons:
            changes.append(
                SourceChange(
                    unit_id=unit.unit_id or unit.path,
                    path=unit.path,
                    planned_size_bytes=unit.size_bytes,
                    current_size_bytes=current.size_bytes,
                    planned_file_count=unit.file_count,
                    current_file_count=current.file_count,
                    planned_mtime_ns=unit.mtime_ns,
                    current_mtime_ns=current.mtime_ns,
                    reason=", ".join(reasons),
                    planned_snapshot_fingerprint=unit.snapshot_fingerprint,
                    current_snapshot_fingerprint=current.snapshot_fingerprint,
                )
            )
    return changes
