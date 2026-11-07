"""Fix 6: the app writes a bounded rotating log file.

setup_logger() was called with no log_file, and a windowed build has no console,
so nothing at all was recorded. Task text must stay short in the log and file
contents must never be written.
"""

from __future__ import annotations

import logging
from pathlib import Path

import pytest

from giscode.logging.logger import (
    MAX_MESSAGE_CHARS,
    TASK_TEXT_PREVIEW_CHARS,
    LogSanitizer,
    default_log_file,
    preview_text,
    setup_logger,
)

LONG_BLOB = "SECRET_FILE_CONTENT_" * 400


@pytest.fixture
def log_file(tmp_path) -> Path:
    return tmp_path / "logs" / "giscode.log"


def _fresh_logger(name: str, log_file: Path, **kwargs) -> logging.Logger:
    logger = logging.getLogger(name)
    logger.handlers.clear()
    logger.filters.clear()
    return setup_logger(name, logging.DEBUG, log_file, **kwargs)


def test_default_log_file_is_under_the_profile():
    path = default_log_file()

    assert path.name == "giscode.log"
    assert path.parent.name == "logs"
    assert path.parent.parent.name == ".giscode"


def test_log_file_is_created(log_file):
    logger = _fresh_logger("t_create", log_file)
    logger.info("hello from GisCode")
    for handler in logger.handlers:
        handler.flush()

    assert log_file.exists()
    assert "hello from GisCode" in log_file.read_text(encoding="utf-8")


def test_log_file_directory_is_created(log_file):
    assert not log_file.parent.exists()

    _fresh_logger("t_mkdir", log_file).info("created")

    assert log_file.parent.is_dir()


def test_long_messages_are_truncated_so_file_contents_never_land(log_file):
    logger = _fresh_logger("t_truncate", log_file)
    logging.getLogger("t_truncate.child").info("file contents: %s", LONG_BLOB)
    for handler in logger.handlers:
        handler.flush()

    text = log_file.read_text(encoding="utf-8")
    assert LONG_BLOB not in text
    assert "truncated" in text
    assert max(len(line) for line in text.splitlines()) < MAX_MESSAGE_CHARS + 200


def test_rotation_keeps_the_log_bounded(log_file):
    logger = _fresh_logger(
        "t_rotate", log_file, max_bytes=1024, backup_count=2
    )
    for index in range(400):
        logger.info("bounded log line %d", index)
    for handler in logger.handlers:
        handler.flush()

    siblings = sorted(log_file.parent.glob("giscode.log*"))
    assert log_file.parent.exists()
    assert len(siblings) <= 3, f"rotation must not keep more than 1 + backup_count files: {siblings}"
    assert all(path.stat().st_size <= 4096 for path in siblings)


def test_preview_text_keeps_only_the_first_100_characters():
    text = "Inspect the GisCode project " * 40

    preview = preview_text(text)

    assert len(preview) <= TASK_TEXT_PREVIEW_CHARS + 3
    assert preview.startswith("Inspect the GisCode project")
    assert preview.endswith("...")


def test_preview_text_flattens_whitespace():
    assert preview_text("a\n\n  b\tc") == "a b c"


def test_preview_text_handles_short_and_empty_text():
    assert preview_text("short") == "short"
    assert preview_text("") == ""
    assert preview_text(None) == ""


def test_sanitizer_keeps_short_records_untouched():
    record = logging.LogRecord(
        "t", logging.INFO, __file__, 1, "a normal message", None, None
    )

    assert LogSanitizer().filter(record) is True
    assert record.getMessage() == "a normal message"


def test_bootstrap_wires_a_log_file(monkeypatch):
    """bootstrap() must actually pass a log file to setup_logger()."""
    import giscode.main as main

    captured: dict = {}

    def _fake_setup_logger(**kwargs):
        captured.update(kwargs)
        raise RuntimeError("stop-here")

    monkeypatch.setattr(main, "setup_logger", _fake_setup_logger)

    with pytest.raises(RuntimeError):
        main.bootstrap()

    assert captured.get("log_file") is not None
    assert Path(captured["log_file"]).name == "giscode.log"