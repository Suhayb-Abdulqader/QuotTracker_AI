import io
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import openpyxl


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
        self.assertEqual(
            self.client.patch("/api/request-status", json={
                "request_no": "REQ-1",
                "status": "Approved",
            }).status_code,
            401,
        )

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

    def test_login_does_not_lock_out_after_repeated_failures(self):
        for _ in range(6):
            response = self.client.post(
                "/api/auth/login",
                json={"username": "test-admin", "password": "wrong-password"},
            )
            self.assertEqual(response.status_code, 401)
        self.assertTrue(self.sign_in())

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

    def test_status_can_be_changed_to_allowed_values_and_is_audited(self):
        csrf_token = self.sign_in()
        conn = db.get_db()
        try:
            db.insert_tracker_row(
                conn, {"Request No": "REQ-STATUS", "Status": "Pending"}
            )
            conn.commit()
        finally:
            conn.close()

        response = self.client.patch(
            "/api/request-status",
            headers={"X-CSRF-Token": csrf_token},
            json={"request_no": "REQ-STATUS", "status": "Approved"},
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["status"], "Approved")

        conn = db.get_db()
        try:
            self.assertEqual(
                db.get_tracker_row(conn, "REQ-STATUS")["status"], "Approved"
            )
            audit = db.get_audit_log(conn, request_no="REQ-STATUS")
            self.assertEqual(audit[0]["source"], "manual")
            self.assertIn('"Approved"', audit[0]["new_values"])
        finally:
            conn.close()

        for status in ("Pending", "Missing"):
            response = self.client.patch(
                "/api/request-status",
                headers={"X-CSRF-Token": csrf_token},
                json={"request_no": "REQ-STATUS", "status": status},
            )
            self.assertEqual(response.status_code, 200)

    def test_status_update_rejects_invalid_status_and_missing_record(self):
        csrf_token = self.sign_in()
        headers = {"X-CSRF-Token": csrf_token}
        invalid = self.client.patch(
            "/api/request-status",
            headers=headers,
            json={"request_no": "REQ-1", "status": "Archived"},
        )
        self.assertEqual(invalid.status_code, 422)

        missing = self.client.patch(
            "/api/request-status",
            headers=headers,
            json={"request_no": "NOT-FOUND", "status": "Missing"},
        )
        self.assertEqual(missing.status_code, 404)

    def test_request_list_marks_status_editability(self):
        self.sign_in()
        conn = db.get_db()
        try:
            db.upsert_request(conn, {"request_no": "SHEET-ONLY"}, source="sheet")
            db.insert_tracker_row(conn, {"Request No": "TRACKED"})
            conn.commit()
        finally:
            conn.close()

        response = self.client.get("/api/requests")
        self.assertEqual(response.status_code, 200)
        items = {item["request_no"]: item for item in response.json()["items"]}
        self.assertTrue(items["TRACKED"]["status_editable"])
        self.assertFalse(items["SHEET-ONLY"]["status_editable"])

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

    def test_pdf_extraction_normalizes_line_item_amounts_to_both_currencies(self):
        ai_response = {
            "door_name": "Event Production KSA 2026",
            "quotation_no": "QT-456",
            "amount_sar": 45900,
            "line_items": [{
                "element": "Event production",
                "category": "Production",
                "brand": "Example Brand",
                "amount_sar": "45,900 SAR",
            }],
        }
        with patch.object(ai_helper, "_call", return_value=json.dumps(ai_response)):
            result = ai_helper.extract_from_document("quotation text")

        self.assertEqual(result["door_name"], "Event Production KSA 2026")
        self.assertEqual(result["amount_sar"], 45900)
        self.assertEqual(result["amount_usd"], 12240)
        self.assertEqual(result["line_items"][0]["amount_sar"], 45900)
        self.assertEqual(result["line_items"][0]["amount_usd"], 12240)

    def test_pdf_upload_persists_elements_and_exports_requested_columns(self):
        csrf_token = self.sign_in()
        extraction = {
            "door_name": "Event Production KSA 2026",
            "quotation_no": "QT-ITEM-1",
            "brand": "Production Brand",
            "amount": 45900,
            "amount_sar": 45900,
            "amount_usd": 12240,
            "line_items": [{
                "element": "Event production",
                "category": "Production",
                "brand": "Production Brand",
                "amount_sar": 45900,
                "amount_usd": 12240,
            }],
        }
        with (
            patch.object(api, "extract_text_from_pdf", return_value="quotation text"),
            patch.object(api, "extract_from_document", return_value=extraction),
        ):
            response = self.client.post(
                "/api/upload",
                headers={"X-CSRF-Token": csrf_token},
                files={"file": ("quotation.pdf", b"pdf bytes", "application/pdf")},
            )
        self.assertEqual(response.status_code, 200, response.text)

        conn = db.get_db()
        try:
            items = db.get_quote_items_by_request(conn)["QT-ITEM-1"]
            self.assertEqual(items[0]["element"], "Event production")
            self.assertEqual(items[0]["amount_sar"], 45900)
            self.assertEqual(items[0]["amount_usd"], 12240)
        finally:
            conn.close()

        exported = self.client.get("/api/export")
        self.assertEqual(exported.status_code, 200)
        workbook = openpyxl.load_workbook(io.BytesIO(exported.content), data_only=False)
        sheet = workbook["Tracker"]
        self.assertEqual(
            [cell.value for cell in sheet[1]],
            [
                "Request No", "Door Name", "Element", "Amount USD",
                "Amount SAR", "Category", "Brand",
            ],
        )
        self.assertEqual(
            [cell.value for cell in sheet[2]],
            [
                "QT-ITEM-1", "Event Production KSA 2026", "Event production",
                12240, 45900, "Production", "Production Brand",
            ],
        )
        self.assertEqual(sheet["D2"].number_format, "$#,##0.00")
        self.assertEqual(sheet["E2"].number_format, '"SAR" #,##0.00')
        workbook.close()

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
        self.assertIn("function renderStatus(row)", response.text)


if __name__ == "__main__":
    unittest.main()
