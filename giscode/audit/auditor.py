from abc import ABC, abstractmethod
from datetime import datetime


class Auditor(ABC):
    @abstractmethod
    def record(self, event: str, data: dict, timestamp: datetime | None = None) -> dict:
        pass

    @abstractmethod
    def get_trail(self, task_id: str) -> list[dict]:
        pass

    @abstractmethod
    def export(self, task_id: str, format: str = "json") -> str:
        pass
