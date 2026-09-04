"""Explicit catalog-only archive status operations."""

from __future__ import annotations

from ..catalog.models import ArchiveRecord
from ..catalog.store import CatalogStore
from ..errors import CatalogError


def mark_obsolete(store: CatalogStore, reference: str) -> ArchiveRecord:
    """Mark one uniquely resolved archive obsolete without touching tape data.

    Physical tape references and manifests intentionally remain unchanged:
    obsolete bytes still consume append-only capacity and remain searchable.
    """

    archive = store.find_archive(reference)
    if archive.status == "obsolete":
        return archive
    if archive.status not in {"active", "unverified", "corrupt"}:
        raise CatalogError(
            f"archive {archive.archive_uuid!r} cannot be marked obsolete from status {archive.status!r}"
        )
    archive.status = "obsolete"
    store.save_archive(archive)
    return archive


def mark_archive_obsolete(store: CatalogStore, reference: str) -> ArchiveRecord:
    """Readable compatibility alias for :func:`mark_obsolete`."""

    return mark_obsolete(store, reference)

