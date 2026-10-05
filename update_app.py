"""One double-click to update an installed desktop without Task Manager."""
import ctypes
from ctypes import wintypes
import json
from pathlib import Path
import queue
import sys
import threading
import traceback

from update_engine import installation_exe, local_storage, perform_update


def payload_path():
    root = Path(sys._MEIPASS) if getattr(sys, "frozen", False) else Path(__file__).resolve().parent / "build"
    return root / "payload/ISIR-Kontrola-1.3-aktualizace.zip"


def acquire_updater_mutex():
    kernel = ctypes.windll.kernel32
    kernel.CreateMutexW.argtypes = [ctypes.c_void_p, wintypes.BOOL, wintypes.LPCWSTR]
    kernel.CreateMutexW.restype = wintypes.HANDLE
    handle = kernel.CreateMutexW(None, False, "Local\\ISIRKontrolaUpdater")
    if not handle:
        raise ctypes.WinError()
    return handle, kernel.GetLastError() == 183


def log_error(storage):
    directory = storage.parent / "ISIR-Kontrola-zalohy"
    directory.mkdir(parents=True, exist_ok=True)
    with (directory / "aktualizace.log").open("a", encoding="utf-8") as output:
        output.write(traceback.format_exc() + "\n")


def show_window():
    import tkinter as tk
    from tkinter import ttk, messagebox
    window = tk.Tk()
    window.title("Aktualizace ISIR Kontrola")
    window.resizable(False, False)
    area = ttk.Frame(window, padding=24)
    area.pack(fill="both", expand=True)
    ttk.Label(area, text="Aktualizovat ISIR Kontrola na verzi 1.3", font=("Segoe UI", 14, "bold")).pack(anchor="w", pady=(0, 14))
    ttk.Label(area, text="Program sám ukončí aplikaci, vytvoří zálohu a znovu ji spustí.\nKlienti, dokumenty i nastavení zůstanou zachováni.\n\nPřed aktualizací uložte rozpracované úpravy.\nProbíhající kontroly a AI úlohy se přeruší.\nZáloha s daty a nastavením se uloží jen na tomto PC.", font=("Segoe UI", 10), justify="left").pack(anchor="w")
    status = tk.StringVar(value="Připraveno. Stačí kliknout na Aktualizovat.")
    ttk.Label(area, textvariable=status, wraplength=520, font=("Segoe UI", 10)).pack(anchor="w", pady=(18, 8))
    progress = ttk.Progressbar(area, mode="indeterminate", length=520)
    progress.pack(fill="x", pady=(0, 12))
    buttons = ttk.Frame(area)
    buttons.pack(fill="x")
    messages = queue.Queue()
    active = False

    def close():
        if active:
            messagebox.showinfo("Probíhá aktualizace", "Prosím vyčkejte na dokončení aktualizace.", parent=window)
        else:
            window.destroy()

    def worker():
        storage = None
        try:
            storage = local_storage()
            executable = installation_exe(storage)
            backup = perform_update(storage, executable, payload_path(), notify=lambda value: messages.put(("status", value)))
            messages.put(("done", str(backup)))
        except Exception as error:
            if storage is not None:
                try:
                    log_error(storage)
                except OSError:
                    pass
            messages.put(("error", str(error)))

    def begin():
        nonlocal active
        active = True
        update_button.configure(state="disabled")
        close_button.configure(state="disabled")
        progress.start(15)
        threading.Thread(target=worker, daemon=True).start()

    def poll():
        nonlocal active
        try:
            while True:
                kind, text = messages.get_nowait()
                if kind == "status":
                    status.set(text)
                else:
                    active = False
                    progress.stop()
                    close_button.configure(state="normal")
                    if kind == "done":
                        status.set("Hotovo. Aplikace je znovu spuštěná.\nZáloha původního stavu: " + text)
                        messagebox.showinfo("Aktualizace dokončena", "ISIR Kontrola byla aktualizována a znovu spuštěna.\nKlienti a dokumenty zůstali zachováni.", parent=window)
                    else:
                        status.set("Aktualizace nebyla dokončena.")
                        update_button.configure(state="normal")
                        messagebox.showerror("Aktualizace nebyla dokončena", text, parent=window)
        except queue.Empty:
            pass
        window.after(100, poll)

    update_button = ttk.Button(buttons, text="Aktualizovat", command=begin)
    update_button.pack(side="left")
    close_button = ttk.Button(buttons, text="Zavřít", command=close)
    close_button.pack(side="right")
    window.protocol("WM_DELETE_WINDOW", close)
    window.after(100, poll)
    window.mainloop()


def main():
    handle, existing = acquire_updater_mutex()
    try:
        if existing:
            if "--headless" not in sys.argv:
                ctypes.windll.user32.MessageBoxW(None, "Aktualizátor již běží. Pokračujte v jeho otevřeném okně.", "ISIR Kontrola", 0x40)
            return
        if "--headless" in sys.argv:
            storage = local_storage()
            try:
                backup = perform_update(storage, installation_exe(storage), payload_path(), headless=True)
                (storage.parent / "ISIR-Kontrola-zalohy/result.json").write_text(json.dumps({"success": True, "backup": str(backup)}), encoding="utf-8")
            except Exception:
                log_error(storage)
                raise
        else:
            show_window()
    finally:
        kernel = ctypes.windll.kernel32
        kernel.CloseHandle.argtypes = [wintypes.HANDLE]
        kernel.CloseHandle.restype = wintypes.BOOL
        kernel.CloseHandle(handle)


if __name__ == "__main__":
    main()
