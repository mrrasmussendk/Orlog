"""server: the MCP server surface (ORLOG-SPEC.md §B9).

Thin FastMCP wiring around server_tools.py's plain functions -- this file
exists so the `mcp` SDK dependency is confined to one place; all the actual
tool logic lives in server_tools.py and is tested without it.

MUST honored (spec §B9): tool errors map to abstentions or the error
taxonomy -- an MCP tool call never returns an unverified claim as if
verified (server_tools.recall_tool always returns a real Answer object,
whose `verified` field is the caller's only source of truth for that).
"""

from __future__ import annotations

import json
from pathlib import Path

from mcp.server.fastmcp import FastMCP

from orlog.runtime import Runtime
from orlog.server_tools import check_action_tool, recall_history_tool, recall_tool, remember_tool, stats_tool

_SPEC_PATH = Path(__file__).resolve().parents[2] / "docs" / "ORLOG-SPEC.md"
_CONFORMANCE_REPORT_PATH = Path("conformance-report.json")


def build_server(runtime: Runtime) -> FastMCP:
    mcp = FastMCP("orlog")
    mode = runtime.config.workspace.mode  # "conformant" | "degraded" -- spec §B9 MUST state this

    @mcp.tool()
    def remember(text: str, occurred_at: str | None = None, type: str = "fact", actor: str = "user", entity: str | None = None, attribute: str | None = None, value: str | None = None) -> dict:
        """Append a scrubbed memory to the log. Returns {event_id}."""
        return remember_tool(runtime, text, occurred_at=occurred_at, type=type, actor=actor, entity=entity, attribute=attribute, value=value)

    @mcp.tool(description=f"Recall a fact as of a point in time. Guarantee mode: {mode}.")
    def recall(query: str, as_of: str = "now") -> dict:
        return recall_tool(runtime, query, as_of=as_of)

    @mcp.tool()
    def recall_history(query: str) -> dict:
        """The fact's full revision chain with validity windows."""
        return recall_history_tool(runtime, query)

    @mcp.tool()
    def check_action(action_description: str) -> dict:
        """Advisory only: warnings and prior outcomes for a similar past action."""
        return check_action_tool(runtime, action_description)

    @mcp.tool()
    def stats() -> dict:
        """Counters: appends, recalls, cache hits/evicts, verifications, abstentions, tokens, latency."""
        return stats_tool(runtime)

    @mcp.resource("orlog://spec")
    def spec_resource() -> str:
        return _SPEC_PATH.read_text(encoding="utf-8") if _SPEC_PATH.exists() else "spec not found"

    @mcp.resource("orlog://conformance-report")
    def conformance_report_resource() -> str:
        if _CONFORMANCE_REPORT_PATH.exists():
            return _CONFORMANCE_REPORT_PATH.read_text(encoding="utf-8")
        return json.dumps({"status": "no conformance run yet"})

    return mcp
