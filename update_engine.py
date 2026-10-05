"""Offline Windows updater: only the selected program and its helper scripts change."""
from contextlib import closing
import ctypes
from ctypes import wintypes
from datetime import datetime
from hashlib import sha256
import json
import os
from pathlib import Path
import shutil
import sqlite3
import subprocess
import tempfile
import time
from urllib.request import build_opener, ProxyHandler
import zipfile

EXE_NAME = "ISIR-Kontrola.exe"


def local_storage():
    value = os.environ.get("LOCALAPPDATA", "").strip()
    if not value:
        raise RuntimeError("Nelze zjistit datové úložiště Windows.")
    return Path(value).resolve() / "ISIR-Kontrola"


def app_processes():
    command = "[Console]::OutputEncoding=[Text.UTF8Encoding]::new($false); Get-CimInstance Win32_Process -Filter \"Name = 'ISIR-Kontrola.exe'\" | Select-Object ProcessId,ExecutablePath | ConvertTo-Json -Compress"
    result = subprocess.run(["powershell.exe", "-NoProfile", "-Command", command], capture_output=True, encoding="utf-8", check=True, creationflags=subprocess.CREATE_NO_WINDOW)
    if not result.stdout.strip():
        return []
    entries = json.loads(result.stdout)
    return entries if isinstance(entries, list) else [entries]


def installation_exe(storage):
    entries = app_processes()
    paths = {Path(entry["ExecutablePath"]).resolve() for entry in entries if entry.get("ExecutablePath")}
    if len(paths) > 1:
        raise RuntimeError("Běží více různých instalací ISIR Kontrola. Aktualizace nebyla spuštěna; je potřeba určit správnou instalaci.")
    if paths:
        return paths.pop()
    candidate = storage / EXE_NAME
    if candidate.is_file():
        return candidate
    raise RuntimeError("Instalaci aplikace se nepodařilo najít. Spusťte původní ISIR Kontrola a poté tento aktualizátor znovu.")


def stop_application(executable):
    """Open stable process handles and check each executable before stopping it."""
    kernel = ctypes.windll.kernel32
    kernel.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    kernel.OpenProcess.restype = wintypes.HANDLE
    kernel.QueryFullProcessImageNameW.argtypes = [wintypes.HANDLE, wintypes.DWORD, wintypes.LPWSTR, ctypes.POINTER(wintypes.DWORD)]
    kernel.QueryFullProcessImageNameW.restype = wintypes.BOOL
    kernel.TerminateProcess.argtypes = [wintypes.HANDLE, wintypes.UINT]
    kernel.TerminateProcess.restype = wintypes.BOOL
    kernel.WaitForSingleObject.argtypes = [wintypes.HANDLE, wintypes.DWORD]
    kernel.WaitForSingleObject.restype = wintypes.DWORD
    kernel.CloseHandle.argtypes = [wintypes.HANDLE]
    kernel.CloseHandle.restype = wintypes.BOOL
    expected = os.path.normcase(str(executable.resolve()))
    for entry in app_processes():
        if not entry.get("ExecutablePath") or os.path.normcase(str(Path(entry["ExecutablePath"]).resolve())) != expected:
            continue
        handle = kernel.OpenProcess(0x1000 | 0x0001 | 0x00100000, False, int(entry["ProcessId"]))
        if not handle:
            # A one-file bootloader may exit when its child was just stopped.
            if ctypes.windll.kernel32.GetLastError() == 87:
                continue
            raise ctypes.WinError()
        try:
            if kernel.WaitForSingleObject(handle, 0) == 0:
                continue
            length = wintypes.DWORD(32768)
            name = ctypes.create_unicode_buffer(length.value)
            if not kernel.QueryFullProcessImageNameW(handle, 0, name, ctypes.byref(length)):
                raise ctypes.WinError()
            if os.path.normcase(str(Path(name.value).resolve())) != expected:
                raise RuntimeError("Proces aplikace se změnil. Aktualizace byla zastavena.")
            if not kernel.TerminateProcess(handle, 0):
                raise ctypes.WinError()
            if kernel.WaitForSingleObject(handle, 10000) != 0:
                raise RuntimeError("Aplikaci se nepodařilo automaticky ukončit.")
        finally:
            kernel.CloseHandle(handle)
    deadline = time.monotonic() + 10
    while time.monotonic() < deadline:
        remaining = [e for e in app_processes() if e.get("ExecutablePath") and os.path.normcase(str(Path(e["ExecutablePath"]).resolve())) == expected]
        if not remaining:
            return
        time.sleep(.2)
    raise RuntimeError("Aplikace stále běží; její program nebyl přepsán.")


