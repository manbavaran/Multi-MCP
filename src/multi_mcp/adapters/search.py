"""
Search Adapter (Tavily) — Multi-MCP

Wraps the Tavily Search API with quota tracking and cost guard.
The API key is NEVER passed by the client — it is retrieved from SecretStore
using the alias provided in the request.

Quota enforcement:
  - daily_request_cap: enforced by in-memory counter (reset at midnight UTC)
  - monthly_credit_budget: tracked via estimated cost per request

Tools exposed:
  - web_search(alias, query, max_results=5, search_depth="basic")

Preferred strategy (AGENTS.md §5.3):
  Use tavily-mcp or mcp-tavily package if available.
  This adapter is the Python fallback that calls the Tavily REST API directly.
"""

from __future__ import annotations

import asyncio
import json
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any

from multi_mcp.models.config import SearchPolicy, ToolCallRequest
from multi_mcp.models.secrets import SecretStore


EXPOSED_TOOLS = ["web_search"]

# Estimated cost per request (USD) — used for budget tracking
_COST_PER_BASIC = 0.001
_COST_PER_ADVANCED = 0.005

_QUOTA_STATE_FILE = Path("logs/search_quota.json")


class SearchAdapter:
    """
    Tavily search adapter with quota/cost guard.
    """

    def __init__(self, policy: SearchPolicy, secret_store: SecretStore) -> None:
        self.policy = policy
        self._secrets = secret_store
        self._quota = self._load_quota()

    def list_tools(self) -> list[str]:
        return EXPOSED_TOOLS

    async def call(self, request: ToolCallRequest) -> dict[str, Any]:
        tool = request.tool_name
        if tool != "web_search":
            raise ValueError(f"Unknown tool: {tool}")

        args = request.args
        alias: str = args["alias"]
        query: str = args["query"]
        max_results: int = min(int(args.get("max_results", 5)), self.policy.max_results)
        search_depth: str = args.get("search_depth", "basic")

        # Retrieve API key from SecretStore (never from client)
        api_key = self._secrets.get(f"search:{alias}")
        if not api_key:
            return {"error": f"No API key found for alias '{alias}'"}
        if api_key.startswith("DISABLED:"):
            return {"error": f"Search alias '{alias}' is disabled"}

        # Quota check
        quota_error = self._check_and_increment_quota(search_depth)
        if quota_error:
            return {"error": quota_error}

        # Call Tavily API
        return await self._call_tavily(api_key, query, max_results, search_depth)

    async def _call_tavily(
        self,
        api_key: str,
        query: str,
        max_results: int,
        search_depth: str,
    ) -> dict[str, Any]:
        """Call the Tavily REST API asynchronously."""
        try:
            import httpx  # type: ignore[import]
        except ImportError:
            return {"error": "httpx not installed. Run: pip install httpx"}

        payload = {
            "api_key": api_key,
            "query": query,
            "max_results": max_results,
            "search_depth": search_depth,
        }
        async with httpx.AsyncClient(timeout=30.0) as client:
            resp = await client.post("https://api.tavily.com/search", json=payload)
            resp.raise_for_status()
            data = resp.json()

        return {
            "query": query,
            "results": data.get("results", []),
            "answer": data.get("answer"),
            "search_depth": search_depth,
        }

    # ---- Quota tracking ----

    def _load_quota(self) -> dict[str, Any]:
        _QUOTA_STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
        if _QUOTA_STATE_FILE.exists():
            try:
                return json.loads(_QUOTA_STATE_FILE.read_text())
            except Exception:  # noqa: BLE001
                pass
        return {"date": str(date.today()), "daily_count": 0, "monthly_cost_usd": 0.0}

    def _save_quota(self) -> None:
        _QUOTA_STATE_FILE.write_text(json.dumps(self._quota, indent=2))

    def _check_and_increment_quota(self, search_depth: str) -> str | None:
        """
        Check quota limits and increment counters.
        Returns an error message if quota is exceeded, else None.
        """
        today = str(date.today())
        if self._quota.get("date") != today:
            # Reset daily counter
            self._quota["date"] = today
            self._quota["daily_count"] = 0

        if self._quota["daily_count"] >= self.policy.daily_request_cap:
            return (
                f"Daily request cap ({self.policy.daily_request_cap}) reached. "
                "Request blocked by cost guard."
            )

        cost = _COST_PER_ADVANCED if search_depth == "advanced" else _COST_PER_BASIC
        if self._quota["monthly_cost_usd"] + cost > self.policy.monthly_credit_budget:
            return (
                f"Monthly budget (${self.policy.monthly_credit_budget:.2f}) would be exceeded. "
                "Request blocked by cost guard."
            )

        self._quota["daily_count"] += 1
        self._quota["monthly_cost_usd"] = round(self._quota["monthly_cost_usd"] + cost, 6)
        self._save_quota()
        return None

    def get_quota_status(self) -> dict[str, Any]:
        """Return current quota usage for GUI display."""
        return {
            "date": self._quota.get("date"),
            "daily_count": self._quota.get("daily_count", 0),
            "daily_cap": self.policy.daily_request_cap,
            "monthly_cost_usd": self._quota.get("monthly_cost_usd", 0.0),
            "monthly_budget_usd": self.policy.monthly_credit_budget,
        }
