import base64
import glob
import json
import os
import pickle
import re
import requests
import secrets
import shutil
import uuid

from functools import lru_cache
from typing import Optional

import bcrypt

from dotenv import load_dotenv
from fastapi import FastAPI, File, Request, HTTPException, Response, UploadFile
from fastapi.middleware.gzip import GZipMiddleware
from fastapi.responses import HTMLResponse, FileResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from pydantic import BaseModel
from starlette.middleware.base import BaseHTTPMiddleware

from book import Book, Metadata, ChapterContent, TOCEntry, generate_book
from constants import DICT_PATH, FREQ_PATH, LIBRARY_PATH


class AuthMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next):
        auth_header = request.headers.get("Authorization")

        if not auth_header or not auth_header.startswith("Basic "):
            return Response(
                content="Authentication required",
                status_code=401,
                headers={"WWW-Authenticate": 'Basic realm="Secure Area"'},
            )

        try:
            credentials = base64.b64decode(auth_header[6:]).decode("utf-8")
            username, password = credentials.split(":", 1)

            username_correct = secrets.compare_digest(username, "admin")
            password_correct = bcrypt.checkpw(
                password.encode(), os.environ.get("MOGAO_ADMIN_HASH").encode()
            )

            if not (username_correct and password_correct):
                return Response(
                    content="Invalid credentials",
                    status_code=401,
                    headers={"WWW-Authenticate": 'Basic realm="Secure Area"'},
                )
        except Exception:
            return Response(
                content="Invalid authentication",
                status_code=401,
                headers={"WWW-Authenticate": 'Basic realm="Secure Area"'},
            )

        response = await call_next(request)
        return response


app = FastAPI()
app.add_middleware(GZipMiddleware, minimum_size=1000)
app.add_middleware(AuthMiddleware)
app.mount("/static", StaticFiles(directory="static"), name="static")
templates = Jinja2Templates(directory="templates")

CHINESE_DICT = {}
CHINESE_FREQ = {}


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


class ProgressRequest(BaseModel):
    book_id: str
    chapter_index: int
    scroll_percentage: float


@app.post("/api/save-progress")
async def save_progress_api(data: ProgressRequest):
    save_progress(data.book_id, data.chapter_index, data.scroll_percentage)
    return {"status": "ok"}


class NewCardRequest(BaseModel):
    word: str
    pinyin: str
    sentence: str


@app.post("/api/new-card")
async def create_new_anki_card(data: NewCardRequest):
    for item in data:
        print(item)
    return {"status": "ok"}


def call_anki(action, **params):
    return requests.post(
        "http://localhost:8765", json={"action": action, "params": params, "version": 6}
    )


def get_all_words_from_anki_deck(deck_name: str, field_name: str) -> set[str]:
    """
    Sends requests to AnkiConnect to get all cards from the deck `deck_name` and returns a set of values of the field `field_name` from them
    """
    call_anki("sync")
    card_ids = call_anki("findCards", query=f'deck:"{deck_name}"').json()["result"]
    cards = call_anki("cardsInfo", cards=card_ids).json()["result"]
    words = {card["fields"][field_name]["value"] for card in cards}
    return words


def convert_pinyin_tone(pinyin_str):
    tone_map = {
        "a": "āáǎàa",
        "e": "ēéěèe",
        "i": "īíǐìi",
        "o": "ōóǒòo",
        "u": "ūúǔùu",
        "v": "ǖǘǚǜü",
        "ü": "ǖǘǚǜü",
    }
    results = []
    for word in pinyin_str.split():
        match = re.match(r"^([a-zA-Zü:]+)([1-5])$", word)
        if not match:
            results.append(word)
            continue
        base, tone = match.groups()
        tone_idx = int(tone) - 1
        base = base.replace("u:", "ü").replace("v", "ü")
        target_char = None
        idx = -1
        for char in ["a", "e", "o"]:
            if char in base:
                idx = base.find(char)
                break
        if idx == -1:
            for i in range(len(base) - 1, -1, -1):
                if base[i] in "iuü":
                    idx = i
                    break
        if idx != -1 and tone_idx < 4:
            char = base[idx]
            replacement = tone_map[char][tone_idx]
            base = base[:idx] + replacement + base[idx + 1 :]
        results.append(base)
    return "".join(results)


