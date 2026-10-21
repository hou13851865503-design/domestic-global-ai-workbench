import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import orchestrator as orch


class EventPrivacyTests(unittest.TestCase):
    def test_reasoning_events_are_discarded(self):
        self.assertIsNone(orch.slim_event({
            "type": "reasoning", "summary": "private reasoning text",
        }))
        self.assertIsNone(orch.slim_event({
            "type": "item.completed",
            "item": {"type": "reasoning", "summary": "private reasoning text"},
        }))

    def test_analysis_role_messages_are_discarded(self):
        for event_type in ("assistant_message", "response_item"):
            with self.subTest(event_type=event_type):
                self.assertIsNone(orch.slim_event({
                    "type": event_type,
                    "item": {
                        "type": "message",
                        "role": "analysis",
                        "content": [{"type": "output_text", "text": "private analysis"}],
                    },
                }))

    def test_user_facing_assistant_message_is_kept(self):
        self.assertEqual(orch.slim_event({
            "type": "assistant_message",
            "item": {
                "type": "message",
                "role": "assistant",
                "content": [{"type": "output_text", "text": "Working on the request."}],
            },
        }), {"type": "assistant", "role": "assistant", "text": "Working on the request."})

    def test_persisted_events_keep_phase_but_not_reasoning(self):
        with tempfile.TemporaryDirectory() as tmp:
            event_dir = Path(tmp)
            with patch.object(orch, "EVENTS_DIR", event_dir):
                callback = orch.record_event("T-EVENT-001", "optimize")
                callback({"type": "reasoning", "text": "private reasoning text"})
                callback({"type": "assistant", "text": "optimized prompt ready"})

            rows = [json.loads(line) for line in
                    (event_dir / "T-EVENT-001.jsonl").read_text(encoding="utf-8").splitlines()]
            self.assertEqual(len(rows), 1)
            self.assertEqual(rows[0]["phase"], "optimize")
            self.assertEqual(rows[0]["text"], "optimized prompt ready")
            self.assertNotIn("private reasoning text", json.dumps(rows, ensure_ascii=False))


if __name__ == "__main__":
    unittest.main()
