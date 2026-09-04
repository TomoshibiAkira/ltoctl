"""Streaming archive inspection, safe extraction, and archive lookup.

Archive files live on sequential media.  The helpers in this module therefore
consume a tape-file stream exactly once; they never call ``read()`` on the
 whole physical file and never enumerate all tar members up front.  The embedded
manifest is spooled to a temporary JSONL file so even very large manifests do
not become a second in-memory copy of the archive.
"""

from __future__ import annotations

import hashlib
import io
import json
import os
import posixpath
import tarfile
import tempfile
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import BinaryIO, Callable, Iterable, Iterator

from ..catalog.models import ArchiveRecord, ManifestEntry
from ..catalog.store import CatalogStore
from ..errors import ArchiveError, CatalogError, RestoreError, SafetyError
from ..tape.header import is_physical_tape_stream, open_backend_file
from ..tape.safety import TapeSafetyService

from .manifest import iter_manifest_file
from .writer import ARCHIVE_DESCRIPTOR_MEMBER, ARCHIVE_MANIFEST_MEMBER


_DESCRIPTOR_LIMIT = 4 * 1024 * 1024
_COPY_CHUNK = 1024 * 1024
_MTIME_TOLERANCE_NS = 1_000_000


class HashingReader:
    """Read-through wrapper hashing exactly bytes consumed from a tape file."""

    def __init__(self, target: BinaryIO):
        self.target = target
        self.hasher = hashlib.sha256()
        self.bytes_read = 0

    def read(self, size: int = -1) -> bytes:
        data = self.target.read(size)
        if data is None:
            return b""
        if not isinstance(data, (bytes, bytearray, memoryview)):
            raise ArchiveError("tape reader returned a non-bytes chunk")
        value = bytes(data)
        self.hasher.update(value)
        self.bytes_read += len(value)
        return value

    def readinto(self, buffer) -> int:
        reader = getattr(self.target, "readinto", None)
        if reader is None:
            data = self.read(len(buffer))
            buffer[: len(data)] = data
            return len(data)
        count = reader(buffer)
        if count:
            value = bytes(memoryview(buffer)[:count])
            self.hasher.update(value)
            self.bytes_read += count
        return count

    def readable(self) -> bool:
        return True

    def seekable(self) -> bool:
        return False

    def tell(self) -> int:
        return self.bytes_read

    def __getattr__(self, name: str):
        return getattr(self.target, name)

    def digest(self) -> str:
        return self.hasher.hexdigest()


@dataclass
class ArchiveInspection:
    """Validated archive metadata with an on-disk, streaming manifest."""

    descriptor: dict[str, object]
    manifest_path: Path
    manifest_count: int
    sha256: str
    tar_bytes: int
    _remove_manifest_on_close: bool = False

    @property
    def archive(self) -> ArchiveRecord:
        value = dict(self.descriptor)
        value["tar_stream_sha256"] = None
        return ArchiveRecord.from_dict(value)

    def iter_manifest(self) -> Iterator[ManifestEntry]:
        """Yield embedded manifest records without materializing them."""

        yield from iter_manifest_file(self.manifest_path)

    def close(self) -> None:
        if self._remove_manifest_on_close:
            try:
                self.manifest_path.unlink(missing_ok=True)
            except OSError:
                pass
            self._remove_manifest_on_close = False

    def __enter__(self) -> "ArchiveInspection":
        return self

    def __exit__(self, _type, _value, _traceback) -> None:
        self.close()

    def __del__(self):  # pragma: no cover - best effort for compatibility helpers
        self.close()


def _read_member(tar: tarfile.TarFile, member: tarfile.TarInfo, limit: int | None = None) -> bytes:
    """Read the current stream-mode tar member without seeking backwards."""

    if limit is not None and member.size > limit:
        raise ArchiveError(f"tar member exceeds the {limit} byte safety limit")
    remaining = member.size
    chunks: list[bytes] = []
    while remaining:
        chunk = tar.fileobj.read(min(_COPY_CHUNK, remaining))
        if not chunk:
            raise ArchiveError("truncated tar member payload")
        chunks.append(chunk)
        remaining -= len(chunk)
    return b"".join(chunks)


