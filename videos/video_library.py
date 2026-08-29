import json
import logging
import os
import pickle
from functools import lru_cache
from typing import Optional

from core.paths import (
    get_video_path,
    get_video_progress_path,
    get_video_settings_path,
)
from videos.video import Video


logger = logging.getLogger(__name__)


@lru_cache(maxsize=10)
def load_video_cached(folder_name: str) -> Optional[Video]:
    """
    Loads the video metadata from the pickle file.
    Cached so we don't re-read the disk on every click.
    """
    try:
        file_path = get_video_path(folder_name, "video.pkl")
    except ValueError:
        return None
    if not os.path.exists(file_path):
        return None

    try:
        with open(file_path, "rb") as f:
            video = pickle.load(f)
        return video
    except Exception:
        logger.exception(f"Error loading video {folder_name}")
        return None


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