def validate_payload(payload, stage):
    with zipfile.ZipFile(payload) as archive:
        metadata = json.loads(archive.read("BUILD.json"))
        if metadata.get("version") != "1.3":
            raise ValueError("Aktualizační balíček má nesprávnou verzi.")
        content = archive.read(EXE_NAME)
        if sha256(content).hexdigest() != metadata.get("exe_sha256"):
            raise ValueError("Aktualizační balíček je poškozený.")
        for name in (EXE_NAME, "reset_data.cmd", "uninstall.cmd"):
            target = stage / name
            with archive.open(name) as source, target.open("wb") as output:
                shutil.copyfileobj(source, output)
                output.flush()
                os.fsync(output.fileno())
        if not content.startswith(b"MZ"):
            raise ValueError("Balíček neobsahuje program pro Windows.")
    return metadata


def write_backup(storage, executable, destination):
    """The local recovery ZIP includes settings; it must never be uploaded."""
    database = storage / "data/app.db"
    if not database.is_file():
        raise RuntimeError("Databáze klientů nebyla nalezena. Automatická aktualizace byla zastavena.")
    temporary_db = None
    archive_created = False
    try:
        with tempfile.NamedTemporaryFile(dir=destination.parent, delete=False) as temporary:
            temporary_db = Path(temporary.name)
        with closing(sqlite3.connect(database.resolve().as_uri() + "?mode=ro", uri=True, timeout=30)) as source, closing(sqlite3.connect(temporary_db)) as target:
            source.backup(target)
            if target.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
                raise RuntimeError("Databáze neprošla kontrolou; aktualizace ji nezměnila.")
            columns = {row[1] for row in target.execute("PRAGMA table_info(clients)")}
            if not {"id", "first_name", "last_name", "birth_date"} <= columns:
                raise RuntimeError("Databáze nepatří aplikaci ISIR Kontrola.")
        with zipfile.ZipFile(destination, "x", zipfile.ZIP_STORED) as archive:
            archive_created = True
            archive.write(temporary_db, "data/app.db")
            archive.write(executable, EXE_NAME)
            for directory in (storage / "data", storage / "downloaded_documents"):
                if directory.is_dir():
                    for path in directory.rglob("*"):
                        if not path.is_file() or (path.parent == database.parent and path.name in {"app.db", "app.db-wal", "app.db-shm", "app.db-journal"}):
                            continue
                        if storage.resolve() not in path.resolve().parents:
                            raise ValueError("Datové úložiště obsahuje odkaz mimo složku aplikace.")
                        archive.write(path, path.relative_to(storage))
            for name in ("reset_data.cmd", "uninstall.cmd"):
                path = executable.parent / name
                if path.is_file():
                    archive.write(path, name)
            archive.writestr("README.txt", "Automatická lokální záloha před aktualizací ISIR Kontrola 1.3. Obsahuje klienty, dokumenty, nastavení/API klíč a původní EXE. Neposílejte ji na GitHub ani jinam veřejně.\n")
        with zipfile.ZipFile(destination) as archive:
            if archive.testzip() is not None:
                raise RuntimeError("Zálohu nelze ověřit; program nebyl změněn.")
    except BaseException:
        if archive_created:
            destination.unlink(missing_ok=True)
        raise
    finally:
        if temporary_db is not None:
            temporary_db.unlink(missing_ok=True)


def launch_application(executable, headless=False):
    command = [str(executable)] + (["--headless"] if headless else [])
    return subprocess.Popen(command, cwd=executable.parent, creationflags=subprocess.CREATE_NO_WINDOW)


