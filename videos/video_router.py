import asyncio
import glob
import hashlib
import json
import logging
import os
import pickle
import re
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import urllib.parse
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import BinaryIO
from uuid import UUID

from fastapi import APIRouter, BackgroundTasks, File, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, RedirectResponse
from pydantic import AnyHttpUrl, BaseModel

from core.config import settings
from core.dependencies import postprocessor, templates
from core.paths import (
    get_video_dict_path,
    get_video_path,
    get_video_progress_path,
    unique_temp_path,
)
from integrations.anki import call_anki_async, get_anki_word_sets
from integrations.llm_processor import (
    cancel_video_dictionary_job,
    load_video_dict,
    schedule_video_dictionary_job,
)
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
DOWNLOAD_INFO_TIMEOUT_SECONDS = 60
SUBTITLE_DOWNLOAD_TIMEOUT_SECONDS = 5 * 60
ACTIVE_VIDEO_JOB_STATUSES = {"queued", "downloading", "processing"}
processing_jobs: dict[UUID, dict] = {}
processing_jobs_lock = threading.RLock()


@dataclass(frozen=True)
class DownloadMetadata:
    title: str
    source_identity: tuple[str, str]
    resolved_url: str


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
    with processing_jobs_lock:
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
    with processing_jobs_lock:
        processing_jobs[upload_id] = job


def snapshot_processing_jobs() -> dict[UUID, dict]:
    prune_processing_jobs()
    with processing_jobs_lock:
        return {
            processing_id: job.copy()
            for processing_id, job in processing_jobs.items()
        }


def _find_video_by_source_identity(
    source_identity: tuple[str, str],
) -> tuple[str, Video] | None:
    if not settings.paths.video_library.is_dir():
        return None

    for item in settings.paths.video_library.iterdir():
        if not item.is_dir():
            continue
        try:
            UUID(item.name)
        except ValueError:
            continue
        video = load_video_cached(item.name)
        if video and getattr(video, "source_identity", None) == source_identity:
            return item.name, video
    return None


def register_download_job(
    processing_id: UUID, metadata: DownloadMetadata
) -> tuple[str, str] | None:
    """Reserve a source identity, returning conflict kind and title if taken."""
    prune_processing_jobs()
    with processing_jobs_lock:
        failed_attempts = []
        for job_id, job in processing_jobs.items():
            if job.get("source_identity") != metadata.source_identity:
                continue
            if job.get("status") in ACTIVE_VIDEO_JOB_STATUSES:
                return "active", str(job.get("title") or metadata.title)
            if job.get("status") == "failed":
                failed_attempts.append(job_id)

        existing = _find_video_by_source_identity(metadata.source_identity)
        if existing is not None:
            _, video = existing
            return "library", video.metadata.title

        for job_id in failed_attempts:
            processing_jobs.pop(job_id, None)
        processing_jobs[processing_id] = {
            "status": "queued",
            "title": metadata.title,
            "source_identity": metadata.source_identity,
        }
    return None


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
    with processing_jobs_lock:
        status = processing_jobs.get(upload_id)
        if status is None:
            raise HTTPException(status_code=404, detail="Processing job not found")
        result = status.copy()
        if status["status"] in ("complete", "failed"):
            processing_jobs.pop(upload_id, None)
    return result


@router.delete("/upload-chunks/{upload_id}")
async def cancel_chunk_upload(upload_id: UUID):
    upload_dir = chunk_upload_dir(upload_id)
    if os.path.exists(upload_dir):
        shutil.rmtree(upload_dir)
    return {"status": "cancelled"}


def _stage_subtitles(source: BinaryIO, destination: Path) -> int:
    """Copy and validate an upload in a worker, without touching live files."""
    with destination.open("wb") as buffer:
        shutil.copyfileobj(source, buffer)
    try:
        subtitles = destination.read_text(encoding="utf-8")
    except UnicodeDecodeError:
        raise HTTPException(status_code=400, detail="Subtitles must be UTF-8 encoded")
    return sum(1 for c in subtitles if "\u4e00" <= c <= "\u9fff")


