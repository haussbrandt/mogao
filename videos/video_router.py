import asyncio
import glob
import json
import logging
import os
import pickle
import re
import shutil
import subprocess
import sys
import tempfile
import time
import uuid
from pathlib import Path
from uuid import UUID

from fastapi import APIRouter, BackgroundTasks, File, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, RedirectResponse
from pydantic import BaseModel

from core.config import settings
from core.dependencies import postprocessor, templates
from core.paths import get_video_path, get_video_progress_path, unique_temp_path
from integrations.anki import call_anki_async, get_anki_word_sets
from integrations.llm_processor import load_video_dict, process_subtitles_background
from videos.video import Video, cut_audio, generate_video, run_media_command, take_screenshot
from videos.video_library import (
    load_video_cached,
    load_video_progress,
    load_video_settings,
    save_video_progress,
    save_video_settings,
)

logger = logging.getLogger(__name__)

VIDEO_BASE_PATH = "/video"

router = APIRouter(prefix=VIDEO_BASE_PATH)

ALLOWED_VIDEO_EXTENSIONS = (".mp4", ".mkv", ".mov", ".avi", ".webm")
ALLOWED_SUBTITLE_EXTENSIONS = (".srt",)
CHUNK_UPLOAD_PATH = "video_uploads"
CHUNK_SIZE = 50 * 1024 * 1024
PROCESSING_JOB_RETENTION_SECONDS = 60 * 60
DOWNLOAD_TIMEOUT_SECONDS = 60 * 60
processing_jobs: dict[UUID, dict] = {}


def format_video_duration(duration: float) -> str:
    total_seconds = max(0, round(duration))
    hours, remainder = divmod(total_seconds, 60 * 60)
    minutes, seconds = divmod(remainder, 60)
    if hours:
        return f"{hours}:{minutes:02}:{seconds:02}"
    return f"{minutes}:{seconds:02}"


def validate_video_filename(filename: str) -> tuple[str, str]:
    safe_filename = os.path.basename(filename)
    extension = os.path.splitext(safe_filename)[1].lower()
    if not safe_filename or extension not in ALLOWED_VIDEO_EXTENSIONS:
        raise HTTPException(
            status_code=400,
            detail=f"Only video files are allowed: {ALLOWED_VIDEO_EXTENSIONS}",
        )
    return safe_filename, extension


def chunk_upload_dir(upload_id: UUID) -> str:
    return os.path.join(CHUNK_UPLOAD_PATH, str(upload_id))


def load_chunk_upload(upload_id: UUID) -> tuple[str, dict]:
    upload_dir = chunk_upload_dir(upload_id)
    metadata_path = os.path.join(upload_dir, "metadata.json")
    try:
        with open(metadata_path) as metadata_file:
            return upload_dir, json.load(metadata_file)
    except (FileNotFoundError, json.JSONDecodeError):
        raise HTTPException(status_code=404, detail="Upload not found")


def prune_processing_jobs():
    cutoff = time.time() - PROCESSING_JOB_RETENTION_SECONDS
    expired = [
        upload_id
        for upload_id, job in processing_jobs.items()
        if job["status"] in ("complete", "failed")
        and job.get("finished_at", 0) < cutoff
    ]
    for upload_id in expired:
        processing_jobs.pop(upload_id, None)


async def cleanup_processing_jobs():
    while True:
        await asyncio.sleep(60)
        prune_processing_jobs()


def write_processing_status(upload_id: UUID, status: str, **details):
    prune_processing_jobs()
    job = {"status": status, **details}
    if status in ("complete", "failed"):
        job["finished_at"] = time.time()
    processing_jobs[upload_id] = job


