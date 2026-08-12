"""Non-UI operations for AIRI Factorio Control Center."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import socket
import subprocess
import sys
import tempfile
from typing import Any, Callable
from urllib import error as urllib_error
from urllib import request as urllib_request

from .control_center_store import ProviderProfile, SessionFiles


@dataclass(frozen=True)
class ProviderTestResult:
    api_mode: str
    model: str
    status: str
    text: str
    output_item_types: tuple[str, ...] = ()
    web_search_calls: int = 0
    usage: dict[str, Any] | None = None


ProviderTestRequester = Callable[
    [str, dict[str, Any], str],
    dict[str, Any],
]


def _request_provider_json(
    url: str,
    payload: dict[str, Any],
    api_key: str,
) -> dict[str, Any]:
    request = urllib_request.Request(
        url,
        data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
        headers={
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
            "Accept": "application/json",
        },
        method="POST",
    )
    try:
        with urllib_request.urlopen(request, timeout=180) as response:
            return json.loads(response.read().decode("utf-8"))
    except urllib_error.HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace")[:1000]
        raise RuntimeError(f"provider HTTP {exc.code}: {detail}") from exc
    except (urllib_error.URLError, TimeoutError, json.JSONDecodeError) as exc:
        raise RuntimeError(f"provider request failed: {exc}") from exc


def test_provider(
    profile: ProviderProfile,
    api_key: str,
    *,
    native_search: bool = False,
    requester: ProviderTestRequester | None = None,
) -> ProviderTestResult:
    """Run one small, secret-free provider probe for the selected profile."""

    profile.validated()
    if not api_key:
        raise RuntimeError("the selected provider has no saved API credential")
    requester = requester or _request_provider_json
    if profile.api_mode == "responses":
        prompt = "Reply with the single word OK."
        payload: dict[str, Any] = {
            "model": profile.model,
            "instructions": "This is a connection test. Follow the request exactly.",
            "input": prompt,
            "max_output_tokens": 200,
            "reasoning": {"effort": profile.reasoning_effort},
        }
        if native_search:
            payload.update(
                {
                    "instructions": (
                        "This is a native web-search capability test. Use web search "
                        "and answer concisely with the source URL."
                    ),
                    "input": (
                        "Search the official Factorio website and report the current "
                        "stable Factorio version."
                    ),
                    "tools": [{"type": "web_search"}],
                    "tool_choice": {"type": "web_search"},
                    "max_output_tokens": 500,
                }
            )
        body = requester(
            profile.base_url.rstrip("/") + "/responses",
            payload,
            api_key,
        )
        output = body.get("output")
        if not isinstance(output, list):
            raise RuntimeError("Responses probe did not return an output Item list")
        item_types = tuple(
            str(item.get("type")) for item in output if isinstance(item, dict)
        )
        text_parts: list[str] = []
        for item in output:
            if not isinstance(item, dict) or item.get("type") != "message":
                continue
            for part in item.get("content") or []:
                if isinstance(part, dict) and part.get("type") in {
                    "output_text",
                    "text",
                }:
                    text = part.get("text")
                    if isinstance(text, str):
                        text_parts.append(text)
        status = str(body.get("status") or "unknown")
        if status not in {"completed", "success"}:
            raise RuntimeError(f"Responses probe finished with status {status}")
        search_calls = sum(1 for item_type in item_types if item_type == "web_search_call")
        if native_search and search_calls == 0:
            raise RuntimeError("provider completed without a web_search_call Item")
        return ProviderTestResult(
            api_mode=profile.api_mode,
            model=str(body.get("model") or profile.model),
            status=status,
            text="\n".join(text_parts),
            output_item_types=item_types,
            web_search_calls=search_calls,
            usage=body.get("usage") or {},
        )

    if native_search:
        raise RuntimeError("native web search requires a Responses provider profile")
    payload = {
        "model": profile.model,
        "messages": [
            {"role": "system", "content": "This is a connection test."},
            {"role": "user", "content": "Reply with the single word OK."},
        ],
        "max_tokens": 50,
        "reasoning_effort": profile.reasoning_effort,
    }
    body = requester(
        profile.base_url.rstrip("/") + "/chat/completions",
        payload,
        api_key,
    )
    try:
        message = body["choices"][0]["message"]
    except (KeyError, IndexError, TypeError) as exc:
        raise RuntimeError("Chat Completions probe did not return a message") from exc
    return ProviderTestResult(
        api_mode=profile.api_mode,
        model=str(body.get("model") or profile.model),
        status="completed",
        text=str(message.get("content") or ""),
        usage=body.get("usage") or {},
    )


def build_bridge_command(
    profile: ProviderProfile,
    session: SessionFiles,
    *,
    bridge_port: int,
    game_udp_port: int,
    python_executable: str | None = None,
) -> list[str]:
    """Build a Bridge command that contains paths and metadata, never the API key."""

    command = [
        python_executable or sys.executable,
        "-u",
        "-m",
        "fle.companion.bridge",
        "--listen-port",
        str(bridge_port),
        "--factorio-port",
        str(game_udp_port),
        "--model",
        profile.model,
        "--base-url",
        profile.base_url,
        "--api-mode",
        profile.api_mode,
        "--reasoning-effort",
        profile.reasoning_effort,
        "--system-prompt-file",
        str(session.prompt_snapshot),
        "--event-log",
        str(session.event_log),
        "--status-file",
        str(session.status_file),
        "--session-id",
        session.session_id,
        "--verbose",
        "--web-search" if profile.web_search else "--no-web-search",
    ]
    return command


def udp_port_available(port: int) -> bool:
    if port < 1024 or port > 65535:
        return False
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        sock.bind(("127.0.0.1", port))
        return True
    except OSError:
        return False
    finally:
        sock.close()


def factorio_process_running() -> bool:
    if os.name != "nt":
        return False
    try:
        result = subprocess.run(
            ["tasklist", "/FI", "IMAGENAME eq Factorio.exe", "/FO", "CSV", "/NH"],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=5,
            check=False,
            creationflags=subprocess.CREATE_NO_WINDOW,
        )
    except (OSError, subprocess.SubprocessError):
        return False
    return "factorio.exe" in result.stdout.lower()


def read_bridge_status(path: str | Path) -> dict[str, Any]:
    status_path = Path(path)
    if not status_path.exists():
        return {}
    try:
        value = json.loads(status_path.read_text(encoding="utf-8-sig"))
    except (OSError, json.JSONDecodeError):
        return {}
    return value if isinstance(value, dict) else {}


def mark_bridge_status_stopped(path: str | Path, exit_code: int | None) -> None:
    status_path = Path(path)
    status = read_bridge_status(status_path)
    status.update(
        {
            "running": False,
            "connected": False,
            "exit_code": exit_code,
            "stopped_at": datetime.now(timezone.utc).isoformat(timespec="milliseconds"),
        }
    )
    status_path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{status_path.name}.",
        suffix=".tmp",
        dir=str(status_path.parent),
    )
    os.close(descriptor)
    temporary = Path(temporary_name)
    try:
        temporary.write_text(
            json.dumps(status, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        temporary.replace(status_path)
    finally:
        temporary.unlink(missing_ok=True)


def bridge_status_connected(status: dict[str, Any], *, max_age: float = 12.0) -> bool:
    if not status.get("running") or not status.get("connected"):
        return False
    timestamp = status.get("last_factorio_packet_at")
    if not isinstance(timestamp, str):
        return False
    try:
        packet_time = datetime.fromisoformat(timestamp.replace("Z", "+00:00"))
    except ValueError:
        return False
    if packet_time.tzinfo is None:
        packet_time = packet_time.replace(tzinfo=timezone.utc)
    return (datetime.now(timezone.utc) - packet_time).total_seconds() <= max_age
