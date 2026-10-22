# Modified for TaleWeft on 2026-10-22: independent default directory, legacy env compatibility.
"""Environment-backed application data directory configuration."""
from __future__ import annotations

import os
import sys
from dataclasses import dataclass
from pathlib import Path


TALEWEFT_PROD_DATA_DIR_ENV = "TALEWEFT_PROD_DATA_DIR"
PLOTPILOT_PROD_DATA_DIR_ENV = "PLOTPILOT_PROD_DATA_DIR"
LEGACY_PROD_DATA_DIR_ENV = "AITEXT_PROD_DATA_DIR"
TAURI_APP_IDENTIFIER = "com.taleweft.desktop"


@dataclass(frozen=True)
class DataDirectoryEnvironmentSettings:
    """Typed view of desktop/runtime data directory environment variables."""

    prod_data_dir: str = ""
    legacy_prod_data_dir: str = ""
    appdata_dir: str = ""
    platform: str = sys.platform
    frozen: bool = False
    app_identifier: str = TAURI_APP_IDENTIFIER
    taleweft_prod_data_dir: str = ""

    @classmethod
    def from_env(cls) -> "DataDirectoryEnvironmentSettings":
        return cls(
            taleweft_prod_data_dir=(os.getenv(TALEWEFT_PROD_DATA_DIR_ENV, "") or "").strip(),
            prod_data_dir=(os.getenv(PLOTPILOT_PROD_DATA_DIR_ENV, "") or "").strip(),
            legacy_prod_data_dir=(os.getenv(LEGACY_PROD_DATA_DIR_ENV, "") or "").strip(),
            appdata_dir=(os.getenv("APPDATA", "") or "").strip(),
            platform=sys.platform,
            frozen=bool(getattr(sys, "frozen", False)),
        )

    @property
    def prod_data_dir_raw(self) -> str:
        return self.taleweft_prod_data_dir or self.prod_data_dir or self.legacy_prod_data_dir

    def frozen_fallback_data_dir(self, home: Path | None = None) -> Path:
        base_home = home or Path.home()
        if self.platform == "win32":
            base = Path(self.appdata_dir) if self.appdata_dir else base_home / "AppData" / "Roaming"
            return base / self.app_identifier / "data"
        if self.platform == "darwin":
            return base_home / "Library" / "Application Support" / self.app_identifier / "data"
        return base_home / ".local" / "share" / self.app_identifier / "data"
