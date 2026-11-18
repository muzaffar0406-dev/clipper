"""Small, validated runtime configuration for clip-watcher."""
from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Mapping


GIB = 1024 ** 3


class ConfigurationError(ValueError):
    """Raised when a configured value is unsafe or invalid."""


@dataclass(frozen=True)
class RetentionSettings:
    """Rules for completed source recordings. Clips are never part of this policy."""

    enabled: bool = True
    retention_hours: float = 168.0
    max_recording_bytes: int = 20 * GIB


def _read_positive_number(environment: Mapping[str, str], name: str, default: float) -> float:
    value = environment.get(name)
    if value is None or value.strip() == "":
        return default
    try:
        parsed = float(value)
    except ValueError as exc:
        raise ConfigurationError(f"{name} must be a positive number") from exc
    if parsed <= 0:
        raise ConfigurationError(f"{name} must be greater than zero")
    return parsed


def _read_bool(environment: Mapping[str, str], name: str, default: bool) -> bool:
    value = environment.get(name)
    if value is None or value.strip() == "":
        return default
    normalized = value.strip().lower()
    if normalized in {"1", "true", "yes", "on"}:
        return True
    if normalized in {"0", "false", "no", "off"}:
        return False
    raise ConfigurationError(f"{name} must be true or false")


def load_retention_settings(environment: Mapping[str, str] | None = None) -> RetentionSettings:
    """Load recording-only retention settings without changing filesystem state."""
    environment = os.environ if environment is None else environment
    max_gb = _read_positive_number(environment, "CLIP_WATCHER_MAX_RECORDING_GB", 20.0)
    retention_hours = _read_positive_number(
        environment, "CLIP_WATCHER_RECORDING_RETENTION_HOURS", 168.0
    )
    enabled = _read_bool(environment, "CLIP_WATCHER_CLEANUP_ENABLED", True)
    return RetentionSettings(
        enabled=enabled,
        retention_hours=retention_hours,
        max_recording_bytes=int(max_gb * GIB),
    )
