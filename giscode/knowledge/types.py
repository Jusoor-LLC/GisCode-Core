from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from typing import Any, Optional


class KnowledgeSourceType(Enum):
    CODE = "code"
    DOCUMENTATION = "documentation"
    ERROR = "error"
    FIX = "fix"
    TEST_RESULT = "test_result"
    BUILD_RESULT = "build_result"
    TASK = "task"
    PLAN = "plan"
    OPENHANDS_RESULT = "openhands_result"
    OCR = "ocr"
    USER_KNOWLEDGE = "user_knowledge"
    CHAT = "chat"
    AGENT = "agent"


@dataclass
class KnowledgeRecord:
    record_id: str
    content: str
    source: str
    source_type: KnowledgeSourceType
    namespace: str
    project_id: Optional[str] = None
    metadata: dict = field(default_factory=dict)
    created_at: str = field(default_factory=lambda: datetime.now().isoformat())
    updated_at: str = field(default_factory=lambda: datetime.now().isoformat())
    content_hash: Optional[str] = None
    parent_id: Optional[str] = None
    tags: list[str] = field(default_factory=list)
    embedding: Optional[list[float]] = None


@dataclass
class FailureRecord:
    failure_id: str
    error_signature: str
    error_text: str
    phase: str
    context: str
    project_id: str
    namespace: str
    timestamp: str = field(default_factory=lambda: datetime.now().isoformat())
    metadata: dict = field(default_factory=dict)


@dataclass
class FixRecord:
    fix_id: str
    failure_id: str
    attempted_fix: str
    result: str
    verification_status: str
    project_id: str
    namespace: str
    timestamp: str = field(default_factory=lambda: datetime.now().isoformat())
    metadata: dict = field(default_factory=dict)


@dataclass
class RetrievalResult:
    record: KnowledgeRecord
    score: float
    retrieval_metadata: dict = field(default_factory=dict)
