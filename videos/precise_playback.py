"""Bounded, disposable media cache for video-only playback and decoded audio."""

import hashlib
import json
import logging
import math
import os
import shutil
import struct
import threading
import time
import xml.etree.ElementTree as ET
from pathlib import Path
from uuid import UUID

from fastapi import APIRouter, HTTPException, Query
from fastapi.responses import FileResponse, Response

from core.paths import get_video_path, named_temp_path
from videos.video import run_media_command

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/stream/precise")
SAMPLE_RATE = 48_000
CHANNELS = 2
FRAME_BYTES = CHANNELS * 2
CHUNK_SECONDS = 8
CACHE_BYTES = 4 * 1024**3
RATES = (0.5, 0.75, 1, 1.25, 1.5, 1.75, 2)
_prepare_lock = threading.Lock()
_namespace = {"d": "urn:mpeg:dash:schema:mpd:2011"}


def cache_root() -> Path:
    root = named_temp_path("precise-playback")
    root.mkdir(mode=0o700, exist_ok=True)
    return root


def source_and_cache(video_id: UUID) -> tuple[Path, Path]:
    source = get_video_path(video_id, "video.mp4")
    if not source.is_file():
        raise HTTPException(404, "Video not found")
    stat = source.stat()
    # Bump the version when changing the generated media format.
    version = hashlib.sha256(f"1:{stat.st_size}:{stat.st_mtime_ns}".encode()).hexdigest()[:16]
    return source, cache_root() / f"{video_id}-{version}"


def _prune(required: int, keep: Path) -> None:
    directories = [p for p in cache_root().iterdir() if p.is_dir() and p != keep]
    sizes = {p: sum(f.stat().st_size for f in p.iterdir() if f.is_file()) for p in directories}
    total = sum(sizes.values())
    for directory in sorted(directories, key=lambda p: p.stat().st_mtime):
        if total + required <= CACHE_BYTES:
            break
        # Asset requests renew this short lease. Never evict a video another
        # page is currently playing just to prepare the next video.
        if (directory / "ready.json").exists() and time.time() - directory.stat().st_mtime < 60:
            continue
        shutil.rmtree(directory)
        total -= sizes[directory]
    if total + required > CACHE_BYTES:
        raise HTTPException(503, "The playback cache is busy. Close another video and retry.")


def _video_manifest(directory: Path) -> tuple[str, list[dict]]:
    root = ET.parse(directory / "manifest.mpd").getroot()
    representation = root.find(".//d:Representation", _namespace)
    template = root.find(".//d:SegmentTemplate", _namespace)
    if representation is None or template is None:
        raise ValueError("Missing video representation")
    scale = int(template.attrib["timescale"])
    number = int(template.attrib.get("startNumber", 1))
    segments = []
    cursor = 0
    origin = None
    for segment in template.findall("d:SegmentTimeline/d:S", _namespace):
        cursor = int(segment.attrib.get("t", cursor))
        if origin is None:
            origin = cursor
        duration = int(segment.attrib["d"])
        repeats = int(segment.attrib.get("r", 0))
        if repeats < 0:
            raise ValueError("Unexpected open-ended segment timeline")
        for _ in range(repeats + 1):
            segments.append({"start": (cursor - origin) / scale,
                             "duration": duration / scale, "number": number})
            cursor += duration
            number += 1
    if not segments:
        raise ValueError("Empty video timeline")
    return f'video/mp4; codecs="{representation.attrib["codecs"]}"', segments


def prepare(video_id: UUID) -> dict:
    source, directory = source_and_cache(video_id)
    with _prepare_lock:
        manifest_path = directory / "ready.json"
        if manifest_path.is_file():
            directory.touch()
            return {**json.loads(manifest_path.read_text()), "rates": RATES}
        probe = run_media_command([
            "ffprobe", "-v", "error", "-show_streams", "-show_format", "-of", "json", source,
        ], timeout=30)
        metadata = json.loads(probe.stdout)
        video = next((s for s in metadata["streams"] if s["codec_type"] == "video"), None)
        if not video or video["codec_name"] != "h264":
            raise HTTPException(422, "This video codec is unsupported. H.264 video is required.")
        duration = float(metadata["format"]["duration"])
        if not math.isfinite(duration) or duration <= 0:
            raise HTTPException(422, "Invalid video duration")
        audio = any(s["codec_type"] == "audio" for s in metadata["streams"])
        required = source.stat().st_size + math.ceil(duration * SAMPLE_RATE * FRAME_BYTES * 1.5)
        if required > CACHE_BYTES:
            raise HTTPException(422, "This video exceeds the playback cache limit.")
        _prune(required, directory)
        # An interrupted preparation is never served as complete.
        if directory.exists():
            shutil.rmtree(directory)
        directory.mkdir(mode=0o700)
        try:
            run_media_command([
                "ffmpeg", "-nostdin", "-v", "error", "-y", "-i", source,
                "-map", "0:v:0", "-c:v", "copy", "-an", "-sn", "-dn",
                "-f", "dash", "-seg_duration", str(CHUNK_SECONDS),
                "-use_template", "1", "-use_timeline", "1",
                "-init_seg_name", "init.mp4", "-media_seg_name", "video-$Number%05d$.m4s",
                directory / "manifest.mpd",
            ], timeout=300)
            mime, segments = _video_manifest(directory)
            duration = segments[-1]["start"] + segments[-1]["duration"]
            frames = {}
            if audio:
                for rate in (1, 2):
                    # Process each rate continuously before slicing the PCM. The tempo
                    # filter's state therefore survives every served chunk boundary.
                    path = directory / f"audio-{rate}.pcm"
                    _write_audio(source, duration, rate, path)
                    frames[str(rate)] = path.stat().st_size // FRAME_BYTES
            manifest = {"version": directory.name.rsplit("-", 1)[-1], "duration": duration,
                        "mime": mime, "segments": segments, "audio": audio,
                        "chunkSeconds": CHUNK_SECONDS, "sampleRate": SAMPLE_RATE,
                        "audioFrames": frames, "rates": RATES}
            if source_and_cache(video_id)[1] != directory:
                raise HTTPException(409, "Video changed while preparing playback. Reload the player.")
            temporary_manifest = directory / "ready.tmp"
            temporary_manifest.write_text(json.dumps(manifest))
            temporary_manifest.replace(manifest_path)
            return manifest
        except Exception:
            shutil.rmtree(directory, ignore_errors=True)
            raise


