from __future__ import annotations

import json
import os
import secrets
import tempfile
import threading

from storage_paths import DATA_DIR

SETTINGS_PATH = DATA_DIR / "settings.json"
settings_lock = threading.RLock()


def load_settings() -> dict:
    if not SETTINGS_PATH.exists():
        return {}
    try:
        settings = json.loads(SETTINGS_PATH.read_text(encoding="utf-8"))
        if not isinstance(settings, dict):
            raise ValueError("Nastavení musí být objekt JSON.")
        return settings
    except (OSError, ValueError) as exc:
        raise RuntimeError("Soubor data/settings.json je poškozený nebo nepřístupný. Původní soubor zůstal zachován; obnovte jej ze zálohy.") from exc


def save_settings(settings: dict) -> None:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    with settings_lock:
        temporary_path = None
        try:
            with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", dir=DATA_DIR, delete=False) as stream:
                temporary_path = stream.name
                json.dump(settings, stream, ensure_ascii=False, indent=2)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary_path, SETTINGS_PATH)
        finally:
            if temporary_path and os.path.exists(temporary_path):
                os.unlink(temporary_path)


def get_gemini_api_key() -> str:
    env_key = os.environ.get("GEMINI_API_KEY", "").strip()
    if env_key:
        return env_key
    return str(load_settings().get("gemini_api_key", "")).strip()


def set_gemini_api_key(api_key: str) -> None:
    with settings_lock:
        settings = load_settings()
        settings["gemini_api_key"] = api_key.strip()
        save_settings(settings)


def has_gemini_api_key() -> bool:
    return bool(get_gemini_api_key())


def get_secret_key() -> str:
    with settings_lock:
        settings = load_settings()
        secret_key = str(settings.get("secret_key", "")).strip()
        if secret_key:
            return secret_key
        secret_key = secrets.token_urlsafe(48)
        settings["secret_key"] = secret_key
        save_settings(settings)
        return secret_key