def process_chunked_video(path: str, original_filename: str, upload_id: UUID):
    try:
        _, output_dir = generate_video(path, original_filename)
        load_video_cached.cache_clear()
        write_processing_status(
            upload_id,
            "complete",
            title=original_filename,
            video_id=output_dir.name,
        )
    except Exception as error:
        logger.exception("Video processing failed for %s", original_filename)
        try:
            os.remove(path)
        except FileNotFoundError:
            pass
        except OSError:
            logger.exception(
                "Failed to remove temporary upload for %s", original_filename
            )
        write_processing_status(
            upload_id, "failed", title=original_filename, detail=str(error)
        )


class NewCardFromVideoRequest(BaseModel):
    book_id: UUID  # FIXME: leftover from copying
    word: str
    pinyin: str
    sentence: str
    definitions: str
    start: float
    end: float


@router.post("/api/new-card-from-video")
async def create_new_anki_card_from_video(data: NewCardFromVideoRequest):
    if not settings.anki.enabled:
        raise HTTPException(status_code=503, detail="Anki integration is disabled")

    video_id_string = str(data.book_id)
    audio_path = cut_audio(video_id_string, data.start, data.end)
    screenshot_path = take_screenshot(video_id_string, data.start)
    audio_path_string = str(audio_path)
    screenshot_path_string = str(screenshot_path)
    tags = [settings.anki.tags.app, f"{settings.anki.tags.app}-{video_id_string}"]
    if settings.postprocessing.text.enabled:
        tags.append(settings.anki.tags.needs_processing)

    note = {
        "deckName": settings.anki.deck,
        "modelName": settings.anki.model,
        "fields": {
            settings.anki.fields.word: data.word,
            settings.anki.fields.pinyin: data.pinyin,
            settings.anki.fields.sentence: data.sentence,
            settings.anki.fields.meaning: data.definitions,
        },
        "audio": [
            {
                "path": audio_path_string,
                "filename": audio_path_string,
                "fields": [settings.anki.fields.sentence_audio],
            },
        ],
        "picture": [
            {
                "path": screenshot_path_string,
                "filename": screenshot_path_string,
                "fields": [settings.anki.fields.sentence_image],
            }
        ],
        "tags": tags,
    }
    try:
        await call_anki_async("addNote", note=note)
        if settings.postprocessing.text.enabled:
            asyncio.create_task(postprocessor.check_and_process())
        try:
            await call_anki_async("sync")
        except RuntimeError:
            logger.exception("Anki note was created, but synchronization failed")
    finally:
        for path in (audio_path, screenshot_path):
            try:
                os.remove(path)
            except OSError:
                logger.exception("Failed to remove temporary video media %s", path)
    return {"status": "ok"}


def process_uploaded_video(path, original_filename):
    try:
        generate_video(path, original_filename)
    finally:
        if os.path.exists(path):
            os.remove(path)


@router.post("/upload")
async def upload_video(
    background_tasks: BackgroundTasks,
    files: list[UploadFile] = File(...),
):
    """
    Handles video upload.
    Saves to temp, processes with generate_video, clears temp, refreshes library.
    """
    for file in files:
        original_filename, extension = validate_video_filename(file.filename or "")
        temp_filename = unique_temp_path(extension)
        try:
            with open(temp_filename, "wb") as buffer:
                shutil.copyfileobj(file.file, buffer)
        except Exception:
            logger.exception(f"Error saving uploaded video {original_filename}")
            raise HTTPException(status_code=500, detail="Failed to process video")
        background_tasks.add_task(
            process_uploaded_video, temp_filename, original_filename
        )

    load_video_cached.cache_clear()
    return RedirectResponse(url=f"{VIDEO_BASE_PATH}/", status_code=303)


class StartChunkUploadRequest(BaseModel):
    filename: str
    size: int


