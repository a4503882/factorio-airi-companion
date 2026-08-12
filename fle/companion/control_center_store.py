"""Persistent, non-secret research configuration for AIRI Control Center."""

from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import datetime
import hashlib
import json
import os
from pathlib import Path
import re
import tempfile
from typing import Any
import uuid

from .bridge import SYSTEM_PROMPT, load_provider_config
from .credentials import CredentialStore


SCHEMA_VERSION = 1
API_MODES = ("responses", "chat-completions")
REASONING_EFFORTS = ("low", "high", "max")
_DANGO_PROMPT_NAME = "团子·异星工厂陪玩"
_DANGO_PROMPT_PATH = Path(__file__).with_name("prompts") / "dango-factorio-zh.txt"
_LEGACY_DANGO_PROMPT_HASHES = frozenset(
    {
        # Known bundled predecessors. Only exact shipped presets are migrated;
        # edited user prompts are never overwritten by name alone.
        # Pre-policy harness: every reply could call only one Factorio tool.
        "a411a61238ecc2c6b8b133e676567c0fe1ebaf0d256f1ea40a838ceda40ddf08",
        # Initial live-Wiki/task-skill prompt, before provider function-call
        # recovery explicitly distinguished namespace functions from API tools.
        "51e24b2d4502a102ff1f3cd14f3033f6936faef43a1ea3ac116752f34efe6fb6",
    }
)


def default_data_dir() -> Path:
    override = os.getenv("AIRI_FACTORIO_DATA_DIR")
    if override:
        return Path(override).expanduser()
    # Microsoft Store Python transparently redirects writes beneath LOCALAPPDATA
    # into its package LocalCache. A home-relative directory remains stable when
    # switching between Store Python, python.org, a venv, or a future packaged EXE.
    return Path.home() / ".airi-factorio"


def _now() -> str:
    return datetime.now().astimezone().isoformat(timespec="seconds")


