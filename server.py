import asyncio
import logging
import os
import shutil
from contextlib import asynccontextmanager
from uuid import UUID

from dotenv import load_dotenv

from logging_config import configure_logging

load_dotenv()
configure_logging()

from fastapi import FastAPI, File, HTTPException, Request, UploadFile
from fastapi.middleware.gzip import GZipMiddleware
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

import video_router
from anki import (
    call_anki,
    ensure_anki_available,
    get_all_words_from_anki_deck,
    validate_anki_configuration,
)
from book import generate_book
from config import settings
from constants import normalize_uuid
from dependencies import postprocessor, templates
from library import (
    get_progress_path,
    load_book_cached,
    load_progress,
    load_settings,
    save_progress,
    save_settings,
)
from llm_processor import (
    load_book_dict,
    process_book_background,
    resume_interrupted_processing,
)
from middleware import AuthMiddleware
from temp_files import new_temp_path

logger = logging.getLogger(__name__)


def validate_runtime_requirements() -> None:
    missing_keys = []
    if (
        settings.postprocessing.text.enabled
        and not os.environ.get("POSTPROCESSING_API_KEY", "").strip()
    ):
        missing_keys.append("POSTPROCESSING_API_KEY for postprocessing.text")
    if (
        settings.dictionary_generation.enabled
        and not os.environ.get("DICTIONARY_GENERATION_API_KEY", "").strip()
    ):
        missing_keys.append("DICTIONARY_GENERATION_API_KEY for dictionary_generation")
    if (
        settings.postprocessing.audio.enabled
        and not os.environ.get("ELEVENLABS_API_KEY", "").strip()
    ):
        missing_keys.append("ELEVENLABS_API_KEY for postprocessing.audio")

    if missing_keys:
        raise RuntimeError(
            "Missing required API configuration: " + ", ".join(missing_keys)
        )

    if settings.anki.enabled:
        ensure_anki_available()
        validate_anki_configuration()


@asynccontextmanager
async def lifespan(app: FastAPI):
    validate_runtime_requirements()
    if settings.postprocessing.audio.enabled:
        postprocessor.initialize_clients()
    if settings.postprocessing.text.enabled or settings.postprocessing.audio.enabled:
        asyncio.create_task(postprocessor.check_and_process())
    if settings.dictionary_generation.enabled:
        asyncio.create_task(resume_interrupted_processing())
    asyncio.create_task(video_router.cleanup_processing_jobs())
    yield


app = FastAPI(lifespan=lifespan)
app.add_middleware(GZipMiddleware, minimum_size=1000)
app.add_middleware(AuthMiddleware)
app.mount("/static", StaticFiles(directory="static"), name="static")
app.mount(
    video_router.router.prefix + "/static",
    StaticFiles(directory="static"),
    name="static",
)
app.include_router(video_router.router)


class ProgressRequest(BaseModel):
    book_id: UUID
    chapter_index: int
    scroll_percentage: float


@app.post("/api/save-progress")
async def save_progress_api(data: ProgressRequest):
    save_progress(str(data.book_id), data.chapter_index, data.scroll_percentage)
    return {"status": "ok"}


class SaveSortRequest(BaseModel):
    sort_order: str


@app.post("/api/save-sort")
async def save_sort_api(data: SaveSortRequest):
    current_settings = load_settings()
    current_settings["sort_order"] = data.sort_order
    save_settings(current_settings)
    return {"status": "ok"}


@app.get("/api/book-dict/{book_id}", response_class=JSONResponse)
async def get_book_dict_api(book_id: UUID):
    data = load_book_dict(str(book_id))
    return JSONResponse(data)


class NewCardRequest(BaseModel):
    book_id: UUID
    word: str
    pinyin: str
    sentence: str
    definitions: str


@app.post("/api/new-card")
async def create_new_anki_card(data: NewCardRequest):
    if not settings.anki.enabled:
        raise HTTPException(status_code=503, detail="Anki integration is disabled")

    tags = [settings.anki.tags.app, f"{settings.anki.tags.app}-{data.book_id}"]
    if settings.postprocessing.text.enabled:
        tags.append(settings.anki.tags.needs_processing)
    if settings.postprocessing.audio.enabled:
        tags.append(settings.anki.tags.needs_audio)

    note = {
        "deckName": settings.anki.deck,
        "modelName": settings.anki.model,
        "fields": {
            settings.anki.fields.word: data.word,
            settings.anki.fields.pinyin: data.pinyin,
            settings.anki.fields.sentence: data.sentence,
            settings.anki.fields.meaning: data.definitions,
        },
        "tags": tags,
    }
    call_anki("addNote", note=note)
    if settings.postprocessing.text.enabled or settings.postprocessing.audio.enabled:
        asyncio.create_task(postprocessor.check_and_process())
    call_anki("sync")
    return {"status": "ok"}


