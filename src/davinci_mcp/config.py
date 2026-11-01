# Copyright Delanoe Pirard / Aedelon. Apache 2.0.
"""Settings loaded from ``config.toml`` and overridden by ``DAVINCI_MCP_*`` env vars.

Env var format: ``DAVINCI_MCP_<SECTION>__<KEY>``, e.g. ``DAVINCI_MCP_CONNECTION__AUTO_LAUNCH=0``.
"""

from __future__ import annotations

import os
import tomllib
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

ENV_PREFIX = "DAVINCI_MCP_"


def config_home() -> Path:
    return Path(os.environ.get("XDG_CONFIG_HOME", "~/.config")).expanduser() / "davinci-mcp"


def state_home() -> Path:
    return Path(os.environ.get("XDG_STATE_HOME", "~/.local/state")).expanduser() / "davinci-mcp"


class _Section(BaseModel):
    model_config = ConfigDict(extra="forbid")


class ConnectionSettings(_Section):
    auto_launch: bool = True
    headless: bool = False
    call_timeout_s: float = Field(default=30.0, gt=0)
    launch_timeout_s: float = Field(default=90.0, gt=0)
    script_api: Path | None = None
    script_lib: Path | None = None


class SafetySettings(_Section):
    allowed_roots: list[Path] = Field(default_factory=lambda: [Path.home()])
    snapshot_dir: Path = Field(default_factory=lambda: state_home() / "snapshots")
    snapshot_retention: int = Field(default=20, ge=0)
    consent_ttl_s: float = Field(default=300.0, gt=0)
    enable_code_execution: bool = False
    auto_snapshot: Literal["destructive", "off", "all"] = Field(
        default="destructive",
        description="When actions that declare a snapshot take it without being asked: "
        "destructive (default) = only destructive actions; all; off. "
        "A call can always force it with snapshot=true/false.",
    )

    @field_validator("allowed_roots", mode="after")
    @classmethod
    def _expand_roots(cls, roots: list[Path]) -> list[Path]:
        return [r.expanduser().resolve() for r in roots]

    @field_validator("snapshot_dir", mode="after")
    @classmethod
    def _expand_dir(cls, p: Path) -> Path:
        return p.expanduser()


class HttpSettings(_Section):
    host: str = "127.0.0.1"
    port: int = Field(default=8765, ge=1, le=65535)
    token_env: str = "DAVINCI_MCP_TOKEN"  # noqa: S105 - env var name, not a secret


class ServerSettings(_Section):
    schema_mode: Literal["compact", "full"] = Field(
        default="compact",
        description="compact: action enum + one-line signatures, details via describe_action. "
        "full: complete per-action JSON schemas in tools/list (much larger).",
    )


class RecipeSettings(_Section):
    dirs: list[Path] = Field(default_factory=lambda: [config_home() / "recipes"])
    allow_python: bool = Field(
        default=False,
        description="Load *.py recipes from the recipe dirs (arbitrary code: off by default).",
    )


class Settings(_Section):
    connection: ConnectionSettings = Field(default_factory=ConnectionSettings)
    safety: SafetySettings = Field(default_factory=SafetySettings)
    http: HttpSettings = Field(default_factory=HttpSettings)
    server: ServerSettings = Field(default_factory=ServerSettings)
    recipes: RecipeSettings = Field(default_factory=RecipeSettings)
    state_dir: Path = Field(default_factory=state_home)
    config_file: Path | None = Field(default=None, description="Set by load_settings.")

    def protected_paths(self) -> list[Path]:
        """Locations tools must never read or write (see safety.paths.check_path)."""
        extra = [self.state_dir, self.safety.snapshot_dir, *self.recipes.dirs]
        if self.config_file is not None:
            extra.append(self.config_file)
        return extra


def _env_overrides(environ: dict[str, str]) -> dict[str, dict[str, str]]:
    overrides: dict[str, dict[str, str]] = {}
    for key, value in environ.items():
        if not key.startswith(ENV_PREFIX) or "__" not in key:
            continue
        section, _, field = key.removeprefix(ENV_PREFIX).lower().partition("__")
        overrides.setdefault(section, {})[field] = value
    return overrides


def _parse_env_value(raw: str) -> bool | str | list[str]:
    lowered = raw.strip().lower()
    if lowered in {"1", "true", "yes", "on"}:
        return True
    if lowered in {"0", "false", "no", "off"}:
        return False
    if raw.startswith("["):
        return [s.strip() for s in raw.strip("[]").split(",") if s.strip()]
    return raw


def load_settings(path: Path | None = None, environ: dict[str, str] | None = None) -> Settings:
    """Load settings: defaults < TOML file < environment."""
    env = dict(os.environ if environ is None else environ)
    config_path = path or Path(env.get(f"{ENV_PREFIX}CONFIG", config_home() / "config.toml"))
    data: dict[str, Any] = {}
    if config_path.exists():
        data = tomllib.loads(config_path.read_text(encoding="utf-8"))
    for section, fields in _env_overrides(env).items():
        target: dict[str, Any] = data.setdefault(section, {})
        for field, raw in fields.items():
            target[field] = _parse_env_value(raw)
    data["config_file"] = str(config_path)
    return Settings.model_validate(data)
