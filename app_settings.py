from __future__ import annotations

import json
import os
import secrets

from storage_paths import DATA_DIR

SETTINGS_PATH = DATA_DIR / "settings.json"


def load_settings() -> dict:
    if not SETTINGS_PATH.exists():
        return {}
    try:
        return json.loads(SETTINGS_PATH.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}


def save_settings(settings: dict) -> None:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    SETTINGS_PATH.write_text(
        json.dumps(settings, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )


def get_gemini_api_key() -> str:
    env_key = os.environ.get("GEMINI_API_KEY", "").strip()
    if env_key:
        return env_key
    return str(load_settings().get("gemini_api_key", "")).strip()


def set_gemini_api_key(api_key: str) -> None:
    settings = load_settings()
    settings["gemini_api_key"] = api_key.strip()
    save_settings(settings)


def has_gemini_api_key() -> bool:
    return bool(get_gemini_api_key())


def get_secret_key() -> str:
    settings = load_settings()
    secret_key = str(settings.get("secret_key", "")).strip()
    if secret_key:
        return secret_key

    secret_key = secrets.token_urlsafe(48)
    settings["secret_key"] = secret_key
    save_settings(settings)
    return secret_key
