from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from typing import Any, Optional


class FixStatus(Enum):
    FAILED_ATTEMPT = "failed_attempt"
    CANDIDATE_FIX = "candidate_fix"
    VERIFIED_PAIR = "verified_pair"


@dataclass
class FailFixPair:
    pair_id: str
    namespace: str
    project_id: Optional[str] = None
    task_id: Optional[str] = None
    failure_type: str = ""
    error_signature: str = ""
    error_text: str = ""
    command: str = ""
    exit_code: int = -1
    fix_proposal: str = ""
    files_changed: list[str] = field(default_factory=list)
    verification_result: str = ""
    fix_succeeded: bool = False
    status: FixStatus = FixStatus.CANDIDATE_FIX
    created_at: str = field(default_factory=lambda: datetime.now().isoformat())
    updated_at: str = field(default_factory=lambda: datetime.now().isoformat())
    metadata: dict = field(default_factory=dict)
    failure_record_id: Optional[str] = None
    fix_record_id: Optional[str] = None
    knowledge_record_id: Optional[str] = None


@dataclass
class LearningEvent:
    event_type: str
    pair_id: str
    status: str
    message: str
    timestamp: str = field(default_factory=lambda: datetime.now().isoformat())
    namespace: str = "default"
    project_id: Optional[str] = None
    metadata: dict = field(default_factory=dict)
