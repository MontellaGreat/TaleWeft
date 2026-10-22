# Modified for TaleWeft on 2026-10-22: independent branding; upstream attribution retained.
from pathlib import Path

from infrastructure.runtime.data_directory_environment import (
    DataDirectoryEnvironmentSettings,
)


def test_data_directory_environment_reads_primary_and_legacy(monkeypatch):
    monkeypatch.delenv("TALEWEFT_PROD_DATA_DIR", raising=False)
    monkeypatch.setenv("PLOTPILOT_PROD_DATA_DIR", "/primary/data")
    monkeypatch.setenv("AITEXT_PROD_DATA_DIR", "/legacy/data")

    settings = DataDirectoryEnvironmentSettings.from_env()

    assert settings.prod_data_dir == "/primary/data"
    assert settings.legacy_prod_data_dir == "/legacy/data"
    assert settings.prod_data_dir_raw == "/primary/data"


def test_data_directory_environment_uses_legacy_when_primary_missing(monkeypatch):
    monkeypatch.delenv("TALEWEFT_PROD_DATA_DIR", raising=False)
    monkeypatch.delenv("PLOTPILOT_PROD_DATA_DIR", raising=False)
    monkeypatch.setenv("AITEXT_PROD_DATA_DIR", "/legacy/data")

    settings = DataDirectoryEnvironmentSettings.from_env()

    assert settings.prod_data_dir_raw == "/legacy/data"


def test_frozen_fallback_data_dir_windows_uses_appdata():
    settings = DataDirectoryEnvironmentSettings(
        appdata_dir="C:/Users/me/AppData/Roaming",
        platform="win32",
    )

    assert (
        settings.frozen_fallback_data_dir(Path("C:/Users/me"))
        == Path("C:/Users/me/AppData/Roaming") / "com.taleweft.desktop" / "data"
    )


def test_frozen_fallback_data_dir_posix():
    settings = DataDirectoryEnvironmentSettings(platform="linux")

    assert (
        settings.frozen_fallback_data_dir(Path("/home/me"))
        == Path("/home/me") / ".local" / "share" / "com.taleweft.desktop" / "data"
    )


def test_taleweft_override_wins_without_dropping_legacy_settings(monkeypatch):
    monkeypatch.setenv("TALEWEFT_PROD_DATA_DIR", " /new/data ")
    monkeypatch.setenv("PLOTPILOT_PROD_DATA_DIR", "/old/data")
    monkeypatch.setenv("AITEXT_PROD_DATA_DIR", "/legacy/data")
    settings = DataDirectoryEnvironmentSettings.from_env()
    assert settings.prod_data_dir_raw == "/new/data"
    assert settings.prod_data_dir == "/old/data"


def test_blank_taleweft_override_uses_existing_override(monkeypatch):
    monkeypatch.setenv("TALEWEFT_PROD_DATA_DIR", "  ")
    monkeypatch.setenv("PLOTPILOT_PROD_DATA_DIR", "/old/data")
    assert DataDirectoryEnvironmentSettings.from_env().prod_data_dir_raw == "/old/data"


def test_frozen_default_matches_desktop_identity():
    import json
    config = Path(__file__).resolve().parents[4] / "frontend/src-tauri/tauri.conf.json"
    identifier = json.loads(config.read_text(encoding="utf-8"))["identifier"]
    settings = DataDirectoryEnvironmentSettings(platform="darwin")
    assert settings.app_identifier == identifier
    assert settings.frozen_fallback_data_dir(Path("/Users/me")) == Path("/Users/me/Library/Application Support") / identifier / "data"
