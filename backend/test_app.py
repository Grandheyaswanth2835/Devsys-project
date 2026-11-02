import http.client
import json
import unittest
from http.server import ThreadingHTTPServer
from pathlib import Path
from threading import Thread
from urllib.parse import parse_qs
from unittest.mock import patch

import app as app_module
from app import (
    MbistRequestHandler,
    _session_cookie_value,
    _valid_admin_session,
    build_analysis,
    build_summary,
    load_dataset,
    query_table,
    validate_new_die,
    validate_request_origin,
)


def valid_die_payload():
    return {
        "lot_id": "LOT21",
        "wafer_id": "LOT21-W01",
        "die_x": -4,
        "die_y": 9,
        "radial_pos": 0.5,
        "ro_speed_mhz": 547.3,
        "leakage_na": 74.7,
        "iddq_ua": 78.0,
        "bitcell_vmin_mv": 640.5,
        "scan_pass": True,
        "march_failing_mems": 0,
        "early_mats_fails": 0,
        "repaired_rows": 0,
        "repaired_cols": 0,
        "retention_fail": False,
        "dynamic_fault_fail": False,
        "vmin_corner_only_fail": False,
        "unrepairable": False,
        "neighbor_fail_cnt": 0,
        "final_bin": "PASS",
        "t_march_ms": 311.6,
        "t_retention_ms": 199.3,
        "t_repair_ms": 60.0,
        "t_setup_readout_ms": 59.6,
        "t_total_baseline_ms": 630.5,
    }


class DashboardDataTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.data = load_dataset(Path(__file__).resolve().parent.parent / "data")

    def test_dataset_relationships_and_row_counts(self):
        self.assertEqual(len(self.data["dies"]), 76080)
        self.assertEqual(len(self.data["memories"]), 1200)
        self.assertEqual(len(self.data["failures"]), 10467)
        die_ids = {row["die_id"] for row in self.data["dies"]}
        mem_ids = {row["mem_id"] for row in self.data["memories"]}
        self.assertTrue(all(row["die_id"] in die_ids for row in self.data["failures"]))
        self.assertTrue(all(row["mem_id"] in mem_ids for row in self.data["failures"]))

    def test_summary_has_expected_dataset_totals(self):
        summary = build_summary(self.data)
        self.assertEqual(summary["totals"]["dies"], 76080)
        self.assertEqual(summary["totals"]["memories"], 1200)
        self.assertEqual(summary["totals"]["fail_events"], 10467)
        self.assertEqual(summary["totals"]["lots"], 20)
        self.assertAlmostEqual(summary["totals"]["baseline_ms"], 634.2, delta=0.5)

    def test_table_filters_and_pagination(self):
        query = parse_qs("lot=LOT01&limit=10&offset=10")
        result = query_table(self.data, "dies", query)
        self.assertEqual(result["total"], 3804)
        self.assertEqual(len(result["items"]), 10)
        self.assertEqual(result["offset"], 10)
        self.assertTrue(all(row["lot_id"] == "LOT01" for row in result["items"]))

    def test_invalid_pagination_is_rejected(self):
        with self.assertRaisesRegex(ValueError, "limit must be"):
            query_table(self.data, "memories", parse_qs("limit=201"))

    def test_analysis_reproduces_reference_algorithm_and_schedule_results(self):
        analysis = build_analysis(self.data)
        by_class = {row["bitcell"]: row for row in analysis["algorithm_pruning"]}
        self.assertEqual(by_class["HC"]["recommended"], "MarchCm_RD")
        self.assertEqual(by_class["HD"]["recommended"], "MarchSS")
        self.assertEqual(analysis["scheduling"]["inventory_order"]["sessions"], 29)
        self.assertEqual(analysis["scheduling"]["longest_first"]["sessions"], 29)
        self.assertAlmostEqual(
            analysis["scheduling"]["longest_first"]["total_ms"], 3.2, delta=0.2
        )
        adaptive = analysis["adaptive_reference"]
        self.assertEqual(adaptive["held_out_auc"], 0.945)
        self.assertEqual(adaptive["best_low_tier_pct"], 58.8)
        self.assertEqual(adaptive["best_added_dppm"], 35.0)
        self.assertEqual(len(adaptive["sweep"]), 7)

    def test_new_die_validation_generates_id_and_normalizes_numbers(self):
        record = validate_new_die(valid_die_payload())
        self.assertRegex(record["die_id"], r"^EXT-[A-F0-9]{12}$")
        self.assertEqual(record["die_x"], -4)
        self.assertEqual(record["scan_pass"], True)
        self.assertEqual(record["t_total_baseline_ms"], 630.5)

    def test_new_die_validation_rejects_incomplete_and_inconsistent_records(self):
        incomplete = valid_die_payload()
        del incomplete["final_bin"]
        with self.assertRaisesRegex(ValueError, "missing fields"):
            validate_new_die(incomplete)

        invalid_time = valid_die_payload()
        invalid_time["t_total_baseline_ms"] = 1
        with self.assertRaisesRegex(ValueError, "sum of the time components"):
            validate_new_die(invalid_time)

        invalid_bool = valid_die_payload()
        invalid_bool["scan_pass"] = "true"
        with self.assertRaisesRegex(ValueError, "scan_pass must be true or false"):
            validate_new_die(invalid_bool)

    def test_admin_session_cookie_expires_and_rejects_tampering(self):
        password = "test-only-admin-secret"
        expiry = 4_000_000_000
        cookie = _session_cookie_value(password, expiry, "nonce")
        with patch.dict("os.environ", {"DIE_ADMIN_PASSWORD": password}):
            self.assertTrue(_valid_admin_session(f"mbist_admin={cookie}"))
            self.assertFalse(_valid_admin_session(f"mbist_admin={cookie}tampered"))
            expired = _session_cookie_value(password, 1, "nonce")
            self.assertFalse(_valid_admin_session(f"mbist_admin={expired}"))

    def test_request_origin_requires_matching_host_and_scheme(self):
        self.assertTrue(
            validate_request_origin(
                {"Origin": "https://dashboard.example", "Host": "dashboard.example",
                 "X-Forwarded-Proto": "https"}
            )
        )
        self.assertFalse(
            validate_request_origin(
                {"Origin": "https://attacker.example", "Host": "dashboard.example",
                 "X-Forwarded-Proto": "https"}
            )
        )


class DieSubmissionApiTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.server = ThreadingHTTPServer(("127.0.0.1", 0), MbistRequestHandler)
        cls.thread = Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()
        cls.host, cls.port = cls.server.server_address

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()
        cls.thread.join(timeout=5)

    def request(self, method, path, payload=None, cookie=None):
        connection = http.client.HTTPConnection(self.host, self.port, timeout=5)
        body = json.dumps(payload).encode("utf-8") if payload is not None else None
        headers = {"Origin": f"http://{self.host}:{self.port}"}
        if body is not None:
            headers["Content-Type"] = "application/json"
        if cookie:
            headers["Cookie"] = cookie
        connection.request(method, path, body=body, headers=headers)
        response = connection.getresponse()
        result = json.loads(response.read())
        response_headers = response.headers
        connection.close()
        return response.status, result, response_headers

    def test_admin_login_and_die_submission(self):
        with patch.dict(
            "os.environ",
            {"DIE_ADMIN_PASSWORD": "test-only-admin-secret",
             "DATABASE_URL": "postgresql://test"},
        ), patch.object(app_module, "_save_added_die") as save_die:
            status, _, headers = self.request(
                "POST", "/api/admin/session",
                {"password": "test-only-admin-secret"},
            )
            self.assertEqual(status, 200)
            cookie = headers["Set-Cookie"].split(";", 1)[0]

            status, result, _ = self.request(
                "POST", "/api/dies", valid_die_payload(), cookie
            )
            self.assertEqual(status, 201)
            self.assertTrue(result["saved"])
            self.assertTrue(result["item"]["die_id"].startswith("EXT-"))
            save_die.assert_called_once()

    def test_die_submission_requires_admin_session(self):
        status, result, _ = self.request("POST", "/api/dies", valid_die_payload())
        self.assertEqual(status, 401)
        self.assertIn("admin password", result["error"])


if __name__ == "__main__":
    unittest.main()
