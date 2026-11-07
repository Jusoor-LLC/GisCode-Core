from abc import ABC, abstractmethod


class Verifier(ABC):
    @abstractmethod
    def verify(self, artifact_path: str, criteria: dict) -> dict:
        pass

    @abstractmethod
    def check_syntax(self, code: str, language: str) -> dict:
        pass