async def _change_subtitles(video_id: UUID, source: BinaryIO | None = None) -> None:
    """Prepare uploads off-loop, then commit without yielding to dictionary jobs."""
    output_dir = get_video_path(video_id)
    subtitles_path = get_video_path(video_id, "subtitles.srt")
    metadata_path = get_video_path(video_id, "video.pkl")
    if not metadata_path.is_file():
        raise HTTPException(status_code=404, detail="Video not found")

    staging_dir = Path(tempfile.mkdtemp(prefix=".subtitles-", dir=output_dir))
    cleanup = True
    try:
        staged_subtitles = staging_dir / "new.srt"
        character_count = 0
        if source is not None:
            character_count = await asyncio.to_thread(
                _stage_subtitles, source, staged_subtitles
            )

        # Re-read after staging: the video may have changed or been deleted.
        # Do not await between here and cancellation; checkpoints use this loop.
        with metadata_path.open("rb") as metadata_file:
            video: Video = pickle.load(metadata_file)
        video.character_count = character_count
        staged_metadata = staging_dir / "new.pkl"
        with staged_metadata.open("wb") as metadata_file:
            pickle.dump(video, metadata_file)

        backups = []
        try:
            # Successful cleanup also discards the old subtitle dictionary.
            for original in (subtitles_path, get_video_dict_path(video_id)):
                backup = None
                if original.exists():
                    backup = staging_dir / original.name
                    os.replace(original, backup)
                backups.append((original, backup))
            if source is not None:
                os.replace(staged_subtitles, subtitles_path)
            # Commit metadata last so a failed update leaves it intact.
            os.replace(staged_metadata, metadata_path)
        except Exception:
            try:
                for original, backup in reversed(backups):
                    if backup is None:
                        original.unlink(missing_ok=True)
                    else:
                        os.replace(backup, original)
            except Exception:
                # These backups may be the only remaining originals.
                cleanup = False
                logger.exception(f"Subtitle rollback failed; backups in {staging_dir}")
            raise

        cancel_video_dictionary_job(str(video_id))
        load_video_cached.cache_clear()
    except FileNotFoundError:
        if not metadata_path.is_file():
            raise HTTPException(status_code=404, detail="Video not found")
        raise
    finally:
        if cleanup:
            shutil.rmtree(staging_dir, ignore_errors=True)


@router.post("/upload-subtitles/{video_id}")
async def upload_subtitles(video_id: UUID, file: UploadFile = File(...)):
    """Replace subtitles and restart their dictionary generation."""
    extension = os.path.splitext(file.filename or "")[1].lower()
    if extension not in ALLOWED_SUBTITLE_EXTENSIONS:
        raise HTTPException(
            status_code=400,
            detail=f"Only subtitle files are allowed: {ALLOWED_SUBTITLE_EXTENSIONS}",
        )
    try:
        await _change_subtitles(video_id, file.file)
    except HTTPException:
        raise
    except Exception:
        logger.exception(f"Error processing subtitles for video {video_id}")
        raise HTTPException(status_code=500, detail="Failed to process subtitles")

    if settings.dictionary_generation.enabled:
        schedule_video_dictionary_job(str(video_id))

    return {"status": "ok"}


@router.post("/remove-subtitles/{video_id}")
async def remove_subtitles(video_id: UUID):
    try:
        await _change_subtitles(video_id)
    except HTTPException:
        raise
    except Exception:
        logger.exception(f"Failed to remove subtitles for video {video_id}")
        raise HTTPException(status_code=500, detail="Failed to remove subtitles")

    return {"status": "ok"}


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


def _source_identity(info: dict, url: str) -> tuple[str, str]:
    extractor = str(info.get("extractor_key") or info.get("extractor") or "").strip()
    source_id = str(info.get("id") or "").strip()
    if not extractor or not source_id:
        raise ValueError("The site did not provide a video identity")

    extractor = extractor.casefold()
    if extractor == "generic":
        source_url = str(
            info.get("webpage_url") or info.get("original_url") or url
        ).strip()
        if not source_url:
            raise ValueError("The generic extractor did not provide a source URL")
        source_url, _ = urllib.parse.urldefrag(source_url)
        source_id = "url:" + hashlib.sha256(source_url.encode()).hexdigest()

    return extractor, source_id


def _remove_downloaded_subtitles(temp_filename: Path) -> None:
    base = os.path.splitext(temp_filename)[0]
    for path in glob.glob(f"{base}.*.srt*"):
        try:
            os.remove(path)
        except FileNotFoundError:
            pass
        except OSError:
            logger.warning("Could not remove temporary subtitle file %s", path)


