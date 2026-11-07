from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Optional


class PackageStatus(Enum):
    UNCONFIGURED = "unconfigured"
    CONFIGURED = "configured"
    BUILD_PENDING = "build_pending"
    BUILD_SUCCEEDED = "build_succeeded"
    BUILD_FAILED = "build_failed"


class SigningStatus(Enum):
    UNSIGNED = "unsigned"
    PREREQUISITES_UNAVAILABLE = "prerequisites_unavailable"
    SIGNING_COMPLETED = "signing_completed"
    VERIFICATION_PASSED = "verification_passed"
    VERIFICATION_FAILED = "verification_failed"


@dataclass
class PackagingConfig:
    app_name: str = "GisCode"
    version: str = "0.1.0"
    publisher: str = "GisCode"
    description: str = "Local-first AI coding agent"
    entry_point: str = "giscode.main:run"
    executable_name: str = "giscode"
    output_dir: Path = Path("dist")
    artifacts_dir: Path = Path("build")
    identity_name: str = "GisCode.LocalAI"
    identity_publisher: str = "GisCode"
    package_type: str = "msix"
    min_windows_build: str = "10.0.17763.0"

    def __post_init__(self) -> None:
        self.output_dir = Path(self.output_dir).expanduser().resolve()
        self.artifacts_dir = Path(self.artifacts_dir).expanduser().resolve()

    def to_metadata(self) -> dict:
        return {
            "app_name": self.app_name,
            "version": self.version,
            "publisher": self.publisher,
            "description": self.description,
            "entry_point": self.entry_point,
            "executable_name": self.executable_name,
            "identity_name": self.identity_name,
            "identity_publisher": self.identity_publisher,
            "package_type": self.package_type,
            "min_windows_build": self.min_windows_build,
        }


@dataclass
class SigningConfig:
    enabled: bool = False
    certificate_path: Optional[Path] = None
    certificate_password: Optional[str] = None
    timestamp_server: str = "http://timestamp.digicert.com"
    signature_algorithm: str = "sha256"

    def __post_init__(self) -> None:
        if self.certificate_path is not None:
            self.certificate_path = Path(self.certificate_path).expanduser().resolve()
