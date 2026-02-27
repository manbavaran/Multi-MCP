"""
Hub Factory — Multi-MCP

Creates and wires together all hub components:
  - SubServerRegistry (with adapters attached to each sub-server)
  - EnforcementMiddleware
  - AuditLogger + ExecutionLogger
  - MCPHub

Usage::

    hub = HubFactory.create(env_config, secret_store)
    response = await hub.call_tool(request, client_profile="Researcher")
"""

from __future__ import annotations

from multi_mcp.adapters.artifact import ArtifactAdapter
from multi_mcp.adapters.exec import ExecAdapter
from multi_mcp.adapters.filesystem import FilesystemAdapter
from multi_mcp.adapters.logs import LogsAdapter
from multi_mcp.adapters.search import SearchAdapter
from multi_mcp.adapters.ssh import SSHAdapter
from multi_mcp.enforcement.middleware import EnforcementMiddleware
from multi_mcp.hub.router import MCPHub, SubServerRegistry
from multi_mcp.logging.audit import AuditLogger
from multi_mcp.logging.execution import ExecutionLogger
from multi_mcp.models.config import EnvironmentConfig, ServerType, SubServerConfig
from multi_mcp.models.secrets import SecretStore


class HubFactory:
    """Builds a fully wired MCPHub from an EnvironmentConfig."""

    @staticmethod
    def create(
        env_config: EnvironmentConfig,
        secret_store: SecretStore,
        audit_log_dir: str = "logs/audit",
        exec_log_dir: str = "logs/execution",
    ) -> MCPHub:
        registry = SubServerRegistry()

        for server_cfg in env_config.sub_servers:
            if not server_cfg.enabled:
                continue
            adapter = HubFactory._build_adapter(server_cfg, env_config, secret_store)
            if adapter is not None:
                server_cfg.adapter = adapter
            registry.register(server_cfg)

        return MCPHub(
            registry=registry,
            enforcement=EnforcementMiddleware(),
            audit_logger=AuditLogger(log_dir=audit_log_dir),
            exec_logger=ExecutionLogger(log_dir=exec_log_dir),
        )

    @staticmethod
    def _build_adapter(
        server_cfg: SubServerConfig,
        env_config: EnvironmentConfig,
        secret_store: SecretStore,
    ):
        policy = server_cfg.policy
        stype = server_cfg.server_type

        if stype == ServerType.filesystem:
            return FilesystemAdapter(policy.filesystem)
        elif stype == ServerType.exec:
            return ExecAdapter(policy.exec)
        elif stype == ServerType.ssh:
            return SSHAdapter(policy.ssh, secret_store, env_config.ssh_aliases)
        elif stype == ServerType.search:
            return SearchAdapter(policy.search, secret_store)
        elif stype == ServerType.logs:
            return LogsAdapter(policy.logs)
        elif stype == ServerType.artifact:
            return ArtifactAdapter(policy.artifact)
        else:
            # For future types (github, rag), return None — not yet implemented
            return None
