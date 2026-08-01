import json
import logging
import os
import pickle
from functools import lru_cache
from typing import Optional

from config import settings
from constants import normalize_uuid
from video import Video


logger = logging.getLogger(__name__)


@lru_cache(maxsize=10)
def load_video_cached(folder_name: str) -> Optional[Video]:
    """
    Loads the video metadata from the pickle file.
    Cached so we don't re-read the disk on every click.
    """
    try:
        safe_id = normalize_uuid(folder_name)
    except ValueError:
        return None
    file_path = os.path.join(settings.paths.video_library, safe_id, "video.pkl")
    if not os.path.exists(file_path):
        return None

    try:
        with open(file_path, "rb") as f:
            video = pickle.load(f)
        return video
    except Exception:
        logger.exception(f"Error loading video {folder_name}")
        return None


def get_video_progress_path(video_id: str) -> str:
    """Returns the path to the progress.json file for a given video."""
    safe_id = normalize_uuid(video_id)
    return os.path.join(settings.paths.video_library, safe_id, "progress.json")


def save_video_progress(video_id: str, seconds_since_start: float = 0.0):
    """Saves the current playback position to the video's progress file."""
    try:
        path = get_video_progress_path(video_id)
        with open(path, "w") as f:
            json.dump({"seconds_since_start": seconds_since_start}, f)
    except Exception:
        logger.exception(f"Error saving progress for video {video_id}")


def load_video_progress(video_id: str):
    """Loads the last watch progress from file."""
    # TODO: Create a class for this, so there is never a possible mismatch between save and load
    default_progress = {"seconds_since_start": 0}
    try:
        path = get_video_progress_path(video_id)
        if os.path.exists(path):
            with open(path, "r") as f:
                return json.load(f)
    except Exception:
        logger.exception(f"Error loading progress for video {video_id}")
    return default_progress


def get_video_settings_path() -> str:
    return os.path.join(settings.paths.video_library, "video_settings.json")


def load_video_settings() -> dict:
    try:
        path = get_video_settings_path()
        if os.path.exists(path):
            with open(path, "r") as f:
                return json.load(f)
    except Exception:
        logger.exception("Error loading video library settings")
    return {}


def save_video_settings(preferences: dict):
    try:
        path = get_video_settings_path()
        with open(path, "w") as f:
            json.dump(preferences, f)
    except Exception:
        logger.exception("Error saving video library settings")
