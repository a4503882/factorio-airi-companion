"""Local AgentBridge for the AIRI Factorio companion mod.

The bridge owns model credentials and network access.  Factorio only exchanges
small, versioned JSON packets with this process over localhost UDP.
"""

from __future__ import annotations

import argparse
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import re
import socket
import tempfile
import threading
import time
from typing import Any, Callable
from urllib import error as urllib_error
from urllib import request as urllib_request

from .policy_harness import (
    CompanionCommandError,
    CompanionFactorioNamespace,
    PolicyCancelledError,
    PolicyValidationError,
    compose_policy_system_prompt,
    parse_policy_text,
    response_contains_policy,
    task_skill_for_message,
)
from .protocol import Packet, ProtocolError, decode_packet, encode_packet, new_packet_id


ResultCallback = Callable[[bool, Any], None]
_STATUS_REPLACE_RETRY_DELAYS = (0.005, 0.010, 0.020, 0.040, 0.080, 0.160)
_WINDOWS_SHARING_ERRORS = {5, 32, 33}
_MODEL_TRANSPORT_RETRY_DELAYS = (1.0, 3.0)


def _replace_file_with_retry(source: Path, target: Path) -> None:
    """Replace a status snapshot despite short-lived Windows reader locks."""

    for attempt in range(len(_STATUS_REPLACE_RETRY_DELAYS) + 1):
        try:
            source.replace(target)
            return
        except OSError as exc:
            retryable = isinstance(exc, PermissionError) or (
                os.name == "nt"
                and getattr(exc, "winerror", None) in _WINDOWS_SHARING_ERRORS
            )
            if not retryable or attempt >= len(_STATUS_REPLACE_RETRY_DELAYS):
                raise
            time.sleep(_STATUS_REPLACE_RETRY_DELAYS[attempt])


@dataclass
class PendingCommand:
    request_id: str
    encoded: bytes
    callback: ResultCallback | None
    created_at: float
    last_sent_at: float
    attempts: int = 1
    acknowledged: bool = False


