import json
import importlib.util
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import orchestrator as orch

spec = importlib.util.spec_from_file_location("relay_platform", ROOT / "platform.py")
platform_app = importlib.util.module_from_spec(spec)
spec.loader.exec_module(platform_app)


class AgentActivityTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.temp_path = Path(self.temp.name)
        self.activity_file = self.temp_path / "agent_activity.json"
        self.lock_file = self.temp_path / ".agent_activity.lock"
        self.file_patches = (
            patch.object(orch, "AGENT_ACTIVITY_FILE", self.activity_file),
            patch.object(orch, "AGENT_ACTIVITY_LOCK_FILE", self.lock_file),
        )
        for item in self.file_patches:
            item.start()

    def tearDown(self):
        for item in reversed(self.file_patches):
            item.stop()
        self.temp.cleanup()

    def test_call_start_and_finish_are_persisted(self):
        call_id = orch.start_agent_activity(
            "optimize", "deepseek", "T-001", "optimize", "deepseek-chat", 60)
        running = orch.load_agent_activity()
        self.assertEqual(len(running["active"]), 1)
        self.assertEqual(running["totals"]["optimize"], 1)

        orch.finish_agent_activity(call_id, True)
        finished = orch.load_agent_activity()
        self.assertFalse(finished["active"])
        self.assertEqual(finished["last"]["optimize"]["status"], "success")

    def test_dead_call_is_reported_as_interrupted(self):
        self.activity_file.write_text(json.dumps({
            "active": {"dead-call": {
                "role": "review", "backend": "codex", "pid": -1,
                "task_id": "T-002", "started_epoch": time.time(), "timeout": 60,
            }},
            "last": {}, "totals": {},
        }), encoding="utf-8")

        snapshot = orch.load_agent_activity()
        self.assertFalse(snapshot["active"])
        self.assertEqual(snapshot["last"]["review"]["status"], "interrupted")

    def test_external_handoff_is_waiting_not_running(self):
        cfg = {
            "roles": {"execute": "codex"},
            "backends": {"codex": {"label": "Codex CLI"},
                         "trae": {"label": "TRAE"}},
        }
        state = {"tasks": {"T-003": {
            "id": "T-003", "status": orch.WAITING_EXECUTE,
            "waiting_agent": "trae", "waiting_for": "执行任务",
            "updated": "2026-10-22 12:30:00",
        }}}

        view = platform_app.agent_activity_view(cfg, state, {
            "active": {}, "last": {}, "totals": {},
        })
        execute = next(row for row in view["roles"] if row["role"] == "execute")
        self.assertEqual(execute["status"], "waiting")
        self.assertEqual(execute["backend"], "trae")
        self.assertIn("无法从此处确认", execute["detail"])

    def test_running_role_view_uses_actual_call_record(self):
        cfg = {
            "roles": {"optimize": "codex"},
            "backends": {"codex": {"label": "Codex CLI"}},
        }
        view = platform_app.agent_activity_view(cfg, {"tasks": {}}, {
            "active": {"call": {
                "role": "optimize", "backend": "codex", "task_id": "T-004",
                "phase": "optimize", "model": "gpt-6-sol",
                "started_epoch": time.time(), "started_at": "2026-10-22 12:30:00",
            }},
            "last": {}, "totals": {"optimize": 1},
        })
        optimize = next(row for row in view["roles"] if row["role"] == "optimize")
        self.assertEqual(optimize["status"], "running")
        self.assertEqual(optimize["task_id"], "T-004")
        self.assertEqual(optimize["model"], "gpt-6-sol")
        self.assertIsNotNone(optimize["started_epoch"])


if __name__ == "__main__":
    unittest.main()
