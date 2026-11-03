"""On-demand video-only fragments and pull-driven, continuous PCM audio."""

import json
import logging
import math
import os
import struct
import shutil
import time
import tempfile
import threading
import xml.etree.ElementTree as ET
from pathlib import Path
from uuid import UUID
from weakref import WeakValueDictionary

from fastapi import APIRouter, HTTPException, Query
from fastapi.responses import StreamingResponse, Response

from core.paths import get_video_path, named_temp_path
from videos.video import run_media_command, load_playback_index, playback_source_version
from videos.playback_audio import (
    SAMPLE_RATE, CHANNELS, FRAME_BYTES, CHUNK_SECONDS, read_chunk, release_session,
)

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/stream/playback")
CACHE_BYTES = 512 * 1024**2
RATES = (0.5, 0.75, 1, 1.25, 1.5, 1.75, 2)
_cache_lock = threading.Lock()
_path_locks = WeakValueDictionary()
_path_locks_lock = threading.Lock()
# Bound simultaneous remux jobs, without locking cached reads behind them.
_jobs = threading.Semaphore(2)
_namespace = {"d": "urn:mpeg:dash:schema:mpd:2011"}


def _path_lock(path):
    with _path_locks_lock:
        lock = _path_locks.get(path)
        if lock is None:
            lock = threading.Lock()
            _path_locks[path] = lock
        return lock


def _box(data: bytes, *path: bytes) -> bytes:
    for name in path:
        offset = 8 if name == b"hvc1" else 78 if name == b"hvcC" else 0
        found = False
        while offset + 8 <= len(data):
            size, kind = struct.unpack_from(">I4s", data, offset)
            header = 8
            if size == 1:
                size = struct.unpack_from(">Q", data, offset + 8)[0]
                header = 16
            if size < header or offset + size > len(data):
                break
            if kind == name:
                data = data[offset + header:offset + size]
                found = True
                break
            offset += size
        if not found:
            raise ValueError("Invalid HEVC configuration")
    return data


def _hevc_codec(init: Path) -> str:
    # ISO/IEC 14496-15 codec string from the actual hvcC record. Some FFmpeg
    # versions leave HEVC's DASH codecs attribute empty.
    data = _box(init.read_bytes(), b"moov", b"trak", b"mdia", b"minf", b"stbl", b"stsd", b"hvc1", b"hvcC")
    profile = " ABC"[data[1] >> 6].strip() + str(data[1] & 31)
    compatibility = int(f'{int.from_bytes(data[2:6], "big"):032b}'[::-1], 2)
    constraints = data[6:12].rstrip(b"\0")
    tier = "H" if data[1] & 32 else "L"
    suffix = "".join(f".{value:02X}" for value in constraints)
    return f"hvc1.{profile}.{compatibility:X}.{tier}{data[12]}{suffix}"


def _fragment_decode_time(path: Path) -> int:
    # Read only box metadata; a fragment's mdat can contain a large GOP.
    with path.open("rb") as file:
        end = path.stat().st_size
        while file.tell() + 8 <= end:
            start = file.tell()
            size, kind = struct.unpack(">I4s", file.read(8))
            header = 8
            if size == 1:
                size = struct.unpack(">Q", file.read(8))[0]
                header = 16
            if size < header or start + size > end:
                break
            if kind == b"moof":
                tfdt = _box(file.read(size - header), b"traf", b"tfdt")
                return struct.unpack_from(">Q" if tfdt[0] == 1 else ">I", tfdt, 4)[0]
            file.seek(start + size)
    raise ValueError("Generated fragment has no decode timestamp")


def _shift_fragment(path: Path, shift: int):
    # Independently remuxed MP4s start tfdt at zero and put the start offset in
    # their initialization edit list. MSE reuses ONE init, so carry that offset
    # into the fragment decode timestamps instead.
    with path.open("r+b") as file:
        def boxes(start, end):
            while start + 8 <= end:
                file.seek(start)
                size, kind = struct.unpack(">I4s", file.read(8))
                header = 8
                if size == 1:
                    size = struct.unpack(">Q", file.read(8))[0]
                    header = 16
                if size < header or start + size > end:
                    raise ValueError("Invalid generated fragment")
                body = start + header
                if kind in {b"moof", b"traf"}:
                    boxes(body, start + size)
                elif kind in {b"tfdt", b"sidx"}:
                    file.seek(body)
                    fmt = ">Q" if file.read(1) == b"\x01" else ">I"
                    offset = body + (4 if kind == b"tfdt" else 12)
                    file.seek(offset)
                    value = struct.unpack(fmt, file.read(struct.calcsize(fmt)))[0]
                    file.seek(offset)
                    file.write(struct.pack(fmt, value + shift))
                start += size
        boxes(0, path.stat().st_size)


def cache_root() -> Path:
    root = named_temp_path("playback-video-v1")
    root.mkdir(mode=0o700, exist_ok=True)
    return root