def verify_start(storage, process, previous_state, timeout=65):
    state_path = storage / "server-state.json"
    opener = build_opener(ProxyHandler({}))
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if process.poll() is not None:
            raise RuntimeError("Aktualizovaná aplikace se neočekávaně ukončila.")
        try:
            raw = state_path.read_bytes()
            if raw == previous_state:
                time.sleep(.2)
                continue
            state = json.loads(raw)
            port = int(state["port"])
            if not 1 <= port <= 65535:
                raise ValueError("Neplatný port.")
            with opener.open(f"http://127.0.0.1:{port}/settings", timeout=3) as response:
                html = response.read(256 * 1024).decode("utf-8")
            if "verze 1.3" in html:
                return
        except (OSError, ValueError, KeyError):
            pass
        time.sleep(.3)
    raise RuntimeError("Aktualizovaná aplikace neodpovídá.")


def restore_previous(storage, executable, backup):
    with zipfile.ZipFile(backup) as archive:
        recovery = Path(tempfile.mkdtemp(prefix="navrat-", dir=backup.parent))
        for suffix in ("", "-wal", "-shm", "-journal"):
            current = storage / ("data/app.db" + suffix)
            if current.exists():
                current.replace(recovery / current.name)
        targets = {"data/app.db": storage / "data/app.db", EXE_NAME: executable}
        for name in ("reset_data.cmd", "uninstall.cmd", "data/settings.json"):
            if name in archive.namelist():
                targets[name] = storage / name if name.startswith("data/") else executable.parent / name
        for name, target in targets.items():
            with tempfile.NamedTemporaryFile(dir=target.parent, delete=False) as output:
                temp = Path(output.name)
                output.write(archive.read(name))
                output.flush()
                os.fsync(output.fileno())
            temp.replace(target)


def perform_update(storage, executable, payload, notify=lambda message: None, *, stop=stop_application, launch=launch_application, verify=verify_start, headless=False):
    storage = storage.resolve()
    executable = executable.resolve()
    backups = storage.parent / "ISIR-Kontrola-zalohy"
    backups.mkdir(parents=True, exist_ok=True)
    backup = backups / f"pred-aktualizaci-{datetime.now():%Y%m%d-%H%M%S-%f}.zip"
    stage = Path(tempfile.mkdtemp(prefix="isir-update-", dir=executable.parent))
    changed = False
    stopped = False
    previous_state = (storage / "server-state.json").read_bytes() if (storage / "server-state.json").exists() else b""
    try:
        notify("Ověřuji aktualizační balíček…")
        validate_payload(payload, stage)
        notify("Automaticky ukončuji běžící aplikaci…")
        stop(executable)
        stopped = True
        notify("Zálohuji klienty, dokumenty a nastavení. Prosím vyčkejte…")
        write_backup(storage, executable, backup)
        notify("Instaluji novou verzi…")
        (stage / EXE_NAME).replace(executable)
        changed = True
        for name in ("reset_data.cmd", "uninstall.cmd"):
            (stage / name).replace(executable.parent / name)
        notify("Znovu spouštím aplikaci…")
        process = launch(executable, headless=headless)
        verify(storage, process, previous_state)
        notify("Aktualizace je hotová. Klienti a dokumenty zůstali zachováni.")
        return backup
    except Exception as exc:
        if changed:
            notify("Aktualizace se nepodařila. Vracím původní program a databázi…")
            try:
                stop(executable)
                restore_previous(storage, executable, backup)
                launch(executable, headless=headless)
            except Exception as rollback_error:
                raise RuntimeError(f"Aktualizace selhala a automatický návrat nebyl dokončen. Záloha původního stavu je v {backup}. Požádejte o pomoc; data ručně nemažte.") from rollback_error
            raise RuntimeError(f"Aktualizace se nepodařila. Původní program i databáze byly vráceny. Záloha: {backup}") from exc
        if stopped:
            launch(executable, headless=headless)
        raise
    finally:
        if stage.resolve().parent == executable.parent.resolve() and stage.name.startswith("isir-update-"):
            try:
                shutil.rmtree(stage)
            except OSError:
                pass
