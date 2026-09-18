import json
import logging
import os
import pickle
import shutil
import signal
import subprocess
import tempfile
import threading
import uuid
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from core.paths import get_video_path, unique_temp_path


logger = logging.getLogger(__name__)

PROBE_TIMEOUT_SECONDS = 30
TRANSCODE_TIMEOUT_SECONDS = 2 * 60 * 60
THUMBNAIL_TIMEOUT_SECONDS = 60

_media_processes: set[subprocess.Popen[str]] = set()
_media_process_lock = threading.RLock()
_media_shutdown = False


def _kill_media_process(process: subprocess.Popen[str]) -> None:
    try:
        if os.name == "posix":
            os.killpg(process.pid, signal.SIGKILL)
        else:
            process.kill()
    except ProcessLookupError:
        pass


def _shutdown_media_processes() -> None:
    global _media_shutdown
    with _media_process_lock:
        _media_shutdown = True
        for process in tuple(_media_processes):
            _kill_media_process(process)


@contextmanager
def manage_media_processes():
    """Stop detached commands before the server waits for background jobs."""
    global _media_shutdown
    with _media_process_lock:
        _media_shutdown = False

    previous_handlers = {}

    def handle_shutdown(signum, frame):
        _shutdown_media_processes()
        previous_handler = previous_handlers[signum]
        if callable(previous_handler):
            previous_handler(signum, frame)
        elif previous_handler == signal.SIG_DFL:
            signal.signal(signum, signal.SIG_DFL)
            signal.raise_signal(signum)

    try:
        if threading.current_thread() is threading.main_thread():
            shutdown_signals = [signal.SIGINT, signal.SIGTERM]
            if hasattr(signal, "SIGBREAK"):
                shutdown_signals.append(signal.SIGBREAK)
            for signum in shutdown_signals:
                previous_handlers[signum] = signal.getsignal(signum)
                signal.signal(signum, handle_shutdown)
        yield
    finally:
        _shutdown_media_processes()
        for signum, previous_handler in previous_handlers.items():
            signal.signal(signum, previous_handler)


@dataclass
class Metadata:
    title: str
    duration_ts: int
    duration: float
    size: int

    def generate_key(self) -> uuid.UUID:
        # I want to have a stable way of generating a unique id for each video.
        # It's possible to directly use str(self), but if any new fields are added in the future
        # it would change all of the IDs.
        id_source = f"{self.title}{self.duration_ts}{self.duration}{self.size}"
        return uuid.uuid5(uuid.NAMESPACE_DNS, id_source)


@dataclass
class Video:
    metadata: Metadata
    processed_at: str
    character_count: int = 0
    cover_image: str | None = None
    source_identity: tuple[str, str] | None = None


def run_media_command(
    command: list[str | os.PathLike[str]], *, timeout: float
) -> subprocess.CompletedProcess[str]:
    """Run a bounded media command, killing its process group on POSIX on failure."""
    with _media_process_lock:
        if _media_shutdown:
            raise RuntimeError("Media processing is shutting down")
        process = subprocess.Popen(
            command,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            errors="replace",
            start_new_session=os.name == "posix",
        )
        _media_processes.add(process)
        # A main-thread signal can arrive during Popen, before registration.
        if _media_shutdown:
            _kill_media_process(process)
    try:
        with process:
            try:
                stdout, stderr = process.communicate(timeout=timeout)
            except BaseException:
                # yt-dlp may have started FFmpeg. Stop children too, before the
                # caller removes temporary files. Reap the parent without waiting
                # for EOF on pipes that an escaped child might still hold open.
                _kill_media_process(process)
                process.wait()
                raise
    finally:
        with _media_process_lock:
            _media_processes.discard(process)

    result = subprocess.CompletedProcess(command, process.returncode, stdout, stderr)
    result.check_returncode()
    return result


def probe_video_codec(path: str | os.PathLike[str]) -> str:
    result = run_media_command(
        [
            "ffprobe",
            "-v",
            "error",
            "-select_streams",
            "v:0",
            "-show_entries",
            "stream=codec_name",
            "-of",
            "default=noprint_wrappers=1:nokey=1",
            path,
        ],
        timeout=PROBE_TIMEOUT_SECONDS,
    )
    return result.stdout.strip()


def probe_audio_codec(path: str | os.PathLike[str]) -> str:
    result = run_media_command(
        [
            "ffprobe",
            "-v",
            "error",
            "-select_streams",
            "a:0",
            "-show_entries",
            "stream=codec_name",
            "-of",
            "default=noprint_wrappers=1:nokey=1",
            path,
        ],
        timeout=PROBE_TIMEOUT_SECONDS,
    )
    return result.stdout.strip()


