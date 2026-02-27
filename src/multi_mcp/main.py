"""
Multi-MCP Hub — Entry Point

Starts the FastAPI application that serves:
  - /        → GUI management console (HTML)
  - /api/... → REST API for the GUI
  - /mcp/... → MCP tool-call endpoint (for clients like LangGraph)
  - /health  → Health check
"""

from __future__ import annotations

import logging
from pathlib import Path

from fastapi import FastAPI
from fastapi.responses import HTMLResponse
from fastapi.staticfiles import StaticFiles

from multi_mcp.gui.api import router as gui_router
from multi_mcp.gui.mcp_endpoint import router as mcp_router
from multi_mcp.models.config import Environment
from multi_mcp.models.settings_manager import SettingsManager

logger = logging.getLogger(__name__)

app = FastAPI(
    title="Multi-MCP Hub",
    description="A gateway for managing and enforcing policies on multiple MCP sub-servers.",
    version="0.1.0",
)


@app.on_event("startup")
async def _bootstrap_on_startup() -> None:
    """
    On every startup, ensure all 6 core built-in servers are registered
    in every environment (dev / stage / prod).

    This is idempotent: existing servers are never overwritten.
    """
    manager = SettingsManager()
    for env in Environment:
        cfg = manager.ensure_bootstrapped(env)
        core_count = sum(1 for s in cfg.sub_servers if "core" in s.tags)
        logger.info(
            "[bootstrap] env=%s total_servers=%d core_servers=%d",
            env.value, len(cfg.sub_servers), core_count,
        )
    logger.info("[bootstrap] Core server bootstrap complete.")

# Mount GUI API
app.include_router(gui_router)

# Mount MCP tool-call endpoint
app.include_router(mcp_router)

# Serve static files (if any)
_static_dir = Path(__file__).parent / "gui" / "static"
if _static_dir.exists():
    app.mount("/static", StaticFiles(directory=str(_static_dir)), name="static")

# Serve the GUI HTML
_template_dir = Path(__file__).parent / "gui" / "templates"


@app.get("/", response_class=HTMLResponse)
def serve_gui() -> str:
    index = _template_dir / "index.html"
    return index.read_text(encoding="utf-8")


@app.get("/health")
def health() -> dict[str, str]:
    return {"status": "ok", "service": "multi-mcp-hub"}