def _copy_member(stream: BinaryIO, destination: Path, size: int) -> None:
    remaining = size
    with destination.open("wb") as output:
        while remaining:
            chunk = stream.read(min(_COPY_CHUNK, remaining))
            if not chunk:
                raise ArchiveError("truncated tar member payload")
            output.write(chunk)
            remaining -= len(chunk)


def _parse_descriptor(tar: tarfile.TarFile) -> tuple[dict[str, object], ArchiveRecord]:
    first = tar.next()
    if first is None or first.name != ARCHIVE_DESCRIPTOR_MEMBER:
        raise ArchiveError(f"archive must begin with {ARCHIVE_DESCRIPTOR_MEMBER}")
    if not first.isreg():
        raise ArchiveError("archive descriptor is not a regular file")
    payload = _read_member(tar, first, _DESCRIPTOR_LIMIT)
    try:
        descriptor = json.loads(payload.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ArchiveError(f"invalid archive descriptor: {exc}") from exc
    if not isinstance(descriptor, dict):
        raise ArchiveError("archive descriptor must be a JSON object")
    if "tar_stream_sha256" in descriptor:
        raise ArchiveError("embedded archive descriptor must not contain tar_stream_sha256")
    value = dict(descriptor)
    value["tar_stream_sha256"] = None
    try:
        record = ArchiveRecord.from_dict(value)
    except CatalogError as exc:
        raise ArchiveError(f"invalid archive descriptor fields: {exc}") from exc
    return descriptor, record


def _spool_manifest(tar: tarfile.TarFile, destination: Path) -> None:
    second = tar.next()
    if second is None or second.name != ARCHIVE_MANIFEST_MEMBER:
        raise ArchiveError(f"archive descriptor must be followed by {ARCHIVE_MANIFEST_MEMBER}")
    if not second.isreg():
        raise ArchiveError("archive manifest is not a regular file")
    destination.parent.mkdir(parents=True, exist_ok=True)
    _copy_member(tar.fileobj, destination, second.size)


def _iter_embedded_manifest(path: Path) -> Iterator[ManifestEntry]:
    try:
        yield from iter_manifest_file(path)
    except (CatalogError, UnicodeDecodeError) as exc:
        raise ArchiveError(f"invalid embedded manifest: {exc}") from exc


def _payload_type(member: tarfile.TarInfo) -> str:
    if member.isdir():
        return "dir"
    if member.issym():
        return "symlink"
    if member.isreg():
        return "file"
    return "other"


def _validate_payload_name(name: str) -> None:
    if not isinstance(name, str) or not name or "\x00" in name:
        raise ArchiveError(f"invalid tar payload path: {name!r}")
    if name.startswith("__LTOCTL__/") or name == "__LTOCTL__":
        raise ArchiveError(f"reserved metadata path appears in payload: {name!r}")


def _path_order_key(name: str) -> bytes:
    return name.encode("utf-8", "surrogatepass")


def _mtime_ns(value: float) -> int:
    try:
        return int(round(float(value) * 1_000_000_000))
    except (TypeError, ValueError, OverflowError) as exc:
        raise ArchiveError(f"invalid tar member mtime: {value!r}") from exc


def _compare_payload(member: tarfile.TarInfo, entry: ManifestEntry) -> None:
    _validate_payload_name(member.name)
    if entry.type not in {"file", "dir", "symlink"}:
        raise ArchiveError(f"unsupported manifest entry type for restore/verify: {entry.type!r}")
    if member.name != entry.path:
        raise ArchiveError(
            f"manifest/payload path mismatch: manifest={entry.path!r}, payload={member.name!r}"
        )
    actual_type = _payload_type(member)
    if actual_type != entry.type:
        raise ArchiveError(
            f"manifest/payload type mismatch for {entry.path!r}: manifest={entry.type}, payload={actual_type}"
        )
    expected_size = entry.size if entry.type == "file" else 0
    if member.size != expected_size:
        raise ArchiveError(
            f"manifest/payload size mismatch for {entry.path!r}: manifest={expected_size}, payload={member.size}"
        )
    if abs(_mtime_ns(member.mtime) - entry.mtime_ns) > _MTIME_TOLERANCE_NS:
        raise ArchiveError(
            f"manifest/payload mtime mismatch for {entry.path!r}: "
            f"manifest={entry.mtime_ns}, payload={_mtime_ns(member.mtime)}"
        )
    if entry.type == "symlink" and member.linkname != entry.link_target:
        raise ArchiveError(
            f"manifest/payload link target mismatch for {entry.path!r}: "
            f"manifest={entry.link_target!r}, payload={member.linkname!r}"
        )


PayloadHandler = Callable[[tarfile.TarFile, tarfile.TarInfo, ManifestEntry], None]


def inspect_archive_stream(
    stream: BinaryIO,
    *,
    manifest_path: str | os.PathLike[str] | None = None,
    payload_handler: PayloadHandler | None = None,
) -> ArchiveInspection:
    """Validate an archive stream and compare every payload to its manifest.

    ``payload_handler`` runs while a member is current in the sequential tar
    reader.  A handler may call ``tar.extractfile(member)`` and consume that
    member, which is how restore extracts without a second tape pass.
    """

    if stream is None or not hasattr(stream, "read"):
        raise ArchiveError("archive input is not a readable stream")
    remove_manifest = manifest_path is None
    if manifest_path is None:
        temporary = tempfile.NamedTemporaryFile(prefix="ltoctl-embedded-", suffix=".jsonl", delete=False)
        manifest_destination = Path(temporary.name)
        temporary.close()
    else:
        manifest_destination = Path(manifest_path)
    reader = HashingReader(stream)
    tar: tarfile.TarFile | None = None
    try:
        try:
            tar = tarfile.open(fileobj=reader, mode="r|*")
            descriptor, archive_record = _parse_descriptor(tar)
            _spool_manifest(tar, manifest_destination)
            manifest_iter = _iter_embedded_manifest(manifest_destination)
            sentinel = object()
            next_entry: ManifestEntry | object = next(manifest_iter, sentinel)
            previous_manifest_key: bytes | None = None
            previous_payload_key: bytes | None = None
            manifest_count = 0
            regular_count = 0
            logical_size = 0
            # ``TarFile.__iter__`` restarts from the cached index when the
            # stream has already yielded members through ``next()``.  That
            # would replay descriptor/manifest and is unsafe for sequential
            # tape reads; continue with explicit ``next()`` calls instead.
            while True:
                member = tar.next()
                if member is None:
                    break
                if next_entry is sentinel:
                    raise ArchiveError(f"payload contains an extra member {member.name!r}")
                assert isinstance(next_entry, ManifestEntry)
                entry = next_entry
                manifest_key = _path_order_key(entry.path)
                if previous_manifest_key is not None and manifest_key <= previous_manifest_key:
                    raise ArchiveError(
                        f"embedded manifest contains duplicate or non-lexical path: {entry.path!r}"
                    )
                previous_manifest_key = manifest_key
                payload_key = _path_order_key(member.name)
                if previous_payload_key is not None and payload_key <= previous_payload_key:
                    raise ArchiveError(
                        f"payload contains duplicate or non-lexical path: {member.name!r}"
                    )
                previous_payload_key = payload_key
                _compare_payload(member, entry)
                manifest_count += 1
                logical_size += entry.size if entry.type == "file" else 0
                regular_count += entry.type == "file"
                if payload_handler is not None:
                    payload_handler(tar, member, entry)
                next_entry = next(manifest_iter, sentinel)
            # A tape stream is read in complete record-sized buffers, and
            # reading again after TarFile.next() observes the two zero blocks
            # crosses the physical filemark.  Ordinary streams still drain
            # and reject non-zero trailing garbage.
            if not is_physical_tape_stream(stream):
                while True:
                    trailing = tar.fileobj.read(_COPY_CHUNK)
                    if not trailing:
                        break
                    if any(trailing):
                        raise ArchiveError("non-zero trailing garbage follows archive tar end")
            if next_entry is not sentinel:
                assert isinstance(next_entry, ManifestEntry)
                raise ArchiveError(f"embedded manifest member is missing from payload: {next_entry.path!r}")
            if archive_record.file_count != regular_count:
                raise ArchiveError(
                    f"archive descriptor file_count {archive_record.file_count} does not match payload/manifest "
                    f"count {regular_count}"
                )
            if archive_record.logical_size_bytes != logical_size:
                raise ArchiveError(
                    f"archive descriptor logical_size_bytes {archive_record.logical_size_bytes} does not match "
                    f"manifest file bytes {logical_size}"
                )
        except ArchiveError:
            raise
        except (tarfile.TarError, OSError, StopIteration) as exc:
            raise ArchiveError(f"cannot consume archive tar stream: {exc}") from exc
    except BaseException:
        if remove_manifest:
            manifest_destination.unlink(missing_ok=True)
        raise
    finally:
        if tar is not None:
            try:
                tar.close()
            except (OSError, tarfile.TarError):
                pass
    return ArchiveInspection(
        descriptor=descriptor,
        manifest_path=manifest_destination,
        manifest_count=manifest_count,
        sha256=reader.digest(),
        tar_bytes=reader.bytes_read,
        _remove_manifest_on_close=remove_manifest,
    )


def inspect_archive_bytes(data: bytes) -> ArchiveInspection:
    """Compatibility helper for small offline tests.

    Production tape paths use :func:`inspect_archive_stream`; this helper
    still accepts an already-materialized byte string by explicit request.
    """

    if not isinstance(data, (bytes, bytearray, memoryview)) or not data:
        raise ArchiveError("archive tape file is empty")
    return inspect_archive_stream(io.BytesIO(bytes(data)))


def read_current_archive(backend) -> ArchiveInspection:
    """Inspect the current physical tape file as a streaming operation."""

    stream = open_backend_file(backend)
    try:
        return inspect_archive_stream(stream)
    finally:
        try:
            stream.close()
        except OSError:
            pass


def resolve_archive(store: CatalogStore, reference: str) -> ArchiveRecord:
    """Resolve UUID, unique name, or physical ``TAPE_ID:file_no``."""

    if not isinstance(reference, str) or not reference.strip():
        raise CatalogError("archive reference must be non-empty")
    if ":" in reference:
        tape_id, _, file_value = reference.rpartition(":")
        if tape_id and file_value.isdigit():
            file_no = int(file_value)
            matches = [
                archive
                for archive in store.iter_archives()
                if archive.tape_id == tape_id and archive.tape_file_no == file_no
            ]
            if len(matches) == 1:
                return matches[0]
            if len(matches) > 1:
                raise CatalogError(f"multiple archives match {reference!r}")
            raise CatalogError(f"unknown archive physical location {reference!r}")
    return store.find_archive(reference)


def _safe_member_name(name: str) -> str:
    if not isinstance(name, str) or not name or "\x00" in name:
        raise RestoreError(f"invalid tar member name: {name!r}")
    if name.startswith("/") or name.startswith("\\"):
        raise RestoreError(f"absolute tar member path is not allowed: {name!r}")
    raw_parts = name.split("/")
    if any(part == ".." for part in raw_parts):
        raise RestoreError(f"tar path traversal is not allowed: {name!r}")
    parts = [part for part in raw_parts if part not in {"", "."}]
    if not parts:
        raise RestoreError(f"empty tar member path: {name!r}")
    normalized = "/".join(parts)
    if normalized != name:
        raise RestoreError(f"non-canonical tar member path is not allowed: {name!r}")
    return normalized


def _safe_link_target(member_name: str, target: str) -> str:
    if not isinstance(target, str) or not target or "\x00" in target:
        raise RestoreError(f"invalid link target for {member_name!r}")
    if target.startswith("/") or target.startswith("\\"):
        raise RestoreError(f"absolute link target is not allowed: {member_name!r} -> {target!r}")
    parent = posixpath.dirname(member_name)
    resolved = posixpath.normpath(posixpath.join(parent, target))
    if resolved == ".." or resolved.startswith("../"):
        raise RestoreError(f"link target escapes restore root: {member_name!r} -> {target!r}")
    return target


def _lexists(path: Path) -> bool:
    return os.path.lexists(path)


def _check_output_path(output: Path, relative: str, *, create_root: bool = True) -> Path:
    if _lexists(output) and output.is_symlink():
        raise RestoreError(f"restore output directory may not be a symlink: {output}")
    if _lexists(output) and not output.is_dir():
        raise RestoreError(f"restore output is not a directory: {output}")
    if create_root:
        try:
            output.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            raise RestoreError(f"restore output is not a directory: {output}: {exc}") from exc
    destination = output.joinpath(*PurePosixPath(relative).parts)
    try:
        resolved_output = output.resolve()
        resolved_destination = destination.resolve(strict=False)
    except OSError as exc:
        raise RestoreError(f"cannot resolve restore destination {relative!r}: {exc}") from exc
    try:
        resolved_destination.relative_to(resolved_output)
    except ValueError as exc:
        raise RestoreError(f"restore path escapes output directory: {relative!r}") from exc
    cursor = output
    for part in PurePosixPath(relative).parts[:-1]:
        cursor = cursor / part
        if cursor.is_symlink():
            raise RestoreError(f"restore path has a symlink ancestor: {relative!r}")
    return destination


@dataclass(frozen=True)
class RestoreResult:
    archive_uuid: str
    output: str
    extracted_count: int
    # Full restore results intentionally omit every path to keep memory
    # bounded.  Explicit selection returns only the request items that were
    # found (not every descendant of a selected directory), so its size is
    # bounded by the caller's selection set.
    extracted: tuple[str, ...] | None = None


def _write_directory_metadata(
    streams: dict[int, object], metadata_root: Path, entry: ManifestEntry, mode: int
) -> None:
    """Spool directory metadata by depth so it can be applied deepest-first."""

    depth = entry.path.count("/")
    stream = streams.get(depth)
    if stream is None:
        stream = (metadata_root / f"{depth:08d}.jsonl").open("a", encoding="utf-8")
        streams[depth] = stream
    stream.write(
        json.dumps(
            {"path": entry.path, "mtime_ns": entry.mtime_ns, "mode": mode & 0o7777},
            sort_keys=True,
            separators=(",", ":"),
        )
        + "\n"
    )


def _apply_directory_metadata(root: Path, metadata_root: Path) -> None:
    """Apply recorded directory metadata after all child writes."""

    for metadata_path in sorted(metadata_root.glob("*.jsonl"), reverse=True):
        try:
            stream = metadata_path.open("r", encoding="utf-8")
        except OSError as exc:
            raise RestoreError(f"cannot read directory metadata journal: {exc}") from exc
        try:
            for line_no, line in enumerate(stream, 1):
                try:
                    value = json.loads(line)
                    relative = value["path"]
                    mtime_ns = value["mtime_ns"]
                    mode = value["mode"]
                    if (
                        not isinstance(relative, str)
                        or isinstance(mtime_ns, bool)
                        or not isinstance(mtime_ns, int)
                        or mtime_ns < 0
                        or isinstance(mode, bool)
                        or not isinstance(mode, int)
                        or mode < 0
                    ):
                        raise ValueError("invalid directory metadata fields")
                    destination = _check_output_path(root, relative)
                    if not destination.is_dir() or destination.is_symlink():
                        raise RestoreError(f"directory metadata target is not a directory: {relative!r}")
                    os.chmod(destination, mode & 0o7777)
                    os.utime(destination, ns=(mtime_ns, mtime_ns), follow_symlinks=False)
                except (json.JSONDecodeError, KeyError, TypeError, ValueError) as exc:
                    raise RestoreError(
                        f"invalid directory metadata journal line {metadata_path}:{line_no}: {exc}"
                    ) from exc
        finally:
            stream.close()


def _iter_stage_records(path: Path) -> Iterator[tuple[str, str, int, int]]:
    """Read the bounded restore staging journal one record at a time."""

    try:
        stream = path.open("r", encoding="utf-8")
    except OSError as exc:
        raise RestoreError(f"cannot read restore staging journal: {exc}") from exc
    try:
        for line_no, line in enumerate(stream, 1):
            try:
                value = json.loads(line)
                if not isinstance(value, dict):
                    raise ValueError("record is not an object")
                relative = value["path"]
                member_type = value["type"]
                mtime_ns = value["mtime_ns"]
                mode = value["mode"]
                if (
                    not isinstance(relative, str)
                    or member_type not in {"file", "dir", "symlink"}
                    or isinstance(mtime_ns, bool)
                    or not isinstance(mtime_ns, int)
                    or mtime_ns < 0
                    or isinstance(mode, bool)
                    or not isinstance(mode, int)
                    or mode < 0
                ):
                    raise ValueError("invalid staging record fields")
                yield relative, member_type, mtime_ns, mode
            except (json.JSONDecodeError, KeyError, TypeError, ValueError) as exc:
                raise RestoreError(f"invalid restore staging journal line {line_no}: {exc}") from exc
    finally:
        stream.close()


class ArchiveReader:
    def __init__(self, store: CatalogStore, backend, *, safety: TapeSafetyService | None = None):
        self.store = store
        self.backend = backend
        self.safety = safety or TapeSafetyService(store)

    def restore(
        self,
        reference: str,
        output: str | os.PathLike[str],
        *,
        selected_files: Iterable[str] | None = None,
        overwrite: bool = False,
    ) -> RestoreResult:
        archive_record = resolve_archive(self.store, reference)
        loaded = self.safety.identify_loaded(self.backend)
        if loaded.catalog.tape_id != archive_record.tape_id or loaded.catalog.uuid != archive_record.tape_uuid:
            raise SafetyError("loaded tape does not match archive catalog identity")
        requested: set[str] | None = None
        if selected_files is not None:
            requested = {_safe_member_name(path) for path in selected_files}
            if not requested:
                raise RestoreError("at least one selected file is required")

        destination_root = Path(output).expanduser()
        # Stage outside the destination.  A corrupt stream or a hash mismatch
        # must not leave a partially restored tree behind.
        with tempfile.TemporaryDirectory(prefix="ltoctl-restore-") as temporary:
            stage_root = Path(temporary) / "payload"
            stage_root.mkdir()
            stage_records_path = Path(temporary) / "selected.jsonl"
            directory_metadata_root = Path(temporary) / "directories"
            directory_metadata_root.mkdir()
            requested_hits: set[str] = set()
            records_stream = stage_records_path.open("w", encoding="utf-8")
            directory_streams: dict[int, object] = {}
            extracted_count = 0

            def handler(tar: tarfile.TarFile, member: tarfile.TarInfo, entry: ManifestEntry) -> None:
                nonlocal extracted_count
                safe_name = _safe_member_name(member.name)
                # Validate link targets even for unselected members.  A
                # restore operation must not silently bless an unsafe archive
                # merely because the caller selected a different file.
                if member.issym():
                    _safe_link_target(safe_name, member.linkname)
                if requested is not None:
                    for selected in requested:
                        if safe_name == selected or safe_name.startswith(selected + "/"):
                            requested_hits.add(selected)
                wanted = requested is None or any(
                    safe_name == selected or safe_name.startswith(selected + "/") for selected in requested
                )
                if not wanted:
                    return
                parts = safe_name.split("/")
                for index in range(1, len(parts)):
                    ancestor = "/".join(parts[:index])
                    ancestor_path = stage_root.joinpath(*PurePosixPath(ancestor).parts)
                    if ancestor_path.is_symlink():
                        raise RestoreError(f"tar symlink ancestor is not allowed: {ancestor}")
                destination = _check_output_path(stage_root, safe_name)
                if _lexists(destination):
                    raise RestoreError(f"duplicate selected staged path: {safe_name}")
                destination.parent.mkdir(parents=True, exist_ok=True)
                if member.isdir():
                    # Keep the stage writable while children are created.
                    # Desired mode/mtime are applied only after the complete
                    # subtree is present.
                    destination.mkdir(parents=True, exist_ok=True, mode=0o700)
                    _write_directory_metadata(directory_streams, directory_metadata_root, entry, member.mode)
                elif member.issym():
                    _safe_link_target(safe_name, member.linkname)
                    os.symlink(member.linkname, destination)
                elif member.isreg():
                    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0)
                    try:
                        fd = os.open(destination, flags, member.mode & 0o7777 or 0o600)
                    except OSError as exc:
                        raise RestoreError(f"cannot create staged file {destination}: {exc}") from exc
                    with os.fdopen(fd, "wb") as output_stream:
                        remaining = member.size
                        while remaining:
                            chunk = tar.fileobj.read(min(_COPY_CHUNK, remaining))
                            if not chunk:
                                raise RestoreError(f"regular tar member is truncated: {safe_name}")
                            output_stream.write(chunk)
                            remaining -= len(chunk)
                else:
                    raise RestoreError(f"unsupported tar member type: {safe_name}")
                records_stream.write(
                    json.dumps(
                        {
                            "path": safe_name,
                            "type": entry.type,
                            "mtime_ns": entry.mtime_ns,
                            "mode": member.mode & 0o7777,
                        },
                        sort_keys=True,
                        separators=(",", ":"),
                    )
                    + "\n"
                )
                extracted_count += 1

            self.backend.seek_file(archive_record.tape_file_no)
            stream = open_backend_file(self.backend)
            inspection: ArchiveInspection | None = None
            try:
                inspection = inspect_archive_stream(stream, payload_handler=handler)
            except RestoreError:
                raise
            except ArchiveError as exc:
                raise RestoreError(str(exc)) from exc
            finally:
                try:
                    stream.close()
                except OSError:
                    pass
                records_stream.close()
                for directory_stream in directory_streams.values():
                    directory_stream.close()
            try:
                embedded = inspection.archive
                if embedded.archive_uuid != archive_record.archive_uuid:
                    raise RestoreError("archive descriptor UUID does not match catalog record")
                if (
                    embedded.tape_id != archive_record.tape_id
                    or embedded.tape_uuid != archive_record.tape_uuid
                    or embedded.tape_file_no != archive_record.tape_file_no
                ):
                    raise RestoreError("archive descriptor tape identity/location does not match catalog record")
                if embedded.logical_size_bytes != archive_record.logical_size_bytes:
                    raise RestoreError("archive descriptor logical size does not match catalog record")
                if embedded.file_count != archive_record.file_count:
                    raise RestoreError("archive descriptor file count does not match catalog record")
                if archive_record.tar_stream_sha256 is None or inspection.sha256 != archive_record.tar_stream_sha256:
                    raise RestoreError("archive tar stream hash does not match catalog record")
                if requested is not None:
                    missing = sorted(requested - requested_hits)
                    if missing:
                        raise RestoreError(f"selected archive path(s) not found: {', '.join(missing)}")

                # Destination preflight happens only after the complete
                # stream/hash succeeds, preserving no-partial-restore safety.
                for relative, member_type, _mtime, _mode in _iter_stage_records(stage_records_path):
                    destination = _check_output_path(destination_root, relative, create_root=False)
                    if not _lexists(destination):
                        continue
                    # ``Path.is_dir()`` follows symlinks.  For overwrite
                    # safety, classify an existing symlink by its own type;
                    # an archive file/link may atomically replace one even
                    # when its target happens to be a directory.
                    destination_is_symlink = destination.is_symlink()
                    destination_is_dir = destination.is_dir() and not destination_is_symlink
                    if member_type == "dir" and destination_is_dir:
                        continue
                    if not overwrite:
                        raise RestoreError(f"restore destination already exists: {destination}")
                    if member_type == "dir" or destination_is_dir:
                        raise RestoreError(f"overwrite of directory/non-directory mismatch is not supported: {destination}")

                for relative, member_type, mtime_ns, mode in _iter_stage_records(stage_records_path):
                    source = stage_root.joinpath(*PurePosixPath(relative).parts)
                    destination = _check_output_path(destination_root, relative)
                    destination.parent.mkdir(parents=True, exist_ok=True)
                    if member_type == "dir":
                        destination.mkdir(parents=True, exist_ok=True, mode=0o700)
                    elif member_type == "symlink":
                        # ``os.replace`` replaces an existing file or
                        # symlink atomically without following the target.
                        os.replace(source, destination)
                    else:
                        os.replace(source, destination)
                        try:
                            os.chmod(destination, mode)
                            os.utime(destination, ns=(mtime_ns, mtime_ns), follow_symlinks=False)
                        except OSError:
                            pass
                # Apply target directory mode/mtime after all child commits.
                _apply_directory_metadata(destination_root, directory_metadata_root)
                return RestoreResult(
                    archive_record.archive_uuid,
                    str(destination_root),
                    extracted_count,
                    tuple(sorted(requested_hits)) if requested is not None else None,
                )
            finally:
                if inspection is not None:
                    inspection.close()


def restore_archive(
    store: CatalogStore,
    backend,
    reference: str,
    output: str | os.PathLike[str],
    *,
    selected_files: Iterable[str] | None = None,
    overwrite: bool = False,
) -> RestoreResult:
    return ArchiveReader(store, backend).restore(
        reference,
        output,
        selected_files=selected_files,
        overwrite=overwrite,
    )