@router.post("/upload-chunks/start")
async def start_chunk_upload(data: StartChunkUploadRequest):
    filename, extension = validate_video_filename(data.filename)
    if data.size <= 0:
        raise HTTPException(status_code=400, detail="Video file is empty")

    upload_id = uuid.uuid4()
    upload_dir = chunk_upload_dir(upload_id)
    os.makedirs(upload_dir, exist_ok=False)
    metadata = {
        "filename": filename,
        "extension": extension,
        "size": data.size,
        "chunk_size": CHUNK_SIZE,
        "total_chunks": (data.size + CHUNK_SIZE - 1) // CHUNK_SIZE,
    }
    with open(os.path.join(upload_dir, "metadata.json"), "w") as metadata_file:
        json.dump(metadata, metadata_file)
    with open(os.path.join(upload_dir, "assembled"), "wb"):
        pass

    return {
        "upload_id": str(upload_id),
        "chunk_size": CHUNK_SIZE,
        "total_chunks": metadata["total_chunks"],
    }


@router.put("/upload-chunks/{upload_id}/{chunk_index}")
async def upload_video_chunk(upload_id: UUID, chunk_index: int, request: Request):
    upload_dir, metadata = load_chunk_upload(upload_id)
    total_chunks = metadata["total_chunks"]
    if chunk_index < 0 or chunk_index >= total_chunks:
        raise HTTPException(status_code=400, detail="Invalid chunk index")

    expected_size = min(
        metadata["chunk_size"],
        metadata["size"] - chunk_index * metadata["chunk_size"],
    )
    expected_offset = chunk_index * metadata["chunk_size"]
    assembled_path = os.path.join(upload_dir, "assembled")
    assembled_size = os.path.getsize(assembled_path)
    if assembled_size == expected_offset + expected_size:
        return {"status": "already_uploaded"}
    if assembled_size != expected_offset:
        raise HTTPException(status_code=409, detail="Chunks must be uploaded in order")

    partial_path = os.path.join(upload_dir, "chunk.partial")
    bytes_written = 0
    try:
        with open(partial_path, "wb") as chunk_file:
            async for data in request.stream():
                bytes_written += len(data)
                if bytes_written > expected_size:
                    raise HTTPException(status_code=400, detail="Chunk is too large")
                chunk_file.write(data)

        if bytes_written != expected_size:
            raise HTTPException(
                status_code=400,
                detail=f"Invalid chunk size: expected {expected_size}, got {bytes_written}",
            )
        with open(assembled_path, "ab") as assembled_file:
            with open(partial_path, "rb") as chunk_file:
                shutil.copyfileobj(chunk_file, assembled_file)
    finally:
        if os.path.exists(partial_path):
            os.remove(partial_path)

    return {"status": "uploaded"}


@router.post("/upload-chunks/{upload_id}/complete")
async def complete_chunk_upload(upload_id: UUID, background_tasks: BackgroundTasks):
    upload_dir, metadata = load_chunk_upload(upload_id)
    assembled_path = os.path.join(upload_dir, "assembled")

    try:
        if os.path.getsize(assembled_path) != metadata["size"]:
            raise HTTPException(status_code=409, detail="Upload is incomplete")

        temp_filename = unique_temp_path(metadata["extension"])
        shutil.move(assembled_path, temp_filename)
    except HTTPException:
        if os.path.exists(assembled_path):
            os.remove(assembled_path)
        raise
    except Exception:
        if os.path.exists(assembled_path):
            os.remove(assembled_path)
        logger.exception(f"Error assembling chunked video upload {upload_id}")
        raise HTTPException(status_code=500, detail="Failed to assemble video upload")

    shutil.rmtree(upload_dir)
    write_processing_status(
        upload_id, "processing", title=metadata["filename"]
    )
    background_tasks.add_task(
        process_chunked_video, temp_filename, metadata["filename"], upload_id
    )
    return {"status": "processing", "processing_id": str(upload_id)}


@router.get("/upload-chunks/{upload_id}/status")
async def chunk_upload_status(upload_id: UUID):
    prune_processing_jobs()
    status = processing_jobs.get(upload_id)
    if status is None:
        raise HTTPException(status_code=404, detail="Processing job not found")
    if status["status"] in ("complete", "failed"):
        processing_jobs.pop(upload_id, None)
    return status


