from dataclasses import dataclass, field
from typing import Optional


@dataclass
class ContextBudget:
    max_context_chars: int = 4096
    max_records: int = 10
    max_record_chars: int = 1024
    max_query_length: int = 1024


@dataclass
class RoutingConfig:
    default_fallback_policy: str = "strict"
    context_budget: ContextBudget = field(default_factory=ContextBudget)
    enable_routing: bool = True
    enable_context_assembly: bool = True
