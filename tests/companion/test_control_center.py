from __future__ import annotations

from datetime import datetime, timezone
import json
import os
from pathlib import Path
import subprocess
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from fle.companion.control_center_backend import (
    bridge_status_connected,
    build_bridge_command,
    factorio_process_running,
    test_provider as run_provider_test,
)
from fle.companion.control_center_store import (
    ControlCenterStore,
    PromptProfile,
    ProviderProfile,
)
from fle.companion.credentials import MemoryCredentialStore


class ControlCenterStoreTests(unittest.TestCase):
    def test_dango_prompt_is_factorio_native_and_tool_honest(self) -> None:
        prompt_path = (
            Path(__file__).resolve().parents[2]
            / "fle"
            / "companion"
            / "prompts"
            / "dango-factorio-zh.txt"
        )
        text = prompt_path.read_text(encoding="utf-8")

        PromptProfile.create(name="团子·异星工厂陪玩", system_prompt=text)
        for required in (
            "Nauvis",
            "FLE harness 执行的 Python policy",
            "执行结果确认以前，绝不声称",
            "inspect_inventory",
            "wiki(...)",
            "skill_help(...)",
            "普通文字回复会立即结束当前任务",
            "iron-ore",
            "网页结果绝不能作为当前存档状态",
            "永远保持团子的身份",
        ):
            self.assertIn(required, text)
        for minecraft_only_term in ("苦力怕", "小麦", "新方块"):
            self.assertNotIn(minecraft_only_term, text)

    def test_only_exact_known_legacy_dango_prompt_is_migrated(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            store = ControlCenterStore(Path(temporary_directory) / "data")
            legacy = store.save_prompt(
                PromptProfile.create(
                    name="团子·异星工厂陪玩",
                    system_prompt="旧版：每次回复最多调用一个 Factorio 工具。",
                )
            )
            custom = store.save_prompt(
                PromptProfile.create(
                    name="团子·异星工厂陪玩",
                    system_prompt="这是 Master 自己修改过、必须保留的提示词。",
                )
            )
            store.save_settings(selected_prompt_id=legacy.id)

            with patch(
                "fle.companion.control_center_store._LEGACY_DANGO_PROMPT_HASHES",
                frozenset({legacy.sha256}),
            ):
                store.ensure_default_prompt()

            prompts = {prompt.id: prompt for prompt in store.load_prompts()}
            bundled = (
                Path(__file__).resolve().parents[2]
                / "fle"
                / "companion"
                / "prompts"
                / "dango-factorio-zh.txt"
            ).read_text(encoding="utf-8").strip()
            self.assertEqual(prompts[legacy.id].system_prompt, bundled)
            self.assertEqual(prompts[legacy.id].created_at, legacy.created_at)
            self.assertEqual(prompts[custom.id].system_prompt, custom.system_prompt)
            self.assertEqual(
                store.load_settings()["selected_prompt_id"],
                legacy.id,
            )

    def test_import_moves_secret_to_credential_store_only(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            source = root / "ds.txt"
            secret = "test-secret-that-must-not-be-serialized"
            source.write_text(
                f"{secret}\nhttps://api.deepseek.com\ndeepseek-v4-flash\n",
                encoding="utf-8",
            )
            data_root = root / "control-center"
            credentials = MemoryCredentialStore()
            store = ControlCenterStore(data_root)

            profile = store.import_three_line_provider(source, credentials)

            self.assertEqual(credentials.get_secret(profile.credential_id), secret)
            self.assertEqual(profile.api_mode, "responses")
            self.assertTrue(profile.web_search)
            serialized = "\n".join(
                path.read_text(encoding="utf-8")
                for path in data_root.rglob("*")
                if path.is_file()
            )
            self.assertNotIn(secret, serialized)

    def test_session_freezes_prompt_and_writes_secret_free_manifest(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            store = ControlCenterStore(Path(temporary_directory) / "data")
            provider = store.save_provider(
                ProviderProfile.create(
                    name="DeepSeek Lab",
                    base_url="https://api.deepseek.com",
                    model="deepseek-v4-flash",
                )
            )
            prompt = store.save_prompt(
                PromptProfile.create(
                    name="Autonomous experiment",
                    system_prompt="You are AIRI. Observe before acting.",
                )
            )

            session = store.create_session(
                provider=provider,
                prompt=prompt,
                factorio_executable=r"E:\Factorio\factorio.exe",
                game_udp_port=31500,
                bridge_port=31501,
            )

            manifest = json.loads(session.manifest.read_text(encoding="utf-8"))
            self.assertEqual(manifest["prompt"]["sha256"], prompt.sha256)
            self.assertEqual(
                session.prompt_snapshot.read_text(encoding="utf-8").strip(),
                prompt.system_prompt,
            )
            self.assertNotIn("api_key", json.dumps(manifest).lower())
            self.assertNotIn("secret", json.dumps(manifest).lower())

    def test_bridge_command_passes_research_files_but_not_credential(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            store = ControlCenterStore(Path(temporary_directory) / "data")
            provider = ProviderProfile.create(
                name="DeepSeek Responses",
                base_url="https://api.deepseek.com",
                model="deepseek-v4-flash",
                reasoning_effort="max",
            )
            prompt = PromptProfile.create(name="Prompt", system_prompt="Research prompt")
            session = store.create_session(
                provider=provider,
                prompt=prompt,
                factorio_executable="factorio.exe",
                game_udp_port=31500,
                bridge_port=31501,
            )

            command = build_bridge_command(
                provider,
                session,
                bridge_port=31501,
                game_udp_port=31500,
                python_executable="python-test",
            )

            rendered = " ".join(command)
            self.assertIn("--system-prompt-file", command)
            self.assertIn(str(session.event_log), command)
            self.assertIn("--reasoning-effort max", rendered)
            self.assertNotIn("credential", rendered.lower())
            self.assertNotIn("api-key", rendered.lower())


class ControlCenterProviderProbeTests(unittest.TestCase):
    @unittest.skipUnless(os.name == "nt", "Windows process probe")
    def test_factorio_process_probe_never_opens_a_console_window(self) -> None:
        with patch(
            "fle.companion.control_center_backend.subprocess.run"
        ) as run_process:
            run_process.return_value = SimpleNamespace(stdout="INFO: No tasks are running")

            self.assertFalse(factorio_process_running())

        self.assertEqual(
            run_process.call_args.kwargs["creationflags"],
            subprocess.CREATE_NO_WINDOW,
        )

    def test_responses_search_probe_requires_real_search_item(self) -> None:
        profile = ProviderProfile.create(
            name="DeepSeek Responses",
            base_url="https://api.deepseek.com",
            model="deepseek-v4-flash",
        )
        captured: dict[str, object] = {}

        def requester(
            url: str,
            payload: dict[str, object],
            api_key: str,
        ) -> dict[str, object]:
            captured.update({"url": url, "payload": payload, "api_key": api_key})
            return {
                "model": "deepseek-v4-flash",
                "status": "completed",
                "output": [
                    {"type": "reasoning"},
                    {"type": "web_search_call", "status": "completed"},
                    {
                        "type": "message",
                        "content": [
                            {"type": "output_text", "text": "Factorio 2.0.77"}
                        ],
                    },
                ],
                "usage": {"total_tokens": 100},
            }

        result = run_provider_test(
            profile,
            "temporary-test-key",
            native_search=True,
            requester=requester,
        )

        self.assertEqual(captured["url"], "https://api.deepseek.com/responses")
        self.assertEqual(captured["api_key"], "temporary-test-key")
        self.assertEqual(
            captured["payload"]["tool_choice"],
            {"type": "web_search"},
        )
        self.assertEqual(result.web_search_calls, 1)
        self.assertEqual(result.text, "Factorio 2.0.77")

    def test_bridge_status_rejects_stale_connections(self) -> None:
        current = datetime.now(timezone.utc).isoformat()
        self.assertTrue(
            bridge_status_connected(
                {
                    "running": True,
                    "connected": True,
                    "last_factorio_packet_at": current,
                }
            )
        )
        self.assertFalse(
            bridge_status_connected(
                {
                    "running": False,
                    "connected": True,
                    "last_factorio_packet_at": current,
                }
            )
        )


if __name__ == "__main__":
    unittest.main()
