from abc import ABC, abstractmethod


class AgentLoop(ABC):
    @abstractmethod
    def run(self, task: str) -> str:
        pass

    @abstractmethod
    def stop(self) -> None:
        pass

    @abstractmethod
    def is_running(self) -> bool:
        pass
