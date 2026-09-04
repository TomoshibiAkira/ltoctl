"""In-memory tape backend used by all non-hardware tests."""

from __future__ import annotations

from io import BytesIO
from typing import BinaryIO, Iterable

from ..errors import TapeError
from .status import TapeDriveStatus


class MockTapeBackend:
    """Model physical tape files as ``list[bytes]``.

    ``seek_eod`` positions at the next physical file number.  A write is only
    allowed from that position and ``finish_tape_file`` appends exactly one
    file, so a caller can never overwrite an existing list item accidentally.
    """

    def __init__(
        self,
        files: Iterable[bytes] | None = None,
        *,
        loaded: bool = True,
        writable: bool = True,
        device: str = "mock://tape",
    ) -> None:
        self.files: list[bytes] = list(files or [])
        self.loaded = loaded
        self.writable = writable
        self.device = device
        self._position = 0
        self._write_buffer: BytesIO | None = None
        self.fail_on_write = False
        self.fail_on_finish = False
        self.fail_on_read = False
        self.fail_on_seek = False

    @property
    def file_count(self) -> int:
        return len(self.files)

    def _require_loaded(self) -> None:
        if not self.loaded:
            raise TapeError("no tape is loaded")

    def status(self) -> TapeDriveStatus:
        return TapeDriveStatus(
            loaded=self.loaded,
            writable=self.writable,
            at_bot=self._position == 0,
            at_eod=self._position == len(self.files),
            file_no=self._position if self.loaded else None,
            device=self.device,
        )

    def rewind(self) -> None:
        self._require_loaded()
        if self._write_buffer is not None:
            raise TapeError("cannot rewind while a tape file is being written")
        self._position = 0

    def eject(self) -> None:
        self._require_loaded()
        if self._write_buffer is not None:
            raise TapeError("cannot eject while a tape file is being written")
        self.loaded = False
        self._position = 0

    def load(self, files: Iterable[bytes] | None = None, *, writable: bool | None = None) -> None:
        if self.loaded:
            raise TapeError("a tape is already loaded")
        if files is not None:
            self.files = list(files)
        if writable is not None:
            self.writable = writable
        self.loaded = True
        self._position = 0

    def seek_eod(self) -> None:
        self._require_loaded()
        if self._write_buffer is not None:
            raise TapeError("cannot seek while a tape file is being written")
        if self.fail_on_seek:
            raise TapeError("mock EOD seek failure")
        self._position = len(self.files)

    def seek_file(self, file_no: int) -> None:
        self._require_loaded()
        if self._write_buffer is not None:
            raise TapeError("cannot seek while a tape file is being written")
        if isinstance(file_no, bool) or not isinstance(file_no, int) or file_no < 0:
            raise TapeError(f"invalid physical tape file number: {file_no!r}")
        if self.fail_on_seek:
            raise TapeError("mock file seek failure")
        if file_no >= len(self.files):
            raise TapeError(f"physical tape file does not exist: {file_no}")
        self._position = file_no

    def current_file_no(self) -> int | None:
        return self._position if self.loaded else None

    def read_tape_file(self) -> BinaryIO:
        self._require_loaded()
        if self.fail_on_read:
            raise TapeError("mock tape read failure")
        if self._position >= len(self.files):
            raise TapeError("tape is positioned at EOD; no file to read")
        return BytesIO(self.files[self._position])

    def write_tape_file(self) -> BinaryIO:
        self._require_loaded()
        if not self.writable:
            raise TapeError("tape is write protected")
        if self._position != len(self.files):
            raise TapeError(
                f"writes are only allowed at EOD (position {self._position}, EOD {len(self.files)})"
            )
        if self._write_buffer is not None:
            raise TapeError("a tape file is already being written")
        if self.fail_on_write:
            raise TapeError("mock tape write failure")
        self._write_buffer = BytesIO()
        return self._write_buffer

    def finish_tape_file(self) -> None:
        self._require_loaded()
        if self._write_buffer is None:
            raise TapeError("no tape file is open for writing")
        if self.fail_on_finish:
            raise TapeError("mock tape file-boundary failure")
        self.files.append(self._write_buffer.getvalue())
        self._write_buffer = None
        self._position = len(self.files)

    def append_file(self, data: bytes) -> int:
        stream = self.write_tape_file()
        stream.write(data)
        self.finish_tape_file()
        return len(self.files) - 1

    def file(self, file_no: int) -> bytes:
        if file_no < 0 or file_no >= len(self.files):
            raise TapeError(f"physical tape file does not exist: {file_no}")
        return self.files[file_no]
