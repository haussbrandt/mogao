from functools import lru_cache
import json
import os
import pickle
from typing import Optional

from book import Book
from constants import LIBRARY_PATH


@lru_cache(maxsize=10)
def load_book_cached(folder_name: str) -> Optional[Book]:
    """
    Loads the book from the pickle file.
    Cached so we don't re-read the disk on every click.
    """
    file_path = os.path.join(LIBRARY_PATH, folder_name, "book.pkl")
    if not os.path.exists(file_path):
        return None

    try:
        with open(file_path, "rb") as f:
            book = pickle.load(f)
        return book
    except Exception as e:
        print(f"Error loading book {folder_name}: {e}")
        return None


def get_progress_path(book_id: str) -> str:
    """Returns the path to the progress.json file for a given book."""
    safe_id = os.path.basename(book_id)
    return os.path.join(LIBRARY_PATH, safe_id, "progress.json")


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
    except Exception as e:
        print(f"Error saving progress for {book_id}: {e}")


def load_progress(book_id: str):
    """Loads the last read chapter reading progress from file."""
    # TODO: Create a class for this, so there is never a possible mismatch between save and load
    default_progress = {"chapter_index": 0, "scroll_percentage": 0.0}
    try:
        path = get_progress_path(book_id)
        if os.path.exists(path):
            with open(path, "r") as f:
                return json.load(f)
    except Exception as e:
        print(f"Error loading progress for {book_id}: {e}")
    return default_progress
