"""Install AIRI Companion and launch one graphical Factorio client plus AgentBridge."""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
from typing import Sequence


MOD_NAME = "airi-companion"
MOD_VERSION = "0.1.0"


def _environment_flag(name: str, default: bool) -> bool:
    value = os.getenv(name)
    if value is None:
        return default
    return value.strip().lower() not in {"0", "false", "no", "off"}


def _udp_port(value: str) -> int:
    port = int(value)
    if port < 1024 or port > 65535:
        raise argparse.ArgumentTypeError("UDP ports must be between 1024 and 65535")
    return port


def _atomic_write_text(path: Path, text: str) -> None:
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{path.name}.",
        suffix=".tmp",
        dir=str(path.parent),
    )
    os.close(descriptor)
    temporary = Path(temporary_name)
    try:
        temporary.write_text(text, encoding="utf-8")
        temporary.replace(path)
    finally:
        temporary.unlink(missing_ok=True)


def discover_factorio_executable(explicit: str | None = None) -> Path:
    candidates = [
        explicit,
        os.getenv("FACTORIO_EXE"),
        r"E:\SteamLibrary\steamapps\common\Factorio\bin\x64\factorio.exe",
        r"C:\Program Files (x86)\Steam\steamapps\common\Factorio\bin\x64\factorio.exe",
        r"C:\Program Files\Factorio\bin\x64\factorio.exe",
    ]
    for candidate in candidates:
        if candidate:
            path = Path(candidate).expanduser().resolve()
            if path.is_file():
                return path
    raise FileNotFoundError(
        "Factorio executable was not found; pass --factorio or set FACTORIO_EXE"
    )


def factorio_user_data_dir() -> Path:
    appdata = os.getenv("APPDATA")
    if not appdata:
        raise RuntimeError("APPDATA is not set")
    return Path(appdata) / "Factorio"


def _enable_mod(mods_dir: Path) -> None:
    mod_list_path = mods_dir / "mod-list.json"
    if mod_list_path.exists():
        body = json.loads(mod_list_path.read_text(encoding="utf-8-sig"))
    else:
        body = {"mods": [{"name": "base", "enabled": True}]}
    mods = body.setdefault("mods", [])
    for entry in mods:
        if entry.get("name") == MOD_NAME:
            entry["enabled"] = True
            break
    else:
        mods.append({"name": MOD_NAME, "enabled": True})
    _atomic_write_text(
        mod_list_path,
        json.dumps(body, ensure_ascii=False, indent=2) + "\n",
    )


def install_mod(mods_dir: Path | None = None) -> Path:
    mods_dir = mods_dir or factorio_user_data_dir() / "mods"
    mods_dir.mkdir(parents=True, exist_ok=True)
    source = Path(__file__).with_name("factorio_mod")
    target = mods_dir / f"{MOD_NAME}_{MOD_VERSION}"

    if target.exists():
        manifest_path = target / "info.json"
        try:
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise RuntimeError(
                f"refusing to replace unrecognized directory {target}"
            ) from exc
        if manifest.get("name") != MOD_NAME:
            raise RuntimeError(f"refusing to replace non-AIRI mod directory {target}")

    staging = Path(tempfile.mkdtemp(prefix=f".{MOD_NAME}-", dir=str(mods_dir)))
    try:
        staged_target = staging / target.name
        previous_target = staging / "previous"
        shutil.copytree(source, staged_target)
        if target.exists():
            target.replace(previous_target)
        try:
            staged_target.replace(target)
        except Exception:
            if previous_target.exists() and not target.exists():
                previous_target.replace(target)
            raise
    finally:
        shutil.rmtree(staging, ignore_errors=True)
    _enable_mod(mods_dir)
    return target


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Install AIRI Companion and launch a single Factorio client"
    )
    parser.add_argument("--factorio", help="Path to factorio.exe")
    parser.add_argument("--save", help="Optional save ZIP to load directly")
    parser.add_argument("--game-udp-port", type=_udp_port, default=31500)
    parser.add_argument("--bridge-port", type=_udp_port, default=31501)
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
        "--web-search",
        action=argparse.BooleanOptionalAction,
        default=_environment_flag("AIRI_FACTORIO_WEB_SEARCH", True),
        help="Expose native web_search when the Responses provider supports it",
    )
    parser.add_argument("--install-only", action="store_true")
    parser.add_argument("--no-install", action="store_true")
    parser.add_argument("factorio_args", nargs=argparse.REMAINDER)
    return parser


def _bridge_command(
    bridge_port: int,
    game_udp_port: int,
    *,
    provider_config: str = "",
    api_mode: str = "chat-completions",
    web_search: bool = True,
) -> list[str]:
    command = [
        sys.executable,
        "-m",
        "fle.companion.bridge",
        "--listen-port",
        str(bridge_port),
        "--factorio-port",
        str(game_udp_port),
        "--api-mode",
        api_mode,
    ]
    if provider_config:
        command.extend(["--provider-config", provider_config])
    command.append("--web-search" if web_search else "--no-web-search")
    return command


def launch(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if not args.install_only and args.game_udp_port == args.bridge_port:
        parser.error("--game-udp-port and --bridge-port must be different")
    if not args.no_install:
        installed = install_mod()
        print(f"Installed AIRI Companion at {installed}")
    if args.install_only:
        return 0

    factorio = discover_factorio_executable(args.factorio)
    bridge_creation_flags = 0
    if os.name == "nt":
        bridge_creation_flags = subprocess.CREATE_NO_WINDOW
    bridge = subprocess.Popen(
        _bridge_command(
            args.bridge_port,
            args.game_udp_port,
            provider_config=args.provider_config,
            api_mode=args.api_mode,
            web_search=args.web_search,
        ),
        creationflags=bridge_creation_flags,
    )
    try:
        bridge.wait(timeout=0.25)
    except subprocess.TimeoutExpired:
        pass
    else:
        raise RuntimeError(
            f"AIRI AgentBridge exited during startup with code {bridge.returncode}"
        )

    factorio_command = [
        str(factorio),
        "--enable-lua-udp",
        str(args.game_udp_port),
    ]
    if args.save:
        factorio_command.extend(["--load-game", str(Path(args.save).resolve())])
    factorio_command.extend(args.factorio_args)

    try:
        game = subprocess.Popen(factorio_command)
        return game.wait()
    finally:
        bridge.terminate()
        try:
            bridge.wait(timeout=5)
        except subprocess.TimeoutExpired:
            bridge.kill()


def main(argv: Sequence[str] | None = None) -> int:
    return launch(argv)


if __name__ == "__main__":
    raise SystemExit(main())
