"""Credential precedence must work in wheels without changing editable clients."""
import pytest

from gipper import config


@pytest.fixture
def credential_files(tmp_path, monkeypatch):
    root = tmp_path / "checkout"
    user = tmp_path / "user-config"
    root.mkdir()
    user.mkdir()
    (root / ".env").write_text("RUNPOD_API_KEY=legacy-placeholder")
    (user / ".env").write_text("RUNPOD_API_KEY=user-placeholder")
    monkeypatch.setattr(config, "ROOT", root)
    monkeypatch.setattr(config, "CONFIG_FILE", user / "config.toml")
    monkeypatch.delenv("GIPPER_ENV", raising=False)
    monkeypatch.delenv("RUNPOD_API_KEY", raising=False)
    return root, user


def test_user_config_overrides_checkout(credential_files):
    assert config.secret("RUNPOD_API_KEY") == "user-placeholder"
    assert config.redact("key=user-placeholder") == "key=<redacted>"


def test_checkout_remains_compatible(credential_files):
    _, user = credential_files
    (user / ".env").unlink()
    assert config.secret("RUNPOD_API_KEY") == "legacy-placeholder"


def test_environment_wins(credential_files, monkeypatch):
    monkeypatch.setenv("RUNPOD_API_KEY", "environment-placeholder")
    def unexpected_read(path):
        raise AssertionError("an environment key must not read dotenv files")
    monkeypatch.setattr(config, "_dotenv", unexpected_read)
    assert config.secret("RUNPOD_API_KEY") == "environment-placeholder"


def test_explicit_file_is_exclusive(credential_files, tmp_path, monkeypatch):
    selected = tmp_path / "selected.env"
    selected.write_text("VAST_API_KEY=explicit-placeholder")
    monkeypatch.setenv("GIPPER_ENV", str(selected))
    assert config.secret("VAST_API_KEY") == "explicit-placeholder"
    assert config.secret("RUNPOD_API_KEY", required=False) is None
    selected.unlink()
    assert config.secret("VAST_API_KEY", required=False) is None
    with pytest.raises(SystemExit, match="RUNPOD_API_KEY is not set"):
        config.secret("RUNPOD_API_KEY")