def source_and_cache(video_id: UUID) -> tuple[Path, Path]:
    source = get_video_path(video_id, "video.mp4")
    if not source.is_file():
        raise HTTPException(404, "Video not found")
    version = playback_source_version(source)
    return source, cache_root() / f"{video_id}-{version}"


def _cache_files(directory):
    files = []
    for path in directory.glob("*/*"):
        try:
            if path.is_file() and path.suffix != ".tmp":
                files.append((path, path.stat()))
        except FileNotFoundError:
            pass
    return files


def _prune(keep: Path):
    root = cache_root()
    # Requests touch ready.json. Keep active indexes, but discard obsolete or
    # idle indexes together with their init and fragments. Active indexes are
    # the only working-set exception to the cache budget.
    directories = []
    for directory in root.iterdir():
        if not directory.is_dir() or directory == keep.parent:
            continue
        try:
            video_id = UUID(directory.name.rsplit("-", 1)[0])
            source = get_video_path(video_id, "video.mp4")
            obsolete = not source.is_file() or source_and_cache(video_id)[1] != directory
            manifest = directory / "ready.json"
            used = manifest.stat().st_mtime if manifest.exists() else directory.stat().st_mtime
            if obsolete:
                try:
                    shutil.rmtree(directory)
                except OSError:
                    continue
            else:
                directories.append((used, directory))
        except (FileNotFoundError, ValueError):
            continue
    files = _cache_files(root)
    total = sum(stat.st_size for _, stat in files)
    for used, directory in sorted(directories):
        if total <= CACHE_BYTES:
            break
        if time.time() - used < 90:
            continue
        size = sum(stat.st_size for path, stat in files if path.parent == directory)
        try:
            shutil.rmtree(directory)
        except OSError:
            continue
        total -= size
    for path, stat in sorted(files, key=lambda item: item[1].st_mtime):
        if total <= CACHE_BYTES:
            break
        if path.suffix != ".m4s" or path == keep:
            continue
        try:
            path.unlink()
            total -= stat.st_size
        except (FileNotFoundError, PermissionError):
            pass
    if keep.suffix == ".m4s":
        try:
            if total > CACHE_BYTES and keep.stat().st_size > CACHE_BYTES:
                keep.unlink()
        except (FileNotFoundError, PermissionError):
            pass


def remove_video_cache(video_id: UUID):
    with _cache_lock:
        for directory in cache_root().glob(f"{video_id}-*"):
            shutil.rmtree(directory, ignore_errors=True)


def _remux(source: Path, directory: Path, segment: dict) -> tuple[Path, Path, str]:
    # Seek to a keyframe and stop after the next keyframe has flushed this GOP.
    # copyts preserves the original timeline; no full-video copy or re-encode.
    run_media_command([
        "ffmpeg", "-nostdin", "-v", "error", "-y", "-seek_timestamp", "1",
        "-ss", f'{segment["start"] + segment["duration"] / 2:.9f}', "-copyts", "-i", source,
        "-to", f'{segment["start"] + segment["duration"] + 1:.9f}',
        "-map", "0:v:0", "-c:v", "copy", "-an", "-sn", "-dn",
        "-tag:v", segment["tag"], "-avoid_negative_ts", "disabled",
        "-f", "dash", "-seg_duration", "0.001",
        "-use_template", "1", "-use_timeline", "1", "-init_seg_name", "init.mp4",
        "-media_seg_name", "part-$Number$.m4s", directory / "manifest.mpd",
    ], timeout=120)
    root = ET.parse(directory / "manifest.mpd").getroot()
    representation = root.find(".//d:Representation", _namespace)
    if representation is None:
        raise HTTPException(422, "Could not read the video codec.")
    codec = representation.attrib.get("codecs") or _hevc_codec(directory / "init.mp4")
    mime = f'video/mp4; codecs="{codec}"'
    # HEVC seeks can include a preceding random-access picture. Select by
    # timestamp, not by assuming the first emitted fragment is the target.
    template = root.find(".//d:SegmentTemplate", _namespace)
    scale = int(template.attrib["timescale"])
    cursor, number = 0, int(template.attrib.get("startNumber", 1))
    for item in template.findall("d:SegmentTimeline/d:S", _namespace):
        cursor = int(item.attrib.get("t", cursor))
        for _ in range(int(item.attrib.get("r", 0)) + 1):
            if abs(cursor / scale - segment["start"]) < 0.00001:
                fragment = directory / f"part-{number}.m4s"
                # tfdt is local decode time. All fragments use the first init's
                # edit list, so preserve each source key packet's DTS relative
                # to the first key packet, including open-GOP reorder offsets.
                local_decode = _fragment_decode_time(fragment)
                shift = round((segment["decodeStart"] - segment["decodeOrigin"]) * scale) - local_decode
                if shift:
                    _shift_fragment(fragment, shift)
                return directory / "init.mp4", fragment, mime
            cursor += int(item.attrib["d"])
            number += 1
    raise HTTPException(422, "Could not locate the requested video keyframe.")