@router.delete("/upload-chunks/{upload_id}")
async def cancel_chunk_upload(upload_id: UUID):
    upload_dir = chunk_upload_dir(upload_id)
    if os.path.exists(upload_dir):
        shutil.rmtree(upload_dir)
    return {"status": "cancelled"}


@router.post("/upload-subtitles/{video_id}")
async def upload_subtitles(video_id: UUID, file: UploadFile = File(...)):
    """
    Handles subtitles upload and processing.
    """

    extension = os.path.splitext(file.filename or "")[1].lower()
    if extension not in ALLOWED_SUBTITLE_EXTENSIONS:
        raise HTTPException(
            status_code=400,
            detail=f"Only subtitle files are allowed: {ALLOWED_SUBTITLE_EXTENSIONS}",
        )
    temp_filename = unique_temp_path(extension)
    video_id_string = str(video_id)
    output_dir = get_video_path(video_id)
    subtitles_path = get_video_path(video_id, "subtitles.srt")
    metadata_path = get_video_path(video_id, "video.pkl")
    if not os.path.exists(output_dir):
        logger.warning(
            f"Cannot upload subtitles: video {video_id_string} was not found"
        )
        raise HTTPException(status_code=404, detail="Video not found")
    try:
        with open(temp_filename, "wb") as buffer:
            shutil.copyfileobj(file.file, buffer)

        shutil.move(temp_filename, subtitles_path)
    except Exception:
        logger.exception(f"Error processing subtitles for video {video_id_string}")
        raise HTTPException(status_code=500, detail="Failed to process subtitles")
    finally:
        if os.path.exists(temp_filename):
            os.remove(temp_filename)
    with open(subtitles_path) as f:
        subtitles = f.read()
        character_count = sum(1 for c in subtitles if "\u4e00" <= c <= "\u9fff")

    with open(metadata_path, "rb") as v:
        video_pickle: Video = pickle.load(v)
    video_pickle.character_count = character_count
    with open(metadata_path, "wb") as v:
        pickle.dump(video_pickle, v)
    load_video_cached.cache_clear()

    if settings.dictionary_generation.enabled:
        asyncio.create_task(process_subtitles_background(video_id_string))

    return RedirectResponse(url=f"{VIDEO_BASE_PATH}/", status_code=303)


# TODO: refactor, move to correct file etc.
def pick_best_subtitle(temp_filename: str | os.PathLike[str]) -> str | None:
    base = os.path.splitext(temp_filename)[0]
    candidates = glob.glob(f"{base}.*.srt")

    def chinese_char_ratio(path: str) -> float:
        with open(path, encoding="utf-8") as f:
            text = f.read()
        text = re.sub(r"^\d+\s*$", "", text, flags=re.MULTILINE)
        text = re.sub(r"\d{2}:\d{2}:\d{2},\d+ --> \d{2}:\d{2}:\d{2},\d+", "", text)
        text = re.sub(r"<[^>]+>", "", text)
        chars = [c for c in text if not c.isspace()]
        if not chars:
            return 0
        chinese = [c for c in chars if "\u4e00" <= c <= "\u9fff"]
        return len(chinese) / len(chars)

    scored = [(path, chinese_char_ratio(path)) for path in candidates]
    scored.sort(key=lambda x: x[1], reverse=True)

    for path, _ in scored[1:]:
        os.remove(path)

    if scored and scored[0][1] > 0.5:
        return scored[0][0]

    if scored:
        os.remove(scored[0][0])
    return None


async def download_and_process(url: str):
    try:
        video_id, has_subtitles = await asyncio.to_thread(process_video_download, url)
    except subprocess.TimeoutExpired as error:
        logger.error(
            "Video download or processing timed out after %s seconds", error.timeout
        )
        return
    except Exception as error:
        # The HTTP response has already been sent; failures must stay in this job.
        logger.exception(
            "Video download or processing failed: %s",
            getattr(error, "stderr", None) or error,
        )
        return

    if has_subtitles and settings.dictionary_generation.enabled:
        asyncio.create_task(process_subtitles_background(video_id))


