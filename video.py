import json
import os
import pickle
import shutil
import subprocess
import uuid
from dataclasses import dataclass
from datetime import datetime

from constants import VIDEO_LIBRARY_PATH, normalize_uuid


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


def probe_video_codec(path: str) -> str:
    result = subprocess.run(
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
        capture_output=True,
        text=True,
        check=True,
    )
    return result.stdout.strip()


def probe_audio_codec(path: str) -> str:
    result = subprocess.run(
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
        capture_output=True,
        text=True,
        check=True,
    )
    return result.stdout.strip()


def generate_video(path, original_filename) -> tuple[Video, str]:
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
    result = subprocess.run(
        cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True
    )
    metadata = json.loads(result.stdout)

    file_size = metadata["format"]["size"]
    duration = metadata["streams"][0]["duration"]
    duration_ts = metadata["streams"][0]["duration_ts"]

    metadata = Metadata(original_filename, duration_ts, duration, file_size)
    unique_key = metadata.generate_key()
    os.makedirs(VIDEO_LIBRARY_PATH, exist_ok=True)
    output_dir = f"{VIDEO_LIBRARY_PATH}/{unique_key}"
    if os.path.exists(output_dir):
        shutil.rmtree(output_dir)
    os.makedirs(output_dir)
    video_codec = probe_video_codec(path)
    audio_codec = probe_audio_codec(path)
    if video_codec in ("hevc", "h264"):
        # Already a Safari-compatible codec — just remux, no quality loss
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
            f"{output_dir}/video.mp4",
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
            f"{output_dir}/video.mp4",
        ]
    subprocess.run(ffmpeg_cmd, check=True, capture_output=True)
    os.remove(path)

    # Generate a thumbnail
    cmd = [
        "ffmpeg",
        "-ss",
        "00:00:05",
        "-i",
        f"{output_dir}/video.mp4",
        "-vframes",
        "1",
        "-q:v",
        "2",
        f"{output_dir}/cover.jpg",
    ]

    subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)

    processed_video = Video(
        metadata=metadata,
        processed_at=datetime.now().isoformat(),
        cover_image=f"cover.jpg",
    )

    # Save to file
    p_path = os.path.join(output_dir, "video.pkl")
    with open(p_path, "wb") as f:
        pickle.dump(processed_video, f)

    return processed_video, output_dir


def take_screenshot(video_id, timestamp):
    safe_id = normalize_uuid(video_id)
    video_path = os.path.join(VIDEO_LIBRARY_PATH, safe_id, "video.mp4")
    output_path = f"/tmp/mogao/{uuid.uuid4()}.jpg"
    os.makedirs("/tmp/mogao", exist_ok=True)
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


def cut_audio(video_id, start, end):
    safe_id = normalize_uuid(video_id)
    video_path = os.path.join(VIDEO_LIBRARY_PATH, safe_id, "video.mp4")
    output_path = f"/tmp/mogao/{uuid.uuid4()}.aac"
    duration = end - start
    os.makedirs("/tmp/mogao", exist_ok=True)
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
