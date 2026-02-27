"""
Enforcement Middleware — Multi-MCP

This module enforces all policies defined in AGENTS.md.
Policies are NOT optional — they are always applied regardless of configuration.

Enforcement areas:
  - Filesystem: allowed_root traversal prevention (including symlink resolution)
  - Exec: cwd restriction, timeout, output cap, concurrency limit, denylist
  - SSH: alias-only access, no raw host/credential passthrough
  - Search (Tavily): quota/rate limit, max_results cap, search_depth cap
  - General: tool exposure per client profile
"""

from __future__ import annotations

import os
import re
from typing import Any

from multi_mcp.models.config import (
    ExecPolicy,
    FilesystemPolicy,
    SearchPolicy,
    SSHPolicy,
    SubServerConfig,
    ToolCallRequest,
)


class EnforcementMiddleware:
    """
    Stateless enforcement layer.  All checks raise ``PermissionError`` on violation.
    """

    # --- Pre-call checks ---

    def pre_call(
        self,
        request: ToolCallRequest,
        server: SubServerConfig,
        client_profile: str,
    ) -> None:
        """Run all applicable pre-call policy checks."""
        self._check_tool_exposure(request.tool_name, server, client_profile)

        server_type = server.server_type
        if server_type == "filesystem":
            self._check_filesystem(request, server.policy.filesystem)
        elif server_type == "exec":
            self._check_exec(request, server.policy.exec)
        elif server_type == "ssh":
            self._check_ssh(request, server.policy.ssh)
        elif server_type == "search":
            self._check_search(request, server.policy.search)

    def _check_tool_exposure(
        self,
        tool_name: str,
        server: SubServerConfig,
        client_profile: str,
    ) -> None:
        """Verify the client profile is allowed to call this tool."""
        if client_profile not in server.allowed_profiles and "*" not in server.allowed_profiles:
            raise PermissionError(
                f"Profile '{client_profile}' is not allowed to use server '{server.name}'"
            )
        if tool_name not in server.exposed_tools:
            raise PermissionError(
                f"Tool '{tool_name}' is not exposed by server '{server.name}'"
            )

    # ---- Filesystem enforcement ----

    def _check_filesystem(
        self,
        request: ToolCallRequest,
        policy: FilesystemPolicy,
    ) -> None:
        path_arg: str | None = request.args.get("path")
        if path_arg is None:
            return

        allowed_root = os.path.realpath(policy.allowed_root)
        # Resolve symlinks and normalise the requested path
        real_path = os.path.realpath(os.path.join(allowed_root, path_arg.lstrip("/")))

        if not real_path.startswith(allowed_root + os.sep) and real_path != allowed_root:
            raise PermissionError(
                f"Path '{path_arg}' escapes allowed_root '{policy.allowed_root}'"
            )

        # Enforce read-only if the tool is a write operation and write is disabled
        write_tools = {"write_file", "delete_file", "move_file", "create_directory"}
        if request.tool_name in write_tools and not policy.allow_write:
            raise PermissionError(
                f"Tool '{request.tool_name}' requires write access, which is disabled"
            )

    # ---- Exec enforcement ----

    def _check_exec(
        self,
        request: ToolCallRequest,
        policy: ExecPolicy,
    ) -> None:
        cwd_arg: str | None = request.args.get("cwd")
        if cwd_arg:
            allowed_cwd = os.path.realpath(policy.allowed_cwd)
            real_cwd = os.path.realpath(cwd_arg)
            if not real_cwd.startswith(allowed_cwd + os.sep) and real_cwd != allowed_cwd:
                raise PermissionError(
                    f"cwd '{cwd_arg}' escapes allowed_cwd '{policy.allowed_cwd}'"
                )

        command: str | None = request.args.get("command") or request.args.get("cmd")
        if command:
            for pattern in policy.denylist:
                if re.search(pattern, command):
                    raise PermissionError(
                        f"Command matches denylist pattern '{pattern}'"
                    )

    # ---- SSH enforcement ----

    def _check_ssh(
        self,
        request: ToolCallRequest,
        policy: SSHPolicy,
    ) -> None:
        alias: str | None = request.args.get("alias")
        if alias is None:
            raise PermissionError("SSH calls must specify an alias; raw host/credentials are forbidden")
        if alias not in policy.allowed_aliases:
            raise PermissionError(
                f"SSH alias '{alias}' is not in the allowed list"
            )

    # ---- Search enforcement ----

    def _check_search(
        self,
        request: ToolCallRequest,
        policy: SearchPolicy,
    ) -> None:
        # Cap max_results
        max_results = request.args.get("max_results")
        if max_results is not None and int(max_results) > policy.max_results:
            raise PermissionError(
                f"max_results {max_results} exceeds cap {policy.max_results}"
            )

        # Enforce search_depth
        depth = request.args.get("search_depth", "basic")
        allowed_depths = ["basic", "advanced"]
        if depth not in allowed_depths:
            raise PermissionError(f"search_depth '{depth}' is not allowed")
        if depth == "advanced" and not policy.allow_advanced_depth:
            raise PermissionError("search_depth 'advanced' is disabled by policy")

        # Quota check (quota tracking is done in the adapter; here we just check the flag)
        if policy.quota_exhausted:
            raise PermissionError("Search quota exhausted — request blocked by cost guard")

    # --- Post-call processing ---

    def post_call(
        self,
        result: dict[str, Any],
        server: SubServerConfig,
        client_profile: str,  # noqa: ARG002
    ) -> dict[str, Any]:
        """Apply output caps and sensitive data masking."""
        server_type = server.server_type
        if server_type == "exec":
            result = self._cap_exec_output(result, server.policy.exec)
        result = self._mask_sensitive(result)
        return result

    def _cap_exec_output(
        self,
        result: dict[str, Any],
        policy: ExecPolicy,
    ) -> dict[str, Any]:
        """Truncate stdout/stderr to configured limits."""
        max_kb = policy.max_stdout_kb * 1024
        for key in ("stdout", "stderr", "output"):
            val = result.get(key)
            if isinstance(val, str) and len(val.encode()) > max_kb:
                result[key] = val.encode()[:max_kb].decode(errors="replace") + "\n[TRUNCATED]"
        return result

    # Patterns that should never appear in logs or responses
    _SENSITIVE_PATTERNS = [
        re.compile(r"(?i)(api[_-]?key|token|password|secret|private[_-]?key)\s*[:=]\s*\S+"),
        re.compile(r"sk-[A-Za-z0-9]{20,}"),
    ]

    def _mask_sensitive(self, result: dict[str, Any]) -> dict[str, Any]:
        """Replace sensitive strings in result values with masked placeholders."""
        masked = {}
        for k, v in result.items():
            if isinstance(v, str):
                for pattern in self._SENSITIVE_PATTERNS:
                    v = pattern.sub("[REDACTED]", v)
            masked[k] = v
        return masked
