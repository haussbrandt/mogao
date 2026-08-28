import json
import logging
import os
import pickle
from functools import lru_cache
from typing import Optional

from books.book import Book
from core.config import settings
from core.constants import normalize_uuid


logger = logging.getLogger(__name__)


@lru_cache(maxsize=10)
def load_book_cached(folder_name: str) -> Optional[Book]:
    """
    Loads the book from the pickle file.
    Cached so we don't re-read the disk on every click.
    """
    try:
        safe_id = normalize_uuid(folder_name)
    except ValueError:
        return None
    file_path = os.path.join(settings.paths.library, safe_id, "book.pkl")
    if not os.path.exists(file_path):
        return None

    try:
        with open(file_path, "rb") as f:
            book = pickle.load(f)
        return book
    except Exception:
        logger.exception(f"Error loading book {folder_name}")
        return None


def get_progress_path(book_id: str) -> str:
    """Returns the path to the progress.json file for a given book."""
    safe_id = normalize_uuid(book_id)
    return os.path.join(settings.paths.library, safe_id, "progress.json")


def save_progress(book_id: str, chapter_index: int, scroll_percentage: float = 0.0):
    """Saves the current reading progress to the book's progress file."""
    try:
        path = get_progress_path(book_id)
        with open(path, "w") as f:
            json.dump(
                {
                    "chapter_index": chapter_index,
                    "scroll_percentage": scroll_percentage,
                },
                f,
            )
    except Exception:
        logger.exception(f"Error saving progress for book {book_id}")


def load_progress(book_id: str):
    """Loads the last read chapter reading progress from file."""
    # TODO: Create a class for this, so there is never a possible mismatch between save and load
    default_progress = {"chapter_index": 0, "scroll_percentage": 0.0}
    try:
        path = get_progress_path(book_id)
        if os.path.exists(path):
            with open(path, "r") as f:
                return json.load(f)
    except Exception:
        logger.exception(f"Error loading progress for book {book_id}")
    return default_progress


def get_settings_path() -> str:
    return os.path.join(settings.paths.library, "settings.json")


def load_settings() -> dict:
    try:
        path = get_settings_path()
        if os.path.exists(path):
            with open(path, "r") as f:
                return json.load(f)
    except Exception:
        logger.exception("Error loading book library settings")
    return {}


def save_settings(preferences: dict):
    try:
        path = get_settings_path()
        with open(path, "w") as f:
            json.dump(preferences, f)
    except Exception:
        logger.exception("Error saving book library settings")
