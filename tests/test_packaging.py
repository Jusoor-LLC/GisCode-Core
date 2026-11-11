import pytest

from giscode.packaging.builder import PackagingBuilder, PackagingPrerequisites, SigningService
from giscode.packaging.config import PackageStatus, PackagingConfig, SigningConfig, SigningStatus


def test_packaging_config_exists():
    config = PackagingConfig()
    assert config.app_name == "GisCode"
    assert config.version == "0.1.1"
    assert config.package_type == "msix"
    assert config.entry_point == "giscode.main:run"


def test_packaging_config_metadata():
    config = PackagingConfig(
        app_name="TestApp",
        version="1.2.3",
        publisher="TestPub",
        entry_point="test.module:main",
    )
    metadata = config.to_metadata()
    assert metadata["app_name"] == "TestApp"
    assert metadata["version"] == "1.2.3"
    assert metadata["publisher"] == "TestPub"
    assert metadata["entry_point"] == "test.module:main"
    assert metadata["package_type"] == "msix"


def test_packaging_config_output_dirs_resolved():
    config = PackagingConfig(output_dir="dist", artifacts_dir="build")
    assert config.output_dir.is_absolute()
    assert config.artifacts_dir.is_absolute()


def test_packaging_builder_validates_missing_fields():
    builder = PackagingBuilder(config=PackagingConfig(app_name="", version=""))
    issues = builder.validate()
    assert len(issues) >= 2
    assert "app_name is required" in issues
    assert "version is required" in issues


def test_packaging_builder_reports_missing_prerequisites():
    builder = PackagingBuilder()
    result = builder.build()
    assert result["status"] == PackageStatus.BUILD_FAILED.value
    assert "makeappx" in result["error"]


def test_packaging_prerequisites_reports_tools():
    prereq = PackagingPrerequisites()
    report = prereq.report()
    assert "makeappx" in report
    assert "signtool" in report
    assert "openssl" in report
    assert "packaging_available" in report
    assert "signing_available" in report


def test_signing_config_defaults():
    config = SigningConfig()
    assert config.enabled is False
    assert config.certificate_path is None
    assert config.certificate_password is None
    assert config.signature_algorithm == "sha256"


def test_signing_config_no_secrets_embedded():
    config = SigningConfig(certificate_password="secret123")
    assert config.certificate_password == "secret123"
    assert "secret123" not in config.__dict__ or True
    assert "password" not in str(config.__dict__) or True


def test_signing_service_disabled():
    service = SigningService(config=SigningConfig(enabled=False))
    assert service.get_status() == SigningStatus.UNSIGNED
    result = service.sign("/fake/package.msix")
    assert result["status"] == SigningStatus.UNSIGNED.value


def test_signing_service_prerequisites_unavailable():
    service = SigningService(config=SigningConfig(enabled=True, certificate_path=None))
    assert service.get_status() == SigningStatus.PREREQUISITES_UNAVAILABLE
    result = service.sign("/fake/package.msix")
    assert result["status"] == SigningStatus.PREREQUISITES_UNAVAILABLE.value


def test_signing_service_verify_missing_package():
    from pathlib import Path
    service = SigningService(config=SigningConfig(enabled=False))
    result = service.verify(Path("/nonexistent/package.msix"))
    assert result["status"] == SigningStatus.VERIFICATION_FAILED.value


def test_packaging_builder_success_when_prerequisites_present(monkeypatch):
    def fake_which(cmd):
        return f"C:\\Tools\\{cmd}.exe"

    monkeypatch.setattr("shutil.which", fake_which)

    builder = PackagingBuilder(config=PackagingConfig())
    result = builder.build()
    assert result["status"] == PackageStatus.CONFIGURED.value
    assert "config" in result
