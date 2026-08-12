"""Graphical single-player smoke test for the AIRI UDP/action loop.

This is intentionally not auto-discovered by unittest. It requires a licensed
local Factorio binary and an existing save containing a player. It opens the
game, drives the protocol without UI automation, and then closes the process.
"""

from __future__ import annotations

import argparse
from pathlib import Path
import socket
import subprocess
import sys
import time
from typing import Any

REPOSITORY_ROOT = Path(__file__).resolve().parents[2]
if str(REPOSITORY_ROOT) not in sys.path:
    sys.path.insert(0, str(REPOSITORY_ROOT))

from fle.companion.protocol import Packet, decode_packet, encode_packet


def wait_for_packet(
    sock: socket.socket,
    predicate,
    *,
    process: subprocess.Popen[bytes],
    timeout: float,
) -> Packet:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if process.poll() is not None:
            raise RuntimeError(f"Factorio exited early with code {process.returncode}")
        try:
            packet = decode_packet(sock.recv(65_535))
        except socket.timeout:
            continue
        if predicate(packet):
            return packet
    raise TimeoutError("timed out waiting for a matching Factorio UDP packet")


def send_command(
    sock: socket.socket,
    factorio_port: int,
    action: str,
    arguments: dict[str, Any],
    *,
    process: subprocess.Popen[bytes],
    timeout: float = 20,
) -> Packet:
    request_id = f"smoke-{action}-{time.monotonic_ns()}"
    encoded = encode_packet(
        "command",
        {"action": action, "arguments": arguments},
        packet_id=request_id,
    )
    sock.sendto(encoded, ("127.0.0.1", factorio_port))
    packet = wait_for_packet(
        sock,
        lambda candidate: candidate.type == "result"
        and candidate.payload.get("request_id") == request_id,
        process=process,
        timeout=timeout,
    )
    if not packet.payload.get("ok"):
        raise RuntimeError(
            f"Factorio action {action!r} failed: {packet.payload.get('result')}"
        )
    return packet


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser()
    parser.add_argument("--factorio", type=Path, required=True)
    parser.add_argument("--mod-directory", type=Path, required=True)
    parser.add_argument("--save", type=Path, required=True)
    parser.add_argument("--game-port", type=int, default=31500)
    parser.add_argument("--bridge-port", type=int, default=31501)
    parser.add_argument("--startup-timeout", type=float, default=30)
    parser.add_argument("--command-timeout", type=float, default=20)
    return parser


def main() -> int:
    args = build_parser().parse_args()
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.bind(("127.0.0.1", args.bridge_port))
    sock.settimeout(0.25)

    command = [
        str(args.factorio.resolve()),
        "--mod-directory",
        str(args.mod_directory.resolve()),
        "--enable-lua-udp",
        str(args.game_port),
        "--load-game",
        str(args.save.resolve()),
    ]
    process = subprocess.Popen(
        command,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        creationflags=0,
    )
    try:
        wait_for_packet(
            sock,
            lambda packet: packet.type in {"hello", "heartbeat"},
            process=process,
            timeout=args.startup_timeout,
        )
        status = send_command(
            sock,
            args.game_port,
            "status",
            {},
            process=process,
            timeout=args.command_timeout,
        ).payload["result"]
        character = status.get("character") or {}
        if not character.get("present"):
            raise RuntimeError("AIRI character was not present in the status result")

        observation = send_command(
            sock,
            args.game_port,
            "observe",
            {"radius": 16},
            process=process,
            timeout=args.command_timeout,
        ).payload["result"]
        for key in ("character", "owner", "movement", "resources", "buildings"):
            if key not in observation:
                raise RuntimeError(f"observation is missing {key!r}")

        position = character["position"]
        move_result = send_command(
            sock,
            args.game_port,
            "move_to",
            {"x": position["x"], "y": position["y"]},
            process=process,
            timeout=args.command_timeout,
        ).payload["result"]
        if move_result.get("action") != "move_to":
            raise RuntimeError("async move_to result did not identify its action")

        print(
            "Factorio UDP smoke passed: character present, observation serialized, "
            "and async move_to completed"
        )
        return 0
    finally:
        sock.close()
        if process.poll() is None:
            process.terminate()
            try:
                process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                process.kill()


if __name__ == "__main__":
    raise SystemExit(main())
