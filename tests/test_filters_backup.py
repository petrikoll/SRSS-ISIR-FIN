from contextlib import closing
"""Regression checks using only synthetic data in a temporary directory."""
from datetime import date
import gc
from io import BytesIO
import json
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest.mock import patch
import zipfile
from test_data_safety import AuditCases


class FiltersAndBackupTests(AuditCases, unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        import storage_paths

        cls.runtime = tempfile.TemporaryDirectory(prefix="isir-regression-")
        root = Path(cls.runtime.name)
        cls.paths = patch.multiple(
            storage_paths, BASE_DIR=root, DATA_DIR=root / "data",
            DOCUMENTS_DIR=root / "downloaded_documents",
        )
        cls.paths.start()
        import scheduler
        with patch.object(scheduler, "start_scheduler"):
            import app
        cls.module = app
        cls.module.app.config.update(TESTING=True)
        cls.root = root

    @classmethod
    def tearDownClass(cls):
        cls.module.engine.dispose()
        gc.collect()
        cls.paths.stop()
        cls.runtime.cleanup()

    def setUp(self):
        self.module.app.config["WTF_CSRF_ENABLED"] = False
        self.module.check_progress["state"] = "idle"
        with closing(sqlite3.connect(self.module.DATABASE_PATH)) as conn, conn:
            for table in ("document_extraction", "case_claims_review", "case_performance_report", "case_completion_report", "case_trustee_accounting"):
                conn.execute(f'DELETE FROM "{table}"')
        session = self.module.SessionLocal()
        try:
            for client in session.query(self.module.Client).all():
                session.delete(client)
            session.flush()
            session.add(self.module.Client(
                first_name="Test", last_name="Klient", birth_date=date(1980, 1, 1),
                project="SRSS II", insolvency_status="Nezkontrolováno",
            ))
            session.commit()
        finally:
            session.close()
        self.client = self.module.app.test_client()
        self.document = self.module.DOCUMENTS_DIR / "synthetic" / "document.pdf"
        self.document.parent.mkdir(parents=True, exist_ok=True)
        self.document.write_bytes(b"%PDF-1.4 synthetic regression document\n")
        (self.root / "data" / "manual_download_rules.json").write_text(
            json.dumps([{"synthetic_rule": True}]), encoding="utf-8",
        )
        (self.root / "data" / "settings.json").write_text(
            json.dumps({"gemini_api_key": "SYNTHETIC_SECRET_DO_NOT_EXPORT"}),
            encoding="utf-8",
        )

    def client_count(self):
        session = self.module.SessionLocal()
        try:
            return session.query(self.module.Client).count()
        finally:
            session.close()

    def test_deadline_empty_result_keeps_filters_and_reset_recovers_clients(self):
        response = self.client.get("/?deadline_filter=1&in_deadline=1")
        html = response.get_data(as_text=True)
        self.assertEqual(response.status_code, 200)
        self.assertIn("Filtru neodpovídá žádný klient.", html)
        self.assertIn('class="status-filter"', html)
        self.assertIn('name="in_deadline"', html)
        self.assertIn("Zrušit všechny filtry", html)
        self.assertIn("Stáhnout zálohu dat (ZIP)", html)
        reset = self.client.get("/?clear_status=1&clear_project=1")
        self.assertIn("Klient Test", reset.get_data(as_text=True))
        self.assertIn("Klient Test", self.client.get("/").get_data(as_text=True))
        self.assertEqual(self.client_count(), 1)

    def test_empty_project_filter_can_be_cleared(self):
        html = self.client.get("/?project_filter=1&project=SRSS+III").get_data(as_text=True)
        self.assertIn("Filtru neodpovídá žádný klient.", html)
        self.assertIn('class="status-filter"', html)
        html = self.client.get("/?clear_status=1&clear_project=1").get_data(as_text=True)
        self.assertIn("Klient Test", html)

    def test_last_status_unchecked_does_not_reuse_cookie(self):
        self.client.get("/?status_filter=1&status=Neexistujici")
        html = self.client.get("/?status_filter=1&deadline_filter=1").get_data(as_text=True)
        self.assertIn("Klient Test", html)
        self.assertEqual(self.client.get_cookie("index_statuses").value, "")
        self.assertEqual(self.client_count(), 1)

    def test_empty_status_does_not_silently_show_other_clients(self):
        html = self.client.get("/?status=Neexistujici").get_data(as_text=True)
        self.assertIn("Filtru neodpovídá žádný klient.", html)
        self.assertNotIn('class="client-link"', html)
        self.assertIn("Zrušit všechny filtry", html)
        self.assertEqual(self.client_count(), 1)

    def test_settings_offer_backup_and_offline_recovery(self):
        html = self.client.get("/settings").get_data(as_text=True)
        self.assertIn("Stáhnout zálohu dat (ZIP)", html)
        self.assertIn("Jak obnovit data po přeinstalaci", html)
        self.assertIn("app.db-wal", html)

    def test_backup_contains_live_wal_database_documents_rules_but_no_secrets(self):
        with closing(sqlite3.connect(self.module.DATABASE_PATH)) as live, live:
            live.execute("PRAGMA journal_mode=WAL")
            live.execute("UPDATE clients SET last_found_change = 'synthetic WAL history'")
            live.commit()
            document_before = self.document.read_bytes()
            response = self.client.get("/data/export")
            self.assertEqual(response.status_code, 200)
            self.assertIn("attachment", response.headers["Content-Disposition"])
            with zipfile.ZipFile(BytesIO(response.data)) as archive:
                self.assertIsNone(archive.testzip())
                self.assertIn("data/app.db", archive.namelist())
                self.assertIn("data/manual_download_rules.json", archive.namelist())
                self.assertEqual(
                    archive.read("downloaded_documents/synthetic/document.pdf"), document_before,
                )
                self.assertNotIn("data/settings.json", archive.namelist())
                self.assertNotIn(b"SYNTHETIC_SECRET_DO_NOT_EXPORT", response.data)
                restored = self.root / "roundtrip.db"
                restored.write_bytes(archive.read("data/app.db"))
                with closing(sqlite3.connect(restored)) as restored_db, restored_db:
                    self.assertEqual(restored_db.execute("PRAGMA integrity_check").fetchone()[0], "ok")
                    row = restored_db.execute(
                        "SELECT first_name, last_name, project, last_found_change FROM clients",
                    ).fetchone()
                    self.assertEqual(row, ("Test", "Klient", "SRSS II", "synthetic WAL history"))
            response.close()
        self.assertEqual(self.document.read_bytes(), document_before)
        self.assertEqual(self.client_count(), 1)

    def test_export_failure_leaves_original_data_and_no_partial_archive(self):
        exports = self.root / "exports"
        before_archives = set(exports.glob("*.zip")) if exports.exists() else set()
        db_before = self.module.DATABASE_PATH.read_bytes()
        doc_before = self.document.read_bytes()
        with patch.object(self.module, "add_documents_to_zip", side_effect=OSError("synthetic disk error")):
            with self.assertLogs(self.module.app.logger, level="ERROR"):
                response = self.client.get("/data/export")
        self.assertEqual(response.status_code, 302)
        self.assertIn("error=", response.headers["Location"])
        self.assertEqual(set(exports.glob("*.zip")), before_archives)
        self.assertEqual(self.module.DATABASE_PATH.read_bytes(), db_before)
        self.assertEqual(self.document.read_bytes(), doc_before)

    def test_two_backups_are_distinct_and_outside_working_directory(self):
        first = self.module.create_data_backup()
        second = self.module.create_data_backup()
        self.assertNotEqual(first, second)
        self.assertEqual(first.parent, self.root / "exports")
        self.assertTrue(first.exists())
        self.assertTrue(second.exists())


if __name__ == "__main__":
    unittest.main()