def load_dictionary():
    global CHINESE_DICT
    if not os.path.exists(DICT_PATH):
        print(f"Warning: {DICT_PATH} not found.")
        return

    print("Loading dictionary...")
    pattern = re.compile(r"(\S+)\s+(\S+)\s+\[(.*?)\]\s+/(.*)/")

    with open(DICT_PATH, "r", encoding="utf-8") as f:
        for line in f:
            if line.startswith("#") or not line.strip():
                continue
            match = pattern.match(line)
            if match:
                trad, simp, pinyin_raw, defs_str = match.groups()
                entry = {
                    "pinyin": convert_pinyin_tone(pinyin_raw),
                    "definitions": defs_str.split("/"),
                }
                if simp not in CHINESE_DICT:
                    CHINESE_DICT[simp] = []
                CHINESE_DICT[simp].append(entry)
    print(f"Dictionary loaded: {len(CHINESE_DICT)} entries.")


def load_frequency():
    """
    Scans FREQ_DIR for Yomitan-formatted JSON files.
    Calculates the Harmonic Mean of ranks across all files.
    """
    global CHINESE_FREQ
    if not os.path.exists(FREQ_PATH):
        print(f"Warning: {FREQ_PATH} directory not found.")
        return

    print("Loading frequency data (this might take a moment)...")

    temp_scores = {}  # word -> [score1, score2, ...]

    files = glob.glob(
        os.path.join(FREQ_PATH, "**", "*term_meta_bank*.json"), recursive=True
    )

    if not files:
        print("No frequency JSON files found in freqs/ folder.")
        return

    for file_path in files:
        try:
            with open(file_path, "r", encoding="utf-8") as f:
                data = json.load(f)

            for entry in data:
                if isinstance(entry, list) and len(entry) >= 3 and entry[1] == "freq":
                    term = entry[0]
                    val = entry[2]

                    if isinstance(val, (int, float)) and val > 0:
                        if term not in temp_scores:
                            temp_scores[term] = []
                        temp_scores[term].append(val)
        except Exception as e:
            print(f"Error reading {file_path}: {e}")

    # Calculate Harmonic Mean
    count = 0
    for term, scores in temp_scores.items():
        try:
            reciprocal_sum = sum(1.0 / s for s in scores)
            if reciprocal_sum > 0:
                hm = len(scores) / reciprocal_sum
                CHINESE_FREQ[term] = int(hm)
                count += 1
        except Exception:
            pass

    print(f"Frequency data loaded: {count} unique terms.")


# Load on startup
load_dictionary()
load_frequency()


@app.get("/api/lookup")
def lookup_word(text: str):
    if not CHINESE_DICT:
        return {"results": []}

    clean_text = re.sub(r"\s+", "", text)
    matches = []
    limit = min(6, len(clean_text))

    for i in range(limit, 0, -1):
        candidate = clean_text[:i]
        if candidate in CHINESE_DICT:
            freq_val = CHINESE_FREQ.get(candidate)
            freq_str = None

            if freq_val is not None:
                if freq_val < 10000:
                    freq_str = str(freq_val)
                else:
                    freq_str = f"{freq_val:,}".replace(",", " ")

            matches.append(
                {
                    "word": candidate,
                    "entries": CHINESE_DICT[candidate],
                    "frequency": freq_str,
                    "length": i,
                }
            )

    return {"results": matches}


@app.get("/", response_class=HTMLResponse)
async def library_view(request: Request):
    """Lists all available processed books."""
    books = []

    if os.path.exists(LIBRARY_PATH):
        for item in os.listdir(LIBRARY_PATH):
            if os.path.isdir(os.path.join(LIBRARY_PATH, item)):
                book = load_book_cached(item)
                if book:
                    books.append(
                        {
                            "id": item,
                            "title": book.metadata.title,
                            "author": ", ".join(book.metadata.authors),
                            "chapters": len(book.spine),
                            "cover_url": f"/read/{item}/images/{book.cover_image}",
                        }
                    )

    return templates.TemplateResponse(
        "library.html", {"request": request, "books": books}
    )


