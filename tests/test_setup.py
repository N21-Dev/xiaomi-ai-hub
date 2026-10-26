"""Installer contracts use only disposable configuration and synthetic files."""

import contextlib
import importlib.util
import io
import os
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

SOURCE = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location("hub_setup", SOURCE / "ops/setup.py")
setup = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(setup)


class SetupTests(unittest.TestCase):
    def make_templates(self, root):
        (root / "config").mkdir()
        (root / ".env.example").write_text("BIND_ADDRESS=127.0.0.1\n")
        (root / "config/bridge.env.example").write_text("PANEL_AUTH_REQUIRED=true\n")

    def test_initialization_creates_private_independent_keys_without_logging_them(self):
        keys = []
        for with_health in (False, True):
            with tempfile.TemporaryDirectory() as temporary:
                root = Path(temporary)
                self.make_templates(root)
                output = io.StringIO()
                with patch.object(setup, "ROOT", root), contextlib.redirect_stdout(output):
                    setup.initialize(with_health)
                key = (root / "config/health-api-key").read_text().strip()
                keys.append(key)
                self.assertGreaterEqual(len(key), 40)
                self.assertNotIn(key, output.getvalue())
                self.assertIn(f"MI_API_KEY={key}\n", (root / "config/health.env").read_text())
                bridge = (root / "config/bridge.env").read_text()
                self.assertNotIn(key, bridge)
                self.assertEqual("HEALTH_SERVICE_URL=" in bridge, with_health)
                self.assertEqual("COMPOSE_PROFILES=health" in (root / ".env").read_text(), with_health)
                if os.name == "posix":
                    for name in (".env", "config/bridge.env", "config/health.env", "config/health-api-key"):
                        self.assertEqual((root / name).stat().st_mode & 0o777, 0o600)
                originals = {p: p.read_bytes() for p in root.rglob("*") if p.is_file()}
                with patch.object(setup, "ROOT", root), self.assertRaises(ValueError):
                    setup.initialize(with_health)
                self.assertTrue(all(p.read_bytes() == content for p, content in originals.items()))
        self.assertNotEqual(*keys)

    def test_existing_config_prevents_partial_initialization(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            self.make_templates(root)
            existing = root / "config/health.env"
            existing.write_text("existing installation")
            with patch.object(setup, "ROOT", root), self.assertRaises(ValueError):
                setup.initialize(False)
            self.assertEqual(existing.read_text(), "existing installation")
            self.assertFalse((root / "config/bridge.env").exists())
            self.assertFalse((root / ".env").exists())

    @unittest.skipUnless(os.name == "posix", "symlink contract requires POSIX")
    def test_dangling_config_symlink_is_preserved(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            self.make_templates(root)
            (root / "config/health-api-key").symlink_to(root / "not-created")
            with patch.object(setup, "ROOT", root), self.assertRaises(ValueError):
                setup.initialize(False)
            self.assertFalse((root / "config/bridge.env").exists())
            self.assertFalse((root / "not-created").exists())

    def test_password_reset_reloads_running_authentication_without_restarting_health(self):
        for running in (False, True):
            calls = []

            def compose(*arguments, **kwargs):
                calls.append(arguments)
                return SimpleNamespace(stdout="fixture-container\n" if running else "")

            with patch.object(setup, "compose", compose), patch("sys.argv", ["setup.py", "admin", "--username", "fixture-admin"]), contextlib.redirect_stdout(io.StringIO()):
                setup.main()
            restarts = [call for call in calls if call[0] == "restart"]
            self.assertEqual(restarts, [("restart", "--no-deps", "xiaomi-ai-bridge")] if running else [])
            self.assertTrue(all("mi-health-hub" not in call for call in calls))


if __name__ == "__main__":
    unittest.main()
