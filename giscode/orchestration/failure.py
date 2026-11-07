import hashlib
import re
from typing import List, Optional

from giscode.orchestration.types import FailureRecord


class FailureCapture:
    def __init__(self):
        self._seen_fingerprints: set = set()

    def capture(
        self,
        failure_type: str,
        command: str,
        exit_code: int = -1,
        stderr: str = "",
        stdout: str = "",
        summary: str = "",
        relevant_path: Optional[str] = None,
        attempt_number: int = 1,
    ) -> FailureRecord:
        fingerprint = self._compute_fingerprint(failure_type, command, exit_code, stderr, summary)
        record = FailureRecord(
            failure_type=failure_type,
            command=command,
            exit_code=exit_code,
            stderr=stderr,
            stdout=stdout,
            summary=summary or self._generate_summary(failure_type, stderr, exit_code),
            relevant_path=relevant_path,
            attempt_number=attempt_number,
            fingerprint=fingerprint,
        )
        self._seen_fingerprints.add(fingerprint)
        return record

    def is_duplicate(self, record: FailureRecord) -> bool:
        return record.fingerprint in self._seen_fingerprints

    def _compute_fingerprint(self, failure_type: str, command: str, exit_code: int, stderr: str, summary: str) -> str:
        normalized = f"{failure_type}:{command}:{exit_code}:{stderr[:500]}:{summary[:200]}"
        return hashlib.sha256(normalized.encode()).hexdigest()[:16]

    def _generate_summary(self, failure_type: str, stderr: str, exit_code: int) -> str:
        stderr_snippet = stderr[:200].replace("\n", " ")
        return f"{failure_type} (exit {exit_code}): {stderr_snippet}"
