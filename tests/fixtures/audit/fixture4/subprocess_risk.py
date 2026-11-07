#!/usr/bin/env python
"""Project with subprocess call using shell=True (security risk)."""

import subprocess
import os
import tempfile


def run_with_shell_true() -> str:
    """Function that uses subprocess with shell=True - risky pattern."""
    # This is a security risk - shell=True can lead to injection
    result = subprocess.run(
        "echo 'Hello from shell'",
        shell=True,
        capture_output=True,
        text=True,
    )
    return result.stdout


def run_without_shell() -> str:
    """Safe alternative - no shell=True."""
    result = subprocess.run(
        ["echo", "Hello from safe mode"],
        capture_output=True,
        text=True,
    )
    return result.stdout


def risky_path_operation() -> str:
    """Function that builds path from external input without validation."""
    user_input = os.environ.get("USER", "unknown")
    # Dangerous: building path from external input without sanitization
    temp_dir = f"/tmp/{user_input}/malicious"
    try:
        with open(temp_dir, "w") as f:
            f.write("dangerous write")
    except Exception:
        pass
    return temp_dir


def safe_path_operation() -> str:
    """Safe version with validation."""
    user_input = os.environ.get("USER", "unknown")
    # Safe: validate/path-sanitize before using
    safe_name = user_input.replace("/", "_").replace("\\", "_")
    temp_dir = f"/tmp/{safe_name}/safe"
    try:
        with open(temp_dir, "w") as f:
            f.write("safe write")
    except Exception:
        pass
    return temp_dir


if __name__ == "__main__":
    print("Running with shell=True (risky):")
    print(run_with_shell_true())
    print()
    print("Running without shell=True (safe):")
    print(run_without_shell())
    print()
    print("Risky path operation:")
    print(risky_path_operation())
    print()
    print("Safe path operation:")
    print(safe_path_operation())