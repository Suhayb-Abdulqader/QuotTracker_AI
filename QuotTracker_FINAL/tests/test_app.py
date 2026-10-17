import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch


_test_root = tempfile.TemporaryDirectory()
os.environ["APP_ENV"] = "test"
os.environ["ADMIN_USERNAME"] = "test-admin"
os.environ["ADMIN_PASSWORD"] = "a-strong-test-password"
os.environ["SESSION_SECRET"] = "test-session-secret-with-at-least-32-bytes"
os.environ["COOKIE_SECURE"] = "false"
os.environ["GROQ_API_KEY"] = ""
os.environ["DB_URL"] = str(Path(_test_root.name) / "test.db")

import ai_helper
import api
import auth
import db
import setup_db
import sync
from fastapi.testclient import TestClient


class QuotTrackerAppTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        db.DB_FILE = str(Path(self.temp.name) / "test.db")
        auth._failed_logins.clear()
        self.client = TestClient(api.app)
        self.client.__enter__()

    def tearDown(self):
        self.client.__exit__(None, None, None)
        self.temp.cleanup()

    def sign_in(self):
        response = self.client.post(
            "/api/auth/login",
            json={"username": "test-admin", "password": "a-strong-test-password"},
        )
        self.assertEqual(response.status_code, 200)
        return response.json()["csrf_token"]

    def test_private_api_requires_authentication(self):
        for path in (
            "/api/stats",
            "/api/activity",
            "/api/attention",
            "/api/requests",
            "/api/export",
        ):
            with self.subTest(path=path):
                self.assertEqual(self.client.get(path).status_code, 401)

    def test_login_sets_secure_session_policy_and_csrf_protection(self):
        response = self.client.post(
            "/api/auth/login",
            json={"username": "test-admin", "password": "a-strong-test-password"},
        )
        self.assertEqual(response.status_code, 200)
        cookie = response.headers["set-cookie"].lower()
        self.assertIn("httponly", cookie)
        self.assertIn("samesite=strict", cookie)

        self.assertEqual(self.client.get("/api/stats").status_code, 200)
        self.assertEqual(self.client.post("/api/auth/logout").status_code, 403)
        self.assertEqual(
            self.client.post(
                "/api/auth/logout",
                headers={"X-CSRF-Token": response.json()["csrf_token"]},
            ).status_code,
            200,
        )
        self.assertEqual(self.client.get("/api/stats").status_code, 401)

    def test_cross_origin_mutation_is_rejected(self):
        csrf_token = self.sign_in()
        response = self.client.post(
            "/api/auth/logout",
            headers={
                "X-CSRF-Token": csrf_token,
                "Origin": "https://attacker.example",
            },
        )
        self.assertEqual(response.status_code, 403)

    def test_login_is_rate_limited_after_repeated_failures(self):
        for _ in range(5):
            response = self.client.post(
                "/api/auth/login",
                json={"username": "test-admin", "password": "wrong-password"},
            )
            self.assertEqual(response.status_code, 401)
        response = self.client.post(
            "/api/auth/login",
            json={"username": "test-admin", "password": "wrong-password"},
        )
        self.assertEqual(response.status_code, 429)
        self.assertIn("retry-after", response.headers)

    def test_untrusted_host_is_rejected(self):
        response = self.client.get("/api/health", headers={"Host": "attacker.example"})
        self.assertEqual(response.status_code, 400)

    def test_health_has_no_record_count(self):
        response = self.client.get("/api/health")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json(), {"status": "ok"})

    def test_setup_creates_schema_without_seed_records(self):
        setup_db.run()
        conn = db.get_db()
        try:
            self.assertEqual(
                conn.execute("SELECT COUNT(*) FROM requests").fetchone()[0], 0
            )
            self.assertEqual(
                conn.execute("SELECT COUNT(*) FROM tracker").fetchone()[0], 0
            )
        finally:
            conn.close()

    def test_sheet_sync_fills_missing_values_without_calling_ai(self):
        conn = db.get_db()
        db.insert_tracker_row(
            conn,
            {"Request No": "REQ-1", "Door Name": "Existing", "Vendor PO": None},
        )
        conn.commit()
        conn.close()

        sheet = Path(self.temp.name) / "requests.csv"
        sheet.write_text(
            "Request No,Door Name,Vendor PO,Brand,Amount\n"
            "REQ-1,Existing,PO-1,Vendor One,0\n"
            " REQ-2 ,New request,PO-2,Vendor Two,125.50\n",
            encoding="utf-8",
        )
        result = sync.run_sync(str(sheet))
        self.assertEqual(result["inserted"], 1)
        self.assertEqual(result["updated"], 1)

        conn = db.get_db()
        try:
            existing = db.get_tracker_row(conn, "REQ-1")
            self.assertEqual(existing["vendor_po"], "PO-1")
            self.assertEqual(existing["amount"], 0)
            self.assertIsNotNone(db.get_tracker_row(conn, "REQ-2"))
        finally:
            conn.close()

    def test_sheet_sync_rejects_duplicate_request_numbers(self):
        sheet = Path(self.temp.name) / "duplicates.csv"
        sheet.write_text(
            "Request No,Door Name\nREQ-1,First\nREQ-1,Duplicate\n",
            encoding="utf-8",
        )
        with self.assertRaisesRegex(ValueError, "Duplicate request number"):
            sync.run_sync(str(sheet))

    def test_ai_search_failure_is_returned_not_silently_fallbacked(self):
        self.sign_in()
        with patch.object(
            api, "answer_search", side_effect=ai_helper.AIServiceError("offline")
        ):
            response = self.client.get("/api/search", params={"q": "pending orders"})
        self.assertEqual(response.status_code, 503)
        self.assertIn("unavailable", response.json()["detail"])

    def test_pdf_extraction_does_not_return_rule_based_results_on_ai_failure(self):
        with patch.object(
            ai_helper, "_call", side_effect=ai_helper.AIServiceError("offline")
        ):
            with self.assertRaises(ai_helper.AIServiceError):
                ai_helper.extract_from_document("Quotation No: QT-123 Total: 250")

    def test_legacy_local_path_sync_endpoint_is_not_available(self):
        self.sign_in()
        response = self.client.post(
            "/api/sync", json={"file_path": "c:/private/anything.xlsx"}
        )
        self.assertEqual(response.status_code, 404)

    def test_csv_upload_requires_csrf_and_imports_real_rows(self):
        csrf_token = self.sign_in()
        content = (
            b"Request No,Door Name,Vendor PO,Brand,Amount\n"
            b"REAL-1,Actual request,PO-REAL-1,Actual vendor,0\n"
        )
        response = self.client.post(
            "/api/upload",
            files={"file": ("requests.csv", content, "text/csv")},
        )
        self.assertEqual(response.status_code, 403)

        response = self.client.post(
            "/api/upload",
            headers={"X-CSRF-Token": csrf_token},
            files={"file": ("requests.csv", content, "text/csv")},
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["inserted"], 1)
        conn = db.get_db()
        try:
            self.assertIsNotNone(db.get_tracker_row(conn, "REAL-1"))
        finally:
            conn.close()

    def test_dashboard_uses_nonce_and_has_no_hardcoded_sample_activity(self):
        response = self.client.get("/")
        self.assertEqual(response.status_code, 200)
        nonce = response.text.split('nonce="', 1)[1].split('"', 1)[0]
        self.assertIn(f"'nonce-{nonce}'", response.headers["content-security-policy"])
        self.assertNotIn("cdn.jsdelivr.net", response.headers["content-security-policy"])
        self.assertNotIn("cdn.jsdelivr.net", response.text)
        self.assertNotIn("Sarah Kim", response.text)
        self.assertNotIn("renderDefaultChart", response.text)


if __name__ == "__main__":
    unittest.main()