class BridgeEventLogger:
    """Write a secret-free research trajectory and a pollable status snapshot."""

    def __init__(
        self,
        *,
        event_log: str | Path | None = None,
        status_file: str | Path | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> None:
        self.event_log = Path(event_log).resolve() if event_log else None
        self.status_file = Path(status_file).resolve() if status_file else None
        self._lock = threading.RLock()
        self._closed = False
        self._status_write_failures = 0
        self._last_status_warning_at = 0.0
        self._status_warning_active = False
        started_at = self._timestamp()
        self._status: dict[str, Any] = {
            "schema_version": 1,
            "pid": os.getpid(),
            "running": True,
            "connected": False,
            "started_at": started_at,
            "last_factorio_packet_at": None,
            "last_factorio_packet_type": None,
            **(metadata or {}),
        }
        if self.event_log is not None:
            self.event_log.parent.mkdir(parents=True, exist_ok=True)
        if self.status_file is not None:
            self.status_file.parent.mkdir(parents=True, exist_ok=True)
        self.emit("bridge_created", metadata or {})

    @staticmethod
    def _timestamp() -> str:
        return datetime.now(timezone.utc).isoformat(timespec="milliseconds")

    def _write_status(self) -> bool:
        if self.status_file is None:
            return True
        text = json.dumps(self._status, ensure_ascii=False, indent=2) + "\n"
        temporary: Path | None = None
        try:
            descriptor, temporary_name = tempfile.mkstemp(
                prefix=f".{self.status_file.name}.",
                suffix=".tmp",
                dir=str(self.status_file.parent),
            )
            os.close(descriptor)
            temporary = Path(temporary_name)
            temporary.write_text(text, encoding="utf-8")
            _replace_file_with_retry(temporary, self.status_file)
        except OSError as exc:
            self._status_write_failures += 1
            now = time.monotonic()
            if (
                self._last_status_warning_at == 0.0
                or now - self._last_status_warning_at >= 30.0
            ):
                print(
                    "Warning: could not update the Control Center status snapshot; "
                    f"AgentBridge will continue and retry: {exc}",
                    flush=True,
                )
                self._last_status_warning_at = now
                self._status_warning_active = True
            return False
        else:
            if self._status_warning_active:
                print(
                    "Control Center status snapshot updates recovered after "
                    f"{self._status_write_failures} failed update(s).",
                    flush=True,
                )
                self._status_warning_active = False
            self._status_write_failures = 0
            return True
        finally:
            if temporary is not None:
                try:
                    temporary.unlink(missing_ok=True)
                except OSError:
                    pass

    def emit(
        self,
        event_type: str,
        payload: dict[str, Any] | None = None,
        *,
        status: dict[str, Any] | None = None,
    ) -> None:
        with self._lock:
            timestamp = self._timestamp()
            record = {
                "timestamp": timestamp,
                "type": event_type,
                "payload": payload or {},
            }
            if self.event_log is not None:
                with self.event_log.open("a", encoding="utf-8", newline="\n") as stream:
                    stream.write(
                        json.dumps(record, ensure_ascii=False, separators=(",", ":"))
                        + "\n"
                    )
            self._status["updated_at"] = timestamp
            self._status["last_event_type"] = event_type
            if status:
                self._status.update(status)
            self._write_status()

    def factorio_packet(self, packet: Packet) -> None:
        timestamp = self._timestamp()
        status = {
            "connected": True,
            "last_factorio_packet_at": timestamp,
            "last_factorio_packet_type": packet.type,
        }
        if packet.type in {"ping", "pong", "ack", "heartbeat"}:
            with self._lock:
                self._status.update(status)
                self._status["updated_at"] = timestamp
                self._write_status()
            return
        self.emit(
            "factorio_packet",
            {"packet_type": packet.type, "payload": packet.payload},
            status=status,
        )

    def close(self) -> None:
        with self._lock:
            if self._closed:
                return
            self._closed = True
        self.emit(
            "bridge_stopped",
            {},
            status={"running": False, "connected": False, "stopped_at": self._timestamp()},
        )


class FactorioBridge:
    def __init__(
        self,
        *,
        listen_port: int = 31501,
        factorio_port: int = 31500,
        token: str = "",
        verbose: bool = False,
        event_logger: BridgeEventLogger | None = None,
    ) -> None:
        self.listen_address = ("127.0.0.1", listen_port)
        self.factorio_address = ("127.0.0.1", factorio_port)
        self.token = token
        self.verbose = verbose
        self.event_logger = event_logger
        self.agent: BaseAgent | None = None
        self._socket: socket.socket | None = None
        self._pending: dict[str, PendingCommand] = {}
        self._lock = threading.RLock()
        self._stop = threading.Event()
        self._last_factorio_packet = 0.0
        self._last_ping = 0.0
        self._udp_unreachable_reported = False

    @property
    def connected(self) -> bool:
        return time.monotonic() - self._last_factorio_packet < 10.0

    def attach_agent(self, agent: "BaseAgent") -> None:
        self.agent = agent
        agent.attach(self)

    def record_event(
        self,
        event_type: str,
        payload: dict[str, Any] | None = None,
        *,
        status: dict[str, Any] | None = None,
    ) -> None:
        if self.event_logger is not None:
            self.event_logger.emit(event_type, payload, status=status)

    def open(self) -> None:
        if self._socket is not None:
            return
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        sock.bind(self.listen_address)
        sock.settimeout(0.10)
        self._socket = sock
        self.record_event(
            "bridge_listening",
            {
                "listen_port": self.listen_address[1],
                "factorio_port": self.factorio_address[1],
            },
        )

    def close(self) -> None:
        self._stop.set()
        callbacks: list[ResultCallback] = []
        with self._lock:
            for pending in self._pending.values():
                if pending.callback is not None:
                    callbacks.append(pending.callback)
            self._pending.clear()
        for callback in callbacks:
            try:
                callback(False, "AgentBridge stopped before Factorio returned a result")
            except Exception:
                pass
        if self._socket is not None:
            self._socket.close()
            self._socket = None
        if self.agent is not None:
            self.agent.close()
        if self.event_logger is not None:
            self.event_logger.close()

    def _send_raw(self, encoded: bytes) -> None:
        if self._socket is None:
            raise RuntimeError("bridge socket is not open")
        self._socket.sendto(encoded, self.factorio_address)

    def send_packet(
        self,
        packet_type: str,
        payload: dict[str, Any] | None = None,
        *,
        packet_id: str | None = None,
    ) -> str:
        request_id = packet_id or new_packet_id("bridge")
        encoded = encode_packet(
            packet_type,
            payload,
            packet_id=request_id,
            token=self.token,
        )
        with self._lock:
            self._send_raw(encoded)
        if packet_type not in {"ping", "ack", "plan"}:
            self.record_event(
                "bridge_packet",
                {"packet_type": packet_type, "payload": payload or {}},
            )
        return request_id

    def send_command(
        self,
        action: str,
        arguments: dict[str, Any] | None = None,
        callback: ResultCallback | None = None,
    ) -> str:
        request_id = new_packet_id("command")
        encoded = encode_packet(
            "command",
            {"action": action, "arguments": arguments or {}},
            packet_id=request_id,
            token=self.token,
        )
        now = time.monotonic()
        pending = PendingCommand(
            request_id=request_id,
            encoded=encoded,
            callback=callback,
            created_at=now,
            last_sent_at=now,
        )
        with self._lock:
            self._pending[request_id] = pending
            self._send_raw(encoded)
        self.record_event(
            "game_command",
            {
                "request_id": request_id,
                "action": action,
                "arguments": arguments or {},
            },
        )
        return request_id

    def execute_command(
        self,
        action: str,
        arguments: dict[str, Any] | None = None,
        timeout: float = 120,
        cancel_requested: Callable[[], bool] | None = None,
    ) -> Any:
        """Synchronously expose a UDP action to the upstream Python namespace.

        Policy execution runs on the agent worker while ``serve_forever`` keeps
        receiving packets on the bridge thread, so waiting here does not block
        Factorio result delivery.
        """

        completed = threading.Event()
        outcome: dict[str, Any] = {}

        def callback(ok: bool, result: Any) -> None:
            outcome.update({"ok": ok, "result": result})
            completed.set()

        if cancel_requested is not None and cancel_requested():
            raise PolicyCancelledError("game command cancelled by the player")
        request_id = self.send_command(action, arguments, callback)
        wait_timeout = max(0.1, float(timeout))
        deadline = time.monotonic() + wait_timeout
        while not completed.wait(min(0.05, max(0.0, deadline - time.monotonic()))):
            if cancel_requested is not None and cancel_requested():
                with self._lock:
                    self._pending.pop(request_id, None)
                self.record_event(
                    "game_command_cancelled",
                    {
                        "request_id": request_id,
                        "action": action,
                        "source": "policy_namespace",
                    },
                )
                raise PolicyCancelledError("game command cancelled by the player")
            if time.monotonic() >= deadline:
                break
        if not completed.is_set():
            with self._lock:
                self._pending.pop(request_id, None)
            self.record_event(
                "game_command_timeout",
                {
                    "request_id": request_id,
                    "action": action,
                    "source": "policy_namespace",
                },
            )
            raise TimeoutError(f"Factorio command {action!r} timed out")
        if not outcome.get("ok"):
            result = outcome.get("result")
            if isinstance(result, str):
                detail = result
            else:
                detail = json.dumps(result, ensure_ascii=False, separators=(",", ":"))
            raise CompanionCommandError(f"Factorio command {action!r} failed: {detail}")
        return outcome.get("result")

    def send_chat_response(
        self,
        text: str,
        *,
        request_id: str | None = None,
    ) -> str:
        payload: dict[str, Any] = {"text": text}
        if request_id:
            payload["request_id"] = request_id
        return self.send_packet("chat_response", payload)

    def send_plan(self, text: str, *, request_id: str | None = None) -> str:
        payload: dict[str, Any] = {"text": text}
        if request_id:
            payload["request_id"] = request_id
        return self.send_packet("plan", payload)

    def _handle_result(self, packet: Packet) -> None:
        request_id = packet.payload.get("request_id")
        if not isinstance(request_id, str):
            return
        with self._lock:
            pending = self._pending.pop(request_id, None)
        self.record_event(
            "game_result",
            {
                "request_id": request_id,
                "ok": bool(packet.payload.get("ok")),
                "result": packet.payload.get("result"),
            },
        )
        if pending and pending.callback:
            pending.callback(
                bool(packet.payload.get("ok")), packet.payload.get("result")
            )

    def _handle_packet(self, packet: Packet) -> None:
        if self.token and packet.token != self.token:
            if self.verbose:
                print("Ignored a Factorio packet with the wrong session token")
            self.record_event("packet_rejected", {"reason": "session_token_mismatch"})
            return

        self._last_factorio_packet = time.monotonic()
        self._udp_unreachable_reported = False
        if self.event_logger is not None:
            self.event_logger.factorio_packet(packet)
        if packet.type == "ack":
            request_id = packet.payload.get("request_id")
            with self._lock:
                pending = self._pending.get(request_id)
                if pending:
                    pending.acknowledged = True
            return
        if packet.type == "result":
            self._handle_result(packet)
            return
        if packet.type == "chat" and self.agent is not None:
            text = packet.payload.get("text")
            if isinstance(text, str) and text.strip():
                self.agent.on_chat(
                    text.strip(),
                    packet.payload.get("context") or {},
                    packet.payload.get("player_index"),
                    packet.id,
                )
            return
        if packet.type == "cancel_chat" and self.agent is not None:
            request_id = packet.payload.get("request_id")
            if not isinstance(request_id, str) or not request_id:
                request_id = None
            cancelled = self.agent.cancel_current_turn(
                reason="Stopped by player",
                request_id=request_id,
                player_index=packet.payload.get("player_index"),
            )
            self.record_event(
                "agent_cancel_request_handled",
                {
                    "request_id": request_id,
                    "player_index": packet.payload.get("player_index"),
                    "cancelled": cancelled,
                },
            )
            return
        if packet.type in {"hello", "heartbeat"}:
            if self.verbose:
                print(
                    f"Factorio {packet.type}: {json.dumps(packet.payload, ensure_ascii=False)}"
                )
            return
        if self.verbose:
            print(
                f"Factorio event {packet.type}: {json.dumps(packet.payload, ensure_ascii=False)}"
            )

    def _retry_pending(self) -> None:
        now = time.monotonic()
        callbacks: list[tuple[ResultCallback, bool, Any]] = []
        with self._lock:
            for request_id, pending in list(self._pending.items()):
                retry_after = 5.0 if pending.acknowledged else 0.75
                max_age = 600.0 if pending.acknowledged else 8.0
                if now - pending.created_at > max_age:
                    self._pending.pop(request_id, None)
                    if pending.callback:
                        callbacks.append(
                            (
                                pending.callback,
                                False,
                                "Factorio command timed out",
                            )
                        )
                    self.record_event(
                        "game_command_timeout",
                        {
                            "request_id": request_id,
                            "attempts": pending.attempts,
                            "acknowledged": pending.acknowledged,
                        },
                    )
                    continue
                if now - pending.last_sent_at >= retry_after:
                    self._send_raw(pending.encoded)
                    pending.last_sent_at = now
                    pending.attempts += 1

        for callback, ok, result in callbacks:
            callback(ok, result)

    def request_stop(self) -> None:
        self._stop.set()

    def serve_forever(self) -> None:
        self.open()
        print(
            f"AIRI AgentBridge listening on 127.0.0.1:{self.listen_address[1]} "
            f"and targeting Factorio UDP port {self.factorio_address[1]}"
        )
        try:
            while not self._stop.is_set():
                try:
                    assert self._socket is not None
                    data, address = self._socket.recvfrom(65_535)
                    if address[0] != "127.0.0.1":
                        continue
                    try:
                        self._handle_packet(decode_packet(data))
                    except ProtocolError as exc:
                        if self.verbose:
                            print(f"Invalid Factorio packet: {exc}")
                except socket.timeout:
                    pass
                except OSError as exc:
                    if self._stop.is_set():
                        break
                    if os.name == "nt" and getattr(exc, "winerror", None) == 10054:
                        # Windows reports an ICMP port-unreachable response as a
                        # connection reset on an otherwise valid UDP socket when
                        # Factorio exits or restarts. Keep the bridge available.
                        had_connected = self._last_factorio_packet > 0.0
                        self._last_factorio_packet = 0.0
                        if had_connected and not self._udp_unreachable_reported:
                            self._udp_unreachable_reported = True
                            self.record_event(
                                "factorio_disconnected",
                                {"reason": "udp_port_unreachable"},
                                status={"connected": False},
                            )
                        continue
                    raise

                now = time.monotonic()
                if now - self._last_ping >= 2.0:
                    self.send_packet("ping", {})
                    self._last_ping = now
                self._retry_pending()
        finally:
            self.close()


class BaseAgent:
    def __init__(self) -> None:
        self.bridge: FactorioBridge | None = None

    def attach(self, bridge: FactorioBridge) -> None:
        self.bridge = bridge

    def on_chat(
        self,
        text: str,
        context: dict[str, Any],
        player_index: int | None,
        request_id: str | None = None,
    ) -> None:
        raise NotImplementedError

    def cancel_current_turn(
        self,
        *,
        reason: str,
        request_id: str | None = None,
        player_index: int | None = None,
    ) -> bool:
        return False

    def close(self) -> None:
        pass


RESOURCE_ALIASES = {
    "铁": "iron-ore",
    "铁矿": "iron-ore",
    "铜": "copper-ore",
    "铜矿": "copper-ore",
    "煤": "coal",
    "煤矿": "coal",
    "石头": "stone",
    "石矿": "stone",
    "铀": "uranium-ore",
    "铀矿": "uranium-ore",
}


class HeuristicAgent(BaseAgent):
    """Dependency-free fallback used before an LLM provider is configured."""

    @staticmethod
    def parse_intent(text: str) -> tuple[str, dict[str, Any]] | None:
        stripped = text.strip()
        lowered = stripped.lower()
        if re.search(r"跟着我|跟随我|跟我来|\bfollow\b", lowered):
            return "follow", {}
        if re.search(r"停下|停止|别动|取消任务|\bstop\b|\bcancel\b", lowered):
            return "stop", {}
        if re.search(r"召唤|出来吧|生成角色|\bspawn\b|\bsummon\b", lowered):
            return "spawn", {}
        if re.search(r"状态|怎么样了|\bstatus\b", lowered):
            return "status", {}
        if re.search(r"观察|看看周围|扫描|\bobserve\b|\bscan\b", lowered):
            return "observe", {}

        english_find = re.search(
            r"\b(?:find|locate)\s+([\w-]+)(?:\s+(\d+))?\b", lowered
        )
        if english_find:
            arguments: dict[str, Any] = {"resource": english_find.group(1)}
            if english_find.group(2):
                arguments["radius"] = int(english_find.group(2))
            return "find_resource", arguments

        chinese_find = re.search(
            r"(?:寻找|查找|定位|找)\s*(?:一下)?\s*"
            r"(铁矿|铜矿|煤矿|煤|石头|石矿|铀矿|[\w-]+)(?:\s+(\d+))?",
            lowered,
        )
        if chinese_find:
            arguments = {
                "resource": RESOURCE_ALIASES.get(
                    chinese_find.group(1), chinese_find.group(1)
                )
            }
            if chinese_find.group(2):
                arguments["radius"] = int(chinese_find.group(2))
            return "find_resource", arguments

        english_mine = re.search(r"\bmine\s+([\w-]+)\s+(\d+)\b", lowered)
        if english_mine:
            return "mine_resource", {
                "resource": english_mine.group(1),
                "count": int(english_mine.group(2)),
            }

        move_match = re.search(
            r"(?:移动到|走到|去|move(?:_to)?)\s*[（(]?\s*(-?\d+(?:\.\d+)?)\s*[,， ]\s*(-?\d+(?:\.\d+)?)",
            lowered,
        )
        if move_match:
            return "move_to", {
                "x": float(move_match.group(1)),
                "y": float(move_match.group(2)),
            }

        mine_match = re.search(
            r"(?:挖|采集|mine)\s*(\d+)?\s*([\w-]+|铁矿|铜矿|煤矿|煤|石头|石矿|铀矿)",
            lowered,
        )
        if mine_match:
            count = int(mine_match.group(1) or 32)
            resource = RESOURCE_ALIASES.get(mine_match.group(2), mine_match.group(2))
            return "mine_resource", {"resource": resource, "count": count}

        return None

    def on_chat(
        self,
        text: str,
        context: dict[str, Any],
        player_index: int | None,
        request_id: str | None = None,
    ) -> None:
        assert self.bridge is not None
        intent = self.parse_intent(text)
        if intent is None:
            message = (
                "现在运行的是无密钥本地模式。我能直接执行召唤、跟随、停止、观察、"
                "移动坐标和采矿；配置模型后就能自由规划。"
            )
            if request_id is None:
                self.bridge.send_chat_response(message)
            else:
                self.bridge.send_chat_response(message, request_id=request_id)
            return

        action, arguments = intent
        if request_id is None:
            self.bridge.send_plan(f"正在执行：{action}")
        else:
            self.bridge.send_plan(f"正在执行：{action}", request_id=request_id)

        def completed(ok: bool, result: Any) -> None:
            assert self.bridge is not None
            if request_id is None:
                self.bridge.send_plan("")
            else:
                self.bridge.send_plan("", request_id=request_id)
            if ok:
                rendered = json.dumps(result, ensure_ascii=False, separators=(",", ":"))
                message = f"完成了。{rendered}"
            else:
                message = f"没完成：{result}"
            if request_id is None:
                self.bridge.send_chat_response(message)
            else:
                self.bridge.send_chat_response(message, request_id=request_id)

        self.bridge.send_command(action, arguments, completed)


SYSTEM_PROMPT = """You are AIRI, an in-game Factorio companion with your own character body.
Never claim success before the game execution result confirms it.
World observation is intentionally limited to AIRI's permitted local perception radius.
Use web search only for external, time-sensitive knowledge; never use it as evidence of in-game state.
Reply concisely in the player's language. Do not reveal hidden reasoning; show only a short plan and useful results.
Resource and item identifiers use Factorio prototype names such as iron-ore and transport-belt.
"""


@dataclass(frozen=True)
class ProviderConfig:
    api_key: str
    base_url: str
    model: str


@dataclass(frozen=True)
class ModelTurn:
    text: str = ""
    reasoning_text: str = ""
    metadata: dict[str, Any] = field(default_factory=dict)


class ModelRequestCancelled(RuntimeError):
    """The player cancelled while a provider request was in flight."""


JsonRequester = Callable[[str, dict[str, Any]], dict[str, Any]]
ModelRetryCallback = Callable[[int, int, float, BaseException], None]
CancelRequested = Callable[[], bool]


def load_provider_config(path: str | Path) -> ProviderConfig:
    """Load the three-line local secret format without logging its contents."""

    config_path = Path(path).expanduser().resolve()
    try:
        lines = [
            line.strip()
            for line in config_path.read_text(encoding="utf-8-sig").splitlines()
            if line.strip()
        ]
    except OSError as exc:
        raise RuntimeError(f"could not read provider config {config_path}: {exc}") from exc
    if len(lines) < 3:
        raise RuntimeError(
            f"provider config {config_path} must contain api_key, base_url, and model"
        )
    api_key, base_url, model = lines[:3]
    if not base_url.startswith(("http://", "https://")):
        raise RuntimeError(f"provider config {config_path} has an invalid base_url")
    return ProviderConfig(api_key=api_key, base_url=base_url.rstrip("/"), model=model)


def _responses_tools(enable_web_search: bool) -> list[dict[str, Any]]:
    # Game actions are Python functions inside FactorioNamespace, not provider
    # function tools. Native web search remains available for external facts.
    return [{"type": "web_search"}] if enable_web_search else []


class _HTTPModelProvider:
    def __init__(
        self,
        *,
        base_url: str,
        model: str,
        api_key: str = "",
        system_prompt: str = SYSTEM_PROMPT,
        reasoning_effort: str = "high",
        request_json: JsonRequester | None = None,
        retry_callback: ModelRetryCallback | None = None,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.model = model
        self.api_key = api_key
        self.system_prompt = system_prompt
        self.reasoning_effort = reasoning_effort
        self._request_json_override = request_json
        self._retry_callback = retry_callback
        self._state_lock = threading.RLock()

    def set_retry_callback(self, callback: ModelRetryCallback | None) -> None:
        self._retry_callback = callback

    @staticmethod
    def _raise_if_cancelled(cancel_requested: CancelRequested | None) -> None:
        if cancel_requested is not None and cancel_requested():
            raise ModelRequestCancelled("model request cancelled by the player")

    def _call_cancellable(
        self,
        operation: Callable[[], dict[str, Any]],
        cancel_requested: CancelRequested | None,
    ) -> dict[str, Any]:
        """Run a blocking HTTP call without holding the agent worker hostage.

        ``urllib`` cannot reliably abort an already transmitted request. When a
        turn is cancellable, the actual I/O therefore runs in a daemon helper;
        the policy worker returns as soon as the cancellation event is set and
        any eventual network result is discarded.
        """

        self._raise_if_cancelled(cancel_requested)
        if cancel_requested is None:
            return operation()

        completed = threading.Event()
        outcome: dict[str, Any] = {}

        def run() -> None:
            try:
                outcome["value"] = operation()
            except BaseException as exc:
                outcome["error"] = exc
            finally:
                completed.set()

        threading.Thread(
            target=run,
            name="airi-model-http",
            daemon=True,
        ).start()
        while not completed.wait(0.05):
            self._raise_if_cancelled(cancel_requested)
        self._raise_if_cancelled(cancel_requested)
        error = outcome.get("error")
        if isinstance(error, BaseException):
            raise error
        value = outcome.get("value")
        if not isinstance(value, dict):
            raise RuntimeError("model request did not return a JSON object")
        return value

    def _wait_before_retry(
        self,
        delay: float,
        cancel_requested: CancelRequested | None,
    ) -> None:
        if cancel_requested is None:
            time.sleep(delay)
            return
        deadline = time.monotonic() + delay
        while True:
            self._raise_if_cancelled(cancel_requested)
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return
            time.sleep(min(0.05, remaining))

    def _request_json(
        self,
        path: str,
        payload: dict[str, Any],
        *,
        cancel_requested: CancelRequested | None = None,
    ) -> dict[str, Any]:
        if self._request_json_override is not None:
            return self._call_cancellable(
                lambda: self._request_json_override(path, payload),
                cancel_requested,
            )

        headers = {"Content-Type": "application/json", "Accept": "application/json"}
        if self.api_key:
            headers["Authorization"] = f"Bearer {self.api_key}"
        request = urllib_request.Request(
            f"{self.base_url}{path}",
            data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
            headers=headers,
            method="POST",
        )
        attempts = len(_MODEL_TRANSPORT_RETRY_DELAYS) + 1
        for attempt in range(1, attempts + 1):
            try:
                def perform_request() -> dict[str, Any]:
                    with urllib_request.urlopen(request, timeout=180) as response:
                        body = json.loads(response.read().decode("utf-8"))
                    if not isinstance(body, dict):
                        raise RuntimeError("model response JSON root was not an object")
                    return body

                return self._call_cancellable(perform_request, cancel_requested)
            except urllib_error.HTTPError as exc:
                detail = exc.read().decode("utf-8", errors="replace")[:1000]
                raise RuntimeError(f"model HTTP {exc.code}: {detail}") from exc
            except json.JSONDecodeError as exc:
                raise RuntimeError(f"model returned invalid JSON: {exc}") from exc
            except (urllib_error.URLError, TimeoutError) as exc:
                if attempt >= attempts:
                    raise RuntimeError(
                        f"model request failed after {attempts} attempts: {exc}"
                    ) from exc
                delay = _MODEL_TRANSPORT_RETRY_DELAYS[attempt - 1]
                if self._retry_callback is not None:
                    try:
                        self._retry_callback(attempt, attempts, delay, exc)
                    except Exception:
                        pass
                self._wait_before_retry(delay, cancel_requested)

        raise RuntimeError("model request failed without a transport result")


class ChatCompletionsProvider(_HTTPModelProvider):
    """OpenAI-compatible Chat Completions adapter with local history."""

    def __init__(self, **kwargs: Any) -> None:
        super().__init__(**kwargs)
        self.messages: list[dict[str, Any]] = [
            {"role": "system", "content": self.system_prompt}
        ]

    def add_user_message(self, text: str) -> None:
        with self._state_lock:
            self.messages.append({"role": "user", "content": text})

    def add_environment_message(self, text: str) -> None:
        self.add_user_message(text)

    def request_turn(
        self,
        *,
        cancel_requested: CancelRequested | None = None,
    ) -> ModelTurn:
        with self._state_lock:
            messages = list(self.messages)
        payload: dict[str, Any] = {
            "model": self.model,
            "messages": messages,
        }
        if self.reasoning_effort:
            payload["reasoning_effort"] = self.reasoning_effort
        body = self._request_json(
            "/chat/completions",
            payload,
            cancel_requested=cancel_requested,
        )
        try:
            message = body["choices"][0]["message"]
        except (KeyError, IndexError, TypeError) as exc:
            raise RuntimeError(
                "model response did not contain choices[0].message"
            ) from exc

        if message.get("tool_calls"):
            raise RuntimeError(
                "provider returned a function call, but the FLE harness requires "
                "a Python policy"
            )

        text = message.get("content") or ""
        reasoning_text = message.get("reasoning_content") or ""
        if not isinstance(reasoning_text, str):
            reasoning_text = ""
        with self._state_lock:
            self._raise_if_cancelled(cancel_requested)
            self.messages.append({"role": "assistant", "content": text})
        return ModelTurn(
            text=text,
            reasoning_text=reasoning_text,
            metadata={
                "reasoning_text_chars": len(reasoning_text),
                "usage": body.get("usage") or {},
            },
        )

class ResponsesProvider(_HTTPModelProvider):
    """Stateless Responses adapter that replays typed Items locally.

    DeepSeek's Responses endpoint does not support previous_response_id or a
    server-side conversation object, so response Items are retained and sent
    back explicitly.  This also preserves web_search_call Items as required by
    the provider while keeping game state under the bridge's control.
    """

    def __init__(self, *, enable_web_search: bool = True, **kwargs: Any) -> None:
        super().__init__(**kwargs)
        self.enable_web_search = enable_web_search
        self.items: list[dict[str, Any]] = []
        self._web_search_suppressed = False
        self._web_search_once = False

    def add_user_message(self, text: str) -> None:
        with self._state_lock:
            self.items.append({"role": "user", "content": text})
            self._web_search_suppressed = False
            self._web_search_once = False

    def add_environment_message(self, text: str) -> None:
        # Environment feedback belongs to the same player turn and must not
        # silently reset local-first search gating.
        with self._state_lock:
            self.items.append({"role": "user", "content": text})

    def suppress_web_search(self) -> None:
        with self._state_lock:
            self._web_search_suppressed = True
            self._web_search_once = False

    def allow_web_search_once(self) -> bool:
        with self._state_lock:
            if not self.enable_web_search:
                return False
            self._web_search_once = True
            return True

    def request_turn(
        self,
        *,
        cancel_requested: CancelRequested | None = None,
    ) -> ModelTurn:
        with self._state_lock:
            items = list(self.items)
            search_exposed = self.enable_web_search and (
                not self._web_search_suppressed or self._web_search_once
            )
        payload: dict[str, Any] = {
            "model": self.model,
            "instructions": self.system_prompt,
            "input": items,
        }
        tools = _responses_tools(search_exposed)
        if tools:
            payload["tools"] = tools
            payload["tool_choice"] = "auto"
        if self.reasoning_effort:
            payload["reasoning"] = {"effort": self.reasoning_effort}
        body = self._request_json(
            "/responses",
            payload,
            cancel_requested=cancel_requested,
        )
        status = body.get("status")
        if status == "failed":
            raise RuntimeError(f"Responses request failed: {body.get('error')}")
        if status == "incomplete":
            raise RuntimeError(
                f"Responses request was incomplete: {body.get('incomplete_details')}"
            )
        output = body.get("output")
        if not isinstance(output, list):
            raise RuntimeError("Responses result did not contain an output Item list")

        function_calls = _responses_function_calls(output)
        reasoning_text = _responses_reasoning_text(output)
        metadata = {
            "response_id": body.get("id"),
            "output_item_types": [item.get("type") for item in output],
            "reasoning_items": sum(
                1 for item in output if item.get("type") == "reasoning"
            ),
            "reasoning_text_chars": len(reasoning_text),
            "web_search_calls": sum(
                1 for item in output if item.get("type") == "web_search_call"
            ),
            "assistant_messages": sum(
                1 for item in output if item.get("type") == "message"
            ),
            "web_search_exposed": search_exposed,
            "rejected_function_calls": function_calls,
            "usage": body.get("usage") or {},
        }
        with self._state_lock:
            self._raise_if_cancelled(cancel_requested)
            self._web_search_once = False
            self.items.extend(output)
            for call in function_calls:
                call_id = call.get("call_id")
                if not isinstance(call_id, str) or not call_id:
                    continue
                self.items.append(
                    {
                        "type": "function_call_output",
                        "call_id": call_id,
                        "output": json.dumps(
                            {
                                "ok": False,
                                "error": (
                                    "This provider function call is not an executable "
                                    "FLE game action. Put wiki(...), harness_help(...), "
                                    "skill_help(...), and all Factorio actions inside one "
                                    "fenced Python policy instead."
                                ),
                                "required_format": "fenced_python_policy",
                            },
                            ensure_ascii=False,
                            separators=(",", ":"),
                        ),
                    }
                )
        return ModelTurn(
            text=_responses_output_text(output),
            reasoning_text=reasoning_text,
            metadata=metadata,
        )


def _responses_output_text(output: list[dict[str, Any]]) -> str:
    messages: list[str] = []
    for item in output:
        if item.get("type") != "message":
            continue
        parts: list[str] = []
        for content in item.get("content") or []:
            if content.get("type") in {"output_text", "text"}:
                text = content.get("text")
                if isinstance(text, str):
                    parts.append(text)
        if parts:
            messages.append("\n".join(parts))
    # DeepSeek may emit multiple pre-search assistant messages in one completed
    # Responses result. Only the final message is the actionable/final answer;
    # concatenating all preambles can turn a valid policy into noisy prose.
    return messages[-1] if messages else ""


def _responses_reasoning_text(output: list[dict[str, Any]]) -> str:
    """Extract DeepSeek's plain-text reasoning Items without mixing output text.

    DeepSeek Responses returns thinking as ``reasoning`` Items whose content
    parts have type ``reasoning_text``. A narrow ``text`` fallback keeps the
    adapter compatible with providers that use the older generic part name,
    while summaries or encrypted payloads are deliberately not mislabeled as
    the model's full reasoning text.
    """

    items: list[str] = []
    for item in output:
        if item.get("type") != "reasoning":
            continue
        parts: list[str] = []
        content = item.get("content")
        if isinstance(content, str):
            parts.append(content)
        elif isinstance(content, list):
            for part in content:
                if not isinstance(part, dict):
                    continue
                if part.get("type") not in {"reasoning_text", "text"}:
                    continue
                text = part.get("text")
                if isinstance(text, str) and text:
                    parts.append(text)
        if not parts:
            legacy_text = item.get("reasoning_content")
            if isinstance(legacy_text, str) and legacy_text:
                parts.append(legacy_text)
        if parts:
            items.append("\n".join(parts))
    return "\n\n".join(items)


def _responses_function_calls(output: list[dict[str, Any]]) -> list[dict[str, str]]:
    """Return a secret-free audit record for unexpected provider calls."""

    calls: list[dict[str, str]] = []
    for item in output:
        if item.get("type") != "function_call":
            continue
        raw_arguments = item.get("arguments")
        if isinstance(raw_arguments, str):
            arguments = raw_arguments[:4000]
        else:
            arguments = json.dumps(
                raw_arguments,
                ensure_ascii=False,
                separators=(",", ":"),
            )[:4000]
        calls.append(
            {
                "name": str(item.get("name") or ""),
                "call_id": str(item.get("call_id") or item.get("id") or ""),
                "arguments": arguments,
            }
        )
    return calls


_CHINESE_FUTURE_WORK_RE = re.compile(
    r"(?:^|[。！？!?；;，,\n])\s*"
    r"(?:(?:我|团子|本猫|AIRI)\s*"
    r"(?:这就|现在|接下来|下一步|先|再|马上|会|要|准备|打算|得|去|来)+"
    r"|(?:让我|让团子)\s*)"
    r"(?:去|来)?\s*(?:游戏里|本地|现场|网页上)?\s*"
    r"(?:查(?:查|看|一下)?|查看|查阅|读(?:一下)?|阅读|搜索|搜(?:索|一下)?|"
    r"研究|检查|翻(?:一下|一眼)?|试(?:试|一下)?|尝试|重试|继续|开始|执行|处理|解决)"
)
_CHINESE_NEXT_WORK_RE = re.compile(
    r"(?:^|[。！？!?；;，,\n])\s*(?:接下来|下一步|然后)\s*"
    r"(?:我|团子|本猫|AIRI)?\s*(?:会|要|准备|打算|先|去|来|马上)*\s*"
    r"(?:查(?:查|看|一下)?|查看|查阅|读(?:一下)?|阅读|搜索|搜(?:索|一下)?|"
    r"研究|检查|翻(?:一下|一眼)?|试(?:试|一下)?|尝试|重试|继续|开始|执行|处理|解决)"
)
_CHINESE_CONTINUING_WORK_RE = re.compile(
    r"(?:^|[。！？!?；;，,\n])\s*(?:还要|还得|再|继续)\s*"
    r"(?:去|来|先|一下)?\s*"
    r"(?:查|查看|查阅|看(?:一下|一眼)?|翻(?:一下|一眼)?|读(?:一下)?|阅读|"
    r"搜索|研究|检查|尝试|重试|执行|处理|解决)"
)
_ENGLISH_FUTURE_WORK_RE = re.compile(
    r"(?:^|[.!?;\n])\s*"
    r"(?:i(?:'ll| will|'m going to| am going to)|let me)\s+"
    r"(?:check|look up|read|search|research|inspect|try|retry|continue|start|"
    r"execute|fix|build|do)\b",
    flags=re.IGNORECASE,
)


def response_promises_unperformed_work(text: str) -> bool:
    """Recognize a future-work promise that cannot be a terminal answer.

    The guard is intentionally narrow: it requires an explicit future marker,
    so a completed report such as ``I checked ...`` or an optional offer does
    not get trapped in the policy loop.
    """

    content = text.strip()
    if not content:
        return False
    return any(
        pattern.search(content) is not None
        for pattern in (
            _CHINESE_FUTURE_WORK_RE,
            _CHINESE_NEXT_WORK_RE,
            _CHINESE_CONTINUING_WORK_RE,
            _ENGLISH_FUTURE_WORK_RE,
        )
    )


_GAME_ACTION_INTENT_RE = re.compile(
    r"(?:继续|未完成|再试|试试|重试|完成|修复|修好|建|搭|放置|制造|制作|"
    r"合成|熔炼|炼|烧|挖|采|搬|插入|旋转|拆|拾取|跟随|跟着|移动|走到|"
    r"观察|检查(?:库存|机器|设备|流水线|生产线)|"
    r"\b(?:continue|finish|retry|build|mine|craft|smelt|place|insert|rotate|"
    r"pick\s+up|follow|move|observe|inspect)\b)",
    flags=re.IGNORECASE,
)
_WEB_SEARCH_REQUEST_RE = re.compile(
    r"^\s*WEB_SEARCH_NEEDED\s*:\s*(\S[^\r\n]{0,500})\s*$",
    flags=re.IGNORECASE | re.MULTILINE,
)
_LOCAL_DOC_SEARCH_DETOUR_RE = re.compile(
    r"(?:(?:本地|游戏里|harness|wiki|技能|skill|手册|工具列表|policy)"
    r".{0,30}(?:查|看|翻|读|找)|"
    r"(?:查|看|翻|读|找).{0,30}"
    r"(?:本地|游戏里|harness|wiki|技能|skill|手册|工具列表|policy))",
    flags=re.IGNORECASE,
)
_READ_ONLY_POLICY_ACTIONS = frozenset(
    {
        "status",
        "observe",
        "find_resource",
        "wiki",
        "can_place_entity",
        "inspect_entity",
        "inspect_inventory",
        "get_entities",
    }
)


def message_prefers_local_policy(text: str) -> bool:
    """Use local game knowledge before exposing web search for action turns."""

    return _GAME_ACTION_INTENT_RE.search(text) is not None


def response_requests_web_search(text: str) -> str | None:
    match = _WEB_SEARCH_REQUEST_RE.search(text)
    return match.group(1).strip() if match else None


def response_is_nonterminal_search_detour(
    text: str, metadata: dict[str, Any]
) -> bool:
    calls = metadata.get("web_search_calls")
    return isinstance(calls, int) and calls > 0 and (
        _LOCAL_DOC_SEARCH_DETOUR_RE.search(text) is not None
    )


@dataclass
class _AgentTurnContext:
    generation: int
    request_id: str | None
    player_index: int | None
    action_turn: bool
    cancelled: threading.Event = field(default_factory=threading.Event)


class OpenAICompatibleAgent(BaseAgent):
    def __init__(
        self,
        *,
        base_url: str,
        model: str,
        api_key: str = "",
        api_mode: str = "chat-completions",
        enable_web_search: bool = True,
        system_prompt: str = SYSTEM_PROMPT,
        reasoning_effort: str = "high",
        request_json: JsonRequester | None = None,
        max_policy_steps: int = 12,
    ) -> None:
        super().__init__()
        provider_arguments = {
            "base_url": base_url,
            "model": model,
            "api_key": api_key,
            "system_prompt": compose_policy_system_prompt(system_prompt),
            "reasoning_effort": reasoning_effort,
            "request_json": request_json,
        }
        if api_mode == "responses":
            self.provider: ChatCompletionsProvider | ResponsesProvider = (
                ResponsesProvider(
                    enable_web_search=enable_web_search, **provider_arguments
                )
            )
        elif api_mode == "chat-completions":
            self.provider = ChatCompletionsProvider(**provider_arguments)
        else:
            raise ValueError(f"unsupported API mode: {api_mode}")
        self.api_mode = api_mode
        self._executor = ThreadPoolExecutor(
            max_workers=1, thread_name_prefix="airi-agent"
        )
        self._lock = threading.RLock()
        self._busy = False
        self._action_turn = False
        self._turn_sequence = 0
        self._active_turn: _AgentTurnContext | None = None
        self._max_policy_steps = max(1, int(max_policy_steps))
        self.namespace: CompanionFactorioNamespace | None = None

    def attach(self, bridge: FactorioBridge) -> None:
        super().attach(bridge)
        self.namespace = CompanionFactorioNamespace(
            bridge.execute_command,
            tcp_port=bridge.listen_address[1],
            cancellable_command_runner=(
                bridge.execute_command if isinstance(bridge, FactorioBridge) else None
            ),
        )
        self.provider.set_retry_callback(self._record_model_retry)

    def _record_model_retry(
        self,
        failed_attempt: int,
        max_attempts: int,
        delay: float,
        error: BaseException,
    ) -> None:
        if self.bridge is None:
            return
        with self._lock:
            turn_context = self._active_turn
            if turn_context is None or turn_context.cancelled.is_set():
                return
        self.bridge.record_event(
            "model_request_retry",
            {
                "request_id": turn_context.request_id,
                "failed_attempt": failed_attempt,
                "next_attempt": failed_attempt + 1,
                "max_attempts": max_attempts,
                "delay_seconds": delay,
                "error_type": type(error).__name__,
                "message": str(error),
            },
        )
        self._send_turn_plan(
            turn_context,
            "模型服务连接波动，正在自动重试……",
        )

    def close(self) -> None:
        with self._lock:
            if self._active_turn is not None:
                self._active_turn.cancelled.set()
            self._active_turn = None
            self._busy = False
            self._action_turn = False
        self._executor.shutdown(wait=False, cancel_futures=True)

    def _turn_is_current(self, turn_context: _AgentTurnContext) -> bool:
        with self._lock:
            return (
                self._active_turn is turn_context
                and not turn_context.cancelled.is_set()
            )

    def _raise_if_turn_cancelled(
        self,
        turn_context: _AgentTurnContext | None,
    ) -> None:
        if turn_context is not None and not self._turn_is_current(turn_context):
            raise ModelRequestCancelled("agent turn cancelled by the player")

    def _send_turn_plan(
        self,
        turn_context: _AgentTurnContext | None,
        text: str,
    ) -> None:
        assert self.bridge is not None
        if turn_context is not None and not self._turn_is_current(turn_context):
            return
        if turn_context is None or turn_context.request_id is None:
            self.bridge.send_plan(text)
        else:
            self.bridge.send_plan(text, request_id=turn_context.request_id)

    def _add_turn_environment(
        self,
        turn_context: _AgentTurnContext | None,
        text: str,
    ) -> bool:
        with self._lock:
            if turn_context is not None and (
                self._active_turn is not turn_context
                or turn_context.cancelled.is_set()
            ):
                return False
            self.provider.add_environment_message(text)
            return True

    def _suppress_turn_web_search(
        self,
        turn_context: _AgentTurnContext | None,
    ) -> bool:
        if not isinstance(self.provider, ResponsesProvider):
            return False
        with self._lock:
            if turn_context is not None and (
                self._active_turn is not turn_context
                or turn_context.cancelled.is_set()
            ):
                return False
            self.provider.suppress_web_search()
            return True

    def _allow_turn_web_search_once(
        self,
        turn_context: _AgentTurnContext | None,
    ) -> bool:
        if not isinstance(self.provider, ResponsesProvider):
            return False
        with self._lock:
            if turn_context is not None and (
                self._active_turn is not turn_context
                or turn_context.cancelled.is_set()
            ):
                return False
            return self.provider.allow_web_search_once()

    def cancel_current_turn(
        self,
        *,
        reason: str,
        request_id: str | None = None,
        player_index: int | None = None,
    ) -> bool:
        assert self.bridge is not None
        with self._lock:
            turn_context = self._active_turn
            if turn_context is None:
                return False
            if request_id is not None and turn_context.request_id != request_id:
                return False
            turn_context.cancelled.set()
            # Put a hard boundary into the retained conversation before a new
            # user message can enter. The next turn must not resume or claim
            # completion of the abandoned task.
            self.provider.add_environment_message(
                "The previous player task was cancelled. Do not resume it, execute "
                "any remaining actions from it, or claim that it completed."
            )
            self._active_turn = None
            self._busy = False
            self._action_turn = False
        self.bridge.record_event(
            "agent_turn_cancel_requested",
            {
                "request_id": turn_context.request_id,
                "player_index": player_index,
                "reason": reason,
                "generation": turn_context.generation,
            },
        )
        return True

    def on_chat(
        self,
        text: str,
        context: dict[str, Any],
        player_index: int | None,
        request_id: str | None = None,
    ) -> None:
        assert self.bridge is not None
        with self._lock:
            if self._busy:
                message = "我还在处理上一件事，先等它完成一下。"
                if request_id is None:
                    self.bridge.send_chat_response(message)
                else:
                    self.bridge.send_chat_response(message, request_id=request_id)
                return
            self._turn_sequence += 1
            self._busy = True
            action_turn = message_prefers_local_policy(text)
            turn_context = _AgentTurnContext(
                generation=self._turn_sequence,
                request_id=request_id,
                player_index=player_index,
                action_turn=action_turn,
            )
            self._active_turn = turn_context
            self.bridge.record_event(
                "user_message",
                {
                    "text": text,
                    "player_index": player_index,
                    "context": context,
                    "request_id": request_id,
                    "generation": turn_context.generation,
                },
            )
            self._action_turn = action_turn
            task_skill = task_skill_for_message(text) if self._action_turn else None
            task_guidance = ""
            if task_skill is not None:
                skill_name, skill_documentation = task_skill
                task_guidance = (
                    "\nPreloaded local task skill (already available; do not spend a "
                    f"policy calling skill_help for it): {skill_name}\n"
                    + skill_documentation
                    + "\nExecution requirement: reuse known facts, batch every "
                    "currently feasible prerequisite and game action into one "
                    "coherent Python policy, and verify the requested end state."
                )
                self.bridge.record_event(
                    "task_skill_preloaded",
                    {"skill": skill_name},
                )
            self.provider.add_user_message(
                text
                + "\nCurrent in-game context:\n"
                + json.dumps(context, ensure_ascii=False, separators=(",", ":"))
                + task_guidance
            )
            if (
                isinstance(self.provider, ResponsesProvider)
                and self._action_turn
            ):
                self.provider.suppress_web_search()
                self.bridge.record_event(
                    "web_search_deferred",
                    {
                        "reason": "local_policy_first",
                        "enabled": self.provider.enable_web_search,
                    },
                )
        self._send_turn_plan(turn_context, "正在观察并规划……")
        self._executor.submit(self._run_policy_loop, turn_context)

    def _fail_turn(
        self,
        message: str,
        turn_context: _AgentTurnContext | None = None,
    ) -> None:
        assert self.bridge is not None
        with self._lock:
            if turn_context is not None and (
                self._active_turn is not turn_context
                or turn_context.cancelled.is_set()
            ):
                return
            self._busy = False
            self._action_turn = False
            if turn_context is not None:
                self._active_turn = None
            self.bridge.record_event(
                "agent_error",
                {
                    "message": message,
                    "request_id": (
                        turn_context.request_id if turn_context is not None else None
                    ),
                },
            )
            if turn_context is None or turn_context.request_id is None:
                self.bridge.send_plan("")
                self.bridge.send_chat_response(message)
            else:
                self.bridge.send_plan("", request_id=turn_context.request_id)
                self.bridge.send_chat_response(
                    message,
                    request_id=turn_context.request_id,
                )

    def _finish_turn(
        self,
        content: str,
        metadata: dict[str, Any],
        turn_context: _AgentTurnContext | None = None,
    ) -> None:
        assert self.bridge is not None
        with self._lock:
            if turn_context is not None and (
                self._active_turn is not turn_context
                or turn_context.cancelled.is_set()
            ):
                return
            self._busy = False
            self._action_turn = False
            if turn_context is not None:
                self._active_turn = None
            self.bridge.record_event(
                "assistant_message",
                {
                    "text": content,
                    "metadata": metadata,
                    "request_id": (
                        turn_context.request_id if turn_context is not None else None
                    ),
                },
            )
            if turn_context is None or turn_context.request_id is None:
                self.bridge.send_plan("")
                self.bridge.send_chat_response(content)
            else:
                self.bridge.send_plan("", request_id=turn_context.request_id)
                self.bridge.send_chat_response(
                    content,
                    request_id=turn_context.request_id,
                )

    def _run_policy_loop(
        self,
        turn_context: _AgentTurnContext | None = None,
    ) -> None:
        assert self.bridge is not None
        assert self.namespace is not None
        last_metadata: dict[str, Any] = {}
        read_only_streak = 0
        action_turn = (
            turn_context.action_turn
            if turn_context is not None
            else self._action_turn
        )
        for step in range(1, self._max_policy_steps + 1):
            try:
                self._raise_if_turn_cancelled(turn_context)
                if turn_context is None:
                    turn = self.provider.request_turn()
                else:
                    turn = self.provider.request_turn(
                        cancel_requested=turn_context.cancelled.is_set,
                    )
                self._raise_if_turn_cancelled(turn_context)
            except ModelRequestCancelled:
                return
            except Exception as exc:
                self._fail_turn(f"模型请求失败：{exc}", turn_context)
                return

            last_metadata = turn.metadata
            if turn.reasoning_text:
                self.bridge.record_event(
                    "model_reasoning",
                    {
                        "step": step,
                        "text": turn.reasoning_text,
                        "api_mode": self.api_mode,
                        "response_id": turn.metadata.get("response_id"),
                        "request_id": (
                            turn_context.request_id
                            if turn_context is not None
                            else None
                        ),
                    },
                )
            self.bridge.record_event(
                "model_response",
                {
                    "step": step,
                    "text": turn.text,
                    "metadata": turn.metadata,
                    "request_id": (
                        turn_context.request_id if turn_context is not None else None
                    ),
                },
            )
            rejected_calls = turn.metadata.get("rejected_function_calls")
            if isinstance(rejected_calls, list) and rejected_calls:
                if isinstance(self.provider, ResponsesProvider):
                    self._suppress_turn_web_search(turn_context)
                call_names = [
                    str(call.get("name") or "(unnamed)")
                    for call in rejected_calls
                    if isinstance(call, dict)
                ]
                feedback = (
                    "The provider emitted unsupported function_call Item(s): "
                    + ", ".join(call_names)
                    + ". Their protocol outputs were returned as rejected, so do not "
                    "repeat them. wiki(...), harness_help(...), skill_help(...), and "
                    "all Factorio actions are Python namespace functions: put them "
                    "inside one fenced Python policy now. Native web_search is only "
                    "for broader external facts; never use it to read local harness "
                    "or skill documentation."
                )
                self.bridge.record_event(
                    "model_function_call_rejected",
                    {
                        "step": step,
                        "calls": rejected_calls,
                        "metadata": turn.metadata,
                    },
                )
                self._add_turn_environment(turn_context, feedback)
                self._send_turn_plan(
                    turn_context,
                    "模型误用了函数调用，正在改写为 Python 策略……",
                )
                continue
            if not turn.text.strip():
                if (
                    isinstance(self.provider, ResponsesProvider)
                    and turn.metadata.get("web_search_calls")
                ):
                    self._suppress_turn_web_search(turn_context)
                feedback = (
                    "The model response contained no final text or Python policy. "
                    "Emit one valid fenced Python policy if game work remains, or "
                    "a concrete prose answer if no game action is needed."
                )
                self.bridge.record_event(
                    "model_empty_response",
                    {"step": step, "metadata": turn.metadata},
                )
                self._add_turn_environment(turn_context, feedback)
                continue
            policy = parse_policy_text(
                turn.text,
                turn.metadata.get("usage")
                if isinstance(turn.metadata.get("usage"), dict)
                else {},
            )
            if policy is None:
                if response_contains_policy(turn.text):
                    if isinstance(self.provider, ResponsesProvider):
                        self._suppress_turn_web_search(turn_context)
                    feedback = (
                        "FLE harness rejected the Python policy. Emit one syntactically "
                        "valid fenced python block, or answer in prose "
                        "with no code block if the task is already complete."
                    )
                    self.bridge.record_event(
                        "policy_parse_error",
                        {"step": step, "text": turn.text},
                    )
                    self._add_turn_environment(turn_context, feedback)
                    continue
                search_query = response_requests_web_search(turn.text)
                if search_query is not None:
                    if (
                        isinstance(self.provider, ResponsesProvider)
                        and self._allow_turn_web_search_once(turn_context)
                    ):
                        self.bridge.record_event(
                            "model_web_search_requested",
                            {"step": step, "query": search_query},
                        )
                        self._add_turn_environment(
                            turn_context,
                            "Native web search is exposed for the next response only. "
                            f"Search specifically for: {search_query}. After using the "
                            "external result, return to a fenced Python policy if game "
                            "work remains; do not search for local harness, Wiki, skill "
                            "functions, or current game state."
                        )
                        self._send_turn_plan(
                            turn_context,
                            "正在查询必要的外部资料……",
                        )
                        continue
                    self._add_turn_environment(
                        turn_context,
                        "Native web search is unavailable in this session. Continue "
                        "with local wiki(...), harness_help(...), and skill_help(...) "
                        "inside a Python policy, or state the exact external fact that "
                        "blocks completion."
                    )
                    continue
                future_work = response_promises_unperformed_work(turn.text)
                search_detour = response_is_nonterminal_search_detour(
                    turn.text, turn.metadata
                )
                if future_work or search_detour:
                    if isinstance(self.provider, ResponsesProvider):
                        self._suppress_turn_web_search(turn_context)
                    feedback = (
                        "This prose promises future work and is not a terminal answer. "
                        "Perform that work now in this same turn. Use wiki(...) for "
                        "live item/entity/recipe prototypes, harness_help(...) for the "
                        "local adapter API, or skill_help(...) for a task playbook. "
                        "Those calls belong inside a fenced Python policy, not a "
                        "provider function call or web search. Use native web_search "
                        "only when it is exposed and broader external Factorio knowledge "
                        "is still genuinely needed. Then emit the "
                        "next Python policy. Only return final prose after verified "
                        "completion, an explicit capability/input blocker, or a genuinely "
                        "required question."
                    )
                    self.bridge.record_event(
                        "model_nonterminal_response",
                        {
                            "step": step,
                            "reason": (
                                "search_detour"
                                if search_detour
                                else "future_work_promise"
                            ),
                            "text": turn.text,
                            "metadata": turn.metadata,
                        },
                    )
                    self._add_turn_environment(turn_context, feedback)
                    self._send_turn_plan(
                        turn_context,
                        "回复还不是终态，正在继续查证并执行……",
                    )
                    continue
                self._finish_turn(
                    turn.text or "完成了。",
                    turn.metadata,
                    turn_context,
                )
                return

            self.bridge.record_event(
                "model_policy",
                {
                    "step": step,
                    "code": str(policy.code),
                    "text_response": policy.meta.text_response,
                    "usage": {
                        "input_tokens": policy.meta.input_tokens,
                        "output_tokens": policy.meta.output_tokens,
                        "total_tokens": policy.meta.total_tokens,
                    },
                    "request_id": (
                        turn_context.request_id if turn_context is not None else None
                    ),
                },
            )
            self._send_turn_plan(
                turn_context,
                f"正在执行 Python 策略（{step}）……",
            )

            try:
                self._raise_if_turn_cancelled(turn_context)
                if turn_context is None:
                    output = self.namespace.evaluate(str(policy.code), timeout=600)
                else:
                    output = self.namespace.evaluate(
                        str(policy.code),
                        timeout=600,
                        cancel_requested=turn_context.cancelled.is_set,
                        cancel_wait=turn_context.cancelled.wait,
                    )
                self._raise_if_turn_cancelled(turn_context)
            except (ModelRequestCancelled, PolicyCancelledError):
                return
            except PolicyValidationError as exc:
                output = f"Policy validation error: {exc}"
            except Exception as exc:
                output = f"Policy harness error: {type(exc).__name__}: {exc}"

            policy_actions = list(self.namespace._policy_action_names())
            material_actions = [
                action
                for action in policy_actions
                if action not in _READ_ONLY_POLICY_ACTIONS
            ]

            try:
                self._raise_if_turn_cancelled(turn_context)
                observation = self.bridge.execute_command(
                    "observe", {"radius": 32}, timeout=120
                )
                self._raise_if_turn_cancelled(turn_context)
                self.namespace._update_player_location(observation)
            except ModelRequestCancelled:
                return
            except Exception as exc:
                observation = {
                    "error": f"fresh observation failed: {type(exc).__name__}: {exc}"
                }
                try:
                    self._raise_if_turn_cancelled(turn_context)
                except ModelRequestCancelled:
                    return

            error_occurred = any(
                marker in output.lower()
                for marker in (
                    "error occurred:",
                    "policy validation error:",
                    "policy harness error:",
                    "exception:",
                )
            )
            self.bridge.record_event(
                "policy_result",
                {
                    "step": step,
                    "ok": not error_occurred,
                    "output": output,
                    "observation": observation,
                    "policy_actions": policy_actions,
                    "material_actions": material_actions,
                },
            )
            no_progress = action_turn and not error_occurred and not material_actions
            if no_progress:
                read_only_streak += 1
                self.bridge.record_event(
                    "policy_no_progress",
                    {
                        "step": step,
                        "read_only_streak": read_only_streak,
                        "policy_actions": policy_actions,
                    },
                )
            else:
                read_only_streak = 0
            rendered_observation = json.dumps(
                observation,
                ensure_ascii=False,
                separators=(",", ":"),
            )
            feedback = (
                "FLE Python policy execution result:\n"
                + (output or "(the policy produced no printed output)")
                + "\nFresh permitted in-game observation:\n"
                + rendered_observation
                + "\nContinue with another Python policy only if work or verification "
                "remains. If the requested outcome is verified, answer in normal prose "
                "without a code block."
            )
            if action_turn:
                remaining_steps = self._max_policy_steps - step
                feedback += (
                    "\nPolicy progress: executed "
                    + (", ".join(policy_actions) if policy_actions else "no game actions")
                    + f"; {remaining_steps} policy step(s) remain."
                )
                if no_progress:
                    feedback += (
                        "\nNO-PROGRESS GUARD: this action turn used only documentation "
                        "or read-only inspection. If the observation itself fully "
                        "answers the user, finish now. Otherwise do not reread the same "
                        "docs or repeat observations in the next model turn. Reuse all "
                        "facts already returned and emit one Python policy that batches "
                        "the largest safe block of acquisition, crafting, waiting, "
                        "placement, fueling, and verification work currently possible."
                    )
                else:
                    feedback += (
                        "\nExecution discipline: do not spend one model turn per "
                        "prerequisite. Reuse prior results and batch all currently "
                        "feasible remaining work into the next policy."
                    )
                if "craft_item" in policy_actions and (
                    "place_entity" not in policy_actions[
                        policy_actions.index("craft_item") + 1 :
                    ]
                ):
                    feedback += (
                        "\nCrafting reminder: craft_item only queued work. In the next "
                        "policy, wait for the required items to appear in the backpack "
                        "and continue directly into placement and verification; do not "
                        "end another policy immediately after queueing or inspecting."
                    )
            self._add_turn_environment(turn_context, feedback)
            self._send_turn_plan(
                turn_context,
                "正在核对结果并决定下一步……",
            )

        self._finish_turn(
            "已经达到本次任务的 Python 策略步数上限；最后状态已保留，请根据游戏内实际结果决定是否继续。",
            last_metadata,
            turn_context,
        )


def _environment_flag(name: str, default: bool) -> bool:
    value = os.getenv(name)
    if value is None:
        return default
    return value.strip().lower() not in {"0", "false", "no", "off"}


def load_system_prompt(path: str | Path | None) -> str:
    if not path:
        return SYSTEM_PROMPT
    prompt_path = Path(path).expanduser().resolve()
    try:
        size = prompt_path.stat().st_size
        if size > 256_000:
            raise RuntimeError("system prompt file exceeds the 256 KB research limit")
        prompt = prompt_path.read_text(encoding="utf-8-sig").strip()
    except OSError as exc:
        raise RuntimeError(f"could not read system prompt file {prompt_path}: {exc}") from exc
    if not prompt:
        raise RuntimeError(f"system prompt file {prompt_path} is blank")
    return prompt


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Run the AIRI Factorio AgentBridge")
    parser.add_argument("--listen-port", type=int, default=31501)
    parser.add_argument("--factorio-port", type=int, default=31500)
    parser.add_argument("--model", default=os.getenv("AIRI_FACTORIO_MODEL", ""))
    parser.add_argument(
        "--base-url",
        default=os.getenv("AIRI_FACTORIO_BASE_URL", ""),
    )
    parser.add_argument(
        "--provider-config",
        default=os.getenv("AIRI_FACTORIO_PROVIDER_CONFIG", ""),
        help="Local three-line file containing api_key, base_url, and model",
    )
    parser.add_argument(
        "--api-mode",
        choices=("chat-completions", "responses"),
        default=os.getenv("AIRI_FACTORIO_API_MODE", "chat-completions"),
    )
    parser.add_argument(
        "--reasoning-effort",
        choices=("low", "high", "max"),
        default=os.getenv("AIRI_FACTORIO_REASONING_EFFORT", "high"),
    )
    parser.add_argument(
        "--system-prompt-file",
        default=os.getenv("AIRI_FACTORIO_SYSTEM_PROMPT_FILE", ""),
        help="UTF-8 system prompt snapshot for this research session",
    )
    parser.add_argument(
        "--event-log",
        default=os.getenv("AIRI_FACTORIO_EVENT_LOG", ""),
        help="Optional JSONL research trajectory path",
    )
    parser.add_argument(
        "--status-file",
        default=os.getenv("AIRI_FACTORIO_STATUS_FILE", ""),
        help="Optional JSON status snapshot path for the Control Center",
    )
    parser.add_argument(
        "--session-id",
        default=os.getenv("AIRI_FACTORIO_SESSION_ID", ""),
    )
    parser.add_argument(
        "--web-search",
        action=argparse.BooleanOptionalAction,
        default=_environment_flag("AIRI_FACTORIO_WEB_SEARCH", True),
        help="Expose the provider's native web_search tool in Responses mode",
    )
    parser.add_argument("--heuristic", action="store_true")
    parser.add_argument("--verbose", action="store_true")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    config = load_provider_config(args.provider_config) if args.provider_config else None
    model = args.model or (config.model if config else "")
    base_url = args.base_url or (
        config.base_url if config else "https://api.openai.com/v1"
    )
    api_key = os.getenv("AIRI_FACTORIO_API_KEY", "") or (
        config.api_key if config else ""
    )
    system_prompt = load_system_prompt(args.system_prompt_file)
    token = os.getenv("AIRI_FACTORIO_SESSION_TOKEN", "")
    event_logger = None
    if args.event_log or args.status_file:
        event_logger = BridgeEventLogger(
            event_log=args.event_log or None,
            status_file=args.status_file or None,
            metadata={
                "session_id": args.session_id or None,
                "model": model or None,
                "api_mode": args.api_mode,
                "web_search": bool(args.web_search),
                "reasoning_effort": args.reasoning_effort,
            },
        )
    bridge = FactorioBridge(
        listen_port=args.listen_port,
        factorio_port=args.factorio_port,
        token=token,
        verbose=args.verbose,
        event_logger=event_logger,
    )
    if model and not args.heuristic:
        agent: BaseAgent = OpenAICompatibleAgent(
            base_url=base_url,
            model=model,
            api_key=api_key,
            api_mode=args.api_mode,
            enable_web_search=args.web_search,
            system_prompt=system_prompt,
            reasoning_effort=args.reasoning_effort,
        )
        search_suffix = " + native web search" if (
            args.api_mode == "responses" and args.web_search
        ) else ""
        print(f"Agent mode: {args.api_mode} model {model}{search_suffix}")
    else:
        agent = HeuristicAgent()
        print("Agent mode: dependency-free local command parser")
    bridge.attach_agent(agent)

    try:
        bridge.serve_forever()
    except KeyboardInterrupt:
        bridge.request_stop()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
