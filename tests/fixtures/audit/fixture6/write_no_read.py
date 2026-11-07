"""Project with a write path that has no corresponding read path."""

import os
from pathlib import Path


def write_log_without_read() -> str:
    """Write to a log file, but never read it back."""
    project_root = Path(__file__).parent.parent / "fixture6_project"
    
    # Write path - log file creation
    log_file = project_root / "app.log"
    log_file.write_text(
        "Application started\n"
        "User logged in\n"
        "Operation completed\n",
        encoding="utf-8",
    )
    
    # Note: There is NO read path for this log file anywhere in the codebase.
    # The log is written but never read for any purpose.
    # This is a "write without read" pattern that can lead to:
    # - Unnecessary disk I/O
    # - Log bloat without analysis
    # - Potential data loss if the log is the only record
    
    # Simulate another write operation
    error_log = project_root / "error.log"
    error_log.write_text(
        "Division by zero at line 42\n",
        encoding="utf-8",
    )
    
    return str(project_root)


def read_error_log() -> str | None:
    """Attempt to read the error log - this function exists but may not be called."""
    error_log = Path("/app/logs/error.log")  # Wrong path, different location
    if error_log.exists():
        return error_log.read_text(encoding="utf-8")
    return None


def main() -> None:
    result = write_log_without_read()
    read_data = read_error_log()
    print(f"Write result: {result}")
    if read_data:
        print(f"Read data: {read_data[:50]}...")
    else:
        print("No read path found for the error log")


if __name__ == "__main__":
    main()