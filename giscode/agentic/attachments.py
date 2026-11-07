"""Attachment metadata shared by core and GisAI.

Core owns the data shape: the GUI builds these and the orchestrators carry
them, so neither side may need the other to describe an attachment.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime


@dataclass
class Attachment:
    path: str
    mime_type: str
    size_bytes: int
    attached_at: datetime = field(default_factory=datetime.now)
