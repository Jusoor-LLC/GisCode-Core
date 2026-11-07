from abc import ABC, abstractmethod


class ToolExecutor(ABC):
    @abstractmethod
    def execute(self, tool_name: str, args: dict) -> dict:
        pass

    @abstractmethod
    def list_tools(self) -> list[str]:
        pass

    @abstractmethod
    def is_sandboxed(self) -> bool:
        pass
