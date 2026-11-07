import json
from pathlib import Path
from typing import Any

from giscode.config.paths import giscode_home


class ConfigManager:
    def __init__(self, config_path: Path | None = None):
        if config_path is None:
            config_dir = giscode_home()
            config_dir.mkdir(parents=True, exist_ok=True)
            config_path = config_dir / "config.json"
        self.config_path = config_path

    def load(self) -> dict[str, Any]:
        if not self.config_path.exists():
            return {}
        try:
            with open(self.config_path, "r", encoding="utf-8") as f:
                return json.load(f)
        except (json.JSONDecodeError, OSError):
            return {}

    def save(self, data: dict[str, Any]) -> None:
        self.config_path.parent.mkdir(parents=True, exist_ok=True)
        with open(self.config_path, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2)

    def get(self, key: str, default: Any = None) -> Any:
        data = self.load()
        keys = key.split(".")
        value = data
        for k in keys:
            if isinstance(value, dict) and k in value:
                value = value[k]
            else:
                return default
        return value

    def set(self, key: str, value: Any) -> None:
        data = self.load()
        keys = key.split(".")
        target = data
        for k in keys[:-1]:
            if k not in target or not isinstance(target[k], dict):
                target[k] = {}
            target = target[k]
        target[keys[-1]] = value
        self.save(data)