def process_video_download(url: str) -> tuple[str, bool]:
    # Keep all downloader sidecars and partial files together for cleanup,
    # including when extraction, conversion, or subtitle processing fails.
    with tempfile.TemporaryDirectory(
        prefix="download-", dir=unique_temp_path("").parent
    ) as directory:
        return download_into_directory(url, Path(directory) / "video.mp4")


def download_into_directory(url: str, temp_filename: Path) -> tuple[str, bool]:
    result = run_media_command(
        [
            sys.executable,
            "-m",
            "yt_dlp",
            "--socket-timeout",
            "30",
            "--retries",
            "3",
            "--fragment-retries",
            "3",
            "--extractor-retries",
            "3",
            "-f",
            "bestvideo[height<=1080][vcodec^=avc][ext=mp4]+bestaudio[acodec=aac]/bestvideo[vcodec^=hev][ext=mp4]+bestaudio[acodec=aac]/bestvideo+bestaudio",
            "--merge-output-format",
            "mp4",
            "--write-subs",
            "--sub-langs",
            "zh.*",
            "--sub-format",
            "srt",
            "--print",
            "title",
            "--no-simulate",
            "-o",
            temp_filename,
            url,
        ],
        timeout=DOWNLOAD_TIMEOUT_SECONDS,
    )
    titles = result.stdout.strip().splitlines()
    if not titles or not temp_filename.is_file():
        raise ValueError("The site did not provide a downloadable video")
    original_title = titles[0] + ".mp4"
    best_subtitle_path = pick_best_subtitle(temp_filename)
    _, output_dir = generate_video(temp_filename, original_title)
    if best_subtitle_path is not None:
        video_id = output_dir.name
        subtitles_path = get_video_path(video_id, "subtitles.srt")
        metadata_path = get_video_path(video_id, "video.pkl")
        shutil.move(best_subtitle_path, subtitles_path)

        with open(subtitles_path) as f:
            subtitles = f.read()
            character_count = sum(1 for c in subtitles if "\u4e00" <= c <= "\u9fff")

        with open(metadata_path, "rb") as v:
            video_pickle: Video = pickle.load(v)
        video_pickle.character_count = character_count
        with open(metadata_path, "wb") as v:
            pickle.dump(video_pickle, v)
    load_video_cached.cache_clear()
    return output_dir.name, best_subtitle_path is not None


class DownloadRequest(BaseModel):
    url: str


@router.post("/download-video")
async def download_video(request: DownloadRequest, background_tasks: BackgroundTasks):
    if not request.url:
        raise HTTPException(status_code=400, detail="No URL provided")

    background_tasks.add_task(download_and_process, request.url)
    return {"status": "queued"}


@router.get("/{video_id}/cover.jpg")
async def serve_thumbnail(video_id: UUID):
    """
    Serves the video thumbnail.
    """
    img_path = get_video_path(video_id, "cover.jpg")

    if not os.path.exists(img_path):
        raise HTTPException(status_code=404, detail="Image not found")

    return FileResponse(img_path)


@router.post("/delete/{video_id}")
async def delete_video(video_id: UUID):
    """
    Deletes a video folder and refreshes the cache.
    """
    video_id_string = str(video_id)
    video_path = get_video_path(video_id)

    if os.path.exists(video_path):
        try:
            shutil.rmtree(video_path)
            load_video_cached.cache_clear()
        except Exception:
            logger.exception(f"Error deleting video {video_id_string}")
            raise HTTPException(status_code=500, detail="Failed to delete video")
    else:
        raise HTTPException(status_code=404, detail="video not found")

    return RedirectResponse(url=f"{VIDEO_BASE_PATH}/", status_code=303)


class VideoProgressRequest(BaseModel):
    video_id: UUID
    seconds_since_start: float


@router.post("/api/save-progress")
async def save_video_progress_api(data: VideoProgressRequest):
    save_video_progress(str(data.video_id), data.seconds_since_start)
    return {"status": "ok"}


