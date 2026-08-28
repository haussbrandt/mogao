import tempfile
import uuid
from pathlib import Path


TEMP_DIRECTORY = Path(tempfile.gettempdir()) / "mogao"


def new_temp_path(suffix: str) -> Path:
    """Return a unique path inside Mogao's system temporary directory."""
    TEMP_DIRECTORY.mkdir(parents=True, exist_ok=True)
    return TEMP_DIRECTORY / f"{uuid.uuid4()}{suffix}"
