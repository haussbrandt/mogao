import asyncio
import logging
import os
import shutil
import subprocess
from contextlib import asynccontextmanager
from uuid import UUID

from dotenv import load_dotenv

from core.logging_config import configure_logging

load_dotenv()
configure_logging()

from fastapi import FastAPI, File, HTTPException, Request, UploadFile
from fastapi.middleware.gzip import GZipMiddleware
from fastapi.responses import (
    FileResponse,
    HTMLResponse,
    JSONResponse,
    RedirectResponse,
    Response,
)
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from books.book import generate_book
from books.library import (
    load_book_cached,
    load_progress,
    load_settings,
    save_progress,
    save_settings,
)
from core.config import settings
from core.dependencies import postprocessor, templates
from core.middleware import AuthMiddleware
from core.paths import get_book_path, get_book_progress_path, unique_temp_path
from core.status import build_status
from integrations.anki import (
    call_anki_async,
    ensure_anki_available,
    get_anki_word_sets,
    validate_anki_configuration,
)
from integrations.llm_processor import (
    load_book_dict,
    process_book_background,
    resume_interrupted_processing,
    schedule_book_dictionary_retry,
    schedule_video_dictionary_retry,
)
from videos import video_router
from videos.video import manage_media_processes

logger = logging.getLogger(__name__)

SYSTEM_EXECUTABLES = {
    "ffmpeg": ("-version",),
    "ffprobe": ("-version",),
}


def validate_system_dependencies() -> None:
    required_executables = set()
    if settings.video.enabled:
        required_executables.update(SYSTEM_EXECUTABLES)
    if settings.postprocessing.audio.enabled:
        required_executables.add("ffmpeg")

    errors = []
    for executable in SYSTEM_EXECUTABLES:
        if executable not in required_executables:
            continue

        version_args = SYSTEM_EXECUTABLES[executable]
        executable_path = shutil.which(executable)
        if executable_path is None:
            errors.append(f"{executable} was not found on PATH")
            continue

        try:
            subprocess.run(
                [executable_path, *version_args],
                check=True,
                capture_output=True,
                timeout=10,
            )
        except (OSError, subprocess.SubprocessError) as error:
            errors.append(f"{executable} could not be executed: {error}")

    if errors:
        formatted_errors = "\n".join(f"- {error}" for error in errors)
        raise RuntimeError(
            "Required system dependencies are unavailable:\n"
            f"{formatted_errors}\n"
            "Install the missing dependencies or disable the features that use "
            "them in config.toml."
        )


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

    validate_system_dependencies()

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
        asyncio.create_task(
            resume_interrupted_processing(include_videos=settings.video.enabled)
        )
    if settings.video.enabled:
        asyncio.create_task(video_router.cleanup_processing_jobs())
    # Uvicorn drains background jobs before lifespan teardown, so their detached
    # processes must also be stopped directly when a shutdown signal arrives.
    with manage_media_processes():
        yield


app = FastAPI(lifespan=lifespan)
app.add_middleware(GZipMiddleware, minimum_size=1000)
app.add_middleware(AuthMiddleware)
app.mount("/static", StaticFiles(directory="static"), name="static")
if settings.video.enabled:
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
    await call_anki_async("addNote", note=note)
    if settings.postprocessing.text.enabled or settings.postprocessing.audio.enabled:
        asyncio.create_task(postprocessor.check_and_process())
    try:
        await call_anki_async("sync")
    except RuntimeError:
        logger.exception("Anki note was created, but synchronization failed")
    return {"status": "ok"}


@app.get("/", response_class=HTMLResponse)
async def library_view(request: Request):
    """Lists all available processed books."""
    books = []

    if os.path.exists(settings.paths.library):
        for item in os.listdir(settings.paths.library):
            try:
                book_path = get_book_path(item)
            except ValueError:
                continue
            if os.path.isdir(book_path):
                book = load_book_cached(item)
                if not book:
                    continue

                tagged_card_ids = []
                if settings.anki.enabled:
                    tagged_card_ids = await call_anki_async(
                        "findCards", query=f"tag:{settings.anki.tags.app}-{item}"
                    )

                progress_path = get_book_progress_path(item)
                last_read_time = (
                    os.path.getmtime(progress_path)
                    if os.path.exists(progress_path)
                    else 0
                )

                cover_image = os.path.basename(
                    getattr(book, "cover_image", None) or ""
                )
                cover_path = get_book_path(item, "images", cover_image)
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
            "video_enabled": settings.video.enabled,
        },
    )


@app.get("/status", response_class=HTMLResponse)
async def status_view(request: Request):
    return templates.TemplateResponse(
        request,
        "status.html",
        {
            "request": request,
            "status": await asyncio.to_thread(
                build_status, postprocessor, video_router.snapshot_processing_jobs()
            ),
            "video_enabled": settings.video.enabled,
        },
    )


@app.get("/status/content", response_class=HTMLResponse)
async def status_content(request: Request):
    return templates.TemplateResponse(
        request,
        "status_content.html",
        {
            "request": request,
            "status": await asyncio.to_thread(
                build_status, postprocessor, video_router.snapshot_processing_jobs()
            ),
        },
    )


