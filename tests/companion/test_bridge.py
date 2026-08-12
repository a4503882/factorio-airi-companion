from __future__ import annotations

import ast
import io
import inspect
import json
from pathlib import Path
import tempfile
import threading
import time
import unittest
from urllib import error as urllib_error
from unittest.mock import patch

from fle.companion.bridge import (
    FactorioBridge,
    BridgeEventLogger,
    ChatCompletionsProvider,
    HeuristicAgent,
    OpenAICompatibleAgent,
    Packet,
    ResponsesProvider,
    _replace_file_with_retry,
    load_provider_config,
    load_system_prompt,
    message_prefers_local_policy,
    response_is_nonterminal_search_detour,
    response_promises_unperformed_work,
    response_requests_web_search,
    task_skill_for_message,
)
from fle.companion.policy_harness import (
    CompanionFactorioNamespace,
    PolicyCancelledError,
    PolicyValidationError,
    UPSTREAM_AGENT_API,
    compose_policy_system_prompt,
    parse_policy_text,
    validate_policy,
)
from fle.env.entities import BuildingBox, Direction, Position, Recipe, ResourcePatch
from fle.env.game_types import Prototype, Resource, Technology


class HeuristicAgentTests(unittest.TestCase):
    def test_follow_in_chinese(self) -> None:
        self.assertEqual(HeuristicAgent.parse_intent("AIRI，跟着我"), ("follow", {}))

    def test_move_to_coordinates(self) -> None:
        self.assertEqual(
            HeuristicAgent.parse_intent("去 20,-10"),
            ("move_to", {"x": 20.0, "y": -10.0}),
        )

    def test_mine_resource_alias(self) -> None:
        self.assertEqual(
            HeuristicAgent.parse_intent("挖 32 铁矿"),
            ("mine_resource", {"resource": "iron-ore", "count": 32}),
        )

    def test_mine_resource_in_english_keeps_requested_count(self) -> None:
        self.assertEqual(
            HeuristicAgent.parse_intent("mine copper-ore 5"),
            ("mine_resource", {"resource": "copper-ore", "count": 5}),
        )

    def test_find_resource_alias(self) -> None:
        self.assertEqual(
            HeuristicAgent.parse_intent("找铁矿"),
            ("find_resource", {"resource": "iron-ore"}),
        )

    def test_find_resource_in_english_keeps_requested_radius(self) -> None:
        self.assertEqual(
            HeuristicAgent.parse_intent("locate copper-ore 48"),
            ("find_resource", {"resource": "copper-ore", "radius": 48}),
        )

    def test_policy_contract_exposes_upstream_python_api(self) -> None:
        prompt = compose_policy_system_prompt("You are AIRI.")
        self.assertIn("FLE Python policy harness", prompt)
        self.assertNotIn("at most 60 lines", prompt)
        self.assertIn("find_resource(resource", prompt)
        self.assertIn("connect_entities", prompt)
        self.assertIn("get_resource_patch", prompt)
        self.assertIn("wiki(subject)", prompt)
        self.assertIn("skill_help(topic=None)", prompt)
        self.assertIn("A prose-only response ends the turn immediately", prompt)

    def test_unknown_text_is_not_guessed(self) -> None:
        self.assertIsNone(HeuristicAgent.parse_intent("给我规划一套蓝瓶产线"))

    def test_obvious_pipeline_request_preloads_existing_mining_skill(self) -> None:
        skill = task_skill_for_message("你试试新建一条流水线")

        self.assertIsNotNone(skill)
        assert skill is not None
        self.assertEqual(skill[0], "burner-mining-line")
        self.assertIn("TURN BUDGET", skill[1])
        self.assertIn("wooden chest", skill[1])
        self.assertIn("craft_item", skill[1])

        smelting = task_skill_for_message("新建一条铁板流水线")
        self.assertIsNotNone(smelting)
        assert smelting is not None
        self.assertEqual(smelting[0], "smelting")


