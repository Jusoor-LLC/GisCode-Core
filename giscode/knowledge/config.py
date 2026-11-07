from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

from giscode.config.paths import giscode_home, profile_root
from giscode.knowledge.errors import PathSafetyError


@dataclass
class KnowledgeStoreConfig:
    storage_path: Optional[Path] = None
    default_namespace: str = "default"
    max_record_size: int = 1024 * 1024
    max_query_length: int = 4096
    default_top_k: int = 10
    max_top_k: int = 100
    enable_deduplication: bool = True
    enable_content_hash: bool = True

    def __post_init__(self):
        if self.storage_path is None:
            self.storage_path = giscode_home() / "knowledge.db"
        else:
            self.storage_path = Path(self.storage_path).expanduser().resolve()
            try:
                self.storage_path.relative_to(profile_root())
            except ValueError:
                raise PathSafetyError(f"Storage path is outside home directory: {self.storage_path}")
