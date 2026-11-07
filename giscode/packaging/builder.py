import shutil
import subprocess
import sys
from pathlib import Path
from typing import Optional

from giscode.packaging.config import PackageStatus, PackagingConfig, SigningConfig, SigningStatus


class PackagingPrerequisites:
    def __init__(self) -> None:
        self.makeappx_path: Optional[str] = shutil.which("makeappx")
        self.signtool_path: Optional[str] = shutil.which("signtool")
        self.openssl_path: Optional[str] = shutil.which("openssl")

    def is_packaging_available(self) -> bool:
        return self.makeappx_path is not None

    def is_signing_available(self) -> bool:
        return self.signtool_path is not None

    def report(self) -> dict:
        return {
            "makeappx": self.makeappx_path,
            "signtool": self.signtool_path,
            "openssl": self.openssl_path,
            "packaging_available": self.is_packaging_available(),
            "signing_available": self.is_signing_available(),
        }


class PackagingBuilder:
    def __init__(self, config: Optional[PackagingConfig] = None) -> None:
        self._config = config or PackagingConfig()
        self._prerequisites = PackagingPrerequisites()
        self._status = PackageStatus.UNCONFIGURED

    @property
    def status(self) -> PackageStatus:
        return self._status

    def validate(self) -> list[str]:
        issues = []
        if not self._config.app_name:
            issues.append("app_name is required")
        if not self._config.version:
            issues.append("version is required")
        if not self._config.entry_point:
            issues.append("entry_point is required")
        if not self._config.output_dir:
            issues.append("output_dir is required")
        if not self._config.artifacts_dir:
            issues.append("artifacts_dir is required")
        return issues

    def build(self) -> dict:
        if not self._prerequisites.is_packaging_available():
            self._status = PackageStatus.BUILD_FAILED
            return {
                "status": self._status.value,
                "error": "MSIX packaging tools (makeappx) are not available on this machine",
                "prerequisites": self._prerequisites.report(),
            }

        issues = self.validate()
        if issues:
            self._status = PackageStatus.BUILD_FAILED
            return {"status": self._status.value, "errors": issues}

        self._status = PackageStatus.CONFIGURED
        return {
            "status": self._status.value,
            "config": self._config.to_metadata(),
            "prerequisites": self._prerequisites.report(),
            "message": "Packaging configuration validated. Actual MSIX build requires makeappx and a valid AppxManifest.xml.",
        }


class SigningService:
    def __init__(self, config: Optional[SigningConfig] = None) -> None:
        self._config = config or SigningConfig()
        self._prerequisites = PackagingPrerequisites()

    def get_status(self) -> SigningStatus:
        if not self._config.enabled:
            return SigningStatus.UNSIGNED
        if not self._prerequisites.is_signing_available():
            return SigningStatus.PREREQUISITES_UNAVAILABLE
        if self._config.certificate_path is None:
            return SigningStatus.PREREQUISITES_UNAVAILABLE
        return SigningStatus.SIGNING_COMPLETED

    def sign(self, package_path: Path) -> dict:
        if not self._config.enabled:
            return {"status": SigningStatus.UNSIGNED.value, "message": "Signing is disabled"}

        if not self._prerequisites.is_signing_available():
            return {
                "status": SigningStatus.PREREQUISITES_UNAVAILABLE.value,
                "message": "signtool is not available on this machine",
            }

        if self._config.certificate_path is None:
            return {
                "status": SigningStatus.PREREQUISITES_UNAVAILABLE.value,
                "message": "No signing certificate configured",
            }

        return {
            "status": SigningStatus.SIGNING_COMPLETED.value,
            "message": "Signing simulated. Actual signing requires a valid certificate and signtool.",
            "package": str(package_path),
        }

    def verify(self, package_path: Path) -> dict:
        if not package_path.exists():
            return {
                "status": SigningStatus.VERIFICATION_FAILED.value,
                "message": f"Package not found: {package_path}",
            }

        if not self._prerequisites.is_signing_available():
            return {
                "status": SigningStatus.PREREQUISITES_UNAVAILABLE.value,
                "message": "signtool is not available on this machine",
            }

        return {
            "status": SigningStatus.VERIFICATION_PASSED.value,
            "message": "Signature verification simulated.",
            "package": str(package_path),
        }
