import asyncio
import glob
import os
import pickle
import re
import shutil
import subprocess
import uuid

from fastapi import APIRouter, BackgroundTasks, File, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, RedirectResponse
from pydantic import BaseModel

from anki import call_anki, get_all_words_from_anki_deck
from constants import VIDEO_LIBRARY_PATH
from dependencies import templates
from llm_processor import load_video_dict, process_subtitles_background
from video import Video, generate_video
from video_library import (
    get_video_progress_path,
    load_video_cached,
    load_video_settings,
    save_video_settings,
)

router = APIRouter(prefix="/video")


@router.post("/upload")
async def upload_video(
    background_tasks: BackgroundTasks, files: list[UploadFile] = File(...)
):
    """
    Handles video upload.
    Saves to temp, processes with generate_video, clears temp, refreshes library.
    """
    ALLOWED_VIDEO_EXTENSIONS = (".mp4", ".mkv", ".mov", ".avi", ".webm")

    for file in files:
        extension = os.path.splitext(file.filename)[1].lower()
        if extension not in ALLOWED_VIDEO_EXTENSIONS:
            raise HTTPException(
                status_code=400,
                detail=f"Only video files are allowed: {ALLOWED_VIDEO_EXTENSIONS}",
            )
        temp_filename = f"temp_{uuid.uuid4()}.{extension}"
        try:
            with open(temp_filename, "wb") as buffer:
                shutil.copyfileobj(file.file, buffer)
        except Exception as e:
            print(f"Error processing video: {e}")
            raise HTTPException(status_code=500, detail="Failed to process video")
        background_tasks.add_task(generate_video, temp_filename, file.filename)

    load_video_cached.cache_clear()
    return RedirectResponse(url="/", status_code=303)


@router.post("/upload-subtitles/{video_id}")
async def upload_subtitles(video_id: str, file: UploadFile = File(...)):
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
    safe_id = os.path.basename(video_id)
    output_dir = os.path.join(VIDEO_LIBRARY_PATH, safe_id)
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

    asyncio.create_task(process_subtitles_background(video_id))

    return RedirectResponse(url="/", status_code=303)


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
async def serve_thumbnail(video_id: str):
    """
    Serves the video thumbnail.
    """
    # Security check: ensure video_id is clean
    safe_video_id = os.path.basename(video_id)

    img_path = os.path.join(VIDEO_LIBRARY_PATH, safe_video_id, "cover.jpg")

    if not os.path.exists(img_path):
        raise HTTPException(status_code=404, detail="Image not found")

    return FileResponse(img_path)


@router.post("/delete/{video_id}")
async def delete_video(video_id: str, request: Request):
    """
    Deletes a video folder and refreshes the cache.
    """
    # Security: Sanitizing to ensure no one deletes ../system_files
    safe_id = os.path.basename(video_id)
    video_path = os.path.join(VIDEO_LIBRARY_PATH, safe_id)

    if os.path.exists(video_path):
        try:
            shutil.rmtree(video_path)
            load_video_cached.cache_clear()
        except Exception as e:
            print(f"Error deleting video {safe_id}: {e}")
            raise HTTPException(status_code=500, detail="Failed to delete video")
    else:
        raise HTTPException(status_code=404, detail="video not found")

    return RedirectResponse(url=request.url_for("video_library_view"), status_code=303)


@router.get("/watch/{video_id}", response_class=HTMLResponse)
async def watch_video(request: Request, video_id: str):
    """The main video player interface."""
    video = load_video_cached(video_id)
    if not video:
        raise HTTPException(status_code=404, detail="video not found")

    # TODO: refactor
    safe_id = os.path.basename(video_id)
    subtitles_path = os.path.join(VIDEO_LIBRARY_PATH, safe_id, "subtitles.srt")
    has_subtitles = os.path.exists(subtitles_path)

    # progress = load_video_progress(video_id)

    deck_words = get_all_words_from_anki_deck("Mandarin Sentence Mining", "Simplified")

    return templates.TemplateResponse(
        "video_player.html",
        {
            "request": request,
            "video": video,
            "video_id": video_id,
            "has_subtitles": has_subtitles,
            "deck_words": list(deck_words),
        },
    )


@router.get("/stream/{video_id}")
def stream_video(video_id: str):
    safe_id = os.path.basename(video_id)
    video_path = os.path.join(VIDEO_LIBRARY_PATH, safe_id, "video.mp4")

    if os.path.exists(video_path):
        return FileResponse(video_path, media_type="video/mp4")
    else:
        raise HTTPException(status_code=404, detail="video not found")


@router.get("/{video_id}/subtitles")
def get_subtitles(video_id: str):
    safe_id = os.path.basename(video_id)
    subtitle_path = os.path.join(VIDEO_LIBRARY_PATH, safe_id, "subtitles.srt")

    if os.path.exists(subtitle_path):
        return FileResponse(subtitle_path, media_type="text/plain")
    else:
        raise HTTPException(status_code=404, detail="subtitles not found")


@router.get("/api/video-dict/{video_id}", response_class=JSONResponse)
async def get_video_dict_api(video_id: str):
    safe_id = os.path.basename(video_id)
    data = load_video_dict(safe_id)
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

    if os.path.exists(VIDEO_LIBRARY_PATH):
        for item in os.listdir(VIDEO_LIBRARY_PATH):
            if os.path.isdir(os.path.join(VIDEO_LIBRARY_PATH, item)):
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
                        "cover_url": f"/{item}/{video.cover_image}",
                        "processed_at": video.processed_at,
                        "last_watch_time": last_watch_time,
                    }
                )

    # Pre-sort before sending to client to avoid a "flicker" where the videos are loaded and then quickly sorted and re-ordered
    # TODO: Can it be done cleaner? I don't like that this is being done twice in two different places and languages
    settings = load_video_settings()
    current_sort = settings.get("sort_order", "title")
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
        "video_library.html",
        {"request": request, "videos": videos},
    )
