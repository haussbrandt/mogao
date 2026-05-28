from functools import lru_cache
import json
import os
import pickle
from typing import Optional

from constants import VIDEO_LIBRARY_PATH
from video import Video


@lru_cache(maxsize=10)
def load_video_cached(folder_name: str) -> Optional[Video]:
    """
    Loads the video metadata from the pickle file.
    Cached so we don't re-read the disk on every click.
    """
    safe_id = os.path.basename(folder_name)
    file_path = os.path.join(VIDEO_LIBRARY_PATH, safe_id, "video.pkl")
    if not os.path.exists(file_path):
        return None

    try:
        with open(file_path, "rb") as f:
            video = pickle.load(f)
        return video
    except Exception as e:
        print(f"Error loading video {folder_name}: {e}")
        return None


def get_video_progress_path(video_id: str) -> str:
    """Returns the path to the progress.json file for a given video."""
    safe_id = os.path.basename(video_id)
    return os.path.join(VIDEO_LIBRARY_PATH, safe_id, "progress.json")


def load_video_progress(video_id: str):
    """Loads the last watch progress from file."""
    # TODO: Create a class for this, so there is never a possible mismatch between save and load
    default_progress = {"seconds_since_start": 0}
    try:
        path = get_video_progress_path(video_id)
        if os.path.exists(path):
            with open(path, "r") as f:
                return json.load(f)
    except Exception as e:
        print(f"Error loading progress for {video_id}: {e}")
    return default_progress


def get_video_settings_path() -> str:
    return os.path.join(VIDEO_LIBRARY_PATH, "video_settings.json")


def load_video_settings() -> dict:
    try:
        path = get_video_settings_path()
        if os.path.exists(path):
            with open(path, "r") as f:
                return json.load(f)
    except Exception as e:
        print(f"Error loading settings: {e}")
    return {}


def save_video_settings(settings: dict):
    try:
        path = get_video_settings_path()
        with open(path, "w") as f:
            json.dump(settings, f)
    except Exception as e:
        print(f"Error saving settings: {e}")
