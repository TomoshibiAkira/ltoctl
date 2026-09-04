"""JSONL manifest serialization."""

from __future__ import annotations

import json
from collections.abc import Iterable, Iterator
from pathlib import Path
from typing import Any

from ..catalog.models import ManifestEntry
from ..errors import CatalogError
from ..utils.atomic import atomic_write_stream


def manifest_dict(entry: ManifestEntry | dict[str, Any]) -> dict[str, Any]:
    value = entry.to_dict() if isinstance(entry, ManifestEntry) else dict(entry)
    value.pop("schema_version", None)
    # Avoid emitting an absent optional value for compact, grep-friendly JSONL.
    if value.get("link_target") is None:
        value.pop("link_target", None)
    return value


def manifest_line(entry: ManifestEntry | dict[str, Any]) -> str:
    return json.dumps(manifest_dict(entry), ensure_ascii=False, sort_keys=True, separators=(",", ":")) + "\n"


def write_manifest(path: str | Path, entries: Iterable[ManifestEntry | dict[str, Any]]) -> Path:
    return atomic_write_stream(path, (manifest_line(entry) for entry in entries))


def iter_manifest_file(path: str | Path) -> Iterator[ManifestEntry]:
    manifest_path = Path(path)
    try:
        stream = manifest_path.open("r", encoding="utf-8")
    except FileNotFoundError as exc:
        raise CatalogError(f"missing manifest: {manifest_path}") from exc
    try:
        for line_no, line in enumerate(stream, 1):
            if not line.strip():
                continue
            try:
                value = json.loads(line)
                if not isinstance(value, dict):
                    raise ValueError("line is not an object")
                yield ManifestEntry.from_dict(value)
            except (UnicodeDecodeError, json.JSONDecodeError, ValueError, CatalogError) as exc:
                raise CatalogError(f"invalid manifest {manifest_path}:{line_no}: {exc}") from exc
    finally:
        stream.close()
