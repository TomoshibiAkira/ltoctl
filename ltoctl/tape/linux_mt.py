"""Linux ``mt``/non-rewinding-device backend.

The backend is intentionally small and semantic.  Higher layers never invoke
``mt`` directly, and uncertain file-number information is represented as
``None`` instead of guessed values.
"""

from __future__ import annotations

import io
import os
import re
import subprocess
from errno import EACCES, EINVAL, ENOSYS, ENOTSUP, EOPNOTSUPP, EPERM
from typing import BinaryIO

from ..errors import TapeError
from .status import TapeDriveStatus


_FILE_NUMBER_PATTERNS = (
    re.compile(
        r"\bfile(?:\s+(?:number|no\.?))?\s*(?:[=:]\s*|\s+)(-?\d+)\b",
        re.IGNORECASE,
    ),
    re.compile(r"\bfileno\s*(?:[=:]\s*|\s+)(-?\d+)\b", re.IGNORECASE),
    re.compile(r"\bfile\s*#\s*(-?\d+)\b", re.IGNORECASE),
)
_NO_MEDIA_RE = re.compile(
    r"\b(?:"
    r"no\s+(?:medium|media|tape|cartridge)|"
    r"(?:medium|media|tape|cartridge)\s+(?:not\s+present|absent|missing)|"
    r"not\s+(?:loaded|ready|online)|"
    r"dr[_ -]?open|"
    r"offline"
    r")\b",
    re.IGNORECASE,
)
_WRITE_PROTECT_RE = re.compile(
    r"\b(?:"
    r"wr[_ -]?prot|"
    r"wprot|"
    r"write[ -]?protect(?:ed)?|"
    r"read[ -]?only"
    r")\b",
    re.IGNORECASE,
)
_PERMISSION_DENIED_RE = re.compile(r"permission denied", re.IGNORECASE)
_TAPE_RECORD_SIZE = 10240


class _TapeReadStream(io.BufferedReader):
    """Buffered reader for one physical tape file."""

    physical_tape_file = True


def _is_permission_error(exc: BaseException | None = None, text: str = "") -> bool:
    if exc is not None:
        errno = getattr(exc, "errno", None)
        if errno in {EACCES, EPERM}:
            return True
        if _PERMISSION_DENIED_RE.search(str(exc)):
            return True
    return _PERMISSION_DENIED_RE.search(text) is not None


def _device_group_name(device: str) -> str:
    try:
        gid = os.stat(device).st_gid
    except OSError:
        return "tape"
    try:
        import grp

        return grp.getgrgid(gid).gr_name
    except (ImportError, KeyError, OSError):
        return "tape"


def permission_denied_message(device: str, *, action: str) -> str:
    """Explain a device EACCES/EPERM without suggesting every command run as root."""

    group = _device_group_name(device)
    return (
        f"{action}: permission denied for {device}. "
        f"Linux tape nodes are usually mode 0660 and owned by group {group!r}. "
        f"Add this account to that group, for example "
        f'`sudo usermod -aG {group} "$USER"`, then start a new login. '
        "Do not run ltoctl as root."
    )


def parse_mt_status(
    output: str,
    *,
    returncode: int = 0,
    device: str | None = None,
) -> TapeDriveStatus:
    """Parse GNU ``mt status`` output without guessing unsafe state.

    Linux ``mt`` output differs between tape drivers.  The file number and
    status-bit tokens are therefore parsed independently, while a failed,
    empty, or explicit no-medium response is represented as an unloaded/error
    status.  Unknown file numbers remain ``None`` so append safety can refuse
    them instead of guessing an EOD position.
    """

    if not isinstance(output, str):
        raise TypeError("mt status output must be text")
    text = output.strip()
    if returncode != 0:
        return TapeDriveStatus(
            loaded=False,
            writable=False,
            device=device,
            error=text or f"mt status exited with status {returncode}",
        )
    if not text:
        return TapeDriveStatus(
            loaded=False,
            writable=False,
            device=device,
            error="mt status returned no output",
        )

    no_media = _NO_MEDIA_RE.search(text) is not None
    file_no: int | None = None
    for pattern in _FILE_NUMBER_PATTERNS:
        match = pattern.search(text)
        if match is not None:
            candidate = int(match.group(1))
            # Some drivers use a negative sentinel when the position is not
            # known.  Treat that as unknown rather than exposing a guessed
            # physical location to append safety checks.
            file_no = candidate if candidate >= 0 else None
            break

    upper = text.upper()
    at_bot = re.search(r"\bBOT\b|\bBEGINNING[ -]OF[ -]TAPE\b", upper) is not None
    at_eod = re.search(r"\bEOD\b|\bEND[ -]OF[ -](?:DATA|TAPE)\b", upper) is not None
    write_protected = _WRITE_PROTECT_RE.search(text) is not None
    error = None
    if no_media:
        error = "mt status reports that no tape is loaded"
    return TapeDriveStatus(
        loaded=not no_media,
        writable=not write_protected and not no_media,
        at_bot=at_bot,
        at_eod=at_eod,
        file_no=file_no,
        device=device,
        error=error,
    )


