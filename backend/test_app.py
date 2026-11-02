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
    validate_added_die,
    build_analysis,
    build_summary,
    load_dataset,
    query_table,
    validate_new_die,
    validate_die_update,
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
        self.assertNotIn("die_id", record)
        added = validate_added_die(valid_die_payload())
        self.assertRegex(added["die_id"], r"^EXT-[A-F0-9]{12}$")
        self.assertEqual(record["die_x"], -4)
        self.assertEqual(record["scan_pass"], True)
        self.assertEqual(record["t_total_baseline_ms"], 630.5)

    def test_die_update_preserves_existing_id(self):
        record = validate_die_update(valid_die_payload(), "D000123")
        self.assertEqual(record["die_id"], "D000123")

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

    def test_public_die_submission_needs_no_sign_in(self):
        with patch.dict("os.environ", {"DATABASE_URL": "postgresql://test"}), \
                patch.object(app_module, "_save_added_die") as save_die:
            status, result, _ = self.request(
                "POST", "/api/dies", valid_die_payload()
            )
            self.assertEqual(status, 201)
            self.assertTrue(result["saved"])
            self.assertTrue(result["item"]["die_id"].startswith("EXT-"))
            save_die.assert_called_once()

    def test_public_die_submission_requires_database(self):
        with patch.dict("os.environ", {}, clear=True):
            status, result, _ = self.request(
                "POST", "/api/dies", valid_die_payload()
            )
        self.assertEqual(status, 503)
        self.assertIn("DATABASE_URL", result["error"])

    def test_public_die_update_requires_existing_record_and_saves_override(self):
        record = validate_added_die(valid_die_payload())
        dataset = {"dies": [record], "memories": [], "failures": []}
        payload = valid_die_payload()
        payload["final_bin"] = "PASS_REPAIRED"
        with patch.dict("os.environ", {"DATABASE_URL": "postgresql://test"}), \
                patch.object(app_module, "get_dataset", return_value=dataset), \
                patch.object(app_module, "_save_die_override") as save_override:
            status, result, _ = self.request(
                "PUT", f"/api/dies/{record['die_id']}", payload
            )
            self.assertEqual(status, 200)
            self.assertEqual(result["item"]["die_id"], record["die_id"])
            self.assertEqual(result["item"]["final_bin"], "PASS_REPAIRED")
            save_override.assert_called_once()

            status, result, _ = self.request(
                "PUT", "/api/dies/UNKNOWN", payload
            )
            self.assertEqual(status, 404)
            self.assertEqual(result["error"], "Die record not found.")

    def test_public_status_reports_available_storage(self):
        with patch.dict("os.environ", {"DATABASE_URL": "postgresql://test"}):
            status, result, _ = self.request("GET", "/api/die-editing/status")
        self.assertEqual(status, 200)
        self.assertTrue(result["configured"])
        self.assertTrue(result["public_access"])

    def test_public_write_rejects_foreign_origin(self):
        connection = http.client.HTTPConnection(self.host, self.port, timeout=5)
        connection.request(
            "POST",
            "/api/dies",
            body=json.dumps(valid_die_payload()),
            headers={
                "Content-Type": "application/json",
                "Origin": "https://attacker.example",
            },
        )
        response = connection.getresponse()
        result = json.loads(response.read())
        connection.close()
        self.assertEqual(response.status, 403)
        self.assertIn("origin", result["error"])


if __name__ == "__main__":
    unittest.main()
