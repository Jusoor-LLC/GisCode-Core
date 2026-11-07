from abc import ABC, abstractmethod


class InferenceEngine(ABC):
    @abstractmethod
    def generate(self, prompt: str, **kwargs) -> str:
        pass

    @abstractmethod
    def is_available(self) -> bool:
        pass

    @abstractmethod
    def unload(self) -> None:
        pass
