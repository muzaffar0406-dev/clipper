"""Safe housekeeping for completed Streamlink recording files."""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Callable

from app_config import RetentionSettings


# An actively written Streamlink file is normally modified repeatedly. This extra
# guard protects a recording from another local process that this watcher did not start.
RECENT_RECORDING_GRACE_SECONDS = 300


@dataclass
class CleanupResult:
    scanned: int = 0
    deleted: list[Path] = field(default_factory=list)
    skipped_active: list[Path] = field(default_factory=list)
    skipped_recent: list[Path] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)


def _is_contained(path: Path, root: Path) -> bool:
    try:
        path.relative_to(root)
        return True
    except ValueError:
        return False


def _log(logger: Callable[[str], None] | None, message: str) -> None:
    if logger is not None:
        logger(message)


def cleanup_recordings(
    recordings_dir: Path,
    settings: RetentionSettings,
    *,
    active_recording: Path | None = None,
    now: float | None = None,
    dry_run: bool = False,
    logger: Callable[[str], None] | None = None,
) -> CleanupResult:
    """Remove only verified, completed .ts recordings that exceed policy.

    The function never traverses subdirectories, never considers clips/subtitles,
    ignores symlinks, skips the caller's active path, and skips recently modified
    files to avoid deleting a recording from another active local process.
    """
    result = CleanupResult()
    if not settings.enabled:
        _log(logger, "cleanup: disabled by configuration")
        return result

    root = recordings_dir.resolve(strict=False)
    if not root.exists():
        _log(logger, f"cleanup: recordings directory does not exist: {root}")
        return result
    if not root.is_dir():
        result.errors.append(f"recordings path is not a directory: {root}")
        _log(logger, f"ERROR: cleanup: {result.errors[-1]}")
        return result

    current_time = datetime.now().timestamp() if now is None else now
    active = active_recording.resolve(strict=False) if active_recording else None
    candidates: list[tuple[Path, float, int]] = []

    for candidate in root.iterdir():
        if candidate.is_symlink() or not candidate.is_file() or candidate.suffix.lower() != ".ts":
            continue
        resolved = candidate.resolve(strict=False)
        if not _is_contained(resolved, root):
            _log(logger, f"cleanup: skipped path outside recordings directory: {candidate.name}")
            continue
        if active is not None and resolved == active:
            result.skipped_active.append(candidate)
            _log(logger, f"cleanup: preserved active recording: {candidate.name}")
            continue
        try:
            stat = candidate.stat()
        except OSError as exc:
            result.errors.append(f"could not inspect {candidate.name}: {exc}")
            _log(logger, f"ERROR: cleanup: {result.errors[-1]}")
            continue
        result.scanned += 1
        if current_time - stat.st_mtime < RECENT_RECORDING_GRACE_SECONDS:
            result.skipped_recent.append(candidate)
            _log(logger, f"cleanup: preserved recently modified recording: {candidate.name}")
            continue
        candidates.append((candidate, stat.st_mtime, stat.st_size))

    def remove(candidate: Path, reason: str) -> bool:
        if dry_run:
            _log(logger, f"cleanup: would remove {candidate.name} ({reason})")
            return True
        try:
            candidate.unlink()
            result.deleted.append(candidate)
            _log(logger, f"cleanup: removed {candidate.name} ({reason})")
            return True
        except OSError as exc:
            result.errors.append(f"could not remove {candidate.name}: {exc}")
            _log(logger, f"ERROR: cleanup: {result.errors[-1]}")
            return False

    max_age_seconds = settings.retention_hours * 3600
    retained: list[tuple[Path, float, int]] = []
    for candidate, modified, size in candidates:
        if current_time - modified > max_age_seconds:
            remove(candidate, "retention age exceeded")
        else:
            retained.append((candidate, modified, size))

    total_size = sum(size for _, _, size in retained)
    for candidate, _modified, size in sorted(retained, key=lambda item: item[1]):
        if total_size <= settings.max_recording_bytes:
            break
        if remove(candidate, "recording storage limit exceeded"):
            total_size -= size

    return result
