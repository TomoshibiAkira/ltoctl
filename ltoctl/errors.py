"""Domain errors used by ltoctl.

Keeping errors small and typed makes it possible for both the CLI and callers
embedding the library to present useful diagnostics without parsing messages.
"""


class LtoctlError(Exception):
    """Base class for expected ltoctl failures."""


class CatalogError(LtoctlError):
    """The canonical catalog is missing, malformed, or inconsistent."""


class CatalogValidationError(CatalogError):
    """Raised when a catalog object fails schema or relationship validation."""


class ScanError(LtoctlError):
    """A source path cannot be safely scanned."""


class PlannerError(LtoctlError):
    """Planner input is invalid."""


class TapeError(LtoctlError):
    """A semantic tape operation cannot be completed safely."""


class SafetyError(TapeError):
    """A required identity, append-position, or write-safety check failed."""


class ArchiveError(LtoctlError):
    """An archive stream is invalid or cannot be created."""


class RestoreError(ArchiveError):
    """An archive cannot be safely restored to the requested output."""


class VerificationError(ArchiveError):
    """An archive or tape failed structural/hash verification."""


class ReconcileError(ArchiveError):
    """An interrupted operation cannot be reconciled safely."""
