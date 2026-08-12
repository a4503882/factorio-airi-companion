"""Exercise the FLE Python-policy loop against a real Factorio client.

This test is intentionally not auto-discovered. It uses a deterministic local
provider stub, so it makes no paid model request. The supplied save and the
companion mod are copied into a temporary directory before Factorio starts. It
uses normal single-player mode because Factorio benchmark mode sends UDP hello
packets but does not consume incoming bridge commands.
"""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import threading
import time
from typing import Any


REPOSITORY_ROOT = Path(__file__).resolve().parents[2]
if str(REPOSITORY_ROOT) not in sys.path:
    sys.path.insert(0, str(REPOSITORY_ROOT))

from fle.companion.bridge import FactorioBridge, OpenAICompatibleAgent


MOD_SOURCE = REPOSITORY_ROOT / "fle" / "companion" / "factorio_mod"
MOD_LIST_FIXTURE = Path(__file__).with_name("fixtures") / "engine-mod-list.json"
FINAL_RESPONSE = "真实 Factorio 已返回角色状态、局部观察和铁板熔炼 Wiki，策略链路完成。"


class RecordingBridge(FactorioBridge):
    """Retain acceptance evidence without changing the real UDP behavior."""

    def __init__(self, **kwargs: Any) -> None:
        super().__init__(**kwargs)
        self.events: list[tuple[str, dict[str, Any]]] = []
        self.chat_responses: list[str] = []
        self.response_ready = threading.Event()
        self._record_lock = threading.RLock()

    def record_event(
        self,
        event_type: str,
        payload: dict[str, Any] | None = None,
        *,
        status: dict[str, Any] | None = None,
    ) -> None:
        with self._record_lock:
            self.events.append((event_type, dict(payload or {})))
        super().record_event(event_type, payload, status=status)

    def send_chat_response(self, text: str) -> str:
        packet_id = super().send_chat_response(text)
        with self._record_lock:
            self.chat_responses.append(text)
        self.response_ready.set()
        return packet_id


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--factorio", type=Path, required=True)
    parser.add_argument("--save", type=Path, required=True)
    parser.add_argument("--game-udp-port", type=int, default=31510)
    parser.add_argument("--bridge-port", type=int, default=31501)
    parser.add_argument("--startup-timeout", type=float, default=30)
    parser.add_argument("--policy-timeout", type=float, default=30)
    return parser


def _tail(path: Path, count: int = 80) -> str:
    try:
        return "\n".join(
            path.read_text(encoding="utf-8", errors="replace").splitlines()[-count:]
        )
    except OSError:
        return ""


def _wait_for_connection(
    bridge: FactorioBridge,
    process: subprocess.Popen[bytes],
    bridge_failures: list[BaseException],
    timeout: float,
) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if bridge.connected:
            return
        if bridge_failures:
            raise RuntimeError(f"AgentBridge failed: {bridge_failures[0]}")
        if process.poll() is not None:
            raise RuntimeError(f"Factorio exited early with code {process.returncode}")
        time.sleep(0.05)
    raise TimeoutError("timed out waiting for Factorio UDP hello/heartbeat")


