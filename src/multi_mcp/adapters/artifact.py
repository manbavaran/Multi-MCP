"""
Artifact Adapter — Multi-MCP

Saves and retrieves result artifacts (reports, outputs, etc.).
Enforces artifact_root, file size limits, and extension allowlist.

Tools exposed:
  - artifact_save(name, content, extension=".txt", metadata=None)
  - artifact_list(run_id=None)
  - artifact_read(name)
"""

from __future__ import annotations

import json
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from multi_mcp.models.config import ArtifactPolicy, ToolCallRequest


EXPOSED_TOOLS = ["artifact_save", "artifact_list", "artifact_read"]


class ArtifactAdapter:
    """
    Stores artifacts in a controlled directory with metadata.
    """

    def __init__(self, policy: ArtifactPolicy) -> None:
        self.policy = policy
        self._root = Path(policy.artifact_root).resolve()
        self._root.mkdir(parents=True, exist_ok=True)

    def list_tools(self) -> list[str]:
        return EXPOSED_TOOLS

    async def call(self, request: ToolCallRequest) -> dict[str, Any]:
        tool = request.tool_name
        args = request.args

        if tool == "artifact_save":
            return self._save(
                name=args["name"],
                content=args["content"],
                extension=args.get("extension", ".txt"),
                metadata=args.get("metadata"),
                run_id=args.get("run_id"),
                profile=args.get("profile"),
                env=args.get("env"),
            )
        elif tool == "artifact_list":
            return self._list(run_id=args.get("run_id"))
        elif tool == "artifact_read":
            return self._read(name=args["name"])
        else:
            raise ValueError(f"Unknown tool: {tool}")

    def _save(
        self,
        name: str,
        content: str,
        extension: str = ".txt",
        metadata: dict | None = None,
        run_id: str | None = None,
        profile: str | None = None,
        env: str | None = None,
    ) -> dict[str, Any]:
        # Validate extension
        if extension not in self.policy.allowed_extensions:
            return {"error": f"Extension '{extension}' not allowed. Allowed: {self.policy.allowed_extensions}"}

        # Sanitise name
        safe_name = "".join(c for c in name if c.isalnum() or c in "-_.")
        filename = f"{safe_name}{extension}"
        filepath = self._root / filename

        # Check size
        encoded = content.encode("utf-8")
        max_bytes = self.policy.max_file_size_mb * 1024 * 1024
        if len(encoded) > max_bytes:
            return {"error": f"Content exceeds max size ({self.policy.max_file_size_mb} MB)"}

        filepath.write_bytes(encoded)

        # Write metadata sidecar
        meta = {
            "name": safe_name,
            "filename": filename,
            "extension": extension,
            "size_bytes": len(encoded),
            "saved_at": datetime.now(timezone.utc).isoformat(),
            "run_id": run_id,
            "profile": profile,
            "env": env,
            **(metadata or {}),
        }
        meta_path = filepath.with_suffix(filepath.suffix + ".meta.json")
        meta_path.write_text(json.dumps(meta, indent=2), encoding="utf-8")

        return {"saved": True, "path": str(filepath), "size_bytes": len(encoded)}

    def _list(self, run_id: str | None = None) -> dict[str, Any]:
        artifacts = []
        for meta_file in sorted(self._root.glob("*.meta.json")):
            try:
                meta = json.loads(meta_file.read_text())
                if run_id and meta.get("run_id") != run_id:
                    continue
                artifacts.append(meta)
            except Exception:  # noqa: BLE001
                pass
        return {"artifacts": artifacts, "count": len(artifacts)}

    def _read(self, name: str) -> dict[str, Any]:
        safe_name = "".join(c for c in name if c.isalnum() or c in "-_.")
        # Try to find the file with any allowed extension
        for ext in self.policy.allowed_extensions:
            filepath = self._root / f"{safe_name}{ext}"
            if filepath.exists():
                content = filepath.read_text(encoding="utf-8", errors="replace")
                return {"name": safe_name, "content": content, "path": str(filepath)}
        return {"error": f"Artifact '{name}' not found"}
