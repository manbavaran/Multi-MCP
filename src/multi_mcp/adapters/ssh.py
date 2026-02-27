"""
SSH Adapter — Multi-MCP

Executes commands on remote servers via SSH.
CRITICAL: Clients MUST use alias names only. Raw host/credentials are forbidden.
The actual connection details are retrieved from SecretStore using the alias.

Tools exposed:
  - ssh_run(alias, cmd)          → run a command on the remote host (read or act)
  - ssh_read(alias, cmd)         → read-only commands only (enforced by policy)

Preferred strategy (AGENTS.md §5.3):
  Use a community SSH MCP server if available.
  This adapter is the Python fallback using asyncssh or paramiko.
"""

from __future__ import annotations

from typing import Any

from multi_mcp.models.config import SSHPolicy, ToolCallRequest
from multi_mcp.models.secrets import SecretStore


EXPOSED_TOOLS = ["ssh_run", "ssh_read"]


class SSHAdapter:
    """
    SSH adapter that resolves alias → credentials from SecretStore.
    """

    def __init__(
        self,
        policy: SSHPolicy,
        secret_store: SecretStore,
        ssh_aliases: list[Any],  # list[SSHAlias]
    ) -> None:
        self.policy = policy
        self._secrets = secret_store
        self._aliases = {a.alias: a for a in ssh_aliases}

    def list_tools(self) -> list[str]:
        tools = ["ssh_read"]
        if self.policy.allow_act:
            tools.append("ssh_run")
        return tools

    async def call(self, request: ToolCallRequest) -> dict[str, Any]:
        tool = request.tool_name
        args = request.args
        alias_name: str = args["alias"]
        cmd: str = args["cmd"]

        if alias_name not in self._aliases:
            return {"error": f"SSH alias '{alias_name}' not found"}

        alias = self._aliases[alias_name]
        secret = self._secrets.get(alias.secret_ref)
        if not secret:
            return {"error": f"No credential found for alias '{alias_name}'"}
        if secret.startswith("DISABLED:"):
            return {"error": f"SSH alias '{alias_name}' is disabled"}

        if tool == "ssh_read" and not self._is_read_only(cmd):
            return {"error": "ssh_read only allows read-only commands"}

        return await self._execute(alias, secret, cmd)

    @staticmethod
    def _is_read_only(cmd: str) -> bool:
        """Heuristic check: only allow commands that don't mutate state."""
        read_only_prefixes = ("ls", "cat", "head", "tail", "grep", "find", "ps", "df", "du", "uptime", "whoami", "id", "pwd", "echo", "env", "printenv")
        stripped = cmd.strip().split()[0] if cmd.strip() else ""
        return stripped in read_only_prefixes

    async def _execute(self, alias: Any, secret: str, cmd: str) -> dict[str, Any]:
        """Execute a command on the remote host using asyncssh."""
        try:
            import asyncssh  # type: ignore[import]
        except ImportError:
            return {"error": "asyncssh not installed. Run: pip install asyncssh"}

        connect_kwargs: dict[str, Any] = {
            "host": alias.host,
            "port": alias.port,
            "username": alias.username,
            "known_hosts": None,  # TODO: enforce known_hosts in production
        }
        if alias.auth_type == "key":
            connect_kwargs["client_keys"] = [asyncssh.import_private_key(secret)]
        else:
            connect_kwargs["password"] = secret

        async with asyncssh.connect(**connect_kwargs) as conn:
            result = await conn.run(cmd)
            return {
                "alias": alias.alias,
                "host": alias.host,
                "exit_code": result.exit_status,
                "stdout": result.stdout or "",
                "stderr": result.stderr or "",
            }
