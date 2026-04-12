import asyncio
from contextlib import asynccontextmanager
import json
import os
import pickle
import shutil
import uuid
from functools import lru_cache
from typing import Optional

from dotenv import load_dotenv

load_dotenv()
from fastapi import FastAPI, File, HTTPException, Request, UploadFile
from fastapi.middleware.gzip import GZipMiddleware
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from pydantic import BaseModel

from anki import Postprocessor, call_anki, get_all_words_from_anki_deck
from book import Book, ChapterContent, Metadata, TOCEntry, generate_book
from constants import LIBRARY_PATH
from library import (
    get_progress_path,
    load_book_cached,
    load_progress,
    load_settings,
    save_progress,
    save_settings,
)
from middleware import AuthMiddleware


@asynccontextmanager
async def lifespan(app: FastAPI):
    asyncio.create_task(postprocessor.check_and_process())
    yield


app = FastAPI(lifespan=lifespan)
app.add_middleware(GZipMiddleware, minimum_size=1000)
app.add_middleware(AuthMiddleware)
app.mount("/static", StaticFiles(directory="static"), name="static")
templates = Jinja2Templates(directory="templates")

postprocessor = Postprocessor()


class ProgressRequest(BaseModel):
    book_id: str
    chapter_index: int
    scroll_percentage: float


@app.post("/api/save-progress")
async def save_progress_api(data: ProgressRequest):
    save_progress(data.book_id, data.chapter_index, data.scroll_percentage)
    return {"status": "ok"}


class SaveSortRequest(BaseModel):
    sort_order: str


@app.post("/api/save-sort")
async def save_sort_api(data: SaveSortRequest):
    current_settings = load_settings()
    current_settings["sort_order"] = data.sort_order
    save_settings(current_settings)
    return {"status": "ok"}


@app.get("/api/get-settings", response_class=JSONResponse)
async def get_settings_api():
    current_settings = load_settings()
    return JSONResponse(current_settings)


class NewCardRequest(BaseModel):
    book_id: str
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
        "tags": ["mogao", "needs-processing", "needs-audio", f"mogao-{data.book_id}"],
    }
    call_anki("addNote", note=note)
    asyncio.create_task(postprocessor.check_and_process())
    call_anki("sync")
    return {"status": "ok"}


@app.get("/", response_class=HTMLResponse)
async def library_view(request: Request):
    """Lists all available processed books."""
    books = []

    if os.path.exists(LIBRARY_PATH):
        for item in os.listdir(LIBRARY_PATH):
            if os.path.isdir(os.path.join(LIBRARY_PATH, item)):
                book = load_book_cached(item)
                if not book:
                    continue

                tagged_card_ids = call_anki(
                    "findCards", query=f"tag:mogao-{item}"
                ).json()["result"]

                progress_path = get_progress_path(item)
                last_read_time = (
                    os.path.getmtime(progress_path)
                    if os.path.exists(progress_path)
                    else 0
                )

                books.append(
                    {
                        "id": item,
                        "title": book.metadata.title,
                        "author": ", ".join(book.metadata.authors),
                        "chapters": len(book.spine),
                        "character_count": getattr(book, "character_count", 0),
                        "tagged_cards_count": len(tagged_card_ids),
                        "cover_url": f"/read/{item}/images/{book.cover_image}",
                        "processed_at": book.processed_at,
                        "last_read_time": last_read_time,
                    }
                )

    # Pre-sort before sending to client to avoid a "flicker" where the books are loaded and then quickly sorted and re-ordered
    # TODO: Can it be done cleaner? I don't like that this is being done twice in two different places and languages
    settings = load_settings()
    current_sort = settings.get("sort_order", "title")
    if current_sort == "title":
        books.sort(key=lambda x: x["title"].lower())
    elif current_sort == "title_rev":
        books.sort(key=lambda x: x["title"].lower(), reverse=True)
    elif current_sort == "last_read":
        books.sort(key=lambda x: x["last_read_time"], reverse=True)
    elif current_sort == "last_read_rev":
        books.sort(key=lambda x: x["last_read_time"])
    elif current_sort == "date_added":
        books.sort(key=lambda x: x["processed_at"], reverse=True)
    elif current_sort == "date_added_rev":
        books.sort(key=lambda x: x["processed_at"])
    elif current_sort == "chars":
        books.sort(key=lambda x: x["character_count"], reverse=True)
    elif current_sort == "chars_rev":
        books.sort(key=lambda x: x["character_count"])
    elif current_sort == "mined":
        books.sort(key=lambda x: x["tagged_cards_count"], reverse=True)
    elif current_sort == "mined_rev":
        books.sort(key=lambda x: x["tagged_cards_count"])
    return templates.TemplateResponse(
        "library.html",
        {"request": request, "books": books},
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

    uvicorn.run(app, host="0.0.0.0", port=8123)
