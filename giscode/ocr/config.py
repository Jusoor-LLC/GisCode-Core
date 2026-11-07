from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

from giscode.config.paths import giscode_home
from giscode.ocr.types import OCREngineType


@dataclass
class OCRConfig:
    engine: OCREngineType = OCREngineType.MOCK
    default_language: str = "eng"
    max_input_size: int = 10 * 1024 * 1024
    timeout: float = 30.0
    preprocessing_enabled: bool = False
    temp_dir: Optional[Path] = None
    enable_screen_capture: bool = False
    enable_clipboard: bool = False

    def __post_init__(self):
        if self.temp_dir is None:
            self.temp_dir = giscode_home() / "ocr_temp"
        else:
            self.temp_dir = Path(self.temp_dir).expanduser().resolve()
