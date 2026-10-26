"""First-run behavior uses empty accounts and disposable directories only."""

import os
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path
from unittest.mock import patch

from xiaomi_ai_bridge.config import Settings
from xiaomi_ai_bridge.panel_auth import PanelAuth
from xiaomi_ai_bridge.xiaomi import XiaomiClient


class FirstRunTests(unittest.IsolatedAsyncioTestCase):
    def test_panel_starts_without_xiaomi_credentials_or_models(self):
        with patch.dict(os.environ, {}, clear=True):
            settings = Settings.from_env()
        self.assertEqual(settings.mi_user_id, "")
        self.assertEqual(settings.mi_pass_token, "")
        self.assertFalse(settings.ai_configured)
        self.assertTrue(settings.panel_auth_required)
        self.assertTrue(settings.ai_gateway_auth_required)

    async def test_empty_xiaomi_account_never_starts_an_outbound_session(self):
        with tempfile.TemporaryDirectory() as temporary, patch.dict(os.environ, {}, clear=True):
            settings = replace(Settings.from_env(), state_dir=Path(temporary))
            client = XiaomiClient(settings)
            with patch("xiaomi_ai_bridge.xiaomi.aiohttp.ClientSession") as session:
                with self.assertRaisesRegex(RuntimeError, "尚未连接小米账号"):
                    await client.connect()
                session.assert_not_called()
            self.assertFalse((Path(temporary) / "mi-token.json").exists())

    def test_new_admin_requires_twelve_characters_and_no_default_account_exists(self):
        with tempfile.TemporaryDirectory() as temporary:
            auth = PanelAuth(Path(temporary))
            self.assertFalse(auth.configured)
            with self.assertRaisesRegex(ValueError, "12"):
                auth.set_admin("admin", "short")
            self.assertFalse(auth.configured)
            auth.set_admin("fixture-admin", "fixture-admin-password")
            self.assertTrue(auth.configured)


if __name__ == "__main__":
    unittest.main()