def _atomic_write_text(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
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


def _write_json(path: Path, value: Any) -> None:
    _atomic_write_text(
        path,
        json.dumps(value, ensure_ascii=False, indent=2) + "\n",
    )


def _new_id(prefix: str) -> str:
    return f"{prefix}-{uuid.uuid4().hex[:12]}"


@dataclass(frozen=True)
class ProviderProfile:
    id: str
    name: str
    base_url: str
    model: str
    api_mode: str = "responses"
    web_search: bool = True
    reasoning_effort: str = "high"
    credential_id: str = ""
    created_at: str = ""
    updated_at: str = ""

    @classmethod
    def create(
        cls,
        *,
        name: str,
        base_url: str,
        model: str,
        api_mode: str = "responses",
        web_search: bool = True,
        reasoning_effort: str = "high",
    ) -> "ProviderProfile":
        profile_id = _new_id("provider")
        timestamp = _now()
        return cls(
            id=profile_id,
            name=name.strip(),
            base_url=base_url.rstrip("/"),
            model=model.strip(),
            api_mode=api_mode,
            web_search=web_search,
            reasoning_effort=reasoning_effort,
            credential_id=profile_id,
            created_at=timestamp,
            updated_at=timestamp,
        ).validated()

    def validated(self) -> "ProviderProfile":
        if not self.id or not re.fullmatch(r"[A-Za-z0-9_-]+", self.id):
            raise ValueError("provider profile id is invalid")
        if not self.name.strip():
            raise ValueError("provider profile name must not be blank")
        if not self.base_url.startswith(("http://", "https://")):
            raise ValueError("provider base URL must start with http:// or https://")
        if not self.model.strip():
            raise ValueError("provider model must not be blank")
        if self.api_mode not in API_MODES:
            raise ValueError(f"unsupported API mode: {self.api_mode}")
        if self.reasoning_effort not in REASONING_EFFORTS:
            raise ValueError(
                f"unsupported reasoning effort: {self.reasoning_effort}"
            )
        if not self.credential_id:
            raise ValueError("provider credential id must not be blank")
        return self

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> "ProviderProfile":
        return cls(**value).validated()


@dataclass(frozen=True)
class PromptProfile:
    id: str
    name: str
    system_prompt: str
    created_at: str
    updated_at: str

    @classmethod
    def create(cls, *, name: str, system_prompt: str) -> "PromptProfile":
        timestamp = _now()
        return cls(
            id=_new_id("prompt"),
            name=name.strip(),
            system_prompt=system_prompt.strip(),
            created_at=timestamp,
            updated_at=timestamp,
        ).validated()

    def validated(self) -> "PromptProfile":
        if not self.id or not re.fullmatch(r"[A-Za-z0-9_-]+", self.id):
            raise ValueError("prompt profile id is invalid")
        if not self.name.strip():
            raise ValueError("prompt profile name must not be blank")
        if not self.system_prompt.strip():
            raise ValueError("system prompt must not be blank")
        if len(self.system_prompt.encode("utf-8")) > 256_000:
            raise ValueError("system prompt exceeds the 256 KB research limit")
        return self

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> "PromptProfile":
        return cls(**value).validated()

    @property
    def sha256(self) -> str:
        return hashlib.sha256(self.system_prompt.encode("utf-8")).hexdigest()


@dataclass(frozen=True)
class SessionFiles:
    session_id: str
    directory: Path
    manifest: Path
    prompt_snapshot: Path
    event_log: Path
    bridge_log: Path
    status_file: Path


class ControlCenterStore:
    def __init__(self, root: str | Path | None = None) -> None:
        self.root = Path(root) if root is not None else default_data_dir()
        self.profiles_file = self.root / "profiles.json"
        self.settings_file = self.root / "settings.json"
        self.prompts_dir = self.root / "prompts"
        self.sessions_dir = self.root / "sessions"
        self.root.mkdir(parents=True, exist_ok=True)
        self.prompts_dir.mkdir(parents=True, exist_ok=True)
        self.sessions_dir.mkdir(parents=True, exist_ok=True)
        self.ensure_default_prompt()

    def _read_json(self, path: Path, default: Any) -> Any:
        if not path.exists():
            return default
        try:
            return json.loads(path.read_text(encoding="utf-8-sig"))
        except (OSError, json.JSONDecodeError) as exc:
            raise RuntimeError(f"could not read Control Center data {path}: {exc}") from exc

    def load_settings(self) -> dict[str, Any]:
        value = self._read_json(self.settings_file, {})
        if not isinstance(value, dict):
            raise RuntimeError("Control Center settings root must be an object")
        return value

    def save_settings(self, **changes: Any) -> dict[str, Any]:
        settings = self.load_settings()
        settings.update(changes)
        settings["schema_version"] = SCHEMA_VERSION
        _write_json(self.settings_file, settings)
        return settings

    def load_providers(self) -> list[ProviderProfile]:
        value = self._read_json(self.profiles_file, {"profiles": []})
        records = value.get("profiles", []) if isinstance(value, dict) else []
        if not isinstance(records, list):
            raise RuntimeError("provider profiles must be a list")
        return [ProviderProfile.from_dict(record) for record in records]

    def save_provider(self, profile: ProviderProfile) -> ProviderProfile:
        profile = profile.validated()
        profiles = self.load_providers()
        updated = False
        records: list[ProviderProfile] = []
        for existing in profiles:
            if existing.id == profile.id:
                profile = ProviderProfile(
                    **{
                        **asdict(profile),
                        "created_at": existing.created_at or profile.created_at,
                        "updated_at": _now(),
                    }
                ).validated()
                records.append(profile)
                updated = True
            else:
                records.append(existing)
        if not updated:
            records.append(profile)
        _write_json(
            self.profiles_file,
            {"schema_version": SCHEMA_VERSION, "profiles": [asdict(p) for p in records]},
        )
        self.save_settings(selected_provider_id=profile.id)
        return profile

    def delete_provider(
        self,
        profile_id: str,
        credentials: CredentialStore | None = None,
    ) -> bool:
        profiles = self.load_providers()
        target = next((profile for profile in profiles if profile.id == profile_id), None)
        if target is None:
            return False
        remaining = [profile for profile in profiles if profile.id != profile_id]
        _write_json(
            self.profiles_file,
            {"schema_version": SCHEMA_VERSION, "profiles": [asdict(p) for p in remaining]},
        )
        if credentials is not None:
            credentials.delete_secret(target.credential_id)
        settings = self.load_settings()
        if settings.get("selected_provider_id") == profile_id:
            self.save_settings(
                selected_provider_id=remaining[0].id if remaining else ""
            )
        return True

    def import_three_line_provider(
        self,
        path: str | Path,
        credentials: CredentialStore,
        *,
        name: str = "DeepSeek Responses",
        api_mode: str = "responses",
    ) -> ProviderProfile:
        imported = load_provider_config(path)
        profile = ProviderProfile.create(
            name=name,
            base_url=imported.base_url,
            model=imported.model,
            api_mode=api_mode,
            web_search=api_mode == "responses",
        )
        credentials.set_secret(profile.credential_id, imported.api_key)
        try:
            return self.save_provider(profile)
        except Exception:
            credentials.delete_secret(profile.credential_id)
            raise

    def ensure_default_prompt(self) -> PromptProfile:
        prompts = self.load_prompts()
        if prompts:
            prompts = self._migrate_known_bundled_prompts(prompts)
            return prompts[0]
        prompt = PromptProfile.create(
            name="AIRI 默认陪玩",
            system_prompt=SYSTEM_PROMPT,
        )
        return self.save_prompt(prompt)

    def _migrate_known_bundled_prompts(
        self, prompts: list[PromptProfile]
    ) -> list[PromptProfile]:
        bundled_text: str | None = None
        migrated: list[PromptProfile] = []
        for prompt in prompts:
            if (
                prompt.name != _DANGO_PROMPT_NAME
                or prompt.sha256 not in _LEGACY_DANGO_PROMPT_HASHES
            ):
                migrated.append(prompt)
                continue
            if bundled_text is None:
                try:
                    bundled_text = _DANGO_PROMPT_PATH.read_text(
                        encoding="utf-8-sig"
                    ).strip()
                except OSError as exc:
                    raise RuntimeError(
                        f"could not read bundled Dango prompt {_DANGO_PROMPT_PATH}: {exc}"
                    ) from exc
            replacement = PromptProfile(
                id=prompt.id,
                name=prompt.name,
                system_prompt=bundled_text,
                created_at=prompt.created_at,
                updated_at=_now(),
            ).validated()
            _write_json(self.prompts_dir / f"{replacement.id}.json", asdict(replacement))
            migrated.append(replacement)
        return migrated

    def load_prompts(self) -> list[PromptProfile]:
        prompts: list[PromptProfile] = []
        for path in sorted(self.prompts_dir.glob("*.json")):
            value = self._read_json(path, {})
            if not isinstance(value, dict):
                raise RuntimeError(f"prompt profile {path} must be an object")
            prompts.append(PromptProfile.from_dict(value))
        return prompts

    def save_prompt(self, prompt: PromptProfile) -> PromptProfile:
        prompt = prompt.validated()
        path = self.prompts_dir / f"{prompt.id}.json"
        if path.exists():
            existing = PromptProfile.from_dict(self._read_json(path, {}))
            prompt = PromptProfile(
                **{
                    **asdict(prompt),
                    "created_at": existing.created_at,
                    "updated_at": _now(),
                }
            ).validated()
        _write_json(path, asdict(prompt))
        self.save_settings(selected_prompt_id=prompt.id)
        return prompt

    def delete_prompt(self, prompt_id: str) -> bool:
        prompts = self.load_prompts()
        if len(prompts) <= 1:
            raise RuntimeError("at least one prompt profile must remain")
        path = self.prompts_dir / f"{prompt_id}.json"
        if not path.exists():
            return False
        path.unlink()
        remaining = [prompt for prompt in prompts if prompt.id != prompt_id]
        settings = self.load_settings()
        if settings.get("selected_prompt_id") == prompt_id:
            self.save_settings(selected_prompt_id=remaining[0].id)
        return True

    def create_session(
        self,
        *,
        provider: ProviderProfile,
        prompt: PromptProfile,
        factorio_executable: str,
        game_udp_port: int,
        bridge_port: int,
    ) -> SessionFiles:
        timestamp = datetime.now().astimezone()
        session_id = timestamp.strftime("airi-%Y%m%d-%H%M%S-%f")
        directory = self.sessions_dir / session_id
        directory.mkdir(parents=True, exist_ok=False)
        files = SessionFiles(
            session_id=session_id,
            directory=directory,
            manifest=directory / "manifest.json",
            prompt_snapshot=directory / "system-prompt.txt",
            event_log=directory / "events.jsonl",
            bridge_log=directory / "bridge.log",
            status_file=directory / "bridge-status.json",
        )
        _atomic_write_text(files.prompt_snapshot, prompt.system_prompt + "\n")
        manifest = {
            "schema_version": SCHEMA_VERSION,
            "session_id": session_id,
            "status": "created",
            "started_at": timestamp.isoformat(timespec="seconds"),
            "provider": {
                "id": provider.id,
                "name": provider.name,
                "base_url": provider.base_url,
                "model": provider.model,
                "api_mode": provider.api_mode,
                "web_search": provider.web_search,
                "reasoning_effort": provider.reasoning_effort,
                "credential_id": provider.credential_id,
            },
            "prompt": {
                "id": prompt.id,
                "name": prompt.name,
                "sha256": prompt.sha256,
                "snapshot": files.prompt_snapshot.name,
            },
            "factorio": {
                "executable": factorio_executable,
                "game_udp_port": game_udp_port,
                "bridge_port": bridge_port,
            },
            "artifacts": {
                "events": files.event_log.name,
                "bridge_log": files.bridge_log.name,
                "bridge_status": files.status_file.name,
            },
        }
        _write_json(files.manifest, manifest)
        return files

    def update_session(self, files: SessionFiles, **changes: Any) -> None:
        manifest = self._read_json(files.manifest, {})
        if not isinstance(manifest, dict):
            raise RuntimeError("session manifest must be an object")
        manifest.update(changes)
        _write_json(files.manifest, manifest)