def download_subtitles(url: str, temp_filename: Path) -> None:
    run_media_command(
        [
            sys.executable,
            "-m",
            "yt_dlp",
            "--no-playlist",
            "--playlist-items",
            "1",
            "--socket-timeout",
            "30",
            "--retries",
            "3",
            "--fragment-retries",
            "3",
            "--extractor-retries",
            "3",
            "--skip-download",
            "--write-subs",
            "--sub-langs",
            "zh.*",
            "--sub-format",
            "srt",
            "-o",
            temp_filename,
            "--",
            url,
        ],
        timeout=SUBTITLE_DOWNLOAD_TIMEOUT_SECONDS,
    )


def install_downloaded_subtitles(output_dir: Path, subtitle_path: Path) -> None:
    subtitles_path = output_dir / "subtitles.srt"
    metadata_path = output_dir / "video.pkl"
    staged_subtitles = output_dir / f".subtitles-{uuid.uuid4()}.srt"
    staged_metadata = output_dir / f".video-{uuid.uuid4()}.pkl"

    try:
        with subtitle_path.open(encoding="utf-8") as subtitle_file:
            subtitles = subtitle_file.read()
        character_count = sum(1 for c in subtitles if "\u4e00" <= c <= "\u9fff")

        with metadata_path.open("rb") as metadata_file:
            video: Video = pickle.load(metadata_file)
        video.character_count = character_count

        shutil.move(subtitle_path, staged_subtitles)
        with staged_metadata.open("wb") as metadata_file:
            pickle.dump(video, metadata_file)

        os.replace(staged_subtitles, subtitles_path)
        try:
            os.replace(staged_metadata, metadata_path)
        except Exception:
            subtitles_path.unlink(missing_ok=True)
            raise
    finally:
        staged_subtitles.unlink(missing_ok=True)
        staged_metadata.unlink(missing_ok=True)


def _extract_download_info(url: str) -> dict:
    result = run_media_command(
        [
            sys.executable,
            "-m",
            "yt_dlp",
            "--no-playlist",
            "--playlist-items",
            "1",
            "--socket-timeout",
            "30",
            "--retries",
            "3",
            "--extractor-retries",
            "3",
            "--dump-single-json",
            "--skip-download",
            "--",
            url,
        ],
        timeout=DOWNLOAD_INFO_TIMEOUT_SECONDS,
    )
    try:
        info = json.loads(result.stdout)
    except json.JSONDecodeError as error:
        raise ValueError("The site returned invalid video information") from error
    if not isinstance(info, dict):
        raise ValueError("The site returned invalid video information")
    return info


def _first_video_info(info: dict) -> dict:
    for _ in range(20):
        if "entries" not in info:
            return info
        entries = [entry for entry in info.get("entries") or [] if entry]
        if not entries:
            raise ValueError("The site did not provide any videos")
        info = entries[0]
        if not isinstance(info, dict):
            raise ValueError("The site returned invalid video information")
    raise ValueError("The site returned too many nested playlists")


def extract_download_metadata(url: str) -> DownloadMetadata:
    info = _extract_download_info(url)
    followed_urls = set()
    for _ in range(20):
        info = _first_video_info(info)
        if info.get("_type") not in {"url", "url_transparent"} or info.get(
            "formats"
        ):
            break
        next_url = str(info.get("url") or "").strip()
        if not next_url or next_url in followed_urls:
            raise ValueError("The site did not resolve a video URL")
        followed_urls.add(next_url)
        info = _extract_download_info(next_url)
    else:
        raise ValueError("The site returned too many nested video references")

    title = str(info.get("title") or "").strip()
    if not title:
        raise ValueError("The site did not provide a video title")
    resolved_url = str(
        info.get("webpage_url") or info.get("original_url") or info.get("url") or ""
    ).strip()
    if not resolved_url:
        raise ValueError("The site did not provide a resolved video URL")

    return DownloadMetadata(
        title=title,
        source_identity=_source_identity(info, resolved_url),
        resolved_url=resolved_url,
    )


async def download_and_process(processing_id: UUID, metadata: DownloadMetadata):
    write_processing_status(
        processing_id,
        "downloading",
        title=metadata.title,
        source_identity=metadata.source_identity,
    )
    try:
        video_id, has_subtitles = await asyncio.to_thread(
            process_video_download, processing_id, metadata
        )
    except subprocess.TimeoutExpired as error:
        logger.error(
            "Video download or processing timed out after %s seconds", error.timeout
        )
        write_processing_status(
            processing_id,
            "failed",
            title=metadata.title,
            source_identity=metadata.source_identity,
            detail=f"Download or processing timed out after {error.timeout} seconds.",
        )
        return
    except Exception as error:
        # The HTTP response has already been sent; failures must stay in this job.
        logger.exception(
            "Video download or processing failed: %s",
            getattr(error, "stderr", None) or error,
        )
        write_processing_status(
            processing_id,
            "failed",
            title=metadata.title,
            source_identity=metadata.source_identity,
            detail="Download or processing failed. See Recent issues for details.",
        )
        return

    write_processing_status(
        processing_id,
        "complete",
        title=metadata.title,
        source_identity=metadata.source_identity,
        video_id=video_id,
    )

    if has_subtitles and settings.dictionary_generation.enabled:
        schedule_video_dictionary_job(video_id)