def prepare(video_id: UUID) -> dict:
    source, directory = source_and_cache(video_id)
    try:
        index = load_playback_index(source)
    except (OSError, ValueError, KeyError, TypeError):
        raise HTTPException(503, "Playback index missing or invalid. Run python -m scripts.index_video_library.") from None
    with _path_lock(directory):
        manifest_path = directory / "ready.json"
        with _cache_lock:
            if manifest_path.is_file():
                os.utime(manifest_path, None)
                return json.loads(manifest_path.read_text())
        with _jobs:
            segments = index["segments"]
            directory.mkdir(mode=0o700, exist_ok=True)
            with tempfile.TemporaryDirectory(prefix="mogao-fragment-") as temporary:
                init, fragment, mime = _remux(source, Path(temporary), segments[0])
                with _cache_lock:
                    init.replace(directory / "init.mp4")
                    fragment.replace(directory / "video-1.m4s")
                    _prune(directory / "video-1.m4s")
            manifest = {**index, "mime": mime, "chunkSeconds": CHUNK_SECONDS,
                        "sampleRate": SAMPLE_RATE, "rates": RATES}
            if source_and_cache(video_id)[1] != directory:
                raise HTTPException(409, "Video changed while preparing playback. Reload the player.")
            with tempfile.NamedTemporaryFile(mode="w", dir=directory, suffix=".tmp", delete=False) as file:
                temporary_manifest = Path(file.name)
                json.dump(manifest, file)
            try:
                with _cache_lock:
                    temporary_manifest.replace(manifest_path)
                    _prune(manifest_path)
            finally:
                temporary_manifest.unlink(missing_ok=True)
            return manifest


def prepared_cache(video_id: UUID, version: str) -> tuple[Path, dict]:
    _, directory = source_and_cache(video_id)
    if directory.name.rsplit("-", 1)[-1] != version:
        raise HTTPException(409, "Video changed. Reload the player.")
    manifest = directory / "ready.json"
    with _cache_lock:
        if manifest.is_file():
            os.utime(manifest, None)
            return directory, json.loads(manifest.read_text())
    return directory, prepare(video_id)


@router.get("/{video_id}/manifest")
def playback_manifest(video_id: UUID):
    try:
        return prepare(video_id)
    except HTTPException:
        raise
    except Exception:
        logger.exception("Failed to prepare playback for %s", video_id)
        raise HTTPException(503, "Could not prepare playback. Please retry.") from None


@router.delete("/{video_id}/audio-session/{session}")
def close_audio_session(video_id: UUID, session: UUID, generation: int = Query(0, ge=0)):
    release_session(session, generation)
    return Response(status_code=204)


def _open_video(video_id: UUID, version: str, number: int):
    directory, manifest = prepared_cache(video_id, version)
    if number < 0 or number > len(manifest["segments"]):
        raise HTTPException(404, "Video segment not found")
    path = directory / ("init.mp4" if number == 0 else f"video-{number}.m4s")
    with _path_lock(path):
        with _cache_lock:
            try:
                file = path.open("rb")
            except FileNotFoundError:
                pass
            else:
                os.utime(file.fileno(), None)
                return file
        source, _ = source_and_cache(video_id)
        with _jobs, tempfile.TemporaryDirectory(prefix="mogao-fragment-") as temporary:
            init, fragment, _ = _remux(source, Path(temporary), manifest["segments"][max(0, number - 1)])
            with _cache_lock:
                (init if number == 0 else fragment).replace(path)
                file = path.open("rb")
                try:
                    _prune(path)
                except Exception:
                    file.close()
                    raise
                return file


@router.get("/{video_id}/{version}/video/{number}")
def video_segment(video_id: UUID, version: str, number: int):
    file = _open_video(video_id, version, number)
    def body():
        with file:
            while data := file.read(65536):
                yield data
    return StreamingResponse(body(), media_type="video/mp4", headers={
        "Cache-Control": "private, no-transform", "Content-Encoding": "identity",
    })


@router.get("/{video_id}/{version}/audio/{chunk}")
def audio_chunk(video_id: UUID, version: str, chunk: int, rate: float = Query(1),
                session: UUID = Query(...), generation: int = Query(0, ge=0), origin: int = Query(0, ge=0)):
    if rate not in RATES:
        raise HTTPException(422, "Unsupported playback speed")
    _, manifest = prepared_cache(video_id, version)
    if not manifest["audio"] or chunk < origin or chunk >= math.ceil(manifest["duration"] / CHUNK_SECONDS):
        raise HTTPException(404, "Audio chunk not found")
    source, _ = source_and_cache(video_id)
    data = read_chunk(session, source, version, generation, origin, rate, manifest["duration"], chunk)
    header = struct.pack("<4sI4s4sIHHIIHH4sI", b"RIFF", 36 + len(data), b"WAVE", b"fmt ",
                         16, 1, CHANNELS, SAMPLE_RATE, SAMPLE_RATE * FRAME_BYTES,
                         FRAME_BYTES, 16, b"data", len(data))
    return Response(header + data, media_type="audio/wav", headers={
        "Cache-Control": "no-store",
    })
