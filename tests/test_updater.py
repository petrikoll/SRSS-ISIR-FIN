from contextlib import closing
from hashlib import sha256
import json
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest.mock import Mock, patch
import zipfile
import update_engine


class UpdaterTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix="isir-updater-test-")
        self.base = Path(self.temporary.name)
        self.storage = self.base / "ISIR-Kontrola"
        (self.storage / "data").mkdir(parents=True)
        (self.storage / "downloaded_documents").mkdir()
        self.document = self.storage / "downloaded_documents/test.pdf"
        self.document.write_bytes(b"synthetic original document")
        self.database = self.storage / "data/app.db"
        with closing(sqlite3.connect(self.database)) as db, db:
            db.execute("CREATE TABLE clients (id INTEGER PRIMARY KEY,first_name TEXT,last_name TEXT,birth_date TEXT)")
            db.execute("INSERT INTO clients VALUES (1,'Test','Klient','1980-01-01')")
        self.settings = self.storage / "data/settings.json"
        self.settings.write_text('{"gemini_api_key":"LOCAL_SYNTHETIC_KEY"}',encoding="utf-8")
        self.exe = self.storage / update_engine.EXE_NAME
        self.exe.write_bytes(b"MZ synthetic old exe")
        (self.storage / "reset_data.cmd").write_bytes(b"old reset")
        (self.storage / "uninstall.cmd").write_bytes(b"old uninstall")
        self.payload = self.base / "payload.zip"
        self.new_exe = b"MZ synthetic new exe"
        with zipfile.ZipFile(self.payload,"w") as archive:
            archive.writestr("BUILD.json",json.dumps({"version":"1.3","exe_sha256":sha256(self.new_exe).hexdigest()}))
            archive.writestr(update_engine.EXE_NAME,self.new_exe)
            archive.writestr("reset_data.cmd",b"new reset")
            archive.writestr("uninstall.cmd",b"new uninstall")
        self.stop = Mock()
        self.launch = Mock(return_value=Mock())
        self.verify = Mock()

    def tearDown(self):
        self.temporary.cleanup()

    def run_update(self):
        return update_engine.perform_update(self.storage,self.exe,self.payload,stop=self.stop,launch=self.launch,verify=self.verify)

    def test_success_keeps_documents_database_settings_and_backs_up_old_exe(self):
        before_db = self.database.read_bytes()
        before_settings = self.settings.read_bytes()
        backup = self.run_update()
        self.assertEqual(self.exe.read_bytes(), self.new_exe)
        self.assertEqual(self.database.read_bytes(),before_db)
        self.assertEqual(self.settings.read_bytes(),before_settings)
        self.assertEqual(self.document.read_bytes(),b"synthetic original document")
        self.assertEqual(backup.parent,self.base / "ISIR-Kontrola-zalohy")
        with zipfile.ZipFile(backup) as archive:
            self.assertEqual(archive.read(update_engine.EXE_NAME),b"MZ synthetic old exe")
            self.assertEqual(archive.read("data/settings.json"),before_settings)
            self.assertEqual(archive.read("downloaded_documents/test.pdf"),self.document.read_bytes())
            self.assertIsNone(archive.testzip())
        self.stop.assert_called_once_with(self.exe)
        self.launch.assert_called_once()
        self.verify.assert_called_once()

    def test_invalid_payload_does_not_stop_or_change_application(self):
        self.payload.write_bytes(b"invalid zip")
        with self.assertRaises(zipfile.BadZipFile):
            self.run_update()
        self.stop.assert_not_called()
        self.assertEqual(self.exe.read_bytes(),b"MZ synthetic old exe")

    def test_failed_backup_restarts_old_application_without_replacing_program(self):
        with patch.object(update_engine,"write_backup",side_effect=OSError("synthetic full disk")):
            with self.assertRaises(OSError):
                self.run_update()
        self.assertEqual(self.exe.read_bytes(),b"MZ synthetic old exe")
        self.launch.assert_called_once()

    def test_failed_start_rolls_back_program_and_migrated_database(self):
        def fail(storage, process, previous_state):
            with closing(sqlite3.connect(self.database)) as db, db:
                db.execute("UPDATE clients SET first_name='bad migration'")
            raise RuntimeError("synthetic startup failure")
        self.verify.side_effect = fail
        with self.assertRaisesRegex(RuntimeError,"Původní program i databáze byly vráceny"):
            self.run_update()
        self.assertEqual(self.exe.read_bytes(),b"MZ synthetic old exe")
        self.assertEqual((self.storage / "reset_data.cmd").read_bytes(),b"old reset")
        with closing(sqlite3.connect(self.database)) as db:
            self.assertEqual(db.execute("SELECT first_name FROM clients").fetchone()[0],"Test")
        self.assertEqual(self.stop.call_count,2)
        self.assertEqual(self.launch.call_count,2)

    def test_stop_failure_never_overwrites_program(self):
        self.stop.side_effect = PermissionError("synthetic denied")
        with self.assertRaises(PermissionError):
            self.run_update()
        self.assertEqual(self.exe.read_bytes(),b"MZ synthetic old exe")
        self.launch.assert_not_called()

    def test_backup_captures_wal_committed_rows(self):
        backup = self.base / "backup.zip"
        with closing(sqlite3.connect(self.database)) as db:
            db.execute("PRAGMA journal_mode=WAL")
            db.execute("INSERT INTO clients VALUES (2,'WAL','Klient','1981-01-01')")
            db.commit()
            update_engine.write_backup(self.storage,self.exe,backup)
        with zipfile.ZipFile(backup) as archive:
            restored=self.base / "restored.db"
            restored.write_bytes(archive.read("data/app.db"))
            self.assertNotIn("data/app.db-wal",archive.namelist())
        with closing(sqlite3.connect(restored)) as db:
            self.assertEqual(db.execute("SELECT count(*) FROM clients").fetchone()[0],2)

    def test_detection_finds_installed_app_and_refuses_multiple_installations(self):
        with patch.object(update_engine,"app_processes",return_value=[]):
            self.assertEqual(update_engine.installation_exe(self.storage),self.exe)
        with patch.object(update_engine,"app_processes",return_value=[{"ExecutablePath":str(self.exe)},{"ExecutablePath":str(self.base / "other/ISIR-Kontrola.exe")}]):
            with self.assertRaisesRegex(RuntimeError,"více různých"):
                update_engine.installation_exe(self.storage)

    def test_process_inventory_uses_utf8_for_czech_windows_names(self):
        entry = {"ProcessId": 1, "ExecutablePath": "C:/Users/Uživatel/ISIR-Kontrola.exe"}
        with patch.object(update_engine.subprocess, "run", return_value=Mock(stdout=json.dumps(entry,ensure_ascii=False))) as run:
            self.assertEqual(update_engine.app_processes(), [entry])
        self.assertEqual(run.call_args.kwargs["encoding"], "utf-8")
        self.assertIn("OutputEncoding",run.call_args.args[0][-1])

    def test_backup_collision_never_removes_existing_backup(self):
        destination = self.base / "existing-backup.zip"
        original = b"existing backup that must be preserved"
        destination.write_bytes(original)
        with self.assertRaises(FileExistsError):
            update_engine.write_backup(self.storage, self.exe, destination)
        self.assertEqual(destination.read_bytes(), original)


if __name__ == "__main__":
    unittest.main()