@router.get("/watch/{video_id}", response_class=HTMLResponse)
async def watch_video(request: Request, video_id: UUID):
    """The main video player interface."""
    video_id_string = str(video_id)
    video = load_video_cached(video_id_string)
    if not video:
        raise HTTPException(status_code=404, detail="video not found")

    # TODO: refactor
    subtitles_path = get_video_path(video_id, "subtitles.srt")
    has_subtitles = os.path.exists(subtitles_path)

    progress = load_video_progress(video_id_string)
    seconds_since_start = progress.get("seconds_since_start", 0)
    save_video_progress(video_id_string, seconds_since_start)

    deck_words = set()
    known_words = set()
    if settings.anki.enabled:
        deck_words, known_words = get_anki_word_sets()

    return templates.TemplateResponse(
        request,
        "video_player.html",
        {
            "request": request,
            "video": video,
            "video_id": video_id_string,
            "initial_playback_time": seconds_since_start,
            "has_subtitles": has_subtitles,
            "deck_words": list(deck_words),
            "known_words": list(known_words),
            "anki_enabled": settings.anki.enabled,
            "video_base_path": VIDEO_BASE_PATH,
        },
    )


@router.get("/stream/{video_id}")
def stream_video(video_id: UUID):
    video_path = get_video_path(video_id, "video.mp4")

    if os.path.exists(video_path):
        return FileResponse(video_path, media_type="video/mp4")
    else:
        raise HTTPException(status_code=404, detail="video not found")


@router.get("/{video_id}/subtitles")
def get_subtitles(video_id: UUID):
    subtitle_path = get_video_path(video_id, "subtitles.srt")

    if os.path.exists(subtitle_path):
        return FileResponse(subtitle_path, media_type="text/plain")
    else:
        raise HTTPException(status_code=404, detail="subtitles not found")


@router.get("/api/video-dict/{video_id}", response_class=JSONResponse)
async def get_video_dict_api(video_id: UUID):
    data = load_video_dict(str(video_id))
    return JSONResponse(data)


class SaveVideoSortRequest(BaseModel):
    sort_order: str


@router.post("/api/save-sort")
async def save_video_sort_api(data: SaveVideoSortRequest):
    current_settings = load_video_settings()
    current_settings["sort_order"] = data.sort_order
    save_video_settings(current_settings)
    return {"status": "ok"}


@router.get("/", response_class=HTMLResponse)
async def video_library_view(request: Request):
    """Lists all available videos."""
    videos = []

    if os.path.exists(settings.paths.video_library):
        for item in os.listdir(settings.paths.video_library):
            try:
                video_path = get_video_path(item)
            except ValueError:
                continue
            if os.path.isdir(video_path):
                video = load_video_cached(item)
                if not video:
                    continue

                tagged_card_ids = []
                if settings.anki.enabled:
                    tagged_card_ids = await call_anki_async(
                        "findCards", query=f"tag:{settings.anki.tags.app}-{item}"
                    )

                progress_path = get_video_progress_path(item)
                last_watch_time = (
                    os.path.getmtime(progress_path)
                    if os.path.exists(progress_path)
                    else 0
                )
                duration = float(video.metadata.duration)

                videos.append(
                    {
                        "id": item,
                        "title": video.metadata.title,
                        "duration": duration,
                        "duration_display": format_video_duration(duration),
                        "character_count": getattr(video, "character_count", 0),
                        "tagged_cards_count": len(tagged_card_ids),
                        "cover_url": (
                            f"{VIDEO_BASE_PATH}/{item}/{video.cover_image}"
                        ),
                        "processed_at": video.processed_at,
                        "last_watch_time": last_watch_time,
                    }
                )

    preferences = load_video_settings()
    current_sort = preferences.get("sort_order", "title")
    return templates.TemplateResponse(
        request,
        "video_library.html",
        {
            "request": request,
            "videos": videos,
            "video_base_path": VIDEO_BASE_PATH,
            "current_sort": current_sort,
        },
    )
