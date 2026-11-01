import hashlib
import json
import logging
import math
import os
import pickle
import shutil
import signal
import subprocess
import tempfile
import threading
import uuid
from contextlib import contextmanager
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from fractions import Fraction
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


@contextmanager
def _media_process(command, *, stderr=subprocess.PIPE):
    with _media_process_lock:
        if _media_shutdown:
            raise RuntimeError("Media processing is shutting down")
        process = subprocess.Popen(
            command,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=stderr,
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
                yield process
            except BaseException:
                # Kill children before closing pipes that they may still hold open.
                _kill_media_process(process)
                process.wait()
                raise
    finally:
        with _media_process_lock:
            _media_processes.discard(process)


def run_media_command(
    command: list[str | os.PathLike[str]], *, timeout: float
) -> subprocess.CompletedProcess[str]:
    """Run a bounded media command, killing its process group on POSIX on failure."""
    with _media_process(command) as process:
        stdout, stderr = process.communicate(timeout=timeout)

    result = subprocess.CompletedProcess(command, process.returncode, stdout, stderr)
    result.check_returncode()
    return result


def stream_media_command(
    command: list[str | os.PathLike[str]], on_line: Callable[[str], None], *, timeout: float
) -> None:
    """Consume stdout incrementally, with the same shutdown/timeout ownership."""
    with tempfile.TemporaryFile(mode="w+t") as errors, _media_process(command, stderr=errors) as process:
        expired = threading.Event()

        def expire():
            expired.set()
            _kill_media_process(process)

        timer = threading.Timer(timeout, expire)
        timer.daemon = True
        try:
            timer.start()
            for line in process.stdout:
                on_line(line)
            process.wait()
            timer.cancel()
            timer.join()
            if expired.is_set():
                raise subprocess.TimeoutExpired(command, timeout)
            if process.returncode:
                errors.seek(0)
                raise subprocess.CalledProcessError(process.returncode, command, stderr=errors.read(65536))
        finally:
            timer.cancel()
            timer.join()


def playback_source_version(source: Path) -> str:
    stat = source.stat()
    return hashlib.sha256(f"playback-index-1:{stat.st_size}:{stat.st_mtime_ns}".encode()).hexdigest()[:16]


def load_playback_index(source: Path) -> dict:
    """Read an import-time index; playback must never generate a missing index."""
    index = json.loads(source.with_name("playback-index.json").read_text())
    if index["version"] != playback_source_version(source):
        raise ValueError("Playback index does not match the video")
    if not index["segments"]:
        raise ValueError("Playback index has no segments")
    return index


def index_video(source: Path) -> dict:
    """Persist keyframe metadata without generating video fragments or audio."""
    source = Path(source)
    version = playback_source_version(source)
    metadata = json.loads(run_media_command([
        "ffprobe", "-v", "error", "-show_streams", "-show_format", "-of", "json", source,
    ], timeout=30).stdout)
    video = next((s for s in metadata["streams"] if s["codec_type"] == "video"), None)
    if not video or video["codec_name"] not in {"h264", "hevc"}:
        raise ValueError("H.264 or HEVC video is required.")
    duration = float(video.get("duration") or metadata["format"]["duration"]) + float(video.get("start_time", 0))
    if not math.isfinite(duration) or duration <= 0:
        raise ValueError("Invalid video duration")
    video_duration = duration
    audio = next((stream for stream in metadata["streams"] if stream["codec_type"] == "audio"), None)
    if audio:
        audio_end = float(audio.get("duration") or metadata["format"]["duration"]) + float(audio.get("start_time", 0))
        if not math.isfinite(audio_end) or audio_end <= 0:
            raise ValueError("Invalid audio duration")
        duration = max(duration, audio_end)
    # Use the demuxer's key packets, which also drive stream-copy seeks
    # and DASH boundaries. Decoding with skip_frame=nokey can omit
    # open-GOP recovery points. Packet probing needs no video decode.
    keyframes = {}

    def index_packet(line):
        packet = dict(field.split("=", 1) for field in line.strip().split("|") if "=" in field)
        if "K" not in packet.get("flags", ""):
            return
        try:
            pts, dts = float(packet["pts_time"]), float(packet["dts_time"])
        except (KeyError, ValueError):
            raise ValueError("Video keyframe has no usable timestamps.") from None
        if not math.isfinite(pts) or not math.isfinite(dts):
            raise ValueError("Invalid video keyframe timestamps.")
        if pts < video_duration:
            keyframes[max(0, pts)] = dts

    stream_media_command([
        "ffprobe", "-v", "error", "-select_streams", "v:0",
        "-show_packets", "-show_entries", "packet=pts_time,dts_time,flags",
        "-of", "compact=p=0", source,
    ], index_packet, timeout=300)
    starts = sorted(keyframes)
    if not starts:
        raise ValueError("Video has no seekable keyframes.")
    tag = "hvc1" if video["codec_name"] == "hevc" else "avc1"
    segments = [{"number": i + 1, "start": start, "duration": end - start, "tag": tag,
                 "decodeStart": keyframes[start], "decodeOrigin": keyframes[starts[0]]}
                for i, (start, end) in enumerate(zip(starts, [*starts[1:], video_duration]))]
    index = {"version": version, "duration": duration, "videoDuration": video_duration,
             "segments": segments, "audio": audio is not None}
    if playback_source_version(source) != version:
        raise ValueError("Video changed while indexing")
    with tempfile.NamedTemporaryFile(mode="w", dir=source.parent, suffix=".tmp", delete=False) as file:
        temporary = Path(file.name)
        json.dump(index, file)
    try:
        temporary.replace(source.with_name("playback-index.json"))
    finally:
        temporary.unlink(missing_ok=True)
    return index


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


def select_mandarin_audio(audio_streams: list[dict]) -> dict | None:
    """Prefer Mandarin, then broadly tagged Chinese, for multi-audio imports."""
    if len(audio_streams) <= 1:
        return None

    chinese_stream = None
    for stream in audio_streams:
        language = (
            stream.get("tags", {}).get("language", "").lower().replace("_", "-")
        )
        parts = language.split("-")
        if parts[0] == "cmn":
            return stream
        if parts[0] in ("zh", "zho", "chi"):
            # A three-letter extlang identifies a specific Chinese language.
            if len(parts) > 1 and len(parts[1]) == 3 and parts[1].isalpha():
                if parts[1] == "cmn":
                    return stream
                continue
            if chinese_stream is None:
                chinese_stream = stream
    return chinese_stream


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
    audio_streams = [
        stream for stream in metadata["streams"] if stream.get("codec_type") == "audio"
    ]
    selected_audio = select_mandarin_audio(audio_streams)
    audio_input_options = []
    if selected_audio is not None:
        # Discard other audio tracks at the input so FFmpeg can still select
        # video automatically, including its default and resolution preferences.
        for stream in audio_streams:
            if stream["index"] != selected_audio["index"]:
                audio_input_options.extend([f"-discard:{stream['index']}", "all"])
        audio_codec = selected_audio.get("codec_name", "")
    else:
        # FFmpeg selects audio automatically. Only copy if every possible
        # selection is AAC; the first track's codec may differ from its choice.
        audio_codec = (
            "aac"
            if all(stream.get("codec_name") == "aac" for stream in audio_streams)
            else ""
        )

    file_size = metadata["format"]["size"]
    first_stream = next(iter(metadata["streams"]), {})
    duration = first_stream.get("duration")
    if duration in (None, "N/A"):
        duration = metadata["format"].get("duration")
    try:
        duration_seconds = float(duration)
    except (TypeError, ValueError):
        raise ValueError("Unable to determine video duration") from None
    if not math.isfinite(duration_seconds) or duration_seconds <= 0:
        raise ValueError("Unable to determine a positive video duration")

    duration_ts = first_stream.get("duration_ts")
    if duration_ts in (None, "N/A"):
        # MKV often reports duration only at container level. Derive ticks in
        # the stream's time base, or use microseconds if no time base is known.
        try:
            time_base = Fraction(first_stream.get("time_base", "1/1000000"))
            if time_base <= 0:
                raise ValueError
        except (TypeError, ValueError, ZeroDivisionError):
            time_base = Fraction(1, 1000000)
        duration_ts = round(Fraction(str(duration)) / time_base)

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
        if video_codec in ("hevc", "h264"):
            # Already a Safari-compatible codec — just remux video, no quality loss, can convert audio
            ffmpeg_cmd = [
                "ffmpeg",
                *audio_input_options,
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
                *audio_input_options,
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

        index_video(video_output_path)

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
