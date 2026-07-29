import asyncio
import glob
import json
import os
import pickle
import re
import shutil
import subprocess
import time
import uuid
from uuid import UUID

from fastapi import APIRouter, BackgroundTasks, File, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, RedirectResponse
from pydantic import BaseModel

from anki import call_anki, get_all_words_from_anki_deck
from config import settings
from constants import normalize_uuid
from dependencies import postprocessor, templates
from llm_processor import load_video_dict, process_subtitles_background
from video import Video, cut_audio, generate_video, take_screenshot
from video_library import (
    get_video_progress_path,
    load_video_cached,
    load_video_settings,
    save_video_settings,
)

router = APIRouter(prefix="/video")

ALLOWED_VIDEO_EXTENSIONS = (".mp4", ".mkv", ".mov", ".avi", ".webm")
CHUNK_UPLOAD_PATH = "video_uploads"
CHUNK_SIZE = 50 * 1024 * 1024
PROCESSING_JOB_RETENTION_SECONDS = 60 * 60
processing_jobs: dict[UUID, dict] = {}


def video_base_path() -> str:
    """Return the configured public URL prefix used by the video frontend."""
    base_path = os.environ.get("MOGAO_VIDEO_BASE_PATH", router.prefix).strip()
    if not base_path:
        return ""
    return f"/{base_path.strip('/')}"


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
            video_id=os.path.basename(output_dir),
        )
    except Exception as e:
        print(f"Error processing chunked video upload {upload_id}: {e}")
        if os.path.exists(path):
            os.remove(path)
        write_processing_status(upload_id, "failed", detail=str(e))


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
    video_id = normalize_uuid(data.book_id)
    audio_path = cut_audio(video_id, data.start, data.end)
    screenshot_path = take_screenshot(video_id, data.start)
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
                "path": audio_path,
                "filename": audio_path,
                "fields": [settings.anki.fields.sentence_audio],
            },
        ],
        "picture": [
            {
                "path": screenshot_path,
                "filename": screenshot_path,
                "fields": [settings.anki.fields.sentence_image],
            }
        ],
        "tags": ["mogao", "needs-processing", f"mogao-{video_id}"],
    }
    call_anki("addNote", note=note)
    asyncio.create_task(postprocessor.check_and_process())
    call_anki("sync")
    os.remove(audio_path)
    os.remove(screenshot_path)
    return {"status": "ok"}


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
        temp_filename = f"temp_{uuid.uuid4()}{extension}"
        try:
            with open(temp_filename, "wb") as buffer:
                shutil.copyfileobj(file.file, buffer)
        except Exception as e:
            print(f"Error processing video: {e}")
            raise HTTPException(status_code=500, detail="Failed to process video")
        background_tasks.add_task(generate_video, temp_filename, original_filename)

    load_video_cached.cache_clear()
    return RedirectResponse(url=f"{video_base_path()}/", status_code=303)


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

        temp_filename = f"temp_{uuid.uuid4()}{metadata['extension']}"
        shutil.move(assembled_path, temp_filename)
    except HTTPException:
        if os.path.exists(assembled_path):
            os.remove(assembled_path)
        raise
    except Exception as e:
        if os.path.exists(assembled_path):
            os.remove(assembled_path)
        print(f"Error assembling chunked video upload: {e}")
        raise HTTPException(status_code=500, detail="Failed to assemble video upload")

    shutil.rmtree(upload_dir)
    write_processing_status(upload_id, "processing")
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
    ALLOWED_VIDEO_EXTENSIONS = ".srt"

    extension = os.path.splitext(file.filename)[1].lower()
    if extension not in ALLOWED_VIDEO_EXTENSIONS:
        raise HTTPException(
            status_code=400,
            detail=f"Only subtitle files are allowed: {ALLOWED_VIDEO_EXTENSIONS}",
        )
    temp_filename = f"temp_{uuid.uuid4()}.{extension}"
    safe_id = normalize_uuid(video_id)
    output_dir = os.path.join(settings.paths.video_library, safe_id)
    if not os.path.exists(output_dir):
        print(f"Video not found")
        raise HTTPException(status_code=500, detail="Failed to process subtitles")
    try:
        with open(temp_filename, "wb") as buffer:
            shutil.copyfileobj(file.file, buffer)

        shutil.move(temp_filename, f"{output_dir}/subtitles.srt")
    except Exception as e:
        print(f"Error processing subtitles: {e}")
        raise HTTPException(status_code=500, detail="Failed to process subtitles")
    finally:
        if os.path.exists(temp_filename):
            os.remove(temp_filename)
    with open(f"{output_dir}/subtitles.srt") as f:
        subtitles = f.read()
        character_count = sum(1 for c in subtitles if "\u4e00" <= c <= "\u9fff")

    with open(f"{output_dir}/video.pkl", "rb") as v:
        video_pickle: Video = pickle.load(v)
    video_pickle.character_count = character_count
    with open(f"{output_dir}/video.pkl", "wb") as v:
        pickle.dump(video_pickle, v)
    load_video_cached.cache_clear()

    asyncio.create_task(process_subtitles_background(safe_id))

    return RedirectResponse(url=f"{video_base_path()}/", status_code=303)


