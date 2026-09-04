"""Drive status independent of a particular tape command implementation."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class TapeDriveStatus:
    loaded: bool
    writable: bool
    at_bot: bool = False
    at_eod: bool = False
    file_no: int | None = None
    device: str | None = None
    error: str | None = None

    @property
    def ok(self) -> bool:
        return self.loaded and self.error is None

    def to_dict(self) -> dict[str, object]:
        return {
            "loaded": self.loaded,
            "writable": self.writable,
            "at_bot": self.at_bot,
            "at_eod": self.at_eod,
            "file_no": self.file_no,
            "device": self.device,
            "error": self.error,
        }