class LinuxTapeBackend:
    def __init__(
        self,
        device: str = "/dev/nst0",
        *,
        mt_program: str = "/usr/bin/mt",
        allow_unvalidated_write: bool = False,
        filemark_strategy: str = "close",
    ) -> None:
        # Only the close-only filemark behavior is exposed.  Never close a
        # stream and then issue ``weof``: depending on the driver that can
        # create two filemarks.  A future backend can add a separate explicit
        # weof implementation without changing this semantic interface.
        if filemark_strategy != "close":
            raise ValueError("only the experimental close-only filemark strategy is currently supported")
        self.device = device
        self.mt_program = mt_program
        self.allow_unvalidated_write = allow_unvalidated_write
        self.filemark_strategy = filemark_strategy
        self._write_stream: BinaryIO | None = None

    def _mt(self, *arguments: str, check: bool = True) -> subprocess.CompletedProcess[str]:
        command = [self.mt_program, "-f", self.device, *arguments]
        try:
            result = subprocess.run(command, text=True, capture_output=True, check=False)
        except OSError as exc:
            if _is_permission_error(exc):
                raise TapeError(
                    permission_denied_message(self.device, action=f"cannot execute tape command {' '.join(command)}")
                ) from exc
            raise TapeError(f"cannot execute tape command {' '.join(command)}: {exc}") from exc
        if check and result.returncode != 0:
            detail = result.stderr.strip() or result.stdout.strip() or f"exit {result.returncode}"
            if _is_permission_error(text=detail):
                raise TapeError(
                    permission_denied_message(self.device, action=f"tape command failed ({' '.join(command)})")
                )
            raise TapeError(f"tape command failed ({' '.join(command)}): {detail}")
        return result

    def status(self) -> TapeDriveStatus:
        try:
            result = self._mt("status", check=False)
        except TapeError as exc:
            return TapeDriveStatus(False, False, device=self.device, error=str(exc))
        stdout = result.stdout if isinstance(result.stdout, str) else ""
        stderr = result.stderr if isinstance(result.stderr, str) else ""
        output = f"{stdout}\n{stderr}"
        if result.returncode != 0 and _is_permission_error(text=output):
            return TapeDriveStatus(
                False,
                False,
                device=self.device,
                error=permission_denied_message(self.device, action="cannot read drive status"),
            )
        return parse_mt_status(output, returncode=result.returncode, device=self.device)

    def rewind(self) -> None:
        self._mt("rewind")

    def eject(self) -> None:
        self._mt("offline")

    def seek_eod(self) -> None:
        # GNU mt calls the end-of-recorded-media operation ``eom``.
        # Keep the backend API's ``seek_eod`` terminology independent of
        # the platform utility's operation names.
        self._mt("eom")

    def seek_file(self, file_no: int) -> None:
        if isinstance(file_no, bool) or not isinstance(file_no, int) or file_no < 0:
            raise TapeError(f"invalid physical tape file number: {file_no!r}")
        self._mt("rewind")
        if file_no:
            self._mt("fsf", str(file_no))

    def current_file_no(self) -> int | None:
        status = self.status()
        return status.file_no

    def read_tape_file(self) -> BinaryIO:
        try:
            # The Linux st driver may return tape records in 10240-byte
            # blocks.  Buffering the raw device prevents tar readers from
            # issuing a smaller first read (for example, 512 bytes), which
            # the driver rejects as an invalid transfer size.
            raw = open(self.device, "rb", buffering=0)
            return _TapeReadStream(raw, buffer_size=_TAPE_RECORD_SIZE)
        except OSError as exc:
            if _is_permission_error(exc):
                raise TapeError(
                    permission_denied_message(self.device, action="cannot open tape device for reading")
                ) from exc
            raise TapeError(f"cannot open tape device for reading: {self.device}: {exc}") from exc

    def write_tape_file(self) -> BinaryIO:
        if not self.allow_unvalidated_write:
            raise TapeError(
                "Linux tape writes require an explicit allow_unvalidated_write=True opt-in; "
                "the production CLI enables the qualified close-only path"
            )
        if self._write_stream is not None:
            raise TapeError("a tape file is already being written")
        status = self.status()
        if not status.loaded:
            raise TapeError(status.error or "no tape is loaded")
        if not status.writable:
            raise TapeError("tape is write protected")
        try:
            self._write_stream = open(self.device, "wb", buffering=0)
        except OSError as exc:
            if _is_permission_error(exc):
                raise TapeError(
                    permission_denied_message(self.device, action="cannot open tape device for writing")
                ) from exc
            raise TapeError(f"cannot open tape device for writing: {self.device}: {exc}") from exc
        return self._write_stream

    def finish_tape_file(self) -> None:
        if self._write_stream is None:
            raise TapeError("no tape file is open for writing")
        stream = self._write_stream
        self._write_stream = None
        try:
            stream.flush()
            try:
                fileno = stream.fileno()
            except (AttributeError, OSError, ValueError):
                fileno = None
            if fileno is not None:
                try:
                    os.fsync(fileno)
                except OSError as exc:
                    # A few tape character-device drivers reject fsync even
                    # though close still commits the filemark.  Keep real
                    # I/O failures fatal, but do not turn an unsupported
                    # optional sync operation into a false write failure.
                    if exc.errno not in {EINVAL, ENOTSUP, EOPNOTSUPP, ENOSYS}:
                        raise
        except OSError as exc:
            try:
                stream.close()
            except OSError:
                pass
            raise TapeError(f"cannot finish tape file: {exc}") from exc
        try:
            stream.close()
        except OSError as exc:
            raise TapeError(f"cannot finish tape file: {exc}") from exc
        # Close-only strategy: no ``weof`` follows the close, so this backend
        # cannot accidentally emit two marks.
