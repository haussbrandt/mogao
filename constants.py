from uuid import UUID


LIBRARY_PATH = "library"
VIDEO_LIBRARY_PATH = "video_library"
DICT_PATH = "dicts/cedict_ts.u8"
FREQ_PATH = "dicts/freqs"


def normalize_uuid(value: str | UUID) -> str:
    """Return a canonical UUID string or raise ValueError."""
    return str(UUID(str(value)))
