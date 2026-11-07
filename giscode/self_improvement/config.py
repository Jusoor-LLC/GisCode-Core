from dataclasses import dataclass
from typing import Optional


@dataclass
class SelfImprovementConfig:
    enabled: bool = True
    namespace: str = "default"
    max_retrieved_patterns: int = 5
    min_verification_status: str = "verified_pair"
    dataset_export_path: str = "build/self_improvement_dataset.json"
    knowledge_store_integration: bool = True
