import asyncio
import base64
import glob
import json
import os
import pickle
import re
import time
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
from constants import LIBRARY_PATH


class Postprocessor:
    def __init__(self, batch_size=10, timeout=3600) -> None:
        self.batch_size = batch_size
        self.timeout = timeout
        self.last_timer_reset = time.time()
        self.lock = asyncio.Lock()
        self.timer_task = None

    async def check_and_process(self):
        async with self.lock:
            card_ids = call_anki("findCards", query="tag:needs-processing").json()[
                "result"
            ]
            print(
                f"[{time.strftime('%Y-%m-%d %H:%M:%S')}] Cards waiting for postprocessing: {len(card_ids)}"
            )

            current_time = time.time()
            if len(card_ids) > 0 and self.timer_task is None:
                print(f"[{time.strftime('%Y-%m-%d %H:%M:%S')}] Starting a new timer")
                self.last_timer_reset = current_time
                self.timer_task = asyncio.create_task(self.start_timer())

            time_since_last = current_time - self.last_timer_reset
            if len(card_ids) >= self.batch_size or (
                len(card_ids) > 0 and time_since_last >= self.timeout
            ):
                print(f"[{time.strftime('%Y-%m-%d %H:%M:%S')}] Starting postprocessing")
                await self.run_postprocessing(card_ids)

                self.last_timer_reset = current_time
                if self.timer_task:
                    self.timer_task.cancel()
                self.timer_task = None
            else:
                print(
                    f"[{time.strftime('%Y-%m-%d %H:%M:%S')}] Not running postprocessing yet, {time_since_last=}"
                )

    async def start_timer(self):
        try:
            await asyncio.sleep(self.timeout)
            await self.check_and_process()
        except asyncio.CancelledError:
            pass

    async def run_postprocessing(self, card_ids):
        cards = call_anki("cardsInfo", cards=card_ids).json()["result"]
        batch_data = []
        for card in cards:
            batch_data.append(
                {
                    "id": card["note"],
                    "source_text": card["fields"]["SentenceSimplified"]["value"],
                    "source_word": card["fields"]["Simplified"]["value"],
                }
            )
        api_key = os.environ.get("GEMINI_API_KEY")
        results = call_gemini_batch(api_key, batch_data)
        if results:
            for result in results:
                try:
                    note_id = result["id"]

                    fields = {
                        "SentenceSimplified": result["formatted_sentence"],
                        "SentencePinyin.1": result["sentence_pinyin"],
                        "SentenceMeaning": result["sentence_meaning"],
                    }

                    call_anki(
                        "updateNoteFields", note={"id": note_id, "fields": fields}
                    )
                    call_anki(
                        "removeTags",
                        notes=[note_id],
                        tags="needs-processing",
                    )
                    print(f"processed {note_id}")
                except:
                    pass
            call_anki("sync")


def call_gemini_batch(api_key, batch_data):
    api_url = f"https://generativelanguage.googleapis.com/v1beta/models/gemini-flash-latest:generateContent?key={api_key}"

    # TODO: This prompt is kind of dumb, but it works
    prompt_template = 'You are an expert Chinese language tutor. Analyze the following sentence and provide its English meaning, pinyin transcription and underline each occurence of the word using <u> and </u>. The sentence is: "{source_text}"\nThe word is "{source_word}"'
    system_prompt = (
        f"You are a helpful assistant. Process the following list of items.\n"
        f"For each item, apply this logic: {prompt_template}\n\n"
        f"Input Data (JSON): {json.dumps(batch_data, ensure_ascii=False)}"
    )
    payload = {
        "contents": [{"role": "user", "parts": [{"text": system_prompt}]}],
        "generationConfig": {
            "responseMimeType": "application/json",
            "responseSchema": {
                "type": "ARRAY",
                "items": {
                    "type": "OBJECT",
                    "properties": {
                        "id": {"type": "INTEGER"},
                        "sentence_meaning": {"type": "STRING"},
                        "sentence_pinyin": {"type": "STRING"},
                        "formatted_sentence": {"type": "STRING"},
                    },
                    "required": [
                        "id",
                        "sentence_meaning",
                        "sentence_pinyin",
                        "formatted_sentence",
                    ],
                },
            },
        },
    }
    try:
        response = requests.post(api_url, json=payload, timeout=60)
        response.raise_for_status()

        result_text = response.json()["candidates"][0]["content"]["parts"][0]["text"]
        return json.loads(result_text)
    except Exception as e:
        print(f"Gemini API Error: {e}")
        return []


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

postprocessor = Postprocessor()


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
    definitions: str


@app.post("/api/new-card")
async def create_new_anki_card(data: NewCardRequest):
    note = {
        "deckName": "Mandarin Sentence Mining",
        "modelName": "Mandarin Sentence Mining",
        "fields": {
            "Simplified": data.word,
            "Pinyin.1": data.pinyin,
            "SentenceSimplified": data.sentence,
            "Meaning": data.definitions,
        },
        "tags": ["mogao", "needs-processing"],
    }
    call_anki("addNote", note=note)
    asyncio.create_task(postprocessor.check_and_process())
    call_anki("sync")
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
    deck_words = get_all_words_from_anki_deck("Mandarin Sentence Mining", "Simplified")

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
            "deck_words": list(deck_words),
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
    uvicorn.run(app, host="0.0.0.0", port=8123)
