import os
import shutil
import subprocess
import time
from datetime import datetime
from functools import lru_cache
from pathlib import Path
from uuid import UUID

import tomllib

from books.library import load_book_cached
from core.config import PROJECT_ROOT, settings
from core.logging_config import get_recent_issues
from core.paths import TEMP_DIRECTORY, get_book_dict_path, get_video_dict_path
from integrations.llm_processor import load_book_dict, load_video_dict
from videos.video_library import load_video_cached


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


def _format_timestamp(
    value: str | None, *, include_seconds: bool = False
) -> str | None:
    if not value:
        return None
    try:
        timestamp_format = (
            "%Y-%m-%d %H:%M:%S" if include_seconds else "%Y-%m-%d %H:%M"
        )
        return datetime.fromisoformat(value).astimezone().strftime(timestamp_format)
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


def _content_directories(root: Path):
    if not root.is_dir():
        return
    for path in root.iterdir():
        if not path.is_dir():
            continue
        try:
            UUID(path.name)
        except ValueError:
            continue
        yield path.name


def _content_signature(
    root: Path, metadata_filename: str, *, include_subtitles: bool = False
) -> tuple[tuple[str, int, int, bool], ...]:
    """Return the lightweight filesystem state needed by the status inventory."""
    signature = []
    for item_id in _content_directories(root) or ():
        item_path = root / item_id
        try:
            metadata = (item_path / metadata_filename).stat()
        except OSError:
            continue
        signature.append(
            (
                item_id,
                metadata.st_mtime_ns,
                metadata.st_size,
                include_subtitles and (item_path / "subtitles.srt").is_file(),
            )
        )
    return tuple(sorted(signature))


@lru_cache(maxsize=1)
def _load_content_inventory(
    book_signature: tuple[tuple[str, int, int, bool], ...],
    video_signature: tuple[tuple[str, int, int, bool], ...],
) -> tuple[tuple[tuple[str, str], ...], tuple[tuple[str, str, bool], ...]]:
    """Load cached titles, invalidating them when content metadata changes."""
    books = []
    for book_id, *_ in book_signature:
        book = load_book_cached(book_id)
        if book:
            books.append((book_id, book.metadata.title))

    videos = []
    for video_id, _, _, has_subtitles in video_signature:
        video = load_video_cached(video_id)
        if video:
            videos.append((video_id, video.metadata.title, has_subtitles))

    return tuple(books), tuple(videos)


def _content_inventory() -> tuple[
    tuple[tuple[str, str], ...],
    tuple[tuple[str, str, bool], ...],
]:
    book_signature = _content_signature(settings.paths.library, "book.pkl")
    video_signature = ()
    if settings.video.enabled:
        video_signature = _content_signature(
            settings.paths.video_library,
            "video.pkl",
            include_subtitles=True,
        )
    return _load_content_inventory(book_signature, video_signature)


def _dictionary_job(content_type: str, item_id: str, title: str, state: dict) -> dict:
    status = state.get("status", "none")
    processed = int(state.get("processed_chunks", 0))
    total = int(state.get("total_chunks", 0))
    failed = len(state.get("failed_chunks", []))
    attempted = processed + failed
    pending = max(total - attempted, 0)
    succeeded_percent = round((processed / total) * 100, 2) if total else 0
    failed_percent = round((failed / total) * 100, 2) if total else 0
    labels = {
        "none": "Pending",
        "processing": "Processing",
        "done": "Complete",
        "error": "Error",
    }
    return {
        "kind": content_type,
        "id": item_id,
        "title": title,
        "status": status,
        "status_label": labels.get(status, status.replace("_", " ").title()),
        "succeeded": processed,
        "failed": failed,
        "attempted": attempted,
        "pending": pending,
        "succeeded_percent": succeeded_percent,
        "failed_percent": failed_percent,
        "total": total,
        "last_error": state.get("last_error"),
        "updated_at": _format_timestamp(
            state.get("updated_at")
            or state.get("completed_at")
            or state.get("started_at"),
            include_seconds=True,
        ),
    }


@lru_cache(maxsize=1024)
def _load_dictionary_summary(
    item_id: str, path: Path, signature: tuple[int, int, int], load_state
) -> dict:
    """Cache only progress metadata; generated word dictionaries can be large."""
    state = load_state(item_id)
    return {
        key: value
        for key, value in state.items()
        if key not in {"words", "completed_indices"}
    }


def _dictionary_summary(item_id: str, path: Path, load_state) -> dict:
    try:
        stat = path.stat()
    except FileNotFoundError:
        return {"status": "none"}
    # Atomic replacements change the inode, even on coarse timestamp filesystems.
    return _load_dictionary_summary(
        item_id,
        path,
        (stat.st_mtime_ns, stat.st_size, stat.st_ino),
        load_state,
    )


def _dictionary_status(
    books: tuple[tuple[str, str], ...],
    videos: tuple[tuple[str, str, bool], ...],
) -> list[dict]:
    if not settings.dictionary_generation.enabled:
        return []

    jobs = [
        _dictionary_job(
            "Book",
            book_id,
            title,
            _dictionary_summary(
                book_id, get_book_dict_path(book_id), load_book_dict
            ),
        )
        for book_id, title in books
    ]
    jobs.extend(
        _dictionary_job(
            "Video",
            video_id,
            title,
            _dictionary_summary(
                video_id, get_video_dict_path(video_id), load_video_dict
            ),
        )
        for video_id, title, has_subtitles in videos
        if has_subtitles
    )

    jobs = [job for job in jobs if job["status"] != "done"]
    priority = {"error": 0, "processing": 1, "none": 2}
    jobs.sort(key=lambda job: (priority.get(job["status"], 4), job["title"].lower()))
    return jobs


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
    books, videos = _content_inventory()
    dictionaries = _dictionary_status(books, videos)
    storage = _storage_status()
    application_version = _application_version()
    issues = [
        {**issue, "timestamp": _format_timestamp(issue["timestamp"])}
        for issue in get_recent_issues()
    ]
    storage_low = any(filesystem["low"] for filesystem in storage)
    dictionary_problem = any(job["status"] == "error" for job in dictionaries)
    dictionary_active = any(
        job["status"] in {"none", "processing"} for job in dictionaries
    )

    if storage_low or dictionary_problem:
        overall = {
            "state": "attention",
            "label": "Needs attention",
            "detail": "One or more current conditions need attention.",
        }
    elif dictionary_active:
        overall = {
            "state": "working",
            "label": "Working",
            "detail": "Dictionary generation is in progress or waiting to run.",
        }
    else:
        overall = {
            "state": "healthy",
            "label": "All clear",
            "detail": "No current problems or pending work.",
        }

    return {
        "overall": overall,
        "dictionary_enabled": settings.dictionary_generation.enabled,
        "dictionaries": dictionaries,
        "issues": issues,
        "storage": storage,
        "server": {
            "version": application_version["display"],
            "version_warning": application_version["warning"],
            "uptime": _format_uptime(time.monotonic() - STARTED_AT),
            "book_count": len(books),
            "video_count": len(videos),
            "video_enabled": settings.video.enabled,
        },
    }
