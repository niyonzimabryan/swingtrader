import asyncio
import unittest
from types import SimpleNamespace
from unittest.mock import patch

from utils import deep_research_client as drc
from utils.deep_research_client import DeepResearchClient, MAX_CONSECUTIVE_POLL_ERRORS


def _run(coro):
    return asyncio.get_event_loop().run_until_complete(coro)


class _StubInteractions:
    """Stub for self._client.interactions with scripted .get() side effects."""

    def __init__(self, side_effects):
        self._side_effects = list(side_effects)
        self.calls = 0

    def get(self, task_id):
        self.calls += 1
        effect = self._side_effects.pop(0)
        if isinstance(effect, Exception):
            raise effect
        return effect


def _make_client(side_effects):
    client = DeepResearchClient.__new__(DeepResearchClient)
    client.provider = "gemini"
    client.api_key = "test"
    client.settings = None
    client._client = SimpleNamespace(interactions=_StubInteractions(side_effects))
    return client


def _completed(text="report body"):
    return SimpleNamespace(status="completed", outputs=[SimpleNamespace(text=text)])


def _running():
    return SimpleNamespace(status="running")


class DeepResearchPollFailFastTests(unittest.TestCase):
    def setUp(self):
        # Skip the actual sleep between polls — return an already-resolved awaitable.
        async def _noop_sleep(*_a, **_k):
            return None
        self._sleep_patch = patch.object(drc.asyncio, "sleep", new=_noop_sleep)
        self._sleep_patch.start()

    def tearDown(self):
        self._sleep_patch.stop()

    def test_transient_errors_then_success_resets_counter(self):
        # 4 errors (below threshold), 1 success → must complete
        side_effects = [
            RuntimeError("transient 1"),
            RuntimeError("transient 2"),
            RuntimeError("transient 3"),
            RuntimeError("transient 4"),
            _completed("ok"),
        ]
        client = _make_client(side_effects)

        result = _run(client._gemini_poll("task-A", poll_interval=0, timeout=60))

        self.assertEqual(result["status"], "completed")
        self.assertEqual(result["report"], "ok")
        self.assertEqual(client._client.interactions.calls, 5)

    def test_running_between_errors_resets_counter(self):
        # 4 errors, then a successful "still running" poll, then 4 more errors, then success
        # Without reset behavior this would trip the threshold; with reset it must complete.
        side_effects = [
            RuntimeError("e1"),
            RuntimeError("e2"),
            RuntimeError("e3"),
            RuntimeError("e4"),
            _running(),  # successful poll → resets counter
            RuntimeError("e5"),
            RuntimeError("e6"),
            RuntimeError("e7"),
            RuntimeError("e8"),
            _completed("done"),
        ]
        client = _make_client(side_effects)

        result = _run(client._gemini_poll("task-B", poll_interval=0, timeout=60))

        self.assertEqual(result["status"], "completed")
        self.assertEqual(result["report"], "done")

    def test_five_consecutive_errors_fails_fast(self):
        # 5 consecutive errors → fail fast, no further polls
        side_effects = [RuntimeError(f"persistent error {i}") for i in range(MAX_CONSECUTIVE_POLL_ERRORS)]
        # Add one extra to make sure we don't poll past the threshold
        side_effects.append(_completed("should not reach"))
        client = _make_client(side_effects)

        result = _run(client._gemini_poll("task-C", poll_interval=0, timeout=60))

        self.assertEqual(result["status"], "failed")
        self.assertIn(f"{MAX_CONSECUTIVE_POLL_ERRORS} consecutive poll errors", result["error"])
        self.assertIn("persistent error", result["error"])
        self.assertIn("duration_s", result)
        # We must have stopped exactly at the threshold, not consumed the success
        self.assertEqual(client._client.interactions.calls, MAX_CONSECUTIVE_POLL_ERRORS)


if __name__ == "__main__":
    unittest.main()
