"""The evaluation must measure real retrieval/evidence behavior, not model accuracy."""
import importlib.util
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

SPEC = importlib.util.spec_from_file_location("df_mcp_evaluation", Path(__file__).resolve().parents[1] / "scripts/evaluate.py")
evaluation = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(evaluation)


class EvaluationTests(unittest.TestCase):
    def test_rank_metrics_use_gold_population_and_first_relevant_rank(self):
        metrics = evaluation.ranking_metrics(["irrelevant", "a", "a", "b"], ["a", "b", "c"], 3)
        self.assertEqual(metrics["recall_at_k"], 2 / 3)
        self.assertEqual(metrics["reciprocal_rank"], 0.5)
        self.assertEqual(evaluation.ranking_metrics(["a"], ["b"], 5)["reciprocal_rank"], 0)
        with self.assertRaises(ValueError):
            evaluation.ranking_metrics([], [], 5)

    def test_generated_keyword_corpus_and_evidence_invariants(self):
        report = evaluation.run_evaluation(repetitions=1)
        self.assertTrue(report["summary"]["all_passed"], report["cases"])
        self.assertEqual(report["retrieval"]["mean_recall_at_k"], 1.0)
        self.assertEqual(report["retrieval"]["mrr_at_k"], 1.0)
        self.assertFalse(report["llm_answer_accuracy"]["evaluated"])
        self.assertFalse(report["scope"]["production_sources_accessed"])
        cases = {case["id"]: case for case in report["cases"]}
        self.assertEqual(cases["enumerated_versioned_exports_decimal_total"]["observations"]["total"], "400.30")
        self.assertEqual(cases["partial_pdf_keeps_explicit_page_gap"]["observations"]["content_coverage"]["missing_text_pages"], [2])
        self.assertGreater(report["latency"]["read"]["samples"], 1)

    def test_wrong_gold_causes_a_failed_report(self):
        wrong_gold = [{"id": "impossible", "query": "expense", "relevant_paths": ["does-not-exist.txt"]}]
        with patch.object(evaluation, "RETRIEVAL_CASES", wrong_gold):
            report = evaluation.run_evaluation(repetitions=1)
        self.assertFalse(report["summary"]["all_passed"])
        self.assertEqual(report["retrieval"]["mean_recall_at_k"], 0)

    def test_hybrid_without_models_is_not_reported_as_semantic_success(self):
        report = evaluation.run_evaluation(mode="hybrid", repetitions=1)
        self.assertFalse(report["summary"]["all_passed"])
        self.assertTrue(any(case["status"] == "failed" and case["id"].startswith("retrieval_") for case in report["cases"]))

    def test_cli_writes_machine_readable_report_and_exit_status(self):
        with tempfile.TemporaryDirectory() as folder:
            output = Path(folder) / "reports/result.json"
            with patch.object(evaluation, "run_evaluation", return_value={"summary": {"passed": 0, "failed": 1, "total": 1, "all_passed": False}}):
                self.assertEqual(evaluation.main(["--output", str(output)]), 1)
            self.assertFalse(json.loads(output.read_text())["summary"]["all_passed"])


if __name__ == "__main__":
    unittest.main()