@app.get("/", response_class=HTMLResponse)
async def library_view(request: Request):
    """Lists all available processed books."""
    books = []

    if os.path.exists(settings.paths.library):
        for item in os.listdir(settings.paths.library):
            if os.path.isdir(os.path.join(settings.paths.library, item)):
                book = load_book_cached(item)
                if not book:
                    continue

                tagged_card_ids = []
                if settings.anki.enabled:
                    tagged_card_ids = call_anki(
                        "findCards", query=f"tag:{settings.anki.tags.app}-{item}"
                    ).json()["result"]

                progress_path = get_progress_path(item)
                last_read_time = (
                    os.path.getmtime(progress_path)
                    if os.path.exists(progress_path)
                    else 0
                )

                cover_image = os.path.basename(
                    getattr(book, "cover_image", None) or ""
                )
                cover_path = os.path.join(
                    settings.paths.library, item, "images", cover_image
                )
                cover_url = (
                    f"/read/{item}/images/{cover_image}"
                    if cover_image and os.path.isfile(cover_path)
                    else None
                )

                books.append(
                    {
                        "id": item,
                        "title": book.metadata.title,
                        "author": ", ".join(book.metadata.authors),
                        "chapters": len(book.spine),
                        "character_count": getattr(book, "character_count", 0),
                        "tagged_cards_count": len(tagged_card_ids),
                        "cover_url": cover_url,
                        "cover_hue": int(UUID(item)) % 360,
                        "processed_at": book.processed_at,
                        "last_read_time": last_read_time,
                    }
                )

    preferences = load_settings()
    current_sort = preferences.get("sort_order", "title")
    return templates.TemplateResponse(
        request,
        "library.html",
        {
            "request": request,
            "books": books,
            "current_sort": current_sort,
            "video_base_path": video_router.VIDEO_BASE_PATH,
        },
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
        temp_filename = new_temp_path(".epub")
        try:
            with open(temp_filename, "wb") as buffer:
                shutil.copyfileobj(file.file, buffer)

            book = generate_book(temp_filename, settings.paths.library)
            load_book_cached.cache_clear()

            book_id = str(book.metadata.generate_key())
            if settings.dictionary_generation.enabled:
                asyncio.create_task(process_book_background(book_id, book))

        except Exception:
            logger.exception("Error processing book")
            raise HTTPException(status_code=500, detail="Failed to process book")
        finally:
            if os.path.exists(temp_filename):
                os.remove(temp_filename)

    return RedirectResponse(url="/", status_code=303)


@app.post("/delete/{book_id}")
async def delete_book(book_id: UUID):
    """
    Deletes a book folder and refreshes the cache.
    """
    safe_id = normalize_uuid(book_id)
    book_path = os.path.join(settings.paths.library, safe_id)

    if os.path.exists(book_path):
        try:
            shutil.rmtree(book_path)
            load_book_cached.cache_clear()
        except Exception:
            logger.exception(f"Error deleting book {safe_id}")
            raise HTTPException(status_code=500, detail="Failed to delete book")
    else:
        raise HTTPException(status_code=404, detail="Book not found")

    return RedirectResponse(url="/", status_code=303)


@app.get("/read/{book_id}", response_class=HTMLResponse)
async def redirect_to_last_read(book_id: UUID):
    """Helper to go to the last read chapter."""
    safe_id = normalize_uuid(book_id)
    progress = load_progress(safe_id)
    last_chapter_index = progress.get("chapter_index", 0)
    return RedirectResponse(url=f"/read/{safe_id}/{last_chapter_index}")


@app.get("/read/{book_id}/{chapter_index}", response_class=HTMLResponse)
async def read_chapter(request: Request, book_id: UUID, chapter_index: int):
    """The main reader interface."""
    safe_id = normalize_uuid(book_id)
    book = load_book_cached(safe_id)
    if not book:
        raise HTTPException(status_code=404, detail="Book not found")

    if chapter_index < 0 or chapter_index >= len(book.spine):
        raise HTTPException(status_code=404, detail="Chapter not found")

    progress = load_progress(safe_id)
    saved_chapter = progress.get("chapter_index", -1)
    initial_scroll_percentage = 0.0
    if saved_chapter == chapter_index:
        initial_scroll_percentage = progress.get("scroll_percentage", 0.0)

    save_progress(safe_id, chapter_index, initial_scroll_percentage)

    current_chapter = book.spine[chapter_index]
    prev_idx = chapter_index - 1 if chapter_index > 0 else None
    next_idx = chapter_index + 1 if chapter_index < len(book.spine) - 1 else None
    deck_words = set()
    if settings.anki.enabled:
        deck_words = get_all_words_from_anki_deck(
            settings.anki.deck, settings.anki.fields.word
        )

    return templates.TemplateResponse(
        request,
        "reader.html",
        {
            "request": request,
            "book": book,
            "current_chapter": current_chapter,
            "chapter_index": chapter_index,
            "book_id": safe_id,
            "prev_idx": prev_idx,
            "next_idx": next_idx,
            "initial_scroll_percentage": initial_scroll_percentage,
            "deck_words": list(deck_words),
            "anki_enabled": settings.anki.enabled,
        },
    )


@app.get("/read/{book_id}/images/{image_name}")
async def serve_image(book_id: UUID, image_name: str):
    """
    Serves images specifically for a book.
    The HTML contains <img src="images/pic.jpg">.
    The browser resolves this to /read/{book_id}/images/pic.jpg.
    """
    safe_book_id = normalize_uuid(book_id)
    safe_image_name = os.path.basename(image_name)

    img_path = os.path.join(
        settings.paths.library, safe_book_id, "images", safe_image_name
    )

    if not os.path.exists(img_path):
        raise HTTPException(status_code=404, detail="Image not found")

    return FileResponse(img_path)


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(app, host="0.0.0.0", port=8123, log_config=None)
