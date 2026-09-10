"""`python main.py` with SERVICE_ROLE=workspace starts the workspace, not the bot.

Railway applies `railway.toml`'s start command to every service built from the
repo, so the second service could otherwise come up as a second bot — and
Telegram allows one polling connection. The guard sits above the bot imports.
"""

from __future__ import annotations

import os
import runpy
import sys
import types
import unittest
from unittest import mock

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


class ServiceRoleGuardTests(unittest.TestCase):
    def _run_main(self, env: dict) -> tuple[int | None, set[str]]:
        fake = types.ModuleType("workspace.server")
        fake.main = lambda argv=None: 7
        before = {m for m in sys.modules if m.startswith("bot")}
        with mock.patch.dict(os.environ, env, clear=False), \
             mock.patch.dict(sys.modules, {"workspace.server": fake}):
            for name in [m for m in sys.modules if m.startswith("bot")]:
                sys.modules.pop(name, None)
            try:
                runpy.run_path(os.path.join(REPO, "main.py"), run_name="__main__")
            except SystemExit as exc:
                code = exc.code
            else:
                code = None
            imported = {m for m in sys.modules if m.startswith("bot")} - before
        return code, imported

    def test_workspace_role_hands_off_before_any_bot_import(self):
        code, imported_bot_modules = self._run_main({"SERVICE_ROLE": "workspace"})
        self.assertEqual(code, 7)
        self.assertEqual(imported_bot_modules, set(), "the bot must not be imported")

    def test_role_is_case_and_whitespace_insensitive(self):
        code, _ = self._run_main({"SERVICE_ROLE": "  Workspace "})
        self.assertEqual(code, 7)


if __name__ == "__main__":
    unittest.main()
