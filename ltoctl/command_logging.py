"""Best-effort command logging kept separate from the canonical catalog.

The log is intentionally JSONL: one append-only event per line is easy to
inspect with ordinary Unix tools and does not affect catalog recovery.  A
logging failure is never allowed to hide the command's real result.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping


class CommandLogger:
    """Append command lifecycle events to a user-configured state file."""

    def __init__(self, path: str | Path | None, command: str):
        self.path = Path(path).expanduser() if path is not None else None
        self.command = command
        self._started = False

    @property
    def available(self) -> bool:
        return self.path is not None

    def event(self, result: str, *, error: str | None = None, **fields: Any) -> None:
        if self.path is None:
            return
        value: dict[str, Any] = {
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "command": self.command,
            "result": result,
        }
        if error:
            value["error"] = error
        # Callers may add stable IDs and explicitly requested source paths;
        # payload bytes and other archive contents never belong here.
        value.update({key: item for key, item in fields.items() if item is not None})
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            with self.path.open("a", encoding="utf-8") as stream:
                stream.write(json.dumps(value, ensure_ascii=False, sort_keys=True) + "\n")
        except (OSError, TypeError, ValueError):
            # Logging is diagnostic only.  In particular, a read-only state
            # directory must not turn a successful archive operation into a
            # failed command.
            return

    def start(self, fields: Mapping[str, Any] | None = None, **extra_fields: Any) -> None:
        if not self._started:
            self._started = True
            values = dict(fields or {})
            values.update(extra_fields)
            self.event("start", **values)

    def finish(self, *, ok: bool, error: str | None = None, **fields: Any) -> None:
        self.event("ok" if ok else "error", error=error, **fields)


def result_fields(value: Any) -> dict[str, Any]:
    """Extract non-sensitive identifiers from common service results."""

    fields: dict[str, Any] = {}
    for name in ("operation_uuid", "archive_uuid", "tape_id", "tape_uuid", "plan_id", "group_no"):
        item = getattr(value, name, None)
        if item is not None:
            fields[name] = item
    archive = getattr(value, "archive", None)
    if archive is not None:
        for name in ("archive_uuid", "tape_id", "tape_uuid"):
            item = getattr(archive, name, None)
            if item is not None:
                fields[name] = item
    tape = getattr(value, "tape", None)
    if tape is not None and getattr(tape, "tape_id", None) is not None:
        fields["tape_id"] = tape.tape_id
        if getattr(tape, "uuid", None) is not None:
            fields["tape_uuid"] = tape.uuid
    return fields
