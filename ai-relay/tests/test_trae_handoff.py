import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import agents
import orchestrator as orch


class TraeHandoffTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        for name in ("00_inbox", "02_prompts", "03_output", "04_review",
                     "_state", "_logs"):
            (self.root / name).mkdir()
        self.patches = [
            patch.object(orch, "ROOT", self.root),
            patch.object(orch, "INBOX", self.root / "00_inbox"),
            patch.object(orch, "PROMPTS", self.root / "02_prompts"),
            patch.object(orch, "OUTPUT", self.root / "03_output"),
            patch.object(orch, "REVIEW", self.root / "04_review"),
            patch.object(orch, "STATE", self.root / "_state"),
            patch.object(orch, "LOGS", self.root / "_logs"),
            patch.object(orch, "PROMPT_VERSIONS", self.root / "_state" / "prompt_versions"),
            patch.object(orch, "EVALUATIONS", self.root / "_state" / "evaluations"),
            patch.object(orch, "PENDING_FILE", self.root / "02_prompts" / "_PENDING_FOR_WORKBUDDY.md"),
            patch.object(orch, "PENDING_TRAE_FILE", self.root / "02_prompts" / "_PENDING_FOR_TRAE.md"),
            patch.object(agents, "AGENTS_FILE", self.root / "_state" / "agents.json"),
        ]
        for item in self.patches:
            item.start()

    def tearDown(self):
        for item in reversed(self.patches):
            item.stop()
        self.temp.cleanup()

    def test_auto_execute_uses_configured_trae_backend(self):
        cfg = agents.load_config()
        cfg["roles"]["execute"] = "trae"
        agents.save_config(cfg)

        self.assertEqual(orch.resolve_role({"executor": "auto"}, "executor"), "trae")

    def test_execute_creates_handoff_and_delivery_advances(self):
        tid = "T-TEST-001"
        prompt = self.root / "02_prompts" / f"{tid}.md"
        prompt.write_text("# Task instructions\n", encoding="utf-8")
        task = {
            "id": tid,
            "requirement": "Build a small example",
            "status": orch.PROMPT_READY,
            "executor": "trae",
            "round": 1,
            "calls": 0,
            "prompt_file": f"02_prompts/{tid}.md",
        }
        state = {"tasks": {tid: task}}

        self.assertFalse(orch.stage_execute(task, state))
        handoff = orch.handoff_file("trae", tid)
        pending = self.root / "02_prompts" / "_PENDING_FOR_TRAE.md"
        self.assertEqual(task["status"], orch.WAITING_EXECUTE)
        self.assertEqual(task["waiting_agent"], "trae")
        self.assertTrue(handoff.is_file())
        self.assertTrue(pending.is_file())
        handoff_text = handoff.read_text(encoding="utf-8").replace("\\", "/")
        self.assertIn("03_output/T-TEST-001/RESULT.md", handoff_text)

        result = self.root / "03_output" / tid / "RESULT.md"
        result.parent.mkdir(parents=True, exist_ok=True)
        result.write_text("Done", encoding="utf-8")
        self.assertTrue(orch.check_agent_delivery(task, state))
        self.assertEqual(task["status"], orch.DONE)
        self.assertNotIn("waiting_agent", task)
        self.assertFalse(pending.exists())

    def test_optimize_and_review_can_also_handoff_to_trae(self):
        optimize_id = "T-TEST-OPT"
        optimize_task = {
            "id": optimize_id,
            "requirement": "Prepare a structured prompt",
            "status": orch.NEW,
            "optimizer": "trae",
            "round": 1,
        }
        state = {"tasks": {optimize_id: optimize_task}}

        self.assertFalse(orch.stage_optimize(optimize_task, state))
        optimize_handoff = orch.handoff_file("trae", optimize_id)
        self.assertIn("02_prompts/T-TEST-OPT.md",
                      optimize_handoff.read_text(encoding="utf-8").replace("\\", "/"))

        prompt = self.root / "02_prompts" / f"{optimize_id}.md"
        prompt.write_text("# Prompt", encoding="utf-8")
        self.assertTrue(orch.check_agent_delivery(optimize_task, state))
        self.assertEqual(optimize_task["status"], orch.PROMPT_READY)

        review_id = "T-TEST-REV"
        review_task = {
            "id": review_id,
            "requirement": "Review the deliverable",
            "status": orch.DONE,
            "reviewer": "trae",
            "round": 1,
            "output_dir": f"03_output/{review_id}",
        }
        state["tasks"][review_id] = review_task
        self.assertFalse(orch.stage_review(review_task, state))
        review_handoff = orch.handoff_file("trae", review_id)
        self.assertIn("04_review/T-TEST-REV.md",
                      review_handoff.read_text(encoding="utf-8").replace("\\", "/"))

        review = self.root / "04_review" / f"{review_id}.md"
        review.write_text("verdict: PASS", encoding="utf-8")
        self.assertTrue(orch.check_agent_delivery(review_task, state))
        self.assertEqual(review_task["status"], orch.PASS)

    def test_trae_backend_check_uses_configured_executable(self):
        exe = self.root / "TRAE.exe"
        exe.write_text("", encoding="utf-8")
        with patch.dict("os.environ", {"TRAE_BIN": str(exe)}):
            ok, message = agents.test_backend("trae")
        self.assertTrue(ok)
        self.assertIn("文件接力", message)


if __name__ == "__main__":
    unittest.main()
