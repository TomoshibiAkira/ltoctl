"""Deterministic first-fit decreasing bin packing."""

from __future__ import annotations

from collections.abc import Iterable

from ..catalog.models import PlanGroup, PlanUnit
from ..errors import PlannerError


def pack_units_ffd(units: Iterable[PlanUnit], capacity_bytes: int) -> tuple[list[PlanGroup], list[PlanUnit]]:
    """Pack units into deterministic FFD groups and return oversized units.

    Ties are resolved by normalized path, making saved plans reproducible for
    the same source snapshot.  A unit larger than the usable capacity is never
    split or silently dropped; it is returned in ``oversized``.
    """

    if isinstance(capacity_bytes, bool) or not isinstance(capacity_bytes, int) or capacity_bytes <= 0:
        raise PlannerError("capacity_bytes must be a positive integer")
    ordered = sorted(units, key=lambda unit: (-unit.size_bytes, unit.path.casefold(), unit.path))
    oversized: list[PlanUnit] = []
    groups: list[PlanGroup] = []
    for unit in ordered:
        if unit.size_bytes > capacity_bytes:
            unit.oversized = True
            oversized.append(unit)
            continue
        unit.oversized = False
        placed = False
        for group in groups:
            if group.used_bytes + unit.size_bytes <= capacity_bytes:
                group.add_unit(unit)
                placed = True
                break
        if not placed:
            groups.append(
                PlanGroup(
                    group_no=len(groups) + 1,
                    units=[unit],
                    used_bytes=unit.size_bytes,
                    capacity_bytes=capacity_bytes,
                )
            )
    # Keep fields consistent for groups created before a caller mutates a
    # unit list and make the return order stable.
    for number, group in enumerate(groups, 1):
        group.group_no = number
    return groups, oversized


def pack_first_fit_decreasing(units: Iterable[PlanUnit], capacity_bytes: int) -> tuple[list[PlanGroup], list[PlanUnit]]:
    """Readable alias for the public MVP algorithm."""

    return pack_units_ffd(units, capacity_bytes)
