import tempfile
import uuid
from pathlib import Path
from uuid import UUID

from core.config import settings


TEMP_DIRECTORY = Path(tempfile.gettempdir()) / "mogao"


def _normalize_uuid(value: str | UUID) -> str:
    """Return a canonical UUID string or raise ValueError."""
    return str(UUID(str(value)))


def named_temp_path(filename: str) -> Path:
    """Return a path with the given filename in Mogao's temporary directory."""
    candidate = Path(filename)
    if (
        not filename
        or "\0" in filename
        or "/" in filename
        or "\\" in filename
        or candidate.is_absolute()
        or candidate.name != filename
        or filename in {".", ".."}
    ):
        raise ValueError("Temporary filename must be a plain filename")

    TEMP_DIRECTORY.mkdir(mode=0o700, parents=True, exist_ok=True)
    path = TEMP_DIRECTORY / filename
    if path.resolve().parent != TEMP_DIRECTORY.resolve():
        raise ValueError("Temporary filename resolves outside the temporary directory")
    return path


def unique_temp_path(suffix: str) -> Path:
    """Return a UUID-named path inside Mogao's temporary directory."""
    return named_temp_path(f"{uuid.uuid4()}{suffix}")


def get_book_path(book_id: str | UUID, *parts: str) -> Path:
    safe_id = _normalize_uuid(book_id)
    return settings.paths.library.joinpath(safe_id, *parts)


def get_video_path(video_id: str | UUID, *parts: str) -> Path:
    safe_id = _normalize_uuid(video_id)
    return settings.paths.video_library.joinpath(safe_id, *parts)


def get_book_progress_path(book_id: str | UUID) -> Path:
    return get_book_path(book_id, "progress.json")


def get_book_settings_path() -> Path:
    return settings.paths.library / "settings.json"


def get_book_dict_path(book_id: str | UUID) -> Path:
    return get_book_path(book_id, "book_dict.json")


def get_video_progress_path(video_id: str | UUID) -> Path:
    return get_video_path(video_id, "progress.json")


def get_video_settings_path() -> Path:
    return settings.paths.video_library / "video_settings.json"


def get_video_dict_path(video_id: str | UUID) -> Path:
    return get_video_path(video_id, "subtitles_dict.json")
