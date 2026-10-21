import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import orchestrator as orch


class PromptOptimizationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        for name in ("02_prompts", "03_output", "04_review", "_state", "_logs"):
            (self.root / name).mkdir()
        self.patches = [
            patch.object(orch, "ROOT", self.root),
            patch.object(orch, "PROMPTS", self.root / "02_prompts"),
            patch.object(orch, "OUTPUT", self.root / "03_output"),
            patch.object(orch, "REVIEW", self.root / "04_review"),
            patch.object(orch, "STATE", self.root / "_state"),
            patch.object(orch, "LOGS", self.root / "_logs"),
            patch.object(orch, "PROMPT_VERSIONS", self.root / "_state" / "prompt_versions"),
            patch.object(orch, "EVALUATIONS", self.root / "_state" / "evaluations"),
        ]
        for item in self.patches:
            item.start()

    def tearDown(self):
        for item in reversed(self.patches):
            item.stop()
        self.temp.cleanup()

    def _prompt(self, tid, text):
        path = self.root / "02_prompts" / f"{tid}.md"
        path.write_text(text, encoding="utf-8")
        return path

    def _review(self, tid, score, verdict="FAIL"):
        path = self.root / "04_review" / f"{tid}.md"
        path.write_text(
            f"verdict: {verdict}\noverall_score: {score}\nscores: []\n",
            encoding="utf-8",
        )
        return path

    def test_first_prompt_freezes_acceptance_criteria(self):
        task = {"id": "T-OPT-001", "round": 1}
        path = self._prompt(task["id"],
                            'acceptance:\n  - "file exists"\n  - "content is valid"\n')
        baseline = orch.snapshot_prompt_version(task, path)
        self.assertEqual(task["evaluation_criteria"], ["file exists", "content is valid"])
        self.assertTrue((self.root / baseline).is_file())

        task["round"] = 2
        path.write_text('acceptance:\n  - "changed scoring rule"\n', encoding="utf-8")
        orch.snapshot_prompt_version(task, path)
        self.assertEqual(task["evaluation_criteria"], ["file exists", "content is valid"])

    def test_score_history_marks_only_strict_improvements_as_best(self):
        task = {"id": "T-OPT-002", "round": 1}
        prompt = self._prompt(task["id"], "baseline")
        orch.snapshot_prompt_version(task, prompt)
        verdict, score = orch.record_review_result(task, self._review(task["id"], 60))
        self.assertEqual((verdict, score), ("FAIL", 60))
        self.assertTrue(task["optimization_history"][-1]["improved"])

        task["round"] = 2
        prompt.write_text("candidate", encoding="utf-8")
        orch.snapshot_prompt_version(task, prompt)
        orch.record_review_result(task, self._review(task["id"], 60))
        self.assertFalse(task["optimization_history"][-1]["improved"])
        self.assertEqual(task["best_score"], 60)

    def test_non_improving_failure_restores_best_and_stops(self):
        tid = "T-OPT-003"
        task = {"id": tid, "round": 1, "status": orch.FAIL}
        active = self._prompt(tid, "best prompt")
        orch.snapshot_prompt_version(task, active)
        orch.record_review_result(task, self._review(tid, 70))

        task["round"] = 2
        active.write_text("regressed candidate", encoding="utf-8")
        orch.snapshot_prompt_version(task, active)
        orch.record_review_result(task, self._review(tid, 60))
        task["status"] = orch.FAIL

        self.assertFalse(orch._advance_inner(task, {"tasks": {tid: task}}))
        self.assertEqual(task["status"], orch.GIVE_UP)
        self.assertEqual(active.read_text(encoding="utf-8"), "best prompt")
        self.assertEqual(task["best_score"], 70)
        self.assertIn("已回退", task["giveup_reason"])

    def test_improved_failure_keeps_candidate_and_continues(self):
        tid = "T-OPT-004"
        task = {"id": tid, "round": 1, "status": orch.FAIL}
        active = self._prompt(tid, "baseline")
        orch.snapshot_prompt_version(task, active)
        orch.record_review_result(task, self._review(tid, 50))

        task["round"] = 2
        active.write_text("improved candidate", encoding="utf-8")
        orch.snapshot_prompt_version(task, active)
        orch.record_review_result(task, self._review(tid, 75))
        task["status"] = orch.FAIL

        self.assertTrue(orch._advance_inner(task, {"tasks": {tid: task}}))
        self.assertEqual(task["status"], orch.NEW)
        self.assertEqual(task["round"], 3)
        self.assertEqual(task["best_score"], 75)
        self.assertEqual(active.read_text(encoding="utf-8"), "improved candidate")

    def test_review_score_is_bounded_and_optional_for_legacy_reports(self):
        report = self.root / "score.md"
        report.write_text("overall_score: 101\n", encoding="utf-8")
        self.assertIsNone(orch.parse_review_score(report))
        report.write_text("verdict: FAIL\n", encoding="utf-8")
        self.assertIsNone(orch.parse_review_score(report))
        report.write_text("overall_score: 85 / 100\n", encoding="utf-8")
        self.assertEqual(orch.parse_review_score(report), 85)

    def test_reset_starts_new_run_without_deleting_snapshots(self):
        task = {"id": "T-OPT-006", "round": 1, "best_score": 80,
                "optimization_history": [{"score": 80}]}
        old = orch.snapshot_prompt_version(task, self._prompt(task["id"], "old"))
        old_run = task["optimization_run"]
        orch.reset_prompt_optimization(task)
        self.assertNotEqual(task["optimization_run"], old_run)
        self.assertNotIn("best_score", task)
        self.assertTrue((self.root / old).is_file())


if __name__ == "__main__":
    unittest.main()
