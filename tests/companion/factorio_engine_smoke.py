"""Run the AIRI action harness in a real headless Factorio benchmark.

This test is intentionally not auto-discovered. It requires a licensed local
Factorio 2.0 binary and copies the supplied save into a temporary directory.
"""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import shutil
import subprocess
import tempfile


REPOSITORY_ROOT = Path(__file__).resolve().parents[2]
MOD_SOURCE = REPOSITORY_ROOT / "fle" / "companion" / "factorio_mod"
HARNESS_SOURCE = Path(__file__).with_name("factorio_harness_mod")
MOD_LIST_FIXTURE = Path(__file__).with_name("fixtures") / "engine-mod-list.json"
PASS_MARKER = (
    "[airi-companion-smoke] PASS: chat UI/hotkey, teammate identity, "
    "resource overview/query including water patches, upstream layout/connection, "
    "live prototype wiki, movement, crafting, placement, mining, entity correction, "
    "inventory transfer, and automated coal output"
)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--factorio", type=Path, required=True)
    parser.add_argument("--save", type=Path, required=True)
    parser.add_argument("--benchmark-ticks", type=int, default=1200)
    parser.add_argument("--game-udp-port", type=int, default=31500)
    parser.add_argument("--timeout", type=float, default=90)
    return parser


def main() -> int:
    args = build_parser().parse_args()
    factorio = args.factorio.expanduser().resolve()
    save = args.save.expanduser().resolve()
    if not factorio.is_file():
        raise FileNotFoundError(f"Factorio executable not found: {factorio}")
    if not save.is_file():
        raise FileNotFoundError(f"Factorio save not found: {save}")
    if args.benchmark_ticks < 900:
        raise ValueError("--benchmark-ticks must be at least 900")
    if args.game_udp_port < 1024 or args.game_udp_port > 65535:
        raise ValueError("--game-udp-port must be between 1024 and 65535")

    with tempfile.TemporaryDirectory(prefix="airi-factorio-engine-smoke-") as temporary:
        root = Path(temporary)
        mods = root / "mods"
        mods.mkdir()
        shutil.copy2(MOD_LIST_FIXTURE, mods / "mod-list.json")
        shutil.copytree(MOD_SOURCE, mods / "airi-companion_0.1.0")
        shutil.copytree(HARNESS_SOURCE, mods / "airi-companion-smoke_0.1.0")
        save_copy = root / "save-copy.zip"
        shutil.copy2(save, save_copy)

        command = [
            str(factorio),
            "--mod-directory",
            str(mods),
            "--enable-lua-udp",
            str(args.game_udp_port),
            "--benchmark",
            str(save_copy),
            "--benchmark-ticks",
            str(args.benchmark_ticks),
            "--benchmark-runs",
            "1",
        ]
        creation_flags = subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0
        completed = subprocess.run(
            command,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=args.timeout,
            creationflags=creation_flags,
            check=False,
        )

    passed = completed.returncode == 0 and PASS_MARKER in completed.stdout
    if passed:
        result_lines = [
            line.strip()
            for line in completed.stdout.splitlines()
            if PASS_MARKER in line or "Performed " in line or "checksum:" in line
        ]
        print(json.dumps({"ok": True, "evidence": result_lines}, ensure_ascii=False))
        return 0

    print(f"Factorio engine smoke failed with exit code {completed.returncode}")
    print("\n".join(completed.stdout.splitlines()[-80:]))
    if completed.stderr:
        print(completed.stderr)
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
