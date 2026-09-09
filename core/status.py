import os
import shutil
import subprocess
import time
from datetime import datetime
from functools import lru_cache
from pathlib import Path
from uuid import UUID

import tomllib

from core.config import PROJECT_ROOT, settings
from core.logging_config import get_recent_issues
from core.paths import TEMP_DIRECTORY


STARTED_AT = time.monotonic()


def _format_bytes(size: int) -> str:
    value = float(size)
    for unit in ("B", "KiB", "MiB", "GiB", "TiB"):
        if value < 1024 or unit == "TiB":
            precision = 0 if unit in {"B", "KiB"} else 1
            return f"{value:.{precision}f} {unit}"
        value /= 1024
    return f"{value:.1f} TiB"


def _format_uptime(seconds: float) -> str:
    minutes = int(seconds) // 60
    days, minutes = divmod(minutes, 24 * 60)
    hours, minutes = divmod(minutes, 60)
    if days:
        return f"{days}d {hours}h {minutes}m"
    if hours:
        return f"{hours}h {minutes}m"
    return f"{minutes}m"


def _format_timestamp(value: str) -> str:
    try:
        return datetime.fromisoformat(value).astimezone().strftime("%Y-%m-%d %H:%M")
    except ValueError:
        return value


def _existing_ancestor(path: Path) -> Path:
    candidate = path
    while not candidate.exists() and candidate != candidate.parent:
        candidate = candidate.parent
    return candidate


def _storage_status() -> list[dict]:
    locations = [("Books", settings.paths.library)]
    if settings.video.enabled:
        locations.append(("Videos", settings.paths.video_library))
    locations.append(("Temporary files", TEMP_DIRECTORY))

    filesystems: dict[int, dict] = {}
    for label, path in locations:
        existing_path = _existing_ancestor(path)
        device = os.stat(existing_path).st_dev
        if device not in filesystems:
            usage = shutil.disk_usage(existing_path)
            used_percent = round((usage.used / usage.total) * 100) if usage.total else 0
            filesystems[device] = {
                "labels": [],
                "free": _format_bytes(usage.free),
                "used": _format_bytes(usage.used),
                "total": _format_bytes(usage.total),
                "used_percent": used_percent,
                "low": used_percent >= 90,
            }
        filesystems[device]["labels"].append(label)

    result = []
    for filesystem in filesystems.values():
        labels = filesystem.pop("labels")
        result.append({**filesystem, "label": ", ".join(labels)})
    return result


def _content_count(root: Path, metadata_filename: str) -> int:
    if not root.is_dir():
        return 0

    count = 0
    for path in root.iterdir():
        if not path.is_dir() or not (path / metadata_filename).is_file():
            continue
        try:
            UUID(path.name)
        except ValueError:
            continue
        count += 1
    return count


@lru_cache(maxsize=1)
def _application_version() -> dict[str, str | None]:
    try:
        with (PROJECT_ROOT / "pyproject.toml").open("rb") as project_file:
            project_version = tomllib.load(project_file)["project"]["version"]
    except (OSError, KeyError, tomllib.TOMLDecodeError):
        project_version = "unknown"

    display_version = project_version
    warning = None
    try:
        result = subprocess.run(
            ["git", "describe", "--tags", "--always", "--dirty", "--long"],
            cwd=PROJECT_ROOT,
            check=True,
            capture_output=True,
            text=True,
            timeout=2,
        )
        description = result.stdout.strip()
    except (OSError, subprocess.SubprocessError):
        description = ""

    if description:
        dirty = description.endswith("-dirty")
        clean_description = description.removesuffix("-dirty")
        tag_part, separator, commit_part = clean_description.rpartition("-g")
        tag, distance_separator, distance_text = tag_part.rpartition("-")

        if separator and distance_separator and distance_text.isdigit():
            distance = int(distance_text)
            tag_version = tag.removeprefix("v")
            if tag_version == project_version:
                if distance:
                    plural = "commit" if distance == 1 else "commits"
                    display_version += f" · {distance} {plural} ahead ({commit_part})"
            elif distance:
                plural = "commit" if distance == 1 else "commits"
                display_version += f" · {distance} {plural} after {tag} ({commit_part})"
            else:
                display_version += f" · checkout at {tag}"
            if dirty:
                display_version += " · modified"
            if tag_version != project_version:
                warning = (
                    f"pyproject.toml declares {project_version}, but the nearest "
                    f"Git tag is {tag}."
                )
        else:
            display_version += f" · {clean_description}"
            if dirty:
                display_version += " · modified"

    return {"display": display_version, "warning": warning}


def build_status() -> dict:
    storage = _storage_status()
    application_version = _application_version()
    issues = [
        {**issue, "timestamp": _format_timestamp(issue["timestamp"])}
        for issue in get_recent_issues()
    ]
    storage_low = any(filesystem["low"] for filesystem in storage)

    if storage_low:
        overall = {
            "state": "attention",
            "label": "Needs attention",
            "detail": "Storage space is running low.",
        }
    else:
        overall = {
            "state": "healthy",
            "label": "All clear",
            "detail": "The server is running and storage levels are normal.",
        }

    return {
        "overall": overall,
        "issues": issues,
        "storage": storage,
        "server": {
            "version": application_version["display"],
            "version_warning": application_version["warning"],
            "uptime": _format_uptime(time.monotonic() - STARTED_AT),
            "book_count": _content_count(settings.paths.library, "book.pkl"),
            "video_count": (
                _content_count(settings.paths.video_library, "video.pkl")
                if settings.video.enabled
                else 0
            ),
            "video_enabled": settings.video.enabled,
        },
    }