# TODO: refactor, move to correct file etc.
def pick_best_subtitle(temp_filename: str) -> str | None:
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


# TODO: refactor
async def download_and_process(url: str):
    temp_filename = f"temp_{uuid.uuid4()}.mp4"
    try:
        result = subprocess.run(
            [
                "yt-dlp",
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
            capture_output=True,
            text=True,
            check=True,
        )
        original_title = result.stdout.strip().splitlines()[0] + ".mp4"
    except subprocess.CalledProcessError as e:
        print(f"[Downloader] Failed to download video: {e.stderr}")
        raise HTTPException(
            status_code=500, detail=f"Failed to download video: {e.stderr}"
        )
    best_subitle_path = pick_best_subtitle(temp_filename)
    _, output_dir = generate_video(temp_filename, original_title)
    if best_subitle_path is not None:
        shutil.move(best_subitle_path, f"{output_dir}/subtitles.srt")

        with open(f"{output_dir}/subtitles.srt") as f:
            subtitles = f.read()
            character_count = sum(1 for c in subtitles if "\u4e00" <= c <= "\u9fff")

        with open(f"{output_dir}/video.pkl", "rb") as v:
            video_pickle: Video = pickle.load(v)
        video_pickle.character_count = character_count
        with open(f"{output_dir}/video.pkl", "wb") as v:
            pickle.dump(video_pickle, v)
        load_video_cached.cache_clear()

        asyncio.create_task(process_subtitles_background(os.path.basename(output_dir)))


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
    safe_video_id = normalize_uuid(video_id)

    img_path = os.path.join(settings.paths.video_library, safe_video_id, "cover.jpg")

    if not os.path.exists(img_path):
        raise HTTPException(status_code=404, detail="Image not found")

    return FileResponse(img_path)


@router.post("/delete/{video_id}")
async def delete_video(video_id: UUID):
    """
    Deletes a video folder and refreshes the cache.
    """
    safe_id = normalize_uuid(video_id)
    video_path = os.path.join(settings.paths.video_library, safe_id)

    if os.path.exists(video_path):
        try:
            shutil.rmtree(video_path)
            load_video_cached.cache_clear()
        except Exception as e:
            print(f"Error deleting video {safe_id}: {e}")
            raise HTTPException(status_code=500, detail="Failed to delete video")
    else:
        raise HTTPException(status_code=404, detail="video not found")

    return RedirectResponse(url=f"{video_base_path()}/", status_code=303)


@router.get("/watch/{video_id}", response_class=HTMLResponse)
async def watch_video(request: Request, video_id: UUID):
    """The main video player interface."""
    safe_id = normalize_uuid(video_id)
    video = load_video_cached(safe_id)
    if not video:
        raise HTTPException(status_code=404, detail="video not found")

    # TODO: refactor
    subtitles_path = os.path.join(
        settings.paths.video_library, safe_id, "subtitles.srt"
    )
    has_subtitles = os.path.exists(subtitles_path)

    # progress = load_video_progress(video_id)

    deck_words = get_all_words_from_anki_deck(
        settings.anki.deck, settings.anki.fields.word
    )

    return templates.TemplateResponse(
        request,
        "video_player.html",
        {
            "request": request,
            "video": video,
            "video_id": safe_id,
            "has_subtitles": has_subtitles,
            "deck_words": list(deck_words),
            "video_base_path": video_base_path(),
        },
    )


@router.get("/stream/{video_id}")
def stream_video(video_id: UUID):
    safe_id = normalize_uuid(video_id)
    video_path = os.path.join(settings.paths.video_library, safe_id, "video.mp4")

    if os.path.exists(video_path):
        return FileResponse(video_path, media_type="video/mp4")
    else:
        raise HTTPException(status_code=404, detail="video not found")


@router.get("/{video_id}/subtitles")
def get_subtitles(video_id: UUID):
    safe_id = normalize_uuid(video_id)
    subtitle_path = os.path.join(settings.paths.video_library, safe_id, "subtitles.srt")

    if os.path.exists(subtitle_path):
        return FileResponse(subtitle_path, media_type="text/plain")
    else:
        raise HTTPException(status_code=404, detail="subtitles not found")


@router.get("/api/video-dict/{video_id}", response_class=JSONResponse)
async def get_video_dict_api(video_id: UUID):
    data = load_video_dict(str(video_id))
    return JSONResponse(data)


@router.get("/api/get-settings", response_class=JSONResponse)
async def get_settings_api():
    current_settings = load_video_settings()
    return JSONResponse(current_settings)


# TODO: This class seems unnecessary
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
            if os.path.isdir(os.path.join(settings.paths.video_library, item)):
                video = load_video_cached(item)
                if not video:
                    continue

                tagged_card_ids = call_anki(
                    "findCards", query=f"tag:mogao-{item}"
                ).json()["result"]

                progress_path = get_video_progress_path(item)
                last_watch_time = (
                    os.path.getmtime(progress_path)
                    if os.path.exists(progress_path)
                    else 0
                )

                videos.append(
                    {
                        "id": item,
                        "title": video.metadata.title,
                        "character_count": getattr(video, "character_count", 0),
                        "tagged_cards_count": len(tagged_card_ids),
                        "cover_url": (
                            f"{video_base_path()}/{item}/{video.cover_image}"
                        ),
                        "processed_at": video.processed_at,
                        "last_watch_time": last_watch_time,
                    }
                )

    # Pre-sort before sending to client to avoid a "flicker" where the videos are loaded and then quickly sorted and re-ordered
    # TODO: Can it be done cleaner? I don't like that this is being done twice in two different places and languages
    preferences = load_video_settings()
    current_sort = preferences.get("sort_order", "title")
    if current_sort == "title":
        videos.sort(key=lambda x: x["title"].lower())
    elif current_sort == "title_rev":
        videos.sort(key=lambda x: x["title"].lower(), reverse=True)
    elif current_sort == "last_read":
        videos.sort(key=lambda x: x["last_read_time"], reverse=True)
    elif current_sort == "last_read_rev":
        videos.sort(key=lambda x: x["last_read_time"])
    elif current_sort == "date_added":
        videos.sort(key=lambda x: x["processed_at"], reverse=True)
    elif current_sort == "date_added_rev":
        videos.sort(key=lambda x: x["processed_at"])
    elif current_sort == "chars":
        videos.sort(key=lambda x: x["character_count"], reverse=True)
    elif current_sort == "chars_rev":
        videos.sort(key=lambda x: x["character_count"])
    elif current_sort == "mined":
        videos.sort(key=lambda x: x["tagged_cards_count"], reverse=True)
    elif current_sort == "mined_rev":
        videos.sort(key=lambda x: x["tagged_cards_count"])
    return templates.TemplateResponse(
        request,
        "video_library.html",
        {
            "request": request,
            "videos": videos,
            "video_base_path": video_base_path(),
        },
    )
