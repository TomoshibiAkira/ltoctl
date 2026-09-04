"""Capacity parsing and human-readable byte formatting."""

from __future__ import annotations

import re
from decimal import Decimal, InvalidOperation


_UNIT_FACTORS: dict[str, int] = {
    "b": 1,
    "kb": 10**3,
    "mb": 10**6,
    "gb": 10**9,
    "tb": 10**12,
    "pb": 10**15,
    "kib": 2**10,
    "mib": 2**20,
    "gib": 2**30,
    "tib": 2**40,
    "pib": 2**50,
}


def parse_bytes(value: str | int | float | Decimal) -> int:
    """Parse bytes or a decimal/binary capacity such as ``2.30TB``."""

    if isinstance(value, bool):
        raise ValueError("capacity must be a number, not a boolean")
    if isinstance(value, int):
        if value < 0:
            raise ValueError("capacity cannot be negative")
        return value
    if isinstance(value, (float, Decimal)):
        number = Decimal(str(value))
        if number < 0:
            raise ValueError("capacity cannot be negative")
        return int(number)
    text = value.strip().replace(" ", "")
    match = re.fullmatch(r"([0-9]+(?:\.[0-9]+)?)([a-zA-Z]*)", text)
    if not match:
        raise ValueError(f"invalid capacity: {value!r}")
    try:
        number = Decimal(match.group(1))
    except InvalidOperation as exc:
        raise ValueError(f"invalid capacity: {value!r}") from exc
    unit = match.group(2).lower() or "b"
    if unit not in _UNIT_FACTORS:
        raise ValueError(f"unknown capacity unit {match.group(2)!r}")
    return int(number * _UNIT_FACTORS[unit])


def format_bytes(value: int | float, *, binary: bool = False, precision: int = 2) -> str:
    """Format bytes using explicit decimal TB/GB or binary TiB/GiB units."""

    value = float(value)
    base = 1024.0 if binary else 1000.0
    units = ("B", "KiB", "MiB", "GiB", "TiB", "PiB") if binary else ("B", "KB", "MB", "GB", "TB", "PB")
    index = 0
    while abs(value) >= base and index < len(units) - 1:
        value /= base
        index += 1
    if index == 0:
        return f"{int(value)} {units[index]}"
    return f"{value:.{precision}f} {units[index]}"
