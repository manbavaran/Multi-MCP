"""
Exec Adapter — Multi-MCP

Executes local shell commands with strict policy enforcement.
All enforcement (cwd, timeout, output cap, concurrency, denylist) is applied
by EnforcementMiddleware BEFORE this adapter is called.

This adapter additionally enforces:
  - timeout via asyncio.wait_for
  - output truncation (belt-and-suspenders, middleware also caps)
  - concurrency via an asyncio.Semaphore

Tools exposed:
  - exec_command(command, cwd=None, env=None) → run a shell command
  - exec_script(script, interpreter="bash")   → run a multi-line script
"""

from __future__ import annotations

import asyncio
import os
from typing import Any

from multi_mcp.models.config import ExecPolicy, ToolCallRequest


EXPOSED_TOOLS = ["exec_command", "exec_script"]


class ExecAdapter:
    """
    Async exec adapter with semaphore-based concurrency control.
    """

    def __init__(self, policy: ExecPolicy) -> None:
        self.policy = policy
        self._semaphore = asyncio.Semaphore(policy.max_concurrency)

    def list_tools(self) -> list[str]:
        return EXPOSED_TOOLS

    async def call(self, request: ToolCallRequest) -> dict[str, Any]:
        tool = request.tool_name
        args = request.args

        if tool == "exec_command":
            return await self._exec(
                command=args["command"],
                cwd=args.get("cwd", self.policy.allowed_cwd),
                env_extra=args.get("env"),
            )
        elif tool == "exec_script":
            interpreter = args.get("interpreter", "bash")
            script = args["script"]
            return await self._exec(
                command=f"{interpreter} -c {_shell_quote(script)}",
                cwd=args.get("cwd", self.policy.allowed_cwd),
            )
        else:
            raise ValueError(f"Unknown tool: {tool}")

    async def _exec(
        self,
        command: str,
        cwd: str | None = None,
        env_extra: dict[str, str] | None = None,
    ) -> dict[str, Any]:
        effective_cwd = cwd or self.policy.allowed_cwd
        env = {**os.environ}
        if env_extra:
            env.update(env_extra)

        async with self._semaphore:
            try:
                proc = await asyncio.create_subprocess_shell(
                    command,
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.PIPE,
                    cwd=effective_cwd,
                    env=env,
                )
                try:
                    stdout_bytes, stderr_bytes = await asyncio.wait_for(
                        proc.communicate(),
                        timeout=self.policy.timeout_sec,
                    )
                except asyncio.TimeoutError:
                    proc.kill()
                    await proc.communicate()
                    return {
                        "exit_code": -1,
                        "stdout": "",
                        "stderr": "",
                        "error": f"Command timed out after {self.policy.timeout_sec}s",
                        "timed_out": True,
                    }

                max_bytes = self.policy.max_stdout_kb * 1024
                stdout = _truncate(stdout_bytes.decode(errors="replace"), max_bytes)
                stderr = _truncate(stderr_bytes.decode(errors="replace"), max_bytes)

                return {
                    "exit_code": proc.returncode,
                    "stdout": stdout,
                    "stderr": stderr,
                    "cwd": effective_cwd,
                }
            except Exception as exc:  # noqa: BLE001
                return {"exit_code": -1, "stdout": "", "stderr": "", "error": str(exc)}


def _truncate(text: str, max_bytes: int) -> str:
    encoded = text.encode()
    if len(encoded) <= max_bytes:
        return text
    return encoded[:max_bytes].decode(errors="replace") + "\n[TRUNCATED]"


def _shell_quote(s: str) -> str:
    """Minimal shell quoting for passing a script to bash -c."""
    return "'" + s.replace("'", "'\\''") + "'"
