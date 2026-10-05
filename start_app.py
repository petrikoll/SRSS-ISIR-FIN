from __future__ import annotations

import os
import socket
import subprocess
import sys
import threading
import time
import traceback
import webbrowser
import ctypes
import json
from ctypes import wintypes
from pathlib import Path
from shutil import which


APP_HOST = "127.0.0.1"
LOG_FILE_NAME = "startup.log"
STATE_FILE_NAME = "server-state.json"
MUTEX_NAME = "Local\\ISIRKontrolaSingleInstance"
ERROR_ALREADY_EXISTS = 183


def _app_base_dir() -> Path:
    from storage_paths import app_base_dir
    return app_base_dir()


def _find_free_port(host: str = APP_HOST) -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as server_socket:
        server_socket.bind((host, 0))
        return int(server_socket.getsockname()[1])


def _app_url(port: int) -> str:
    return f"http://{APP_HOST}:{port}"


def _write_log(base_dir: Path, message: str) -> None:
    timestamp = time.strftime("%Y-%m-%d %H:%M:%S")
    with (base_dir / LOG_FILE_NAME).open("a", encoding="utf-8") as log_file:
        log_file.write(f"[{timestamp}] {message}\n")


def _read_state(base_dir: Path) -> dict:
    try:
        with (base_dir / STATE_FILE_NAME).open("r", encoding="utf-8") as state_file:
            state = json.load(state_file)
            return state if isinstance(state, dict) else {}
    except (OSError, json.JSONDecodeError):
        return {}


def _write_state(base_dir: Path, port: int) -> None:
    state = {
        "pid": os.getpid(),
        "port": port,
        "url": _app_url(port),
        "started_at": time.strftime("%Y-%m-%d %H:%M:%S"),
    }
    with (base_dir / STATE_FILE_NAME).open("w", encoding="utf-8") as state_file:
        json.dump(state, state_file)


def _create_single_instance_mutex(base_dir: Path):
    create_mutex = ctypes.windll.kernel32.CreateMutexW
    create_mutex.argtypes = [ctypes.c_void_p, wintypes.BOOL, wintypes.LPCWSTR]
    create_mutex.restype = wintypes.HANDLE
    # Retain the legacy name so versions 1.2 and 1.3 cannot write concurrently.
    mutex = create_mutex(None, False, MUTEX_NAME)
    if not mutex:
        raise ctypes.WinError()
    already_running = ctypes.windll.kernel32.GetLastError() == ERROR_ALREADY_EXISTS
    return mutex, already_running


def _open_existing_instance(base_dir: Path, open_browser: bool = True) -> bool:
    state = _read_state(base_dir)
    try:
        port = int(state.get("port", 0))
    except (TypeError, ValueError):
        return False

    if not port or not _wait_for_server(port, timeout=2):
        return False

    _write_log(base_dir, f"Aplikace uz bezi na {_app_url(port)}. Oteviram existujici instanci.")
    if open_browser and not _open_app_window(port):
        _open_browser_when_ready(port)
    return True


def _wait_for_server(port: int, host: str = APP_HOST, timeout: int = 20) -> bool:
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            with socket.create_connection((host, port), timeout=1):
                return True
        except OSError:
            time.sleep(0.3)
    return False


def _open_browser_when_ready(port: int) -> None:
    if _wait_for_server(port):
        webbrowser.open(_app_url(port))


def _app_path_from_registry(exe_name: str) -> Path | None:
    try:
        import winreg

        registry_path = rf"SOFTWARE\Microsoft\Windows\CurrentVersion\App Paths\{exe_name}"
        with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, registry_path) as key:
            value, _ = winreg.QueryValueEx(key, "")
            path = Path(value)
            return path if path.exists() else None
    except OSError:
        return None


def _browser_app_paths() -> list[Path]:
    msedge_path = which("msedge")
    chrome_path = which("chrome")
    candidates = [
        Path(chrome_path) if chrome_path else None,
        _app_path_from_registry("chrome.exe"),
        Path(os.environ.get("ProgramFiles", "")) / "Google" / "Chrome" / "Application" / "chrome.exe",
        Path(os.environ.get("ProgramFiles(x86)", "")) / "Google" / "Chrome" / "Application" / "chrome.exe",
        Path(os.environ.get("LOCALAPPDATA", "")) / "Google" / "Chrome" / "Application" / "chrome.exe",
        Path(msedge_path) if msedge_path else None,
        _app_path_from_registry("msedge.exe"),
        Path(os.environ.get("ProgramFiles", "")) / "Microsoft" / "Edge" / "Application" / "msedge.exe",
        Path(os.environ.get("ProgramFiles(x86)", "")) / "Microsoft" / "Edge" / "Application" / "msedge.exe",
        Path(os.environ.get("LOCALAPPDATA", "")) / "Microsoft" / "Edge" / "Application" / "msedge.exe",
    ]
    unique_paths = []
    for path in candidates:
        if path and path.exists() and path not in unique_paths:
            unique_paths.append(path)
    return unique_paths


def _open_app_window(port: int) -> bool:
    if not _wait_for_server(port):
        return False

    for browser_path in _browser_app_paths():
        subprocess.Popen(
            [
                str(browser_path),
                f"--app={_app_url(port)}",
                "--new-window",
            ],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        return True
    return False


def _run_server(port: int) -> None:
    from app import app
    from waitress import serve

    serve(app, host=APP_HOST, port=port, threads=8)


def main(open_browser: bool = True) -> None:
    base_dir = _app_base_dir()
    base_dir.mkdir(parents=True, exist_ok=True)
    os.chdir(base_dir)
    (base_dir / "data").mkdir(parents=True, exist_ok=True)

    mutex, already_running = _create_single_instance_mutex(base_dir)
    try:
        if already_running:
            deadline = time.monotonic() + 20
            while time.monotonic() < deadline:
                if _open_existing_instance(base_dir, open_browser=open_browser):
                    return
                time.sleep(0.3)
            raise RuntimeError("Jiná instance aplikace již běží, ale neodpovídá. Ukončete ISIR-Kontrola.exe ve Správci úloh a spusťte aplikaci znovu.")

        _write_log(base_dir, "Spoustim ISIR Kontrola.")
        port = _find_free_port()
        _write_log(base_dir, f"Vybrany port: {port}.")

        server_errors = []

        def server_worker():
            try:
                _run_server(port)
            except Exception:
                server_errors.append(traceback.format_exc())
                _write_log(base_dir, "Chyba serveru:\n" + server_errors[-1])

        threading.Thread(target=server_worker, daemon=True).start()
        if not _wait_for_server(port):
            raise RuntimeError("Flask server se nepodařilo spustit. Podrobnosti jsou v startup.log.")

        _write_state(base_dir, port)
        _write_log(base_dir, f"Server bezi na {_app_url(port)}.")
        if open_browser and not _open_app_window(port):
            _open_browser_when_ready(port)
        while True:
            time.sleep(1)
            if server_errors:
                raise RuntimeError("Server aplikace se neočekávaně ukončil. Podrobnosti jsou v startup.log.")
    except Exception:
        _write_log(base_dir, "Chyba pri startu aplikace:")
        _write_log(base_dir, traceback.format_exc())
        raise

    finally:
        close_handle = ctypes.windll.kernel32.CloseHandle
        close_handle.argtypes = [wintypes.HANDLE]
        close_handle.restype = wintypes.BOOL
        close_handle(mutex)


if __name__ == "__main__":
    main(open_browser="--headless" not in sys.argv)
