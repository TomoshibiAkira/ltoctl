"""Self-describing tape-file-0 header helpers."""

from __future__ import annotations

import io
import json
import tarfile
from typing import BinaryIO

from ..catalog.models import TapeRecord
from ..errors import CatalogValidationError, TapeError

TAPE_HEADER_MEMBER = "__LTOCTL__/tape.json"
TAPE_METADATA_ROOT = "__LTOCTL__"
_HEADER_LIMIT = 4 * 1024 * 1024
_HEADER_PHYSICAL_LIMIT = 16 * 1024 * 1024
_HEADER_COPY_CHUNK = 1024 * 1024


def is_physical_tape_stream(stream: BinaryIO) -> bool:
    """Return whether a stream represents one physical tape file."""

    return bool(getattr(stream, "physical_tape_file", False))


class _BoundedReader:
    """Count and cap bytes consumed from a physical file-0 stream."""

    def __init__(self, target: BinaryIO, limit: int):
        self.target = target
        self.limit = limit
        self.bytes_read = 0

    def read(self, size: int = -1) -> bytes:
        remaining = self.limit - self.bytes_read
        if remaining < 0:
            raise TapeError("tape header exceeds physical size limit")
        request = remaining + 1 if size < 0 else min(size, remaining + 1)
        data = self.target.read(request)
        if data is None:
            return b""
        if self.bytes_read + len(data) > self.limit:
            raise TapeError(f"tape header exceeds physical size limit of {self.limit} bytes")
        self.bytes_read += len(data)
        return data

    def readinto(self, buffer) -> int:
        reader = getattr(self.target, "readinto", None)
        if reader is None:
            data = self.read(len(buffer))
            buffer[: len(data)] = data
            return len(data)
        remaining = self.limit - self.bytes_read
        if remaining < 0:
            raise TapeError("tape header exceeds physical size limit")
        # Ask for at most the remaining bytes; a source that ignores the
        # buffer contract is handled by the count check below.
        view = memoryview(buffer)[:remaining]
        count = reader(view)
        if count is None:
            return 0
        if count < 0 or self.bytes_read + count > self.limit:
            raise TapeError(f"tape header exceeds physical size limit of {self.limit} bytes")
        self.bytes_read += count
        return count

    def __getattr__(self, name: str):
        return getattr(self.target, name)


def build_tape_header(record: TapeRecord) -> bytes:
    """Return a tiny ordinary tar stream containing ``tape.json``."""

    payload = json.dumps(record.to_dict(), ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    output = io.BytesIO()
    with tarfile.open(fileobj=output, mode="w", format=tarfile.PAX_FORMAT) as archive:
        member = tarfile.TarInfo(TAPE_HEADER_MEMBER)
        member.size = len(payload)
        member.mtime = 0
        member.mode = 0o600
        archive.addfile(member, io.BytesIO(payload))
    return output.getvalue()


def parse_tape_header(data: bytes | BinaryIO) -> TapeRecord:
    """Parse and validate exactly one tape identity member from a tar stream."""

    owned_stream = False
    if hasattr(data, "read"):
        stream = data
    else:
        if not isinstance(data, bytes):
            raise TapeError("tape header stream did not return bytes")
        stream = io.BytesIO(data)
        owned_stream = True
    bounded_stream = _BoundedReader(stream, _HEADER_PHYSICAL_LIMIT)
    try:
        archive = tarfile.open(fileobj=bounded_stream, mode="r|*")
    except (tarfile.TarError, OSError) as exc:
        raise TapeError(f"tape file 0 is not a readable tar header: {exc}") from exc
    try:
        matches = 0
        payload = None
        for member in archive:
            if member.name != TAPE_HEADER_MEMBER:
                raise TapeError(f"tape header contains an unexpected tar member: {member.name!r}")
            if member.name == TAPE_HEADER_MEMBER:
                matches += 1
                if not member.isreg():
                    raise TapeError("tape header metadata member is not a regular file")
                if member.size > _HEADER_LIMIT:
                    raise TapeError(f"tape header metadata exceeds {_HEADER_LIMIT} bytes")
                remaining = member.size
                chunks: list[bytes] = []
                while remaining:
                    chunk = archive.fileobj.read(min(_HEADER_COPY_CHUNK, remaining))
                    if not chunk:
                        raise TapeError("truncated tape header metadata member")
                    chunks.append(chunk)
                    remaining -= len(chunk)
                payload = b"".join(chunks)
        # Tar readers may stop as soon as the two terminating zero blocks are
        # observed.  For ordinary streams, consume the remainder so a
        # non-zero tail cannot be mistaken for a valid header.  A Linux tape
        # stream is already read in complete tape-record-sized buffers; an
        # additional read crosses the physical filemark and is not portable.
        if not is_physical_tape_stream(stream):
            while True:
                trailing = archive.fileobj.read(_HEADER_COPY_CHUNK)
                if not trailing:
                    break
                if any(trailing):
                    raise TapeError("non-zero trailing bytes follow tape header tar")
        if matches != 1:
            raise TapeError(
                f"tape header must contain exactly one {TAPE_HEADER_MEMBER}; found {matches}"
            )
    except tarfile.TarError as exc:
        raise TapeError(f"cannot read tape header tar: {exc}") from exc
    finally:
        archive.close()
        if owned_stream:
            stream.close()
    try:
        assert payload is not None
        value = json.loads(payload.decode("utf-8"))
        if not isinstance(value, dict):
            raise ValueError("metadata is not a JSON object")
        return TapeRecord.from_dict(value)
    except (UnicodeDecodeError, json.JSONDecodeError, ValueError, CatalogValidationError) as exc:
        raise TapeError(f"invalid tape header metadata: {exc}") from exc


def open_backend_file(backend) -> BinaryIO:
    """Return the current physical tape-file stream.

    Consumers must close it.  Keeping this as a stream is important for large
    archive files: tar readers and hash pumps can consume a tape file without
    first materializing it in RAM.
    """

    try:
        return backend.read_tape_file()
    except Exception as exc:
        if isinstance(exc, TapeError):
            raise
        raise TapeError(f"cannot open tape file for reading: {exc}") from exc


def read_tape_header(backend) -> TapeRecord:
    """Seek physical file 0 and return its validated identity."""

    status = backend.status()
    if not status.loaded:
        raise TapeError(status.error or "no tape is loaded")
    backend.seek_file(0)
    stream = open_backend_file(backend)
    try:
        return parse_tape_header(stream)
    finally:
        try:
            stream.close()
        except OSError:
            pass
