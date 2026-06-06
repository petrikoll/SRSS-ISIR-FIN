from __future__ import annotations

import os
import sys
from pathlib import Path


APP_DIR_NAME = "ISIR-Kontrola"


def app_base_dir() -> Path:
    if getattr(sys, "frozen", False):
        local_app_data = Path(os.environ.get("LOCALAPPDATA", "")).expanduser()
        if local_app_data:
            return local_app_data / APP_DIR_NAME
    return Path(__file__).resolve().parent


BASE_DIR = app_base_dir()
DATA_DIR = BASE_DIR / "data"
DOCUMENTS_DIR = BASE_DIR / "downloaded_documents"
