import unittest
from pathlib import Path
from urllib.parse import parse_qs

from app import build_analysis, build_summary, load_dataset, query_table


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


if __name__ == "__main__":
    unittest.main()
