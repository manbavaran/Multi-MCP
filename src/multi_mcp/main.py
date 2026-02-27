"""
Multi-MCP Hub — Entry Point

Starts the FastAPI application that serves:
  - /        → GUI management console (HTML)
  - /api/... → REST API for the GUI
  - /mcp/... → MCP tool-call endpoint (for clients like LangGraph)
  - /health  → Health check
"""

from __future__ import annotations

from pathlib import Path

from fastapi import FastAPI
from fastapi.responses import HTMLResponse
from fastapi.staticfiles import StaticFiles

from multi_mcp.gui.api import router as gui_router
from multi_mcp.gui.mcp_endpoint import router as mcp_router

app = FastAPI(
    title="Multi-MCP Hub",
    description="A gateway for managing and enforcing policies on multiple MCP sub-servers.",
    version="0.1.0",
)

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