def _prepare_mods(root: Path, bridge_port: int) -> Path:
    mods = root / "mods"
    mods.mkdir()
    mod_list = json.loads(MOD_LIST_FIXTURE.read_text(encoding="utf-8"))
    mod_list["mods"] = [
        entry
        for entry in mod_list["mods"]
        if entry.get("name") != "airi-companion-smoke"
    ]
    (mods / "mod-list.json").write_text(
        json.dumps(mod_list, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    companion_mod = mods / "airi-companion_0.1.0"
    shutil.copytree(MOD_SOURCE, companion_mod)
    transport_path = companion_mod / "scripts" / "transport.lua"
    transport = transport_path.read_text(encoding="utf-8")
    setting_lookup = 'return settings.global["airi-companion-bridge-port"].value'
    if setting_lookup not in transport:
        raise RuntimeError("could not isolate the companion Bridge port in test mod")
    transport_path.write_text(
        transport.replace(setting_lookup, f"return {bridge_port}", 1),
        encoding="utf-8",
    )
    return mods


def main() -> int:
    args = build_parser().parse_args()
    factorio = args.factorio.expanduser().resolve()
    save = args.save.expanduser().resolve()
    if not factorio.is_file():
        raise FileNotFoundError(f"Factorio executable not found: {factorio}")
    if not save.is_file():
        raise FileNotFoundError(f"Factorio save not found: {save}")
    for label, port in (
        ("--game-udp-port", args.game_udp_port),
        ("--bridge-port", args.bridge_port),
    ):
        if port < 1024 or port > 65535:
            raise ValueError(f"{label} must be between 1024 and 65535")
    requests: list[dict[str, Any]] = []

    def request_json(path: str, payload: dict[str, Any]) -> dict[str, Any]:
        if path != "/chat/completions":
            raise AssertionError(f"unexpected provider path: {path}")
        requests.append(payload)
        if len(requests) == 1:
            return {
                "choices": [
                    {
                        "message": {
                            "role": "assistant",
                            "content": (
                                "咦，直接 craft_item 炼铁板失败了。"
                                "团子去查查 harness 有没有烧炉子的正确姿势。"
                            ),
                        }
                    }
                ],
                "usage": {"prompt_tokens": 12, "completion_tokens": 14},
            }
        if len(requests) == 2:
            return {
                "choices": [
                    {
                        "message": {
                            "role": "assistant",
                            "content": (
                                "计划：读取角色状态、最近角色、局部环境和实时熔炼资料。\n"
                                "```python\n"
                                "state = status()\n"
                                "companion = nearest(type='character')\n"
                                "nearby = observe(16)\n"
                                "iron = wiki(Prototype.IronPlate)\n"
                                "guide = skill_help('smelting')\n"
                                "print(state['character']['present'])\n"
                                "print(companion)\n"
                                "print(nearby['character']['position'])\n"
                                "print(iron['recipe']['category'])\n"
                                "print('SUCCESS' in guide)\n"
                                "```"
                            ),
                        }
                    }
                ],
                "usage": {"prompt_tokens": 24, "completion_tokens": 18},
            }
        if len(requests) == 3:
            return {
                "choices": [
                    {
                        "message": {
                            "role": "assistant",
                            "content": FINAL_RESPONSE,
                        }
                    }
                ],
                "usage": {"prompt_tokens": 48, "completion_tokens": 10},
            }
        raise AssertionError("policy smoke made more than three provider requests")

    bridge = RecordingBridge(
        listen_port=args.bridge_port,
        factorio_port=args.game_udp_port,
    )
    agent = OpenAICompatibleAgent(
        base_url="https://deterministic.invalid/v1",
        model="deterministic-policy-smoke",
        request_json=request_json,
        max_policy_steps=3,
    )
    bridge.attach_agent(agent)
    bridge_failures: list[BaseException] = []

    def run_bridge() -> None:
        try:
            bridge.serve_forever()
        except BaseException as exc:
            bridge_failures.append(exc)

    with tempfile.TemporaryDirectory(prefix="airi-factorio-policy-smoke-") as temp:
        root = Path(temp)
        mods = _prepare_mods(root, args.bridge_port)
        save_copy = root / "save-copy.zip"
        shutil.copy2(save, save_copy)
        config_source = (
            Path(os.environ.get("APPDATA", ""))
            / "Factorio"
            / "config"
            / "config.ini"
        )
        config_copy = root / "config.ini"
        if config_source.is_file():
            shutil.copy2(config_source, config_copy)
        # The Steam build looks for this file in its process working directory.
        # Keeping it in the disposable test root prevents Steam from replacing
        # our tracked child process with a separately launched game process.
        (root / "steam_appid.txt").write_text("427520\n", encoding="ascii")
        factorio_log = root / "factorio-output.log"
        command = [
            str(factorio),
            "--mod-directory",
            str(mods),
            "--enable-lua-udp",
            str(args.game_udp_port),
            "--disable-audio",
            "--window-size",
            "800x600",
        ]
        if config_copy.is_file():
            command.extend(["--config", str(config_copy)])
        command.extend(["--load-game", str(save_copy)])
        creation_flags = subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0
        process: subprocess.Popen[bytes] | None = None
        bridge_thread: threading.Thread | None = None
        try:
            bridge.open()
            bridge_thread = threading.Thread(
                target=run_bridge,
                name="airi-policy-smoke-bridge",
                daemon=True,
            )
            bridge_thread.start()
            with factorio_log.open("wb") as output:
                process = subprocess.Popen(
                    command,
                    stdout=output,
                    stderr=subprocess.STDOUT,
                    cwd=root,
                    creationflags=creation_flags,
                )
                _wait_for_connection(
                    bridge,
                    process,
                    bridge_failures,
                    args.startup_timeout,
                )
                agent.on_chat(
                    "检查真实游戏状态，确认 FLE Python policy 可以连续执行。",
                    {"source": "deterministic-policy-smoke"},
                    1,
                )
                if not bridge.response_ready.wait(args.policy_timeout):
                    if bridge_failures:
                        raise RuntimeError(
                            f"AgentBridge failed: {bridge_failures[0]}"
                        )
                    if process.poll() is not None:
                        raise RuntimeError(
                            f"Factorio exited before policy completion: "
                            f"{process.returncode}"
                        )
                    raise TimeoutError("policy loop did not return a final response")

            event_types = [event_type for event_type, _ in bridge.events]
            actions = [
                payload.get("action")
                for event_type, payload in bridge.events
                if event_type == "game_command"
            ]
            required_events = {
                "user_message",
                "model_response",
                "model_nonterminal_response",
                "model_policy",
                "policy_result",
                "assistant_message",
            }
            missing_events = sorted(required_events.difference(event_types))
            if missing_events:
                raise RuntimeError(f"trajectory is missing events: {missing_events}")
            if actions != ["status", "nearest", "observe", "wiki", "observe"]:
                raise RuntimeError(f"unexpected policy action sequence: {actions}")
            if bridge.chat_responses != [FINAL_RESPONSE]:
                raise RuntimeError(
                    f"unexpected final response: {bridge.chat_responses!r}"
                )
            if len(requests) != 3 or any("tools" in payload for payload in requests):
                raise RuntimeError("provider did not use the tool-free policy contract")
            recovery_messages = requests[1].get("messages") or []
            recovery_feedback = (
                recovery_messages[-1].get("content", "") if recovery_messages else ""
            )
            if "future work" not in recovery_feedback or "skill_help" not in recovery_feedback:
                raise RuntimeError("future-work promise did not trigger harness recovery")
            messages = requests[2].get("messages") or []
            feedback = messages[-1].get("content", "") if messages else ""
            if (
                "FLE Python policy execution result" not in feedback
                or "Fresh permitted in-game observation" not in feedback
                or "True" not in feedback
                or "smelting" not in feedback
            ):
                raise RuntimeError(
                    "real Factorio feedback was not returned to the model"
                )

            print(
                json.dumps(
                    {
                        "ok": True,
                        "provider": "deterministic-local-stub",
                        "paid_model_requests": 0,
                        "policy_actions": actions,
                        "trajectory_events": event_types,
                        "final_response": bridge.chat_responses[0],
                    },
                    ensure_ascii=False,
                )
            )
            return 0
        except Exception:
            diagnostic_events = [
                {"type": event_type, "payload": payload}
                for event_type, payload in bridge.events
                if event_type
                in {
                    "agent_error",
                    "game_command",
                    "game_command_timeout",
                    "game_result",
                    "model_policy",
                    "policy_result",
                }
            ]
            if diagnostic_events:
                print(
                    json.dumps(diagnostic_events, ensure_ascii=False),
                    file=sys.stderr,
                )
            try:
                mod_lines = [
                    line
                    for line in factorio_log.read_text(
                        encoding="utf-8", errors="replace"
                    ).splitlines()
                    if "[airi-companion]" in line
                ]
            except OSError:
                mod_lines = []
            if mod_lines:
                print("\n".join(mod_lines[-80:]), file=sys.stderr)
            factorio_tail = _tail(factorio_log)
            if factorio_tail:
                print(factorio_tail, file=sys.stderr)
            raise
        finally:
            bridge.request_stop()
            if process is not None and process.poll() is None:
                process.terminate()
                try:
                    process.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait(timeout=5)
            if bridge_thread is not None:
                bridge_thread.join(timeout=5)
            bridge.close()


if __name__ == "__main__":
    raise SystemExit(main())