class ProviderTests(unittest.TestCase):
    def test_agent_preloads_pipeline_skill_in_first_provider_request(self) -> None:
        requests: list[dict[str, object]] = []
        completed = threading.Event()

        def request(path: str, payload: dict[str, object]) -> dict[str, object]:
            requests.append(payload)
            return {
                "choices": [
                    {
                        "message": {
                            "role": "assistant",
                            "content": "当前没有指定目标矿种，需要先确认煤矿或铁矿。",
                        }
                    }
                ]
            }

        class FakeBridge:
            listen_address = ("127.0.0.1", 31501)

            def __init__(self) -> None:
                self.events: list[tuple[str, dict[str, object]]] = []

            def execute_command(
                self, action: str, arguments: dict[str, object], timeout: float
            ) -> object:
                raise AssertionError(action)

            def record_event(
                self, event_type: str, payload: dict[str, object] | None = None
            ) -> None:
                self.events.append((event_type, payload or {}))

            def send_plan(self, value: str) -> str:
                return value

            def send_chat_response(self, value: str) -> str:
                completed.set()
                return value

        bridge = FakeBridge()
        agent = OpenAICompatibleAgent(
            base_url="https://provider.example/v1",
            model="test-model",
            request_json=request,
        )
        try:
            agent.attach(bridge)  # type: ignore[arg-type]
            agent.on_chat("你试试新建一条流水线", {"character": {}}, 1)
            self.assertTrue(completed.wait(2), "agent response did not complete")
        finally:
            agent.close()

        first_message = requests[0]["messages"][-1]["content"]
        self.assertIn("Preloaded local task skill", first_message)
        self.assertIn("burner-mining-line", first_message)
        self.assertIn("do not spend a policy calling skill_help", first_message)
        self.assertIn(
            "task_skill_preloaded",
            [event_type for event_type, _ in bridge.events],
        )

    def test_model_transport_error_retries_then_succeeds(self) -> None:
        class Response:
            def __enter__(self) -> Response:
                return self

            def __exit__(self, *args: object) -> None:
                return None

            @staticmethod
            def read() -> bytes:
                return json.dumps(
                    {
                        "choices": [
                            {"message": {"role": "assistant", "content": "OK"}}
                        ]
                    }
                ).encode("utf-8")

        retries: list[tuple[int, int, float, str]] = []
        provider = ChatCompletionsProvider(
            base_url="https://provider.example/v1",
            model="test-model",
            retry_callback=lambda attempt, maximum, delay, error: retries.append(
                (attempt, maximum, delay, type(error).__name__)
            ),
        )
        provider.add_user_message("test")

        with patch(
            "fle.companion.bridge.urllib_request.urlopen",
            side_effect=[urllib_error.URLError(TimeoutError("transient")), Response()],
        ) as request, patch("fle.companion.bridge.time.sleep") as sleep:
            turn = provider.request_turn()

        self.assertEqual(turn.text, "OK")
        self.assertEqual(request.call_count, 2)
        sleep.assert_called_once_with(1.0)
        self.assertEqual(retries, [(1, 3, 1.0, "URLError")])

    def test_model_http_error_is_not_retried(self) -> None:
        provider = ChatCompletionsProvider(
            base_url="https://provider.example/v1",
            model="test-model",
        )
        provider.add_user_message("test")
        error = urllib_error.HTTPError(
            "https://provider.example/v1/chat/completions",
            401,
            "Unauthorized",
            {},
            io.BytesIO(b"invalid credential"),
        )

        with patch(
            "fle.companion.bridge.urllib_request.urlopen",
            side_effect=error,
        ) as request, patch("fle.companion.bridge.time.sleep") as sleep:
            with self.assertRaisesRegex(RuntimeError, "model HTTP 401"):
                provider.request_turn()

        self.assertEqual(request.call_count, 1)
        sleep.assert_not_called()

    def test_status_replace_retries_a_transient_reader_lock(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            source = root / "status.tmp"
            target = root / "status.json"
            source.write_text("ready", encoding="utf-8")
            path_type = type(source)
            real_replace = path_type.replace
            attempts = 0

            def replace_with_one_lock(path: Path, destination: Path) -> Path:
                nonlocal attempts
                attempts += 1
                if attempts == 1:
                    raise PermissionError(13, "simulated reader lock", str(destination))
                return real_replace(path, destination)

            with patch.object(path_type, "replace", replace_with_one_lock), patch(
                "fle.companion.bridge.time.sleep"
            ) as sleep:
                _replace_file_with_retry(source, target)

            self.assertEqual(attempts, 2)
            sleep.assert_called_once_with(0.005)
            self.assertEqual(target.read_text(encoding="utf-8"), "ready")

    def test_status_lock_never_stops_event_processing(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            events = root / "events.jsonl"
            status = root / "status.json"
            locked = PermissionError(13, "simulated persistent reader lock", str(status))

            with patch(
                "fle.companion.bridge._replace_file_with_retry",
                side_effect=locked,
            ), patch("builtins.print") as print_message:
                logger = BridgeEventLogger(event_log=events, status_file=status)
                logger.factorio_packet(
                    Packet(id="hello-locked", type="hello", payload={})
                )
                logger.close()

            event_types = [
                json.loads(line)["type"]
                for line in events.read_text(encoding="utf-8").splitlines()
            ]
            self.assertEqual(
                event_types,
                ["bridge_created", "factorio_packet", "bridge_stopped"],
            )
            self.assertFalse(status.exists())
            self.assertEqual(list(root.glob(".*.tmp")), [])
            self.assertEqual(print_message.call_count, 1)

    def test_system_prompt_file_and_provider_settings_are_applied(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            prompt_path = Path(temporary_directory) / "prompt.txt"
            prompt_path.write_text("Custom research prompt", encoding="utf-8")
            captured: dict[str, object] = {}

            def request(path: str, payload: dict[str, object]) -> dict[str, object]:
                captured.update({"path": path, "payload": payload})
                return {
                    "status": "completed",
                    "output": [
                        {
                            "type": "message",
                            "content": [{"type": "output_text", "text": "OK"}],
                        }
                    ],
                }

            provider = ResponsesProvider(
                base_url="https://api.example",
                model="example",
                system_prompt=load_system_prompt(prompt_path),
                reasoning_effort="max",
                request_json=request,
            )
            provider.add_user_message("test")
            provider.request_turn()

            payload = captured["payload"]
            self.assertEqual(payload["instructions"], "Custom research prompt")
            self.assertEqual(payload["reasoning"], {"effort": "max"})

    def test_event_logger_writes_status_and_trajectory(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            events = root / "events.jsonl"
            status = root / "status.json"
            logger = BridgeEventLogger(
                event_log=events,
                status_file=status,
                metadata={"model": "test-model"},
            )
            logger.factorio_packet(
                Packet(
                    id="hello-1",
                    type="hello",
                    payload={"mod": "airi-companion"},
                )
            )
            logger.close()

            status_body = json.loads(status.read_text(encoding="utf-8"))
            event_types = [
                json.loads(line)["type"]
                for line in events.read_text(encoding="utf-8").splitlines()
            ]
            self.assertFalse(status_body["running"])
            self.assertFalse(status_body["connected"])
            self.assertEqual(status_body["model"], "test-model")
            self.assertIn("factorio_packet", event_types)
            self.assertIn("bridge_stopped", event_types)

    def test_three_line_provider_config(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            path = Path(temporary_directory) / "provider.txt"
            path.write_text(
                "secret-key\nhttps://api.deepseek.com/\ndeepseek-v4-flash\n",
                encoding="utf-8",
            )

            config = load_provider_config(path)

            self.assertEqual(config.api_key, "secret-key")
            self.assertEqual(config.base_url, "https://api.deepseek.com")
            self.assertEqual(config.model, "deepseek-v4-flash")

    def test_responses_provider_replays_search_and_policy_feedback(self) -> None:
        requests: list[tuple[str, dict[str, object]]] = []
        replies = [
            {
                "status": "completed",
                "output": [
                    {"type": "reasoning", "id": "reasoning-1", "content": []},
                    {
                        "type": "web_search_call",
                        "id": "search-1",
                        "status": "completed",
                    },
                    {
                        "type": "message",
                        "role": "assistant",
                        "content": [
                            {
                                "type": "output_text",
                                "text": "计划：跟随。\n```python\nfollow()\n```",
                            }
                        ],
                    },
                ],
            },
            {
                "status": "completed",
                "output": [
                    {
                        "type": "message",
                        "role": "assistant",
                        "content": [
                            {"type": "output_text", "text": "已经开始跟随。"}
                        ],
                    }
                ],
            },
        ]

        def request(path: str, payload: dict[str, object]) -> dict[str, object]:
            requests.append((path, payload))
            return replies.pop(0)

        provider = ResponsesProvider(
            base_url="https://api.deepseek.com",
            model="deepseek-v4-flash",
            api_key="not-logged",
            request_json=request,
            enable_web_search=True,
        )
        provider.add_user_message("查一下资料，然后跟着我")

        first = provider.request_turn()
        self.assertIn("follow()", first.text)
        provider.add_environment_message("FLE policy result: follow mode active")
        second = provider.request_turn()

        self.assertEqual(second.text, "已经开始跟随。")
        self.assertEqual([request[0] for request in requests], ["/responses"] * 2)
        first_payload = requests[0][1]
        self.assertEqual(first_payload["tools"], [{"type": "web_search"}])
        replay_types = [
            item.get("type")
            for item in requests[1][1]["input"]
            if isinstance(item, dict)
        ]
        replay_roles = [
            item.get("role")
            for item in requests[1][1]["input"]
            if isinstance(item, dict)
        ]
        self.assertIn("web_search_call", replay_types)
        self.assertIn("message", replay_types)
        self.assertIn("user", replay_roles)

    def test_responses_provider_separates_reasoning_from_output(self) -> None:
        def request(path: str, payload: dict[str, object]) -> dict[str, object]:
            self.assertEqual(path, "/responses")
            return {
                "id": "response-with-reasoning",
                "status": "completed",
                "output": [
                    {
                        "type": "reasoning",
                        "id": "reasoning-1",
                        "content": [
                            {
                                "type": "reasoning_text",
                                "text": "先检查库存。",
                            },
                            {
                                "type": "reasoning_text",
                                "text": "然后执行动作。",
                            },
                        ],
                        "summary": [],
                    },
                    {
                        "type": "message",
                        "role": "assistant",
                        "content": [
                            {"type": "output_text", "text": "最终输出。"}
                        ],
                    },
                ],
            }

        provider = ResponsesProvider(
            base_url="https://api.deepseek.com",
            model="deepseek-v4-flash",
            request_json=request,
        )
        provider.add_user_message("执行任务")

        turn = provider.request_turn()

        self.assertEqual(turn.reasoning_text, "先检查库存。\n然后执行动作。")
        self.assertEqual(turn.text, "最终输出。")
        self.assertNotIn(turn.reasoning_text, turn.text)
        self.assertEqual(turn.metadata["reasoning_items"], 1)
        self.assertEqual(
            turn.metadata["reasoning_text_chars"], len(turn.reasoning_text)
        )

    def test_responses_provider_rejects_and_replays_unexpected_function_call(
        self,
    ) -> None:
        requests: list[tuple[str, dict[str, object]]] = []
        replies = [
            {
                "status": "completed",
                "output": [
                    {"type": "reasoning", "id": "reasoning-1", "content": []},
                    {
                        "type": "message",
                        "role": "assistant",
                        "content": [
                            {"type": "output_text", "text": "团子先调用本地 Wiki。"}
                        ],
                    },
                    {
                        "type": "function_call",
                        "id": "function-1",
                        "call_id": "call-1",
                        "name": "wiki",
                        "arguments": '{"subject":"iron-plate"}',
                    },
                ],
            },
            {
                "status": "completed",
                "output": [
                    {
                        "type": "message",
                        "role": "assistant",
                        "content": [
                            {
                                "type": "output_text",
                                "text": (
                                    "```python\n"
                                    "info = wiki(Prototype.IronPlate)\n"
                                    "print(info['recipe']['category'])\n"
                                    "```"
                                ),
                            }
                        ],
                    }
                ],
            },
        ]

        def request(path: str, payload: dict[str, object]) -> dict[str, object]:
            requests.append((path, payload))
            return replies.pop(0)

        provider = ResponsesProvider(
            base_url="https://api.deepseek.com",
            model="deepseek-v4-flash",
            api_key="not-logged",
            request_json=request,
            enable_web_search=True,
        )
        provider.add_user_message("继续烧铁板")

        first = provider.request_turn()
        self.assertEqual(first.text, "团子先调用本地 Wiki。")
        self.assertEqual(
            first.metadata["rejected_function_calls"],
            [
                {
                    "name": "wiki",
                    "call_id": "call-1",
                    "arguments": '{"subject":"iron-plate"}',
                }
            ],
        )
        provider.add_environment_message("Rewrite the call as a Python policy.")
        second = provider.request_turn()

        self.assertIn("wiki(Prototype.IronPlate)", second.text)
        replay = requests[1][1]["input"]
        function_output = next(
            item
            for item in replay
            if isinstance(item, dict) and item.get("type") == "function_call_output"
        )
        self.assertEqual(function_output["call_id"], "call-1")
        self.assertIn("fenced Python policy", function_output["output"])

    def test_responses_provider_can_gate_search_for_local_first_turn(self) -> None:
        requests: list[dict[str, object]] = []
        replies = [
            {
                "status": "completed",
                "output": [
                    {
                        "type": "message",
                        "role": "assistant",
                        "content": [{"type": "output_text", "text": "local"}],
                    }
                ],
            },
            {
                "status": "completed",
                "output": [
                    {
                        "type": "web_search_call",
                        "id": "search-1",
                        "status": "completed",
                    },
                    {
                        "type": "message",
                        "role": "assistant",
                        "content": [{"type": "output_text", "text": "searched"}],
                    },
                ],
            },
            {
                "status": "completed",
                "output": [
                    {
                        "type": "message",
                        "role": "assistant",
                        "content": [{"type": "output_text", "text": "local again"}],
                    }
                ],
            },
        ]

        def request(path: str, payload: dict[str, object]) -> dict[str, object]:
            self.assertEqual(path, "/responses")
            requests.append(payload)
            return replies.pop(0)

        provider = ResponsesProvider(
            base_url="https://api.deepseek.com",
            model="deepseek-v4-flash",
            request_json=request,
            enable_web_search=True,
        )
        provider.add_user_message("继续完成铁板任务")
        provider.suppress_web_search()
        first = provider.request_turn()
        self.assertFalse(first.metadata["web_search_exposed"])

        provider.add_environment_message("External knowledge is required.")
        self.assertTrue(provider.allow_web_search_once())
        second = provider.request_turn()
        self.assertTrue(second.metadata["web_search_exposed"])

        provider.add_environment_message("Return to the local policy.")
        third = provider.request_turn()
        self.assertFalse(third.metadata["web_search_exposed"])
        self.assertNotIn("tools", requests[0])
        self.assertEqual(requests[1]["tools"], [{"type": "web_search"}])
        self.assertNotIn("tools", requests[2])

    def test_chat_completions_provider_requests_python_policy_without_tools(
        self,
    ) -> None:
        requests: list[tuple[str, dict[str, object]]] = []

        def request(path: str, payload: dict[str, object]) -> dict[str, object]:
            requests.append((path, payload))
            return {
                "choices": [
                    {
                        "message": {
                            "role": "assistant",
                            "reasoning_content": "先观察，再打印状态。",
                            "content": (
                                "```python\nstate = observe(32)\nprint(state)\n```"
                            ),
                        }
                    }
                ]
            }

        provider = ChatCompletionsProvider(
            base_url="https://provider.example/v1",
            model="example-model",
            request_json=request,
        )
        provider.add_user_message("观察")
        turn = provider.request_turn()

        self.assertEqual(requests[0][0], "/chat/completions")
        self.assertNotIn("tools", requests[0][1])
        self.assertIn("observe(32)", turn.text)
        self.assertEqual(turn.reasoning_text, "先观察，再打印状态。")
        self.assertNotIn(turn.reasoning_text, turn.text)


class CancellationTests(unittest.TestCase):
    def test_bridge_correlates_chat_and_routes_cancel_request(self) -> None:
        class FakeAgent:
            def __init__(self) -> None:
                self.chats: list[tuple[object, ...]] = []
                self.cancels: list[dict[str, object]] = []

            def on_chat(self, *args: object) -> None:
                self.chats.append(args)

            def cancel_current_turn(self, **kwargs: object) -> bool:
                self.cancels.append(kwargs)
                return True

        bridge = FactorioBridge(listen_port=0, factorio_port=0)
        agent = FakeAgent()
        bridge.agent = agent  # type: ignore[assignment]

        bridge._handle_packet(
            Packet(
                id="chat-turn-1",
                type="chat",
                payload={"text": "hello", "context": {}, "player_index": 3},
            )
        )
        bridge._handle_packet(
            Packet(
                id="cancel-packet-1",
                type="cancel_chat",
                payload={"request_id": "chat-turn-1", "player_index": 3},
            )
        )

        self.assertEqual(agent.chats, [("hello", {}, 3, "chat-turn-1")])
        self.assertEqual(
            agent.cancels,
            [
                {
                    "reason": "Stopped by player",
                    "request_id": "chat-turn-1",
                    "player_index": 3,
                }
            ],
        )

    def test_cancelled_http_turn_does_not_block_or_publish_late_reply(self) -> None:
        first_started = threading.Event()
        release_first = threading.Event()
        second_completed = threading.Event()
        request_lock = threading.Lock()
        request_count = 0

        def request(path: str, payload: dict[str, object]) -> dict[str, object]:
            nonlocal request_count
            with request_lock:
                request_count += 1
                current = request_count
            if current == 1:
                first_started.set()
                release_first.wait(5)
                content = "这条已经取消，不应显示。"
            else:
                content = "第二轮完成。"
            return {
                "choices": [
                    {"message": {"role": "assistant", "content": content}}
                ]
            }

        class FakeBridge:
            listen_address = ("127.0.0.1", 31501)

            def __init__(self) -> None:
                self.events: list[tuple[str, dict[str, object]]] = []
                self.responses: list[tuple[str, str | None]] = []
                self.plans: list[tuple[str, str | None]] = []

            def execute_command(
                self, action: str, arguments: dict[str, object], timeout: float
            ) -> object:
                raise AssertionError(action)

            def record_event(
                self, event_type: str, payload: dict[str, object] | None = None
            ) -> None:
                self.events.append((event_type, payload or {}))

            def send_plan(
                self, text: str, *, request_id: str | None = None
            ) -> str:
                self.plans.append((text, request_id))
                return "plan"

            def send_chat_response(
                self, text: str, *, request_id: str | None = None
            ) -> str:
                self.responses.append((text, request_id))
                if request_id == "chat-turn-2":
                    second_completed.set()
                return "chat"

        bridge = FakeBridge()
        agent = OpenAICompatibleAgent(
            base_url="https://provider.example/v1",
            model="test-model",
            request_json=request,
        )
        try:
            agent.attach(bridge)  # type: ignore[arg-type]
            agent.on_chat("先等着", {}, 1, "chat-turn-1")
            self.assertTrue(first_started.wait(1), "first provider call did not start")

            started = time.monotonic()
            self.assertTrue(
                agent.cancel_current_turn(
                    reason="Stopped by player",
                    request_id="chat-turn-1",
                    player_index=1,
                )
            )
            self.assertLess(time.monotonic() - started, 0.5)

            agent.on_chat("第二条消息", {}, 1, "chat-turn-2")
            self.assertTrue(
                second_completed.wait(2),
                "cancelled provider call kept the next turn blocked",
            )
        finally:
            release_first.set()
            agent.close()

        self.assertEqual(bridge.responses, [("第二轮完成。", "chat-turn-2")])
        self.assertIn(
            "agent_turn_cancel_requested",
            [event_type for event_type, _ in bridge.events],
        )

    def test_policy_wait_is_interrupted_by_turn_cancellation(self) -> None:
        cancelled = threading.Event()

        def command_runner(
            action: str, arguments: dict[str, object], timeout: float
        ) -> object:
            raise AssertionError(action)

        def cancel_soon() -> None:
            time.sleep(0.05)
            cancelled.set()

        namespace = CompanionFactorioNamespace(command_runner)
        threading.Thread(target=cancel_soon, daemon=True).start()
        started = time.monotonic()
        with self.assertRaises(PolicyCancelledError):
            namespace.evaluate(
                "wait(30)",
                timeout=2,
                cancel_requested=cancelled.is_set,
                cancel_wait=cancelled.wait,
            )
        self.assertLess(time.monotonic() - started, 1.0)

    def test_pending_factorio_command_is_interrupted_by_turn_cancellation(self) -> None:
        cancelled = threading.Event()

        class BlockingBridge(FactorioBridge):
            def send_command(
                self,
                action: str,
                arguments: dict[str, object] | None = None,
                callback: object = None,
            ) -> str:
                return "blocked-command"

        def cancel_soon() -> None:
            time.sleep(0.05)
            cancelled.set()

        bridge = BlockingBridge(listen_port=0, factorio_port=0)
        threading.Thread(target=cancel_soon, daemon=True).start()
        started = time.monotonic()
        with self.assertRaises(PolicyCancelledError):
            bridge.execute_command(
                "observe",
                {},
                timeout=10,
                cancel_requested=cancelled.is_set,
            )
        self.assertLess(time.monotonic() - started, 1.0)


class PolicyHarnessTests(unittest.TestCase):
    def test_namespace_exposes_every_discovered_upstream_agent_tool(self) -> None:
        def unused_command(
            action: str, arguments: dict[str, object], timeout: float
        ) -> object:
            raise AssertionError(action)

        namespace = CompanionFactorioNamespace(unused_command)
        agent_tool_root = (
            Path(__file__).resolve().parents[2] / "fle" / "env" / "tools" / "agent"
        )
        discovered = {
            directory.name
            for directory in agent_tool_root.iterdir()
            if directory.is_dir()
            and (directory / "client.py").is_file()
            and (directory / "server.lua").is_file()
        }
        missing = {
            name
            for name in UPSTREAM_AGENT_API
            if not callable(getattr(namespace, name, None))
        }

        self.assertEqual(UPSTREAM_AGENT_API, discovered)
        self.assertEqual(missing, set())
        self.assertIn("(0, 0)", namespace.evaluate("print(score())"))

    def test_namespace_parameter_names_track_upstream_clients(self) -> None:
        namespace = CompanionFactorioNamespace(lambda *args: None)
        agent_tool_root = (
            Path(__file__).resolve().parents[2] / "fle" / "env" / "tools" / "agent"
        )

        for name in sorted(UPSTREAM_AGENT_API):
            client = agent_tool_root / name / "client.py"
            tree = ast.parse(client.read_text(encoding="utf-8"))
            upstream_call = next(
                node
                for node in ast.walk(tree)
                if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
                and node.name == "__call__"
            )
            upstream_parameters = [
                argument.arg
                for argument in [
                    *upstream_call.args.posonlyargs,
                    *upstream_call.args.args,
                ][1:]
            ]
            if upstream_call.args.vararg is not None:
                upstream_parameters.append("*" + upstream_call.args.vararg.arg)
            upstream_parameters.extend(
                argument.arg for argument in upstream_call.args.kwonlyargs
            )
            if upstream_call.args.kwarg is not None:
                upstream_parameters.append("**" + upstream_call.args.kwarg.arg)

            local_parameters = []
            for parameter in inspect.signature(getattr(namespace, name)).parameters.values():
                if parameter.kind is inspect.Parameter.VAR_POSITIONAL:
                    local_parameters.append("*" + parameter.name)
                elif parameter.kind is inspect.Parameter.VAR_KEYWORD:
                    local_parameters.append("**" + parameter.name)
                else:
                    local_parameters.append(parameter.name)

            with self.subTest(tool=name):
                self.assertEqual(local_parameters, upstream_parameters)

    def test_upstream_parser_extracts_fenced_policy_and_usage(self) -> None:
        policy = parse_policy_text(
            "计划：先观察。\n```python\nstate = observe(32)\nprint(state)\n```",
            {"input_tokens": 11, "output_tokens": 7},
        )

        self.assertIsNotNone(policy)
        assert policy is not None
        self.assertIn("state = observe(32)", policy.code)
        self.assertEqual(policy.meta.total_tokens, 18)

    def test_plain_final_reply_is_not_misread_as_policy(self) -> None:
        self.assertIsNone(parse_policy_text("已经确认流水线开始产煤了。"))

    def test_future_work_promise_is_not_a_terminal_reply(self) -> None:
        stopped_reply = (
            "咦，采到 16 块铁矿了，但直接 craft_item 炼铁板失败了——"
            "看来铁板要用石炉烧，不能凭空手搓喵。"
            "团子去查查 harness 有没有烧炉子的正确姿势。"
        )
        self.assertTrue(response_promises_unperformed_work(stopped_reply))
        self.assertTrue(response_promises_unperformed_work("I'll check the docs next."))
        self.assertTrue(
            response_promises_unperformed_work(
                "找到官方文档了，再看一眼工具列表喵~"
            )
        )
        self.assertTrue(
            response_promises_unperformed_work("团子现在去游戏里翻手册确认任务喵~")
        )
        self.assertFalse(
            response_promises_unperformed_work(
                "已经检查过炉子库存，铁板从 0 增加到了 4。"
            )
        )

    def test_local_first_and_explicit_web_search_markers_are_distinct(self) -> None:
        self.assertTrue(message_prefers_local_policy("我更新了，现在继续未完成的"))
        self.assertTrue(message_prefers_local_policy("build a coal mining line"))
        self.assertFalse(message_prefers_local_policy("Factorio 2.1 哪天发布？"))
        self.assertEqual(
            response_requests_web_search(
                "WEB_SEARCH_NEEDED: Factorio 2.0 mod furnace mechanics"
            ),
            "Factorio 2.0 mod furnace mechanics",
        )
        self.assertIsNone(response_requests_web_search("团子直接查本地 Wiki。"))
        self.assertTrue(
            response_is_nonterminal_search_detour(
                "找到官方文档了，再看一眼工具列表喵~",
                {"web_search_calls": 11},
            )
        )
        self.assertFalse(
            response_is_nonterminal_search_detour(
                "Factorio 2.1 的发布日期是……",
                {"web_search_calls": 1},
            )
        )
        self.assertFalse(
            response_promises_unperformed_work(
                "团子还能再给它塞煤，或者顺便看看箱子容量。"
            )
        )

    def test_policy_rejects_host_imports(self) -> None:
        with self.assertRaises(PolicyValidationError):
            validate_policy("import os\nprint(os.getcwd())")

    def test_policy_has_no_arbitrary_source_line_limit(self) -> None:
        source = "\n".join(f"value_{index} = {index}" for index in range(80))

        validate_policy(source)

    def test_policy_rejects_internal_namespace_escape_hatches(self) -> None:
        for source in (
            "eval_with_timeout(\"__import__('os')\")",
            "load(b'not-a-safe-namespace')",
            "raise KeyboardInterrupt()",
            "value = 10 ** 100000",
        ):
            with self.subTest(source=source):
                with self.assertRaises(PolicyValidationError):
                    validate_policy(source)

    def test_namespace_executes_multiple_upstream_style_actions(self) -> None:
        calls: list[tuple[str, dict[str, object]]] = []

        def command(action: str, arguments: dict[str, object], timeout: float):
            calls.append((action, arguments))
            if action == "can_place_entity":
                return {"can_place": True}
            if action == "place_entity":
                return {
                    "name": arguments["item"],
                    "type": "mining-drill",
                    "position": {"x": arguments["x"], "y": arguments["y"]},
                    "direction": arguments["direction_value"],
                    "direction_name": arguments["direction"],
                    "pickup_position": {"x": 10, "y": 19.5},
                    "drop_position": {"x": 10, "y": 21.5},
                }
            if action == "insert_item":
                return {
                    "inserted": arguments["count"],
                    "target": {
                        "name": arguments["target_name"],
                        "position": {"x": arguments["x"], "y": arguments["y"]},
                        "direction": 8,
                        "inventories": {"fuel": {"coal": arguments["count"]}},
                    },
                }
            if action == "inspect_inventory":
                return {"contents": {"coal": 5}}
            raise AssertionError(action)

        namespace = CompanionFactorioNamespace(command)
        output = namespace.evaluate(
            "\n".join(
                (
                    "p = Position(10, 20)",
                    "assert can_place_entity("
                    "Prototype.BurnerMiningDrill, Direction.SOUTH, p)",
                    "drill = place_entity("
                    "Prototype.BurnerMiningDrill, Direction.SOUTH, p)",
                    "print(drill.drop_position)",
                    "drill = insert_item(Prototype.Coal, drill, 5)",
                    "print(inspect_inventory(drill)[Prototype.Coal])",
                )
            )
        )

        self.assertEqual([name for name, _ in calls], [
            "can_place_entity",
            "place_entity",
            "insert_item",
            "inspect_inventory",
        ])
        self.assertEqual(calls[1][1]["direction"], "south")
        self.assertEqual(calls[1][1]["direction_value"], Direction.SOUTH.value)
        self.assertIn("Position(x=10.0, y=21.5)", output)
        self.assertIn("5", output)

    def test_upstream_query_results_use_upstream_value_models(self) -> None:
        calls: list[tuple[str, dict[str, object]]] = []

        def command(
            action: str, arguments: dict[str, object], timeout: float
        ) -> object:
            calls.append((action, arguments))
            if action == "nearest":
                return {
                    "name": "water",
                    "position": {"x": 12.5, "y": -3.5},
                    "distance": 4.0,
                }
            if action == "get_resource_patch":
                return {
                    "name": "water",
                    "size": 9,
                    "bounding_box": {
                        "left_top": {"x": 10, "y": -5},
                        "right_bottom": {"x": 13, "y": -2},
                    },
                }
            if action == "wiki":
                return {
                    "query": "transport-belt",
                    "recipe": {
                        "name": "transport-belt",
                        "category": "crafting",
                        "energy": 0.5,
                        "force_enabled": True,
                        "ingredients": [
                            {"name": "iron-plate", "amount": 1, "type": "item"},
                            {"name": "iron-gear-wheel", "amount": 1, "type": "item"},
                        ],
                        "products": [
                            {
                                "name": "transport-belt",
                                "amount": 2,
                                "probability": 1,
                                "type": "item",
                            }
                        ],
                    },
                }
            raise AssertionError(action)

        namespace = CompanionFactorioNamespace(command)
        water = namespace.nearest(type=Resource.Water)
        patch = namespace.get_resource_patch(Resource.Water, water, 30)
        recipe = namespace.get_prototype_recipe(Prototype.TransportBelt)

        self.assertEqual(water, Position(12.5, -3.5))
        self.assertIsInstance(patch, ResourcePatch)
        self.assertEqual(patch.size, 9)
        self.assertEqual(patch.bounding_box.center, Position(11.5, -3.5))
        self.assertIsInstance(recipe, Recipe)
        self.assertEqual(recipe.ingredients[1].name, "iron-gear-wheel")
        self.assertEqual(recipe.products[0].count, 2)
        self.assertEqual(
            [name for name, _ in calls],
            ["nearest", "get_resource_patch", "wiki"],
        )

    def test_upstream_mutations_and_single_agent_exception_keep_signatures(self) -> None:
        calls: list[tuple[str, dict[str, object]]] = []

        def command(
            action: str, arguments: dict[str, object], timeout: float
        ) -> object:
            calls.append((action, arguments))
            if action == "extract_item":
                return {"extracted": 3}
            if action == "set_research":
                return [
                    {"name": "automation-science-pack", "count": 10, "type": "item"}
                ]
            if action == "get_research_progress":
                return [
                    {"name": "automation-science-pack", "count": 4, "type": "item"}
                ]
            if action == "set_entity_recipe":
                return {
                    "name": "assembling-machine-1",
                    "type": "assembling-machine",
                    "position": {"x": 2, "y": 3},
                    "recipe": arguments["recipe"],
                }
            if action == "launch_rocket":
                return {
                    "name": "rocket-silo",
                    "type": "rocket-silo",
                    "position": {"x": 20, "y": 30},
                    "launched": True,
                }
            raise AssertionError(action)

        namespace = CompanionFactorioNamespace(command)
        source = Position(2, 3)

        self.assertEqual(namespace.extract_item(Prototype.IronPlate, source, 3), 3)
        required = namespace.set_research(Technology.Automation)
        remaining = namespace.get_research_progress(Technology.Automation)
        machine = namespace.set_entity_recipe(source, Prototype.TransportBelt)
        silo = namespace.launch_rocket(Position(20, 30))
        self.assertTrue(namespace.send_message("single-agent no-op"))

        self.assertEqual(required[0].count, 10)
        self.assertEqual(remaining[0].count, 4)
        self.assertEqual(machine.recipe, "transport-belt")
        self.assertEqual(silo.name, "rocket-silo")
        self.assertEqual(
            [name for name, _ in calls],
            [
                "extract_item",
                "set_research",
                "get_research_progress",
                "set_entity_recipe",
                "launch_rocket",
            ],
        )

    def test_upstream_defaults_and_return_contracts_are_preserved(self) -> None:
        calls: list[tuple[str, dict[str, object]]] = []

        def command(
            action: str, arguments: dict[str, object], timeout: float
        ) -> object:
            calls.append((action, arguments))
            if action == "nearest":
                return {"position": {"x": 4, "y": 5}}
            if action == "rotate_entity":
                return {
                    "name": "transport-belt",
                    "position": {"x": arguments["x"], "y": arguments["y"]},
                    "direction": arguments["direction_value"],
                }
            raise AssertionError(action)

        namespace = CompanionFactorioNamespace(command)

        self.assertEqual(
            namespace.nearest(type=Prototype.TransportBelt),
            Position(4, 5),
        )
        rotated = namespace.rotate_entity(Position(1, 2))
        self.assertEqual(rotated.direction, Direction.NORTH)
        self.assertTrue(namespace.sleep(0))
        self.assertEqual(namespace.wait(0), 0.0)
        self.assertEqual(namespace.score("ignored", benchmark=True), (0, 0))
        self.assertEqual(calls[0][1]["radius"], 500.0)
        self.assertEqual(calls[1][1]["direction"], "north")

    def test_craft_item_waits_for_completed_output_not_only_queue_acceptance(self) -> None:
        inventory_reads = iter(({}, {"transport-belt": 2}))
        calls: list[str] = []

        def command(
            action: str, arguments: dict[str, object], timeout: float
        ) -> object:
            calls.append(action)
            if action == "inspect_inventory":
                return {"contents": next(inventory_reads)}
            if action == "craft_item":
                return {
                    "recipe": "transport-belt",
                    "requested": 2,
                    "queued": 1,
                    "expected_items": 2,
                    "output_per_craft": 2,
                    "energy": 0.5,
                }
            raise AssertionError(action)

        namespace = CompanionFactorioNamespace(command)

        self.assertEqual(namespace.craft_item(Prototype.TransportBelt, 2), 2)
        self.assertEqual(
            calls,
            ["inspect_inventory", "craft_item", "inspect_inventory"],
        )

    def test_layout_and_connection_calls_keep_upstream_argument_order(self) -> None:
        calls: list[tuple[str, dict[str, object]]] = []

        def command(
            action: str, arguments: dict[str, object], timeout: float
        ) -> object:
            calls.append((action, arguments))
            if action == "nearest_buildable":
                return {
                    "left_top": {"x": 5, "y": 6},
                    "right_bottom": {"x": 8, "y": 10},
                }
            if action == "connect_entities":
                if arguments["dry_run"]:
                    return {
                        "number_of_entities_required": 4,
                        "number_of_entities_available": 20,
                    }
                return {
                    "name": "belt-group",
                    "connection_type": "transport-belt",
                    "position": {"x": 1, "y": 0},
                    "entities": [
                        {
                            "name": "transport-belt",
                            "position": {"x": x, "y": 0},
                            "direction": Direction.EAST.value,
                        }
                        for x in range(4)
                    ],
                }
            raise AssertionError(action)

        namespace = CompanionFactorioNamespace(command)
        box = namespace.nearest_buildable(
            Prototype.StoneFurnace,
            BuildingBox(height=4, width=3),
            Position(0, 0),
        )
        amount = namespace.get_connection_amount(
            Position(0, 0),
            Position(3, 0),
            Prototype.TransportBelt,
        )
        group = namespace.connect_entities(
            Position(0, 0),
            Position(3, 0),
            Prototype.TransportBelt,
        )

        self.assertEqual(box.width(), 3)
        self.assertEqual(box.height(), 4)
        self.assertEqual(amount, 4)
        self.assertEqual(len(group.belts), 4)
        self.assertEqual(calls[0][1]["building_box"], {"height": 4, "width": 3})
        self.assertTrue(calls[1][1]["dry_run"])
        self.assertFalse(calls[2][1]["dry_run"])

    def test_pickup_entity_composes_over_a_connection_group(self) -> None:
        calls: list[dict[str, object]] = []

        def command(
            action: str, arguments: dict[str, object], timeout: float
        ) -> object:
            if action == "connect_entities":
                return {
                    "name": "pipe-group",
                    "connection_type": "pipe",
                    "position": {"x": 0.5, "y": 0},
                    "entities": [
                        {"name": "pipe", "position": {"x": 0, "y": 0}},
                        {"name": "pipe", "position": {"x": 1, "y": 0}},
                    ],
                }
            if action == "pickup_entity":
                calls.append(arguments)
                return {"picked_up": True}
            raise AssertionError(action)

        namespace = CompanionFactorioNamespace(command)
        group = namespace.connect_entities(
            Position(0, 0), Position(1, 0), Prototype.Pipe
        )

        self.assertTrue(namespace.pickup_entity(group))
        self.assertEqual([call["name"] for call in calls], ["pipe", "pipe"])
        self.assertEqual([call["x"] for call in calls], [0.0, 1.0])

    def test_get_entities_preserves_upstream_group_requests(self) -> None:
        def command(
            action: str, arguments: dict[str, object], timeout: float
        ) -> object:
            self.assertEqual(action, "get_entities")
            self.assertTrue(arguments["upstream_api"])
            self.assertEqual(arguments["radius"], 1000.0)
            self.assertEqual(
                set(arguments["names"]),
                {
                    "transport-belt",
                    "fast-transport-belt",
                    "express-transport-belt",
                    "underground-belt",
                    "fast-underground-belt",
                    "express-underground-belt",
                },
            )
            return [
                {
                    "name": "transport-belt",
                    "position": {"x": 0, "y": 0},
                    "direction": Direction.EAST.value,
                    "bounding_box": {
                        "left_top": {"x": -0.5, "y": -0.5},
                        "right_bottom": {"x": 0.5, "y": 0.5},
                    },
                },
                {
                    "name": "transport-belt",
                    "position": {"x": 1, "y": 0},
                    "direction": Direction.EAST.value,
                    "bounding_box": {
                        "left_top": {"x": 0.5, "y": -0.5},
                        "right_bottom": {"x": 1.5, "y": 0.5},
                    },
                },
            ]

        namespace = CompanionFactorioNamespace(command)
        groups = namespace.get_entities(Prototype.BeltGroup)

        self.assertEqual(len(groups), 1)
        self.assertEqual(groups[0].name, "belt-group")
        self.assertEqual(len(groups[0].belts), 2)
        self.assertEqual(groups[0].belts[0].prototype, Prototype.TransportBelt)
        self.assertEqual(groups[0].belts[0].bounding_box.width(), 1)

    def test_namespace_rejects_ambiguous_raw_direction_integer(self) -> None:
        calls: list[tuple[str, dict[str, object]]] = []

        def command(
            action: str, arguments: dict[str, object], timeout: float
        ) -> object:
            calls.append((action, arguments))
            return {"can_place": True}

        namespace = CompanionFactorioNamespace(command)
        output = namespace.evaluate(
            "can_place_entity(Prototype.TransportBelt, 4, Position(0, 0))"
        )

        self.assertIn("not a raw integer", output)
        self.assertEqual(calls, [])

    def test_namespace_exposes_live_wiki_and_local_task_skills(self) -> None:
        calls: list[tuple[str, dict[str, object]]] = []

        def command(
            action: str, arguments: dict[str, object], timeout: float
        ) -> object:
            calls.append((action, arguments))
            self.assertEqual(action, "wiki")
            return {
                "source": "current-game-prototypes",
                "query": arguments["query"],
                "recipe": {"category": "smelting"},
            }

        namespace = CompanionFactorioNamespace(command)
        documentation = namespace.evaluate(
            "print(harness_help('smelting'))\nprint(skill_help('smelting'))"
        )
        self.assertIn("natural Factorio simulation", documentation)
        self.assertIn("PRECHECK", documentation)
        self.assertIn("SUCCESS", documentation)
        self.assertEqual(calls, [])

        output = namespace.evaluate(
            "info = wiki(Prototype.IronPlate)\nprint(info['recipe']['category'])"
        )
        self.assertIn("smelting", output)
        self.assertEqual(calls, [("wiki", {"query": "iron-plate"})])

    def test_action_turn_warns_after_read_only_policy(self) -> None:
        requests: list[dict[str, object]] = []
        replies = [
            (
                "计划：再读一遍放置文档。\n"
                "```python\n"
                "print(harness_help('placement'))\n"
                "```"
            ),
            "缺少明确的目标资源，当前需要主人指定煤矿或铁矿。",
        ]

        def request(path: str, payload: dict[str, object]) -> dict[str, object]:
            requests.append(payload)
            return {
                "choices": [
                    {"message": {"role": "assistant", "content": replies.pop(0)}}
                ]
            }

        class FakeBridge:
            listen_address = ("127.0.0.1", 31501)

            def __init__(self) -> None:
                self.events: list[tuple[str, dict[str, object]]] = []
                self.responses: list[str] = []

            def execute_command(
                self, action: str, arguments: dict[str, object], timeout: float
            ) -> object:
                if action == "observe":
                    return {
                        "character": {"present": True, "position": {"x": 0, "y": 0}},
                        "buildings": [],
                        "resources": [],
                    }
                raise AssertionError(action)

            def record_event(
                self, event_type: str, payload: dict[str, object] | None = None
            ) -> None:
                self.events.append((event_type, payload or {}))

            def send_plan(self, value: str) -> str:
                return value

            def send_chat_response(self, value: str) -> str:
                self.responses.append(value)
                return value

        bridge = FakeBridge()
        agent = OpenAICompatibleAgent(
            base_url="https://provider.example/v1",
            model="test-model",
            request_json=request,
            max_policy_steps=3,
        )
        try:
            agent.attach(bridge)  # type: ignore[arg-type]
            agent._action_turn = True
            agent.provider.add_user_message("新建一条流水线")
            agent._run_policy_loop()
        finally:
            agent.close()

        event_types = [event_type for event_type, _ in bridge.events]
        self.assertIn("policy_no_progress", event_types)
        self.assertEqual(len(requests), 2)
        second_feedback = requests[1]["messages"][-1]["content"]
        self.assertIn("NO-PROGRESS GUARD", second_feedback)
        self.assertIn("batch", second_feedback)

    def test_agent_continues_after_promising_to_read_harness_docs(self) -> None:
        requests: list[dict[str, object]] = []
        replies = [
            (
                "咦，直接 craft_item 炼铁板失败了。"
                "团子去查查 harness 有没有烧炉子的正确姿势。"
            ),
            "已经查明需要石炉，但当前背包没有石头，明确缺少制作输入。",
        ]

        def request(path: str, payload: dict[str, object]) -> dict[str, object]:
            self.assertEqual(path, "/chat/completions")
            requests.append(payload)
            return {
                "choices": [
                    {"message": {"role": "assistant", "content": replies.pop(0)}}
                ]
            }

        class FakeBridge:
            listen_address = ("127.0.0.1", 31501)

            def __init__(self) -> None:
                self.events: list[tuple[str, dict[str, object]]] = []
                self.plans: list[str] = []
                self.responses: list[str] = []

            def execute_command(
                self, action: str, arguments: dict[str, object], timeout: float
            ) -> object:
                raise AssertionError("documentation recovery must not guess a game action")

            def record_event(
                self, event_type: str, payload: dict[str, object] | None = None
            ) -> None:
                self.events.append((event_type, payload or {}))

            def send_plan(self, value: str) -> str:
                self.plans.append(value)
                return "plan"

            def send_chat_response(self, value: str) -> str:
                self.responses.append(value)
                return "chat"

        bridge = FakeBridge()
        agent = OpenAICompatibleAgent(
            base_url="https://provider.example/v1",
            model="test-model",
            request_json=request,
            max_policy_steps=3,
        )
        try:
            agent.attach(bridge)  # type: ignore[arg-type]
            agent.provider.add_user_message("继续完成铁板任务")
            agent._run_policy_loop()
        finally:
            agent.close()

        self.assertEqual(len(requests), 2)
        self.assertEqual(
            bridge.responses,
            ["已经查明需要石炉，但当前背包没有石头，明确缺少制作输入。"],
        )
        self.assertEqual(
            [event_type for event_type, _ in bridge.events],
            [
                "model_response",
                "model_nonterminal_response",
                "model_response",
                "assistant_message",
            ],
        )
        second_messages = requests[1]["messages"]
        self.assertIn("wiki(...)", second_messages[-1]["content"])
        self.assertIn("skill_help(...)", second_messages[-1]["content"])
        self.assertIn("native web_search", second_messages[-1]["content"])

    def test_agent_recovers_from_responses_function_call_without_ending_turn(
        self,
    ) -> None:
        requests: list[dict[str, object]] = []
        replies = [
            {
                "status": "completed",
                "output": [
                    {"type": "reasoning", "id": "reasoning-1", "content": []},
                    {
                        "type": "function_call",
                        "id": "function-1",
                        "call_id": "call-1",
                        "name": "wiki",
                        "arguments": '{"subject":"iron-plate"}',
                    },
                ],
            },
            {
                "status": "completed",
                "output": [
                    {
                        "type": "message",
                        "role": "assistant",
                        "content": [
                            {
                                "type": "output_text",
                                "text": (
                                    "计划：在 policy 里查实时配方。\n"
                                    "```python\n"
                                    "info = wiki(Prototype.IronPlate)\n"
                                    "print(info['recipe']['category'])\n"
                                    "```"
                                ),
                            }
                        ],
                    }
                ],
            },
            {
                "status": "completed",
                "output": [
                    {
                        "type": "message",
                        "role": "assistant",
                        "content": [
                            {
                                "type": "output_text",
                                "text": "已经确认铁板需要熔炼，下一步可用石炉完成。",
                            }
                        ],
                    }
                ],
            },
        ]

        def request(path: str, payload: dict[str, object]) -> dict[str, object]:
            self.assertEqual(path, "/responses")
            requests.append(payload)
            return replies.pop(0)

        class FakeBridge:
            listen_address = ("127.0.0.1", 31501)

            def __init__(self) -> None:
                self.commands: list[tuple[str, dict[str, object]]] = []
                self.events: list[tuple[str, dict[str, object]]] = []
                self.plans: list[str] = []
                self.responses: list[str] = []

            def execute_command(
                self, action: str, arguments: dict[str, object], timeout: float
            ) -> object:
                self.commands.append((action, arguments))
                if action == "wiki":
                    return {
                        "source": "current-game-prototypes",
                        "query": "iron-plate",
                        "recipe": {"category": "smelting"},
                    }
                if action == "observe":
                    return {
                        "character": {
                            "present": True,
                            "position": {"x": 1, "y": 2},
                        },
                        "buildings": [],
                        "resources": [],
                    }
                raise AssertionError(action)

            def record_event(
                self, event_type: str, payload: dict[str, object] | None = None
            ) -> None:
                self.events.append((event_type, payload or {}))

            def send_plan(self, value: str) -> str:
                self.plans.append(value)
                return "plan"

            def send_chat_response(self, value: str) -> str:
                self.responses.append(value)
                return "chat"

        bridge = FakeBridge()
        agent = OpenAICompatibleAgent(
            base_url="https://api.deepseek.com",
            model="deepseek-v4-flash",
            api_mode="responses",
            request_json=request,
            max_policy_steps=4,
        )
        try:
            agent.attach(bridge)  # type: ignore[arg-type]
            agent.provider.add_user_message("继续完成铁板任务")
            agent._run_policy_loop()
        finally:
            agent.close()

        self.assertEqual(len(requests), 3)
        self.assertEqual(
            [action for action, _ in bridge.commands],
            ["wiki", "observe"],
        )
        self.assertEqual(
            bridge.responses,
            ["已经确认铁板需要熔炼，下一步可用石炉完成。"],
        )
        event_types = [event_type for event_type, _ in bridge.events]
        self.assertIn("model_function_call_rejected", event_types)
        self.assertNotIn("agent_error", event_types)
        rejected_event = next(
            payload
            for event_type, payload in bridge.events
            if event_type == "model_function_call_rejected"
        )
        self.assertEqual(rejected_event["calls"][0]["name"], "wiki")
        second_input = requests[1]["input"]
        self.assertTrue(
            any(
                isinstance(item, dict)
                and item.get("type") == "function_call_output"
                and item.get("call_id") == "call-1"
                for item in second_input
            )
        )

    def test_agent_runs_policy_then_returns_real_feedback_to_model(self) -> None:
        requests: list[dict[str, object]] = []
        replies = [
            {
                "choices": [
                    {
                        "message": {
                            "role": "assistant",
                            "reasoning_content": "需要先检查煤炭库存。",
                            "content": (
                                "计划：检查背包。\n"
                                "```python\n"
                                "inventory = inspect_inventory()\n"
                                "print(inventory[Prototype.Coal])\n"
                                "```"
                            ),
                        }
                    }
                ],
                "usage": {"prompt_tokens": 20, "completion_tokens": 8},
            },
            {
                "choices": [
                    {
                        "message": {
                            "role": "assistant",
                            "reasoning_content": "库存结果已经足够回答。",
                            "content": "已经确认背包里有 8 个煤。",
                        }
                    }
                ],
                "usage": {"prompt_tokens": 40, "completion_tokens": 6},
            },
        ]

        def request(path: str, payload: dict[str, object]) -> dict[str, object]:
            self.assertEqual(path, "/chat/completions")
            requests.append(payload)
            return replies.pop(0)

        class FakeBridge:
            listen_address = ("127.0.0.1", 31501)

            def __init__(self) -> None:
                self.commands: list[tuple[str, dict[str, object]]] = []
                self.events: list[tuple[str, dict[str, object]]] = []
                self.plans: list[str] = []
                self.responses: list[str] = []

            def execute_command(
                self, action: str, arguments: dict[str, object], timeout: float
            ) -> object:
                self.commands.append((action, arguments))
                if action == "inspect_inventory":
                    return {"coal": 8}
                if action == "observe":
                    return {
                        "tick": 100,
                        "character": {
                            "present": True,
                            "position": {"x": 3, "y": 4},
                            "inventory": {"coal": 8},
                        },
                        "buildings": [],
                        "resources": [],
                    }
                raise AssertionError(action)

            def record_event(
                self, event_type: str, payload: dict[str, object] | None = None
            ) -> None:
                self.events.append((event_type, payload or {}))

            def send_plan(self, text: str) -> str:
                self.plans.append(text)
                return "plan"

            def send_chat_response(self, text: str) -> str:
                self.responses.append(text)
                return "chat"

        bridge = FakeBridge()
        agent = OpenAICompatibleAgent(
            base_url="https://provider.example/v1",
            model="test-model",
            request_json=request,
            max_policy_steps=3,
        )
        try:
            agent.attach(bridge)  # type: ignore[arg-type]
            agent.provider.add_user_message("检查煤炭库存")
            agent._run_policy_loop()
        finally:
            agent.close()

        self.assertEqual(
            [action for action, _ in bridge.commands],
            ["inspect_inventory", "observe"],
        )
        self.assertEqual(bridge.responses, ["已经确认背包里有 8 个煤。"])
        self.assertEqual(
            [event_type for event_type, _ in bridge.events],
            [
                "model_reasoning",
                "model_response",
                "model_policy",
                "policy_result",
                "model_reasoning",
                "model_response",
                "assistant_message",
            ],
        )
        reasoning_events = [
            payload["text"]
            for event_type, payload in bridge.events
            if event_type == "model_reasoning"
        ]
        self.assertEqual(
            reasoning_events,
            ["需要先检查煤炭库存。", "库存结果已经足够回答。"],
        )
        self.assertNotIn("tools", requests[0])
        second_messages = requests[1]["messages"]
        self.assertIn(
            "FLE Python policy execution result",
            second_messages[-1]["content"],
        )
        self.assertIn("8", second_messages[-1]["content"])

    def test_agent_never_turns_an_empty_model_response_into_success(self) -> None:
        requests = 0

        def request(path: str, payload: dict[str, object]) -> dict[str, object]:
            nonlocal requests
            requests += 1
            content = "" if requests == 1 else "没有执行任何动作，未谎报完成。"
            return {
                "choices": [
                    {"message": {"role": "assistant", "content": content}}
                ]
            }

        class FakeBridge:
            listen_address = ("127.0.0.1", 31501)

            def __init__(self) -> None:
                self.events: list[str] = []
                self.responses: list[str] = []

            def execute_command(
                self, action: str, arguments: dict[str, object], timeout: float
            ) -> object:
                raise AssertionError("empty-response recovery must not touch Factorio")

            def record_event(
                self, event_type: str, payload: dict[str, object] | None = None
            ) -> None:
                self.events.append(event_type)

            def send_plan(self, text: str) -> str:
                return "plan"

            def send_chat_response(self, text: str) -> str:
                self.responses.append(text)
                return "chat"

        bridge = FakeBridge()
        agent = OpenAICompatibleAgent(
            base_url="https://provider.example/v1",
            model="test-model",
            request_json=request,
            max_policy_steps=3,
        )
        try:
            agent.attach(bridge)  # type: ignore[arg-type]
            agent.provider.add_user_message("你好")
            agent._run_policy_loop()
        finally:
            agent.close()

        self.assertEqual(requests, 2)
        self.assertIn("model_empty_response", bridge.events)
        self.assertEqual(bridge.responses, ["没有执行任何动作，未谎报完成。"])


if __name__ == "__main__":
    unittest.main()
