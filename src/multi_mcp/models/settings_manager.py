"""
Settings Manager — Multi-MCP

Handles loading and saving EnvironmentConfig objects to/from disk.
Secrets are stored separately in SecretStore; this file only persists non-secret config.

Storage layout:
  config/
    dev.json
    stage.json
    prod.json
"""

from __future__ import annotations

import json
from pathlib import Path

from multi_mcp.models.config import Environment, EnvironmentConfig


_DEFAULT_CONFIG_DIR = Path("config")


class SettingsManager:
    """
    Load/save EnvironmentConfig from/to JSON files.

    Secrets are NOT included in the serialised output — only alias references.
    """

    def __init__(self, config_dir: Path | str = _DEFAULT_CONFIG_DIR) -> None:
        self._dir = Path(config_dir)
        self._dir.mkdir(parents=True, exist_ok=True)

    def _path(self, env: Environment) -> Path:
        return self._dir / f"{env.value}.json"

    def save(self, config: EnvironmentConfig) -> None:
        """Serialise the config to JSON (secrets excluded)."""
        data = config.model_dump(exclude={"sub_servers": {"__all__": {"adapter"}}})
        path = self._path(config.name)
        path.write_text(json.dumps(data, indent=2, default=str), encoding="utf-8")

    def load(self, env: Environment) -> EnvironmentConfig | None:
        """Load an EnvironmentConfig from disk, or return None if not found."""
        path = self._path(env)
        if not path.exists():
            return None
        data = json.loads(path.read_text(encoding="utf-8"))
        return EnvironmentConfig.model_validate(data)

    def list_environments(self) -> list[Environment]:
        """Return all environments that have a saved config file."""
        envs = []
        for env in Environment:
            if self._path(env).exists():
                envs.append(env)
        return envs

    def get_or_create_default(self, env: Environment) -> EnvironmentConfig:
        """Return the saved config or a fresh default."""
        existing = self.load(env)
        if existing:
            return existing
        default = EnvironmentConfig(name=env)
        self.save(default)
        return default
