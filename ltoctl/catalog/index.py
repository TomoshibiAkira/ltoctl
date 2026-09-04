"""Rebuildable TSV search index for canonical archive manifests."""

from __future__ import annotations

import csv
import re
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path

from ..errors import CatalogError
from ..utils.atomic import atomic_write_stream
from .models import ArchiveRecord
from .store import CatalogStore

INDEX_COLUMNS = ("tape_id", "tape_file_no", "archive_uuid", "archive_name", "size", "mtime_ns", "path")


def _tsv_line(values: list[str]) -> str:
    # csv.writer handles tabs/newlines in user paths without producing an
    # ambiguous index.  It writes to a tiny in-memory row only.
    from io import StringIO

    buffer = StringIO()
    writer = csv.writer(buffer, delimiter="\t", lineterminator="\n", quoting=csv.QUOTE_MINIMAL)
    writer.writerow(values)
    return buffer.getvalue()


def rebuild_index(store: CatalogStore) -> Path:
    """Recreate ``index/files.tsv`` from archives and manifests atomically."""

    destination = store.root / "index" / "files.tsv"

    def rows() -> Iterator[str]:
        yield _tsv_line(list(INDEX_COLUMNS))
        for archive in store.iter_archives():
            for entry in store.iter_manifest(archive.archive_uuid):
                yield _tsv_line(
                    [
                        archive.tape_id,
                        str(archive.tape_file_no),
                        archive.archive_uuid,
                        archive.name,
                        str(entry.size),
                        str(entry.mtime_ns),
                        entry.path,
                    ]
                )

    return atomic_write_stream(destination, rows())


@dataclass(frozen=True)
class SearchResult:
    tape_id: str
    tape_file_no: int
    archive_uuid: str
    archive_name: str
    size: int
    mtime_ns: int
    path: str

    @classmethod
    def from_row(cls, row: dict[str, str]) -> "SearchResult":
        try:
            return cls(
                tape_id=row["tape_id"],
                tape_file_no=int(row["tape_file_no"]),
                archive_uuid=row["archive_uuid"],
                archive_name=row["archive_name"],
                size=int(row["size"]),
                mtime_ns=int(row["mtime_ns"]),
                path=row["path"],
            )
        except (KeyError, ValueError) as exc:
            raise CatalogError(f"invalid index row: {row!r}") from exc

    def to_dict(self) -> dict[str, str | int]:
        return {
            "tape_id": self.tape_id,
            "tape_file_no": self.tape_file_no,
            "archive_uuid": self.archive_uuid,
            "archive_name": self.archive_name,
            "size": self.size,
            "mtime_ns": self.mtime_ns,
            "path": self.path,
        }


def search_index(
    index_path: str | Path,
    query: str,
    *,
    exact: bool = False,
    regex: bool = False,
    tape: str | None = None,
    archive: str | None = None,
) -> Iterator[SearchResult]:
    """Stream matching rows; never load the complete TSV into memory."""

    if exact and regex:
        raise ValueError("exact and regex search modes are mutually exclusive")
    if regex:
        try:
            pattern = re.compile(query, re.IGNORECASE)
        except re.error as exc:
            raise ValueError(f"invalid search regular expression: {exc}") from exc
    else:
        pattern = None
    folded_query = query.casefold()
    path = Path(index_path)
    try:
        stream = path.open("r", encoding="utf-8", newline="")
    except FileNotFoundError as exc:
        raise CatalogError(f"search index does not exist: {path}; run catalog rebuild-index") from exc
    try:
        reader = csv.DictReader(stream, delimiter="\t")
        if tuple(reader.fieldnames or ()) != INDEX_COLUMNS:
            raise CatalogError(f"index header must be: {' '.join(INDEX_COLUMNS)}")
        for raw in reader:
            result = SearchResult.from_row(raw)
            if tape is not None and result.tape_id != tape:
                continue
            if archive is not None and archive not in {result.archive_uuid, result.archive_name}:
                continue
            haystack = result.path
            if regex:
                matched = bool(pattern and pattern.search(haystack))
            elif exact:
                matched = haystack.casefold() == folded_query
            else:
                matched = folded_query in haystack.casefold()
            if matched:
                yield result
    finally:
        stream.close()


def rebuild(store: CatalogStore) -> Path:
    """Compatibility alias used by callers and the CLI."""

    return rebuild_index(store)
