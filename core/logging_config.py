import logging
from copy import copy

from uvicorn.logging import AccessFormatter, DefaultFormatter


ANSI_RESET = "\033[0m"
TAG_COLORS = {
    "anki": "\033[36m",  # cyan
    "llm_processor": "\033[35m",  # magenta
    "build_dictionary": "\033[95m",  # bright magenta
    "book": "\033[34m",  # blue
    "library": "\033[34m",
    "video": "\033[32m",  # green
    "video_library": "\033[32m",
    "video_router": "\033[32m",
    "server": "\033[94m",  # bright blue
    "middleware": "\033[94m",
    "uvicorn": "\033[96m",  # bright cyan
    "uvicorn.error": "\033[96m",
    "uvicorn.asgi": "\033[36m",
    "uvicorn.access": "\033[92m",  # bright green
}

LOG_FORMAT = "%(asctime)s | %(levelprefix)s | [%(tag)s] %(message)s"
ACCESS_LOG_FORMAT = (
    '%(asctime)s | %(levelprefix)s | [%(tag)s] '
    '%(client_addr)s - "%(request_line)s" %(status_code)s'
)
LOG_DATE_FORMAT = "%Y-%m-%d %H:%M:%S"


def add_colored_tag(record: logging.LogRecord, use_colors: bool) -> logging.LogRecord:
    """Copy a log record and add its exact logger name as a colored tag."""
    record_copy = copy(record)
    tag = record.name
    color = TAG_COLORS.get(tag) or TAG_COLORS.get(tag.rsplit(".", 1)[-1])
    if use_colors and color:
        tag = f"{color}{tag}{ANSI_RESET}"
    record_copy.__dict__["tag"] = tag
    return record_copy


class TaggedDefaultFormatter(DefaultFormatter):
    def formatMessage(self, record: logging.LogRecord) -> str:
        return super().formatMessage(add_colored_tag(record, self.use_colors))


class TaggedAccessFormatter(AccessFormatter):
    def formatMessage(self, record: logging.LogRecord) -> str:
        return super().formatMessage(add_colored_tag(record, self.use_colors))


def configure_logging() -> None:
    """Configure consistent timestamped logging for Mogao and Uvicorn."""
    application_handler = logging.StreamHandler()
    application_handler.setFormatter(
        TaggedDefaultFormatter(LOG_FORMAT, datefmt=LOG_DATE_FORMAT)
    )

    root_logger = logging.getLogger()
    root_logger.handlers.clear()
    root_logger.addHandler(application_handler)
    root_logger.setLevel(logging.INFO)
    logging.captureWarnings(True)

    # Route Uvicorn's application messages through the root logger so they
    # share Mogao's format and retain Uvicorn's colored level names.
    uvicorn_logger = logging.getLogger("uvicorn")
    uvicorn_logger.handlers.clear()
    uvicorn_logger.propagate = True

    uvicorn_error_logger = logging.getLogger("uvicorn.error")
    uvicorn_error_logger.handlers.clear()
    uvicorn_error_logger.propagate = True

    # AccessFormatter additionally colors the request line and status code.
    access_handler = logging.StreamHandler()
    access_handler.setFormatter(
        TaggedAccessFormatter(ACCESS_LOG_FORMAT, datefmt=LOG_DATE_FORMAT)
    )
    uvicorn_access_logger = logging.getLogger("uvicorn.access")
    uvicorn_access_logger.handlers.clear()
    uvicorn_access_logger.addHandler(access_handler)
    uvicorn_access_logger.propagate = False
