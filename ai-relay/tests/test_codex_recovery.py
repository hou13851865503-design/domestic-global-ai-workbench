import sys
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import orchestrator as orch


class CodexRecoveryTests(unittest.TestCase):
    def setUp(self):
        self.offline = dict(orch.OFFLINE)

    def tearDown(self):
        orch.OFFLINE.clear()
        orch.OFFLINE.update(self.offline)

    def test_offline_state_recovers_after_reachable_probe(self):
        orch.OFFLINE.update(on=True, reason="network", last_probe=100.0)
        with (
            patch.object(orch, "CODEX_BIN", Path("codex.exe")),
            patch.object(orch, "load_health", return_value={"fails": 0, "last_fail": 0}),
            patch.object(orch.time, "monotonic", return_value=131.0),
            patch.object(orch, "probe_codex", return_value=(True, "HTTP 200")) as probe,
            patch.object(orch, "mark_codex_ok") as mark_ok,
        ):
            self.assertTrue(orch.codex_available())

        probe.assert_called_once_with()
        mark_ok.assert_called_once_with()
        self.assertFalse(orch.OFFLINE["on"])
        self.assertEqual(orch.OFFLINE["reason"], "")

    def test_offline_state_throttles_repeated_probes(self):
        orch.OFFLINE.update(on=True, reason="network", last_probe=100.0)
        with (
            patch.object(orch, "CODEX_BIN", Path("codex.exe")),
            patch.object(orch, "load_health", return_value={"fails": 1, "last_fail": 100}),
            patch.object(orch.time, "monotonic", return_value=110.0),
            patch.object(orch, "probe_codex") as probe,
        ):
            self.assertFalse(orch.codex_available())

        probe.assert_not_called()
        self.assertTrue(orch.OFFLINE["on"])


if __name__ == "__main__":
    unittest.main()