@app.post("/upload")
async def upload_book(files: list[UploadFile] = File(...)):
    """
    Handles EPUB upload.
    Saves to temp, processes with generate_book, clears temp, refreshes library.
    """

    for file in files:
        if not file.filename.endswith(".epub"):
            raise HTTPException(status_code=400, detail="Only .epub files are allowed")

        # Save uploaded file temporarily
        temp_filename = f"temp_{uuid.uuid4()}.epub"
        try:
            with open(temp_filename, "wb") as buffer:
                shutil.copyfileobj(file.file, buffer)

            generate_book(temp_filename, LIBRARY_PATH)
            load_book_cached.cache_clear()

        except Exception as e:
            print(f"Error processing book: {e}")
            raise HTTPException(status_code=500, detail="Failed to process book")
        finally:
            # Cleanup temp file
            if os.path.exists(temp_filename):
                os.remove(temp_filename)

    return RedirectResponse(url="/", status_code=303)


@app.post("/delete/{book_id}")
async def delete_book(book_id: str):
    """
    Deletes a book folder and refreshes the cache.
    """
    # Security: Sanitizing to ensure no one deletes ../system_files
    safe_id = os.path.basename(book_id)
    book_path = os.path.join(LIBRARY_PATH, safe_id)

    if os.path.exists(book_path):
        try:
            shutil.rmtree(book_path)
            load_book_cached.cache_clear()
        except Exception as e:
            print(f"Error deleting book {safe_id}: {e}")
            raise HTTPException(status_code=500, detail="Failed to delete book")
    else:
        raise HTTPException(status_code=404, detail="Book not found")

    return RedirectResponse(url="/", status_code=303)


@app.get("/read/{book_id}", response_class=HTMLResponse)
async def redirect_to_last_read(request: Request, book_id: str):
    """Helper to go to the last read chapter."""
    progress = load_progress(book_id)
    last_chapter_index = progress.get("chapter_index", 0)
    return RedirectResponse(url=f"/read/{book_id}/{last_chapter_index}")


@app.get("/read/{book_id}/{chapter_index}", response_class=HTMLResponse)
async def read_chapter(request: Request, book_id: str, chapter_index: int):
    """The main reader interface."""
    book = load_book_cached(book_id)
    if not book:
        raise HTTPException(status_code=404, detail="Book not found")

    if chapter_index < 0 or chapter_index >= len(book.spine):
        raise HTTPException(status_code=404, detail="Chapter not found")

    progress = load_progress(book_id)
    saved_chapter = progress.get("chapter_index", -1)
    initial_scroll_percentage = 0.0
    if saved_chapter == chapter_index:
        initial_scroll_percentage = progress.get("scroll_percentage", 0.0)

    save_progress(book_id, chapter_index, initial_scroll_percentage)

    current_chapter = book.spine[chapter_index]
    # Calculate Prev/Next links
    prev_idx = chapter_index - 1 if chapter_index > 0 else None
    next_idx = chapter_index + 1 if chapter_index < len(book.spine) - 1 else None

    return templates.TemplateResponse(
        "reader.html",
        {
            "request": request,
            "book": book,
            "current_chapter": current_chapter,
            "chapter_index": chapter_index,
            "book_id": book_id,
            "prev_idx": prev_idx,
            "next_idx": next_idx,
            "initial_scroll_percentage": initial_scroll_percentage,
        },
    )


@app.get("/read/{book_id}/images/{image_name}")
async def serve_image(book_id: str, image_name: str):
    """
    Serves images specifically for a book.
    The HTML contains <img src="images/pic.jpg">.
    The browser resolves this to /read/{book_id}/images/pic.jpg.
    """
    # Security check: ensure book_id is clean
    safe_book_id = os.path.basename(book_id)
    safe_image_name = os.path.basename(image_name)

    img_path = os.path.join(LIBRARY_PATH, safe_book_id, "images", safe_image_name)

    if not os.path.exists(img_path):
        raise HTTPException(status_code=404, detail="Image not found")

    return FileResponse(img_path)


if __name__ == "__main__":
    import uvicorn

    load_dotenv()
    words = get_all_words_from_anki_deck("Mandarin Sentence Mining", "Simplified")
    print(words)
    uvicorn.run(app, host="0.0.0.0", port=8123)
