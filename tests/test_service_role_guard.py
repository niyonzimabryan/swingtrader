"""`python main.py` with SERVICE_ROLE=workspace starts the workspace, not the bot.

Railway applies `railway.toml`'s start command to every service built from the
repo, so the second service could otherwise come up as a second bot — and
Telegram allows one polling connection. The guard sits above the bot imports.
"""

from __future__ import annotations

import os
import runpy
import sys
import unittest
from unittest import mock

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


class ServiceRoleGuardTests(unittest.TestCase):
    def _run_main(self, env: dict) -> tuple[list | None, set[str]]:
        """Run main.py with `os.execv` captured; return its argv and any bot modules imported."""
        captured: dict = {}

        def fake_execv(path, argv):
            captured["path"], captured["argv"] = path, list(argv)
            raise SystemExit(7)  # execv never returns; stand in for the process swap

        before = {m for m in sys.modules if m.startswith("bot")}
        with mock.patch.dict(os.environ, env, clear=False), \
             mock.patch("os.execv", fake_execv):
            for name in [m for m in sys.modules if m.startswith("bot")]:
                sys.modules.pop(name, None)
            try:
                runpy.run_path(os.path.join(REPO, "main.py"), run_name="__main__")
            except SystemExit as exc:
                if exc.code != 7:
                    raise
            imported = {m for m in sys.modules if m.startswith("bot")} - before
        return captured.get("argv"), imported

    def test_workspace_role_hands_off_before_any_bot_import(self):
        argv, imported_bot_modules = self._run_main({"SERVICE_ROLE": "workspace"})
        self.assertEqual(argv[:3], [sys.executable, "-m", "workspace.server"])
        self.assertEqual(imported_bot_modules, set(), "the bot must not be imported")
        self.assertNotIn("workspace.server", sys.modules, "main.py must exec, never import, the workspace")

    def test_role_is_case_and_whitespace_insensitive(self):
        argv, _ = self._run_main({"SERVICE_ROLE": "  Workspace "})
        self.assertEqual(argv[1:3], ["-m", "workspace.server"])


if __name__ == "__main__":
    unittest.main()
