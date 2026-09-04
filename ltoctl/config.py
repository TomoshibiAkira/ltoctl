"""Small configuration loader with explicit precedence."""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any

try:  # Python 3.11+ standard library
    import tomllib
except ModuleNotFoundError:  # pragma: no cover - only for local legacy interpreters
    tomllib = None  # type: ignore[assignment]

from .catalog.store import default_catalog_root


@dataclass(frozen=True)
class Config:
    catalog_root: Path
    device: str = "/dev/nst0"
    media: str = "lto6"
    log_path: Path | None = None


def config_path() -> Path:
    return Path(os.environ.get("XDG_CONFIG_HOME", Path.home() / ".config")) / "ltoctl" / "config.toml"


def _file_config(path: Path | None = None) -> dict[str, Any]:
    path = path or config_path()
    if tomllib is None or not path.is_file():
        return {}
    try:
        with path.open("rb") as stream:
            data = tomllib.load(stream)
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def load_config(
    *,
    catalog_root: str | os.PathLike[str] | None = None,
    device: str | None = None,
    media: str | None = None,
    config_file: str | os.PathLike[str] | None = None,
) -> Config:
    file_values = _file_config(Path(config_file).expanduser() if config_file else None)
    root_value = catalog_root or os.environ.get("LTOCTL_CATALOG") or file_values.get("catalog_root")
    device_value = device or os.environ.get("LTOCTL_DEVICE") or file_values.get("device") or "/dev/nst0"
    media_value = media or os.environ.get("LTOCTL_MEDIA") or file_values.get("media") or "lto6"
    # Environment variables override the TOML file just like the other
    # settings.  Accept the long-form alias as well for shell conventions.
    log_value = os.environ.get("LTOCTL_LOG") or os.environ.get("LTOCTL_LOG_PATH") or file_values.get("log_path")
    root = Path(root_value).expanduser() if root_value else default_catalog_root()
    if log_value:
        log_path = Path(log_value).expanduser()
    else:
        state_root = Path(os.environ.get("XDG_STATE_HOME", Path.home() / ".local" / "state"))
        log_path = state_root / "ltoctl" / "ltoctl.log"
    return Config(catalog_root=root, device=str(device_value), media=str(media_value), log_path=log_path)