def _status_action_response(request: Request) -> Response:
    if request.headers.get("X-Requested-With") == "status-page":
        return Response(status_code=204)
    return RedirectResponse(url="/status", status_code=303)


async def _run_postprocessing_from_status() -> None:
    try:
        await postprocessor.check_and_process(force=True)
    except Exception:
        logger.exception("Postprocessing requested from the status page failed")


@app.post("/status/postprocessing/run")
async def run_postprocessing_now(request: Request):
    postprocessing_enabled = settings.anki.enabled and (
        settings.postprocessing.text.enabled
        or settings.postprocessing.audio.enabled
    )
    if not postprocessing_enabled:
        logger.warning(
            "Postprocessing request from the status page was rejected because "
            "postprocessing is disabled"
        )
        raise HTTPException(status_code=409, detail="Postprocessing is disabled")

    asyncio.create_task(_run_postprocessing_from_status())
    return _status_action_response(request)


@app.post("/status/dictionaries/books/{item_id}/retry")
async def retry_book_dictionary_job(request: Request, item_id: UUID):
    if not settings.dictionary_generation.enabled:
        raise HTTPException(status_code=409, detail="Dictionary generation is disabled")

    try:
        retry_scheduled = schedule_book_dictionary_retry(str(item_id))
    except FileNotFoundError:
        raise HTTPException(status_code=404, detail="Book not found")
    if not retry_scheduled:
        raise HTTPException(status_code=409, detail="Dictionary job is not retryable")
    return _status_action_response(request)


@app.post("/status/dictionaries/videos/{item_id}/retry")
async def retry_video_dictionary_job(request: Request, item_id: UUID):
    if not settings.dictionary_generation.enabled:
        raise HTTPException(status_code=409, detail="Dictionary generation is disabled")

    try:
        retry_scheduled = schedule_video_dictionary_retry(str(item_id))
    except FileNotFoundError:
        raise HTTPException(status_code=404, detail="Video or subtitles not found")
    if not retry_scheduled:
        raise HTTPException(status_code=409, detail="Dictionary job is not retryable")
    return _status_action_response(request)


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
        temp_filename = unique_temp_path(".epub")
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
    book_id_string = str(book_id)
    book_path = get_book_path(book_id)

    if os.path.exists(book_path):
        try:
            shutil.rmtree(book_path)
            load_book_cached.cache_clear()
        except Exception:
            logger.exception(f"Error deleting book {book_id_string}")
            raise HTTPException(status_code=500, detail="Failed to delete book")
    else:
        raise HTTPException(status_code=404, detail="Book not found")

    return RedirectResponse(url="/", status_code=303)


@app.get("/read/{book_id}", response_class=HTMLResponse)
async def redirect_to_last_read(book_id: UUID):
    """Helper to go to the last read chapter."""
    book_id_string = str(book_id)
    progress = load_progress(book_id_string)
    last_chapter_index = progress.get("chapter_index", 0)
    return RedirectResponse(url=f"/read/{book_id_string}/{last_chapter_index}")


@app.get("/read/{book_id}/{chapter_index}", response_class=HTMLResponse)
async def read_chapter(request: Request, book_id: UUID, chapter_index: int):
    """The main reader interface."""
    book_id_string = str(book_id)
    book = load_book_cached(book_id_string)
    if not book:
        raise HTTPException(status_code=404, detail="Book not found")

    if chapter_index < 0 or chapter_index >= len(book.spine):
        raise HTTPException(status_code=404, detail="Chapter not found")

    progress = load_progress(book_id_string)
    saved_chapter = progress.get("chapter_index", -1)
    initial_scroll_percentage = 0.0
    if saved_chapter == chapter_index:
        initial_scroll_percentage = progress.get("scroll_percentage", 0.0)

    current_chapter = book.spine[chapter_index]
    prev_idx = chapter_index - 1 if chapter_index > 0 else None
    next_idx = chapter_index + 1 if chapter_index < len(book.spine) - 1 else None
    deck_words = set()
    known_words = set()
    if settings.anki.enabled:
        deck_words, known_words = get_anki_word_sets()

    return templates.TemplateResponse(
        request,
        "reader.html",
        {
            "request": request,
            "book": book,
            "current_chapter": current_chapter,
            "chapter_index": chapter_index,
            "book_id": book_id_string,
            "prev_idx": prev_idx,
            "next_idx": next_idx,
            "initial_scroll_percentage": initial_scroll_percentage,
            "deck_words": list(deck_words),
            "known_words": list(known_words),
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
    safe_image_name = os.path.basename(image_name)
    img_path = get_book_path(book_id, "images", safe_image_name)

    if not os.path.exists(img_path):
        raise HTTPException(status_code=404, detail="Image not found")

    return FileResponse(img_path, headers={"Content-Security-Policy": "sandbox"})


if __name__ == "__main__":
    import argparse

    import uvicorn

    parser = argparse.ArgumentParser(description="Run the Mogao server")
    parser.add_argument(
        "--port", type=int, default=8123, help="Port to listen on (default: 8123)"
    )
    args = parser.parse_args()

    uvicorn.run(app, host="0.0.0.0", port=args.port, log_config=None)