def process_video_download(
    processing_id: UUID, metadata: DownloadMetadata
) -> tuple[str, bool]:
    # Keep all downloader sidecars and partial files together for cleanup,
    # including when extraction, conversion, or subtitle processing fails.
    with tempfile.TemporaryDirectory(
        prefix="download-", dir=unique_temp_path("").parent
    ) as directory:
        return download_into_directory(
            Path(directory) / "video.mp4", processing_id, metadata
        )


def download_into_directory(
    temp_filename: Path,
    processing_id: UUID,
    metadata: DownloadMetadata,
) -> tuple[str, bool]:
    run_media_command(
        [
            sys.executable,
            "-m",
            "yt_dlp",
            "--no-playlist",
            "--playlist-items",
            "1",
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
            "--no-simulate",
            "-o",
            temp_filename,
            "--",
            metadata.resolved_url,
        ],
        timeout=DOWNLOAD_TIMEOUT_SECONDS,
    )
    if not temp_filename.is_file():
        raise ValueError("The site did not provide a downloadable video")

    subtitle_filename = temp_filename.with_name("subtitles.mp4")
    best_subtitle_path = None
    try:
        download_subtitles(metadata.resolved_url, subtitle_filename)
        selected_subtitle = pick_best_subtitle(subtitle_filename)
        if selected_subtitle is not None:
            best_subtitle_path = Path(selected_subtitle)
    except Exception:
        logger.warning(
            "Video download will continue without subtitles because subtitle "
            "retrieval failed for %s",
            metadata.title,
            exc_info=True,
        )
        _remove_downloaded_subtitles(subtitle_filename)

    write_processing_status(
        processing_id,
        "processing",
        title=metadata.title,
        source_identity=metadata.source_identity,
    )
    original_title = metadata.title + ".mp4"
    _, output_dir = generate_video(
        temp_filename,
        original_title,
        source_identity=metadata.source_identity,
    )
    has_subtitles = False
    if best_subtitle_path is not None:
        try:
            install_downloaded_subtitles(output_dir, best_subtitle_path)
        except Exception:
            logger.warning(
                "Video was imported without subtitles because subtitle installation "
                "failed for %s",
                metadata.title,
                exc_info=True,
            )
            (output_dir / "subtitles.srt").unlink(missing_ok=True)
            _remove_downloaded_subtitles(subtitle_filename)
        else:
            has_subtitles = True
    load_video_cached.cache_clear()
    return output_dir.name, has_subtitles


class DownloadRequest(BaseModel):
    url: AnyHttpUrl


@router.post("/download-video")
async def download_video(request: DownloadRequest, background_tasks: BackgroundTasks):
    url = str(request.url)
    try:
        metadata = await asyncio.to_thread(extract_download_metadata, url)
    except subprocess.TimeoutExpired as error:
        logger.error(
            "Video information lookup timed out after %s seconds", error.timeout
        )
        raise HTTPException(
            status_code=504, detail="Video information lookup timed out."
        )
    except Exception as error:
        logger.error(
            "Video information lookup failed: %s",
            getattr(error, "stderr", None) or error,
        )
        raise HTTPException(
            status_code=422, detail="Could not retrieve video information."
        )

    processing_id = uuid.uuid4()
    conflict = register_download_job(processing_id, metadata)
    if conflict is not None:
        conflict_kind, conflict_title = conflict
        if conflict_kind == "active":
            detail = f'"{conflict_title}" is already being downloaded.'
        else:
            detail = f'"{conflict_title}" is already in the video library.'
        raise HTTPException(status_code=409, detail=detail)

    background_tasks.add_task(download_and_process, processing_id, metadata)
    return {
        "status": "queued",
        "processing_id": str(processing_id),
        "title": metadata.title,
    }


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
            # A partial deletion must not leave the dictionary job running.
            cancel_video_dictionary_job(video_id_string)
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
