"""Project with a "success" verifier that only checks file existence, not actual tests."""

import os
import tempfile
from pathlib import Path


def verify_success_only_checks_file_existence() -> bool:
    """
    "Verification" that only checks if files exist, not if tests pass.
    
    This simulates a false sense of security where the verifier claims success
    just because some files are present, without actually running the test suite.
    """
    project_root = Path(__file__).parent.parent / "fixture5_project"
    
    # Check that certain files exist (this is ALL the "verification" does)
    required_files = [
        "some_module.py",
        "config.json",
        "data.csv",
    ]
    
    all_exist = True
    for fname in required_files:
        fpath = project_root / fname
        if not fpath.exists():
            all_exist = False
            # In a real audit, this would be flagged, but the fake verifier
            # just says "files exist" without checking test results
    
    # Fake verifier: if files exist, return True (even if tests would fail)
    if all_exist:
        # The "verifier" does NOT run actual tests - it just checks file presence
        # This is insufficient and gives a false positive
        return True
    else:
        return False


def actual_test_run() -> bool:
    """Real test run that actually executes the test suite."""
    # This would actually run pytest or similar
    # For this fixture, we just check that the test infrastructure exists
    test_dir = project_root / "tests"
    if test_dir.exists():
        # Real verification would run the tests here
        # and check actual pass/fail status
        test_files = list(test_dir.glob("test_*.py"))
        if test_files:
            # Even if test files exist, we don't run them
            # The fake verifier would just say "tests exist = success"
            return True
    return False


def fake_success_verifier() -> dict[str, any]:
    """
    Fake verifier that gives a false sense of security.
    
    Returns a "success" report based solely on file existence,
    without actually running tests or checking behavior.
    """
    files_ok = verify_success_only_checks_file_existence()
    actual_ok = actual_test_run()
    
    # The fake report only mentions file existence
    report = {
        "status": "success" if files_ok else "failure",
        "message": "Verification complete" if files_ok else "Files missing",
        "files_checked": ["some_module.py", "config.json", "data.csv"],
        # NOTE: Does NOT mention test results at all!
    }
    
    return report