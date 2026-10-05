from contextlib import closing
"""Audit cases mixed into the isolated synthetic-data test fixture."""
from concurrent.futures import ThreadPoolExecutor
from datetime import date
from io import BytesIO
import json
from pathlib import Path
import sqlite3
import threading
from types import SimpleNamespace
from unittest.mock import Mock, patch
import zipfile
from werkzeug.datastructures import FileStorage


class AuditCases:
    def archive_upload(self, content):
        return FileStorage(stream=BytesIO(content), filename="backup.zip")

    def archive_bytes(self):
        return self.module.create_data_backup().read_bytes()

    def change_archive(self, content, changes):
        output = BytesIO()
        with zipfile.ZipFile(BytesIO(content)) as source, zipfile.ZipFile(output, "w") as target:
            for name in source.namelist():
                if name not in changes:
                    target.writestr(name, source.read(name))
            for name, data in changes.items():
                if data is not None:
                    target.writestr(name, data)
        return output.getvalue()

    def seed_case(self):
        session = self.module.SessionLocal()
        try:
            client = session.query(self.module.Client).first()
            case = self.module.InsolvencyCase(client=client, spisova_znacka="TEST INS 1/2026", ai_summary="Dřívější shrnutí", ai_case_study="Dřívější kazuistika")
            session.add(case)
            session.flush()
            document = self.module.InsolvencyDocument(case=case, title="Testovací dokument", document_type="hlavní dokument", source_url="https://example.invalid/document.pdf", local_path=str(self.document))
            session.add(document)
            session.commit()
            return client.id, case.id, document.id
        finally:
            session.close()

    def test_restore_roundtrip_relinks_other_pc_and_keeps_settings(self):
        _, case_id, document_id = self.seed_case()
        session = self.module.SessionLocal()
        document = session.get(self.module.InsolvencyDocument, document_id)
        document.local_path = "C:/AnotherPC/ISIR-Kontrola/downloaded_documents/synthetic/document.pdf"
        session.commit()
        session.close()
        archive = self.archive_bytes()
        settings_before = (self.root / "data/settings.json").read_bytes()
        original_document = self.document.read_bytes()
        self.document.write_bytes(b"current different content")
        backup, counts = self.module.restore_data_from_zip(self.archive_upload(archive))
        self.assertTrue(backup.exists())
        self.assertEqual(counts["clients"], 1)
        self.assertEqual(self.document.read_bytes(), original_document)
        self.assertEqual((self.root / "data/settings.json").read_bytes(), settings_before)
        session = self.module.SessionLocal()
        try:
            self.assertEqual(session.get(self.module.InsolvencyDocument, document_id).local_path, str(self.document))
            self.assertEqual(session.get(self.module.InsolvencyCase, case_id).ai_case_study, "Dřívější kazuistika")
        finally:
            session.close()

    def test_restore_rejects_invalid_archive_without_changing_data(self):
        original = self.archive_bytes()
        for archive in (
            b"not a ZIP",
            self.change_archive(original, {"data/app.db": b"invalid database"}),
            self.change_archive(original, {"data/app.db": None}),
            self.change_archive(original, {"../outside.txt": b"bad"}),
            self.change_archive(original, {"downloaded_documents/CON.txt": b"bad"}),
            self.change_archive(original, {"downloaded_documents/bad.pdf.": b"bad"}),
            self.change_archive(original, {"data/manual_download_rules.json": b"{}"}),
        ):
            with self.subTest(length=len(archive)):
                before = self.module.DATABASE_PATH.read_bytes()
                with self.assertRaises(Exception):
                    self.module.restore_data_from_zip(self.archive_upload(archive))
                self.assertEqual(self.module.DATABASE_PATH.read_bytes(), before)
                self.assertTrue(self.document.exists())
                self.assertEqual(self.client_count(), 1)

    def test_restore_rejects_missing_document(self):
        self.seed_case()
        archive = self.change_archive(self.archive_bytes(), {"downloaded_documents/synthetic/document.pdf": None})
        with self.assertRaisesRegex(ValueError, "chybí dokument"):
            self.module.restore_data_from_zip(self.archive_upload(archive))
        self.assertTrue(self.document.exists())

    def test_restore_failure_after_replacement_rolls_back_database_and_documents(self):
        self.seed_case()
        archive = self.archive_bytes()
        session = self.module.SessionLocal()
        session.query(self.module.Client).first().first_name = "Současný"
        session.commit()
        session.close()
        self.document.write_bytes(b"current original document")
        with patch.object(self.module, "relink_document_paths", side_effect=OSError("synthetic migration failure")):
            with self.assertRaisesRegex(ValueError, "původní data byla vrácena"):
                self.module.restore_data_from_zip(self.archive_upload(archive))
        session = self.module.SessionLocal()
        try:
            self.assertEqual(session.query(self.module.Client).first().first_name, "Současný")
        finally:
            session.close()
        self.assertEqual(self.document.read_bytes(), b"current original document")
        self.assertTrue(list((self.root / "exports").glob("pred-obnovou-*.zip")))

    def test_restore_preserves_rules_when_legacy_backup_has_none(self):
        archive = self.change_archive(self.archive_bytes(), {"data/manual_download_rules.json": None})
        path = self.root / "data/manual_download_rules.json"
        before = path.read_bytes()
        self.module.restore_data_from_zip(self.archive_upload(archive))
        self.assertEqual(path.read_bytes(), before)

    def test_queued_job_blocks_restore_export_and_mutations_but_allows_reading(self):
        from runtime_guard import coordinator, DataBusyError
        release = coordinator.reserve_task()
        try:
            with self.assertRaises(DataBusyError):
                self.module.restore_data_from_zip(self.archive_upload(b"anything"))
            self.assertEqual(self.client.get("/data/export").status_code, 503)
            self.assertEqual(self.client.get("/").status_code, 200)
            response = self.client.post("/data/clear")
            self.assertEqual(response.status_code, 302)
            self.assertIn("error=", response.location)
            self.assertEqual(self.client_count(), 1)
        finally:
            release()

    def test_maintenance_blocks_other_threads(self):
        from runtime_guard import DataCoordinator, DataBusyError
        control = DataCoordinator()
        def request():
            with control.operation():
                pass
        with control.maintenance(), ThreadPoolExecutor(1) as pool:
            with self.assertRaises(DataBusyError):
                pool.submit(request).result(timeout=2)

    def test_background_scheduler_serializes_and_waits_for_request_commit(self):
        from runtime_guard import coordinator, CoordinatedExecutor, add_background_job
        from apscheduler.schedulers.background import BackgroundScheduler
        scheduler = BackgroundScheduler(executors={"default": CoordinatedExecutor()})
        events = []
        started = threading.Event()
        finish = threading.Event()
        second_done = threading.Event()
        def first():
            events.append("first-start")
            started.set()
            finish.wait(3)
            events.append("first-end")
        scheduler.start()
        try:
            with coordinator.operation(mutating=True):
                add_background_job(scheduler, first, id="first")
                self.assertFalse(started.wait(.1))
            self.assertTrue(started.wait(2))
            def second():
                events.append("second")
                second_done.set()
            add_background_job(scheduler, second, id="second")
            self.assertNotIn("second", events)
            finish.set()
            self.assertTrue(second_done.wait(3))
        finally:
            finish.set()
            scheduler.shutdown(wait=True)
        self.assertEqual(events, ["first-start", "first-end", "second"])
        self.assertEqual(coordinator.pending_tasks, 0)

    def test_queue_failure_releases_reservation_and_progress(self):
        from runtime_guard import coordinator
        failed = Mock()
        failed.add_job.side_effect = RuntimeError("synthetic scheduling failure")
        with patch.object(self.module, "scheduler", failed):
            with self.assertRaises(RuntimeError):
                self.module.queue_tracked_check("Test")
        self.assertEqual(coordinator.pending_tasks, 0)
        self.assertEqual(self.module.check_progress["state"], "idle")

    def test_duplicate_check_does_not_queue_again_and_cancel_is_preserved(self):
        from runtime_guard import DataBusyError, coordinator
        queued = []
        scheduler = Mock()
        scheduler.add_job.side_effect = lambda function, **kwargs: queued.append(function)
        with patch.object(self.module, "scheduler", scheduler):
            self.module.queue_tracked_check("Test")
            with self.assertRaises(DataBusyError):
                self.module.queue_tracked_check("Duplicate")
            self.module.request_check_cancel()
            with patch.object(self.module, "check_all_clients") as check:
                queued[0]()
                self.assertTrue(check.call_args.kwargs["cancel_callback"]())
        self.module.check_progress["state"] = "idle"
        self.assertEqual(coordinator.pending_tasks, 0)

    def test_clear_data_is_recoverable_and_removes_structured_tables(self):
        _, case_id, _ = self.seed_case()
        with closing(sqlite3.connect(self.module.DATABASE_PATH)) as conn, conn:
            conn.execute("INSERT INTO document_extraction (case_id, raw_json, created_at, updated_at) VALUES (?, '{}', 'test', 'test')", (case_id,))
        backup = self.module.clear_all_client_data()
        self.assertEqual(self.client_count(), 0)
        with closing(sqlite3.connect(self.module.DATABASE_PATH)) as conn, conn:
            self.assertEqual(conn.execute("SELECT count(*) FROM document_extraction").fetchone()[0], 0)
        self.module.restore_data_from_zip(self.archive_upload(backup.read_bytes()))
        self.assertEqual(self.client_count(), 1)
        self.assertTrue(self.document.exists())

    def test_client_delete_commit_failure_restores_file_and_client(self):
        client_id, _, _ = self.seed_case()
        session_class = type(self.module.SessionLocal())
        with patch.object(session_class, "commit", side_effect=RuntimeError("synthetic commit failure")):
            with self.assertRaises(RuntimeError):
                self.client.post(f"/clients/{client_id}/delete")
        self.assertEqual(self.client_count(), 1)
        self.assertTrue(self.document.exists())

    def test_delete_document_is_backed_up_and_does_not_redownload(self):
        import scheduler
        _, _, document_id = self.seed_case()
        response = self.client.post(f"/documents/{document_id}/delete")
        self.assertEqual(response.status_code, 302)
        self.assertFalse(self.document.exists())
        session = self.module.SessionLocal()
        try:
            document = session.get(self.module.InsolvencyDocument, document_id)
            self.assertIsNotNone(document.deleted_at)
            http = Mock()
            scheduler._download_document(http, document)
            http.get.assert_not_called()
        finally:
            session.close()
        self.assertTrue(list((self.root / "exports").glob("pred-smazanim-*.zip")))

    def test_document_route_cannot_read_outside_storage(self):
        _, _, document_id = self.seed_case()
        session = self.module.SessionLocal()
        session.get(self.module.InsolvencyDocument, document_id).local_path = str(self.root / "data/settings.json")
        session.commit()
        session.close()
        self.assertEqual(self.client.get(f"/documents/{document_id}").status_code, 404)

    def test_ai_failure_rolls_back_and_retains_previous_outputs(self):
        import ai_analysis
        _, case_id, _ = self.seed_case()
        def failed(case):
            case.ai_summary = "bad partial result"
            case.ai_case_study = "bad partial study"
            raise RuntimeError("synthetic network failure")
        ai_analysis._run_ai_job(case_id, failed)
        session = self.module.SessionLocal()
        try:
            case = session.get(self.module.InsolvencyCase, case_id)
            self.assertEqual(case.ai_summary, "Dřívější shrnutí")
            self.assertEqual(case.ai_case_study, "Dřívější kazuistika")
            self.assertIn("selhala", case.ai_last_error)
            self.assertIsNone(case.ai_pending_kind)
        finally:
            session.close()

    def test_ai_success_and_restart_clear_pending_without_losing_output(self):
        import ai_analysis
        _, case_id, _ = self.seed_case()
        session = self.module.SessionLocal()
        session.get(self.module.InsolvencyCase, case_id).ai_pending_kind = "study"
        session.commit()
        session.close()
        self.module.recover_interrupted_ai()
        session = self.module.SessionLocal()
        self.assertIsNone(session.get(self.module.InsolvencyCase, case_id).ai_pending_kind)
        self.assertEqual(session.get(self.module.InsolvencyCase, case_id).ai_case_study, "Dřívější kazuistika")
        session.close()
        ai_analysis._run_ai_job(case_id, lambda case: setattr(case, "ai_summary", "new valid result"))
        session = self.module.SessionLocal()
        self.assertIsNone(session.get(self.module.InsolvencyCase, case_id).ai_last_error)
        session.close()

    def test_ai_non_object_json_is_retried(self):
        import ai_analysis
        client = Mock()
        client.models.generate_content.side_effect = [SimpleNamespace(text="[]"), SimpleNamespace(text='{"ok":true}')]
        with patch.object(ai_analysis.time, "sleep"):
            self.assertEqual(ai_analysis._generate_json_with_retry(client, []), {"ok": True})
        self.assertEqual(client.models.generate_content.call_count, 2)

    def test_pdf_html_and_truncated_download_do_not_replace_good_file(self):
        import pdf_io
        from pypdf import PdfWriter
        buffer = BytesIO()
        writer = PdfWriter()
        writer.add_blank_page(width=72, height=72)
        writer.write(buffer)
        good = buffer.getvalue()
        pdf_io.write_pdf_atomic(self.document, good)
        for invalid in (b"<html>Service unavailable</html>", good[:150]):
            with self.assertRaises(ValueError):
                pdf_io.write_pdf_atomic(self.document, invalid)
            self.assertEqual(self.document.read_bytes(), good)
        with patch.object(pdf_io.os, "replace", side_effect=OSError("synthetic disk failure")):
            with self.assertRaises(OSError):
                pdf_io.write_pdf_atomic(self.document, good)
        self.assertEqual(self.document.read_bytes(), good)
        self.assertEqual(list(self.document.parent.iterdir()), [self.document])

    def test_settings_failed_atomic_write_preserves_old_key_and_corruption_is_not_overwritten(self):
        import app_settings
        before = app_settings.SETTINGS_PATH.read_bytes()
        with patch.object(app_settings.os, "replace", side_effect=OSError("synthetic disk failure")):
            with self.assertRaises(OSError):
                app_settings.set_gemini_api_key("NEW_SYNTHETIC_KEY")
        self.assertEqual(app_settings.SETTINGS_PATH.read_bytes(), before)
        app_settings.SETTINGS_PATH.write_text("broken-json", encoding="utf-8")
        with self.assertRaises(RuntimeError):
            app_settings.set_gemini_api_key("NEW_SYNTHETIC_KEY")
        self.assertEqual(app_settings.SETTINGS_PATH.read_text(encoding="utf-8"), "broken-json")

    def test_isir_outage_preserves_last_known_status(self):
        import scheduler
        session = self.module.SessionLocal()
        session.query(self.module.Client).first().insolvency_status = "Oddlužení"
        session.commit()
        session.close()
        with patch.object(scheduler, "make_soap_client", side_effect=RuntimeError("synthetic outage")), self.assertLogs(scheduler.logger, level="ERROR"):
            scheduler.check_all_clients()
        session = self.module.SessionLocal()
        try:
            client = session.query(self.module.Client).first()
            self.assertEqual(client.insolvency_status, "Oddlužení")
            self.assertIn("selhala", client.last_check_error)
        finally:
            session.close()

    def test_sql_failure_rolls_back_and_next_client_is_checked(self):
        import scheduler
        from sqlalchemy.exc import OperationalError
        session = self.module.SessionLocal()
        session.add(self.module.Client(first_name="Druhý", last_name="Klient", birth_date=date(1981, 1, 1), insolvency_status="Oddlužení"))
        session.commit()
        session.close()
        seen = []
        def check(client, soap):
            seen.append(client.id)
            if len(seen) == 1:
                client.insolvency_status = "bad partial status"
                raise OperationalError("synthetic", {}, RuntimeError("failure"))
            client.last_check_error = None
        with patch.object(scheduler, "make_soap_client", return_value=Mock()), patch.object(scheduler, "check_client_with_retry", side_effect=check), patch.object(scheduler.time, "sleep"), self.assertLogs(scheduler.logger, level="ERROR"):
            scheduler.check_all_clients()
        self.assertEqual(len(seen), 2)
        session = self.module.SessionLocal()
        try:
            clients = session.query(self.module.Client).order_by(self.module.Client.id).all()
            self.assertEqual(clients[0].insolvency_status, "Nezkontrolováno")
            self.assertIn("selhala", clients[0].last_check_error)
            self.assertEqual(clients[1].insolvency_status, "Oddlužení")
        finally:
            session.close()

    def test_restore_form_and_csrf_require_confirmation(self):
        html = self.client.get("/settings").get_data(as_text=True)
        self.assertIn('name="confirm_restore"', html)
        self.assertIn('name="backup_file"', html)
        self.module.app.config["WTF_CSRF_ENABLED"] = True
        try:
            self.assertEqual(self.client.post("/data/import").status_code, 400)
        finally:
            self.module.app.config["WTF_CSRF_ENABLED"] = False
        response = self.client.post("/data/import")
        self.assertIn("error=", response.location)
        self.assertEqual(self.client_count(), 1)

    def test_ai_queue_failure_clears_pending_and_keeps_outputs(self):
        from runtime_guard import coordinator
        _, case_id, _ = self.seed_case()
        failed = Mock()
        failed.add_job.side_effect = RuntimeError("synthetic scheduler failure")
        with patch.object(self.module, "scheduler", failed):
            response = self.client.post(f"/cases/{case_id}/analyze")
        self.assertEqual(response.status_code, 302)
        session = self.module.SessionLocal()
        try:
            case = session.get(self.module.InsolvencyCase, case_id)
            self.assertIsNone(case.ai_pending_kind)
            self.assertEqual(case.ai_summary, "Dřívější shrnutí")
            self.assertEqual(case.ai_case_study, "Dřívější kazuistika")
        finally:
            session.close()
        self.assertEqual(coordinator.pending_tasks, 0)

    def test_client_delete_preserves_shared_document_and_removes_its_structured_rows(self):
        client_id, case_id, document_id = self.seed_case()
        session = self.module.SessionLocal()
        other = self.module.Client(first_name="Další", last_name="Klient", birth_date=date(1982, 1, 1))
        case = self.module.InsolvencyCase(client=other, spisova_znacka="TEST INS 2/2026")
        document = self.module.InsolvencyDocument(case=case, title="Shared", document_type="hlavní dokument", source_url="https://example.invalid/shared.pdf", local_path=str(self.document))
        session.add(document)
        session.commit()
        session.close()
        with closing(sqlite3.connect(self.module.DATABASE_PATH)) as conn, conn:
            conn.execute("INSERT INTO document_extraction (case_id, raw_json, created_at, updated_at) VALUES (?, '{}', 'test', 'test')", (case_id,))
        self.assertEqual(self.client.post(f"/clients/{client_id}/delete").status_code, 302)
        self.assertTrue(self.document.exists())
        self.assertEqual(self.client_count(), 1)
        with closing(sqlite3.connect(self.module.DATABASE_PATH)) as conn:
            self.assertEqual(conn.execute("SELECT count(*) FROM document_extraction WHERE case_id=?", (case_id,)).fetchone()[0], 0)

    def test_interrupted_restore_is_detected_before_new_database_is_created(self):
        import models
        recovery = self.root / "data-recovery-synthetic"
        recovery.mkdir()
        recovery_database = recovery / "app.db"
        recovery_database.write_bytes(b"retained original")
        try:
            with self.assertRaisesRegex(RuntimeError, "Předchozí obnovu"):
                models.init_db()
            self.assertEqual(recovery_database.read_bytes(), b"retained original")
        finally:
            recovery_database.unlink()
            recovery.rmdir()
