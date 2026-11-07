from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import Optional, Sequence


@dataclass
class SystemResources:
    total_vram_bytes: int = 0
    available_vram_bytes: int = 0
    total_ram_bytes: int = 0
    available_ram_bytes: int = 0
    gpu_name: Optional[str] = None


class ResourceBudget(ABC):
    @abstractmethod
    def can_load(self, model_size_bytes: int, context_length: int) -> bool:
        pass

    @abstractmethod
    def get_system_resources(self) -> SystemResources:
        pass

    @abstractmethod
    def estimate_model_memory(self, model_size_bytes: int, context_length: int) -> int:
        pass


class ResourceBudgetChecker:
    def __init__(self, budget: ResourceBudget):
        self._budget = budget

    def check(self, model_size_bytes: int, context_length: int) -> bool:
        return self._budget.can_load(model_size_bytes, context_length)

    def estimate(self, model_size_bytes: int, context_length: int) -> int:
        return self._budget.estimate_model_memory(model_size_bytes, context_length)


class ModelDiscovery(ABC):
    @abstractmethod
    def discover(self) -> Sequence[str]:
        pass