def prepared_cache(video_id: UUID, version: str) -> tuple[Path, dict]:
    _, directory = source_and_cache(video_id)
    if directory.name.rsplit("-", 1)[-1] != version:
        raise HTTPException(409, "Video changed. Reload the player.")
    manifest = directory / "ready.json"
    if not manifest.is_file():
        raise HTTPException(409, "Playback cache expired. Reload the player.")
    directory.touch()
    return directory, json.loads(manifest.read_text())


@router.get("/{video_id}/manifest")
def playback_manifest(video_id: UUID):
    try:
        return prepare(video_id)
    except HTTPException:
        raise
    except Exception:
        logger.exception("Failed to prepare precise playback for %s", video_id)
        raise HTTPException(503, "Could not prepare playback. Please retry.") from None


@router.get("/{video_id}/{version}/video/{number}")
def video_segment(video_id: UUID, version: str, number: int):
    directory, manifest = prepared_cache(video_id, version)
    if number == 0:
        path = directory / "init.mp4"
    elif any(segment["number"] == number for segment in manifest["segments"]):
        path = directory / f"video-{number:05d}.m4s"
    else:
        raise HTTPException(404, "Video segment not found")
    return FileResponse(path, media_type="video/mp4", headers={"Cache-Control": "private, no-transform"})


def _write_audio(source: Path, duration: float, rate: float, path: Path) -> None:
    temporary = path.with_suffix(".tmp")
    try:
        run_media_command([
            "ffmpeg", "-nostdin", "-v", "error", "-y", "-i", source,
            "-map", "0:a:0", "-vn", "-sn", "-dn", "-ac", str(CHANNELS),
            "-af", f"aresample={SAMPLE_RATE}:async=1:first_pts=0,atempo={rate},apad,atrim=duration={duration / rate:.9f}",
            "-ar", str(SAMPLE_RATE), "-c:a", "pcm_s16le", "-f", "s16le", temporary,
        ], timeout=300)
        temporary.replace(path)
    finally:
        temporary.unlink(missing_ok=True)


def _audio_file(video_id: UUID, directory: Path, manifest: dict, rate: float):
    path = directory / f"audio-{rate:g}.pcm"
    try:
        file = path.open("rb")
    except FileNotFoundError:
        # Cached playback must not wait behind another request's FFmpeg work.
        # Serialize only generation, and recheck after acquiring the lock.
        with _prepare_lock:
            if not path.is_file():
                required = math.ceil(manifest["duration"] / rate * SAMPLE_RATE * FRAME_BYTES)
                existing = sum(p.stat().st_size for p in directory.iterdir() if p.is_file())
                # Optional speed variants expire before evicting other videos.
                # Recent audio requests lease each variant for 60 seconds.
                for old in sorted(directory.glob("audio-*.pcm"), key=lambda p: p.stat().st_mtime):
                    if existing + required <= CACHE_BYTES:
                        break
                    if old.name not in {"audio-1.pcm", "audio-2.pcm"} and time.time() - old.stat().st_mtime > 60:
                        existing -= old.stat().st_size
                        old.unlink()
                _prune(existing + required, directory)
                source, current = source_and_cache(video_id)
                if current != directory:
                    raise HTTPException(409, "Video changed. Reload the player.")
                _write_audio(source, manifest["duration"], rate, path)
            file = path.open("rb")
    # Renew via the open descriptor: reads remain valid even if an eviction
    # raced with open(), and no generation lock is needed for cached chunks.
    os.utime(file.fileno(), None)
    return file


@router.get("/{video_id}/{version}/audio/{chunk}")
def audio_chunk(video_id: UUID, version: str, chunk: int, rate: float = Query(1)):
    if rate not in RATES:
        raise HTTPException(422, "Unsupported playback speed")
    directory, manifest = prepared_cache(video_id, version)
    if not manifest["audio"] or chunk < 0 or chunk >= math.ceil(manifest["duration"] / CHUNK_SECONDS):
        raise HTTPException(404, "Audio chunk not found")
    # Tempo is changed continuously before slicing, preserving pitch and joins.
    with _audio_file(video_id, directory, manifest, rate) as file:
        first = round(chunk * CHUNK_SECONDS * SAMPLE_RATE / rate)
        last = min(round((chunk + 1) * CHUNK_SECONDS * SAMPLE_RATE / rate), os.fstat(file.fileno()).st_size // FRAME_BYTES)
        file.seek(first * FRAME_BYTES)
        data = file.read(max(0, last - first) * FRAME_BYTES)
    if not data:
        raise HTTPException(404, "Audio chunk not found")
    header = struct.pack("<4sI4s4sIHHIIHH4sI", b"RIFF", 36 + len(data), b"WAVE", b"fmt ",
                         16, 1, CHANNELS, SAMPLE_RATE, SAMPLE_RATE * FRAME_BYTES,
                         FRAME_BYTES, 16, b"data", len(data))
    return Response(header + data, media_type="audio/wav", headers={"Cache-Control": "private, no-transform"})