def generate_video(
    path: str | os.PathLike[str],
    original_filename: str,
    *,
    source_identity: tuple[str, str] | None = None,
) -> tuple[Video, Path]:
    cmd = [
        "ffprobe",
        "-v",
        "quiet",
        "-print_format",
        "json",
        "-show_format",
        "-show_streams",
        path,
    ]
    result = run_media_command(cmd, timeout=PROBE_TIMEOUT_SECONDS)
    metadata = json.loads(result.stdout)

    file_size = metadata["format"]["size"]
    duration = metadata["streams"][0]["duration"]
    duration_ts = metadata["streams"][0]["duration_ts"]

    metadata = Metadata(original_filename, duration_ts, duration, file_size)
    unique_key = metadata.generate_key()
    output_dir = get_video_path(unique_key)
    output_dir.parent.mkdir(parents=True, exist_ok=True)
    # Only publish a library entry once all processing has succeeded. Failed
    # imports leave no partial folder and cannot overwrite an existing video.
    with tempfile.TemporaryDirectory(
        prefix=".processing-", dir=output_dir.parent
    ) as temporary_dir:
        processing_dir = Path(temporary_dir) / str(unique_key)
        processing_dir.mkdir()
        video_output_path = processing_dir / "video.mp4"
        cover_output_path = processing_dir / "cover.jpg"
        metadata_output_path = processing_dir / "video.pkl"
        video_codec = probe_video_codec(path)
        audio_codec = probe_audio_codec(path)
        if video_codec in ("hevc", "h264"):
            # Already a Safari-compatible codec — just remux video, no quality loss, can convert audio
            ffmpeg_cmd = [
                "ffmpeg",
                "-i",
                path,
                "-c:v",
                "copy",
                "-c:a",
                "aac" if audio_codec != "aac" else "copy",
                "-movflags",
                "+faststart",
                "-y",
                video_output_path,
            ]
        else:
            # Re-encode to H.264
            ffmpeg_cmd = [
                "ffmpeg",
                "-i",
                path,
                "-c:v",
                "libx264",
                "-crf",
                "23",
                "-preset",
                "veryfast",
                "-pix_fmt",
                "yuv420p",
                "-c:a",
                "aac",
                "-b:a",
                "128k",
                "-movflags",
                "+faststart",
                "-y",
                video_output_path,
            ]
        run_media_command(ffmpeg_cmd, timeout=TRANSCODE_TIMEOUT_SECONDS)
        os.remove(path)

        # Generate a thumbnail
        cmd = [
            "ffmpeg",
            "-ss",
            "00:00:05",
            "-i",
            video_output_path,
            "-vframes",
            "1",
            "-q:v",
            "2",
            cover_output_path,
        ]

        run_media_command(cmd, timeout=THUMBNAIL_TIMEOUT_SECONDS)

        processed_video = Video(
            metadata=metadata,
            processed_at=datetime.now().isoformat(),
            cover_image=f"cover.jpg",
            source_identity=source_identity,
        )

        with open(metadata_output_path, "wb") as f:
            pickle.dump(processed_video, f)

        backup_dir = None
        if output_dir.exists():
            backup_dir = output_dir.with_name(f".replaced-{uuid.uuid4()}")
            output_dir.rename(backup_dir)
        try:
            processing_dir.rename(output_dir)
        except OSError:
            if backup_dir is not None:
                backup_dir.rename(output_dir)
            raise
        if backup_dir is not None:
            try:
                shutil.rmtree(backup_dir)
            except OSError:
                logger.exception(
                    "Failed to remove replaced video folder for %s: %s",
                    original_filename,
                    backup_dir,
                )

    return processed_video, output_dir


def take_screenshot(video_id: str | uuid.UUID, timestamp: float) -> Path:
    video_path = get_video_path(video_id, "video.mp4")
    output_path = unique_temp_path(".jpg")
    cmd = [
        "ffmpeg",
        "-ss",
        str(timestamp),
        "-i",
        video_path,
        "-frames:v",
        "1",
        "-q:v",
        "5",
        "-vf",
        "scale=640:-1",
        output_path,
        "-y",
    ]
    subprocess.run(cmd, check=True, capture_output=True)
    return output_path


def cut_audio(video_id: str | uuid.UUID, start: float, end: float) -> Path:
    video_path = get_video_path(video_id, "video.mp4")
    output_path = unique_temp_path(".aac")
    duration = end - start
    cmd = [
        "ffmpeg",
        "-ss",
        str(start),
        "-i",
        video_path,
        "-t",
        str(duration),
        "-vn",
        "-c:a",
        "aac",
        "-b:a",
        "128k",
        output_path,
        "-y",
    ]
    subprocess.run(cmd, check=True, capture_output=True)
    return output_path
