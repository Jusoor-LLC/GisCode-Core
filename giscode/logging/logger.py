import logging
import sys
from logging.handlers import RotatingFileHandler
from pathlib import Path
from typing import Optional

from giscode.config.paths import giscode_home


# Log hygiene. A short preview is enough to identify which request produced a
# record, and file contents must never reach the log, so anything logged goes
# through the sanitizer below.
TASK_TEXT_PREVIEW_CHARS = 100
MAX_MESSAGE_CHARS = 1000
DEFAULT_MAX_BYTES = 1024 * 1024
DEFAULT_BACKUP_COUNT = 3


def preview_text(text: Optional[str], limit: int = TASK_TEXT_PREVIEW_CHARS) -> str:
    """Return a log-safe preview of task text.

    Collapses whitespace and keeps only the first ``limit`` characters so a
    request can be identified without copying a whole prompt into the log.
    """
    flat = " ".join((text or "").split())
    if len(flat) <= limit:
        return flat
    return flat[:limit] + "..."


class LogSanitizer(logging.Filter):
    """Bound every record so no record can carry a whole file or prompt.

    Attached to handlers rather than to the logger so it also applies to
    records propagated from child loggers such as ``giscode.gui.*``.
    """

    def filter(self, record: logging.LogRecord) -> bool:
        try:
            message = record.getMessage()
        except Exception:
            return True
        if isinstance(message, str) and len(message) > MAX_MESSAGE_CHARS:
            dropped = len(message) - MAX_MESSAGE_CHARS
            record.msg = message[:MAX_MESSAGE_CHARS] + f"... [truncated {dropped} chars]"
            record.args = ()
        return True


def default_log_file() -> Path:
    """Rotating log location inside the user's profile."""
    return giscode_home() / "logs" / "giscode.log"


def setup_logger(
    name: str = "giscode",
    level: int = logging.INFO,
    log_file: Optional[Path] = None,
    max_bytes: int = DEFAULT_MAX_BYTES,
    backup_count: int = DEFAULT_BACKUP_COUNT,
) -> logging.Logger:
    logger = logging.getLogger(name)
    logger.setLevel(level)

    if logger.handlers:
        return logger

    formatter = logging.Formatter(
        "%(asctime)s | %(levelname)-8s | %(name)s | %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )

    # A windowed build has no usable stdout/stderr; adding the console handler
    # there would raise on every emit and could take the app down.
    console_stream = sys.stdout or sys.stderr
    if console_stream is not None:
        console_handler = logging.StreamHandler(console_stream)
        console_handler.setLevel(level)
        console_handler.setFormatter(formatter)
        console_handler.addFilter(LogSanitizer())
        logger.addHandler(console_handler)

    if log_file:
        log_file = Path(log_file)
        log_file.parent.mkdir(parents=True, exist_ok=True)
        file_handler = RotatingFileHandler(
            log_file,
            maxBytes=max_bytes,
            backupCount=backup_count,
            encoding="utf-8",
        )
        file_handler.setLevel(level)
        file_handler.setFormatter(formatter)
        file_handler.addFilter(LogSanitizer())
        logger.addHandler(file_handler)

    return logger


def get_logger(name: str = "giscode") -> logging.Logger:
    return logging.getLogger(name)