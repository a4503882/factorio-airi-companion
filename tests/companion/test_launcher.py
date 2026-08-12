from __future__ import annotations

import json
from pathlib import Path
import tempfile
import unittest

from fle.companion.launcher import _bridge_command, install_mod


class LauncherInstallTests(unittest.TestCase):
    def test_bridge_command_passes_provider_mode_without_secret_contents(self) -> None:
        command = _bridge_command(
            31501,
            31500,
            provider_config=r"F:\ds.txt",
            api_mode="responses",
            web_search=True,
        )

        self.assertIn("--provider-config", command)
        self.assertIn(r"F:\ds.txt", command)
        self.assertIn("responses", command)
        self.assertIn("--web-search", command)
        self.assertNotIn("api-key", " ".join(command).lower())

    def test_install_preserves_other_mod_entries_and_enables_airi(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            mods = Path(temporary_directory)
            mod_list = mods / "mod-list.json"
            mod_list.write_text(
                json.dumps(
                    {
                        "mods": [
                            {"name": "base", "enabled": True},
                            {"name": "other-mod", "enabled": False},
                            {"name": "airi-companion", "enabled": False},
                        ]
                    }
                ),
                encoding="utf-8",
            )

            installed = install_mod(mods)

            self.assertEqual(installed.name, "airi-companion_0.1.0")
            self.assertTrue((installed / "control.lua").is_file())
            self.assertTrue((installed / "locale" / "zh-CN" / "locale.cfg").is_file())
            result = json.loads(mod_list.read_text(encoding="utf-8"))
            self.assertEqual(
                result["mods"],
                [
                    {"name": "base", "enabled": True},
                    {"name": "other-mod", "enabled": False},
                    {"name": "airi-companion", "enabled": True},
                ],
            )

    def test_install_refuses_to_replace_an_unrecognized_directory(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            mods = Path(temporary_directory)
            target = mods / "airi-companion_0.1.0"
            target.mkdir()
            (target / "info.json").write_text(
                json.dumps({"name": "not-airi"}),
                encoding="utf-8",
            )

            with self.assertRaisesRegex(RuntimeError, "non-AIRI"):
                install_mod(mods)


if __name__ == "__main__":
    unittest.main()
