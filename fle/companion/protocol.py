"""Versioned JSON-over-UDP protocol shared by Factorio and AgentBridge."""

from __future__ import annotations

from dataclasses import dataclass
import json
import uuid
from typing import Any, Mapping


PROTOCOL_VERSION = 1
MAX_DATAGRAM_BYTES = 60_000


class ProtocolError(ValueError):
    """Raised when a bridge packet is malformed or unsafe to send."""


@dataclass(frozen=True)
class Packet:
    id: str
    type: str
    payload: dict[str, Any]
    version: int = PROTOCOL_VERSION
    token: str = ""
    tick: int | None = None


def new_packet_id(prefix: str = "bridge") -> str:
    return f"{prefix}-{uuid.uuid4().hex}"


def encode_packet(
    packet_type: str,
    payload: Mapping[str, Any] | None = None,
    *,
    packet_id: str | None = None,
    token: str = "",
    tick: int | None = None,
) -> bytes:
    if not isinstance(packet_type, str) or not packet_type:
        raise ProtocolError("packet type must be a non-empty string")
    if payload is not None and not isinstance(payload, Mapping):
        raise ProtocolError("packet payload must be a mapping")

    body: dict[str, Any] = {
        "version": PROTOCOL_VERSION,
        "id": packet_id or new_packet_id(),
        "type": packet_type,
        "token": token,
        "payload": dict(payload or {}),
    }
    if tick is not None:
        body["tick"] = tick

    try:
        encoded = json.dumps(
            body,
            ensure_ascii=False,
            separators=(",", ":"),
        ).encode("utf-8")
    except (TypeError, ValueError) as exc:
        raise ProtocolError(f"packet is not JSON serializable: {exc}") from exc

    if len(encoded) > MAX_DATAGRAM_BYTES:
        raise ProtocolError(
            f"packet is {len(encoded)} bytes; maximum is {MAX_DATAGRAM_BYTES}"
        )
    return encoded


def decode_packet(data: bytes | bytearray | memoryview | str) -> Packet:
    if isinstance(data, str):
        raw = data.encode("utf-8")
    else:
        raw = bytes(data)
    if len(raw) > MAX_DATAGRAM_BYTES:
        raise ProtocolError("received packet exceeds the safe datagram size")

    try:
        body = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ProtocolError(f"packet is not valid UTF-8 JSON: {exc}") from exc
    if not isinstance(body, dict):
        raise ProtocolError("packet root must be a JSON object")
    if body.get("version") != PROTOCOL_VERSION:
        raise ProtocolError(f"unsupported protocol version: {body.get('version')!r}")

    packet_id = body.get("id")
    packet_type = body.get("type")
    payload = body.get("payload", {})
    token = body.get("token", "")
    tick = body.get("tick")
    if not isinstance(packet_id, str) or not packet_id:
        raise ProtocolError("packet id must be a non-empty string")
    if not isinstance(packet_type, str) or not packet_type:
        raise ProtocolError("packet type must be a non-empty string")
    if not isinstance(payload, dict):
        raise ProtocolError("packet payload must be a JSON object")
    if not isinstance(token, str):
        raise ProtocolError("packet token must be a string")
    if tick is not None and not isinstance(tick, int):
        raise ProtocolError("packet tick must be an integer when present")

    return Packet(
        id=packet_id,
        type=packet_type,
        payload=payload,
        version=PROTOCOL_VERSION,
        token=token,
        tick=tick,
    )
