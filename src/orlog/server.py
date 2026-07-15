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
from orlog.server_tools import (
    check_action_tool,
    list_attributes_tool,
    list_entities_tool,
    recall_history_tool,
    recall_tool,
    remember_tool,
    stats_tool,
)

_SPEC_PATH = Path(__file__).resolve().parents[2] / "docs" / "ORLOG-SPEC.md"
_CONFORMANCE_REPORT_PATH = Path("conformance-report.json")


def build_server(runtime: Runtime) -> FastMCP:
    mcp = FastMCP("orlog")
    mode = runtime.config.workspace.mode  # "conformant" | "degraded" -- spec §B9 MUST state this

    @mcp.tool()
    def remember(
        text: str, occurred_at: str | None = None, type: str = "fact", actor: str = "user",
        entity: str | None = None, attribute: str | None = None, value: str | None = None,
        entity_detail: str | None = None, register_new_type: bool = False, register_new_attribute: bool = False,
    ) -> dict:
        """Append a scrubbed memory to the log. Returns {event_id}.

        entity, attribute, AND value are all REQUIRED (alongside text) --
        those three make it a queryable fact (key "{entity}.{attribute}"),
        e.g. remember(text=..., entity="user:42", attribute="email",
        value="..."), later recallable as recall("user:42.email"). A
        text-only call (any of the three omitted) is rejected with an
        E_SCHEMA error: it would be durably logged but permanently
        unretrievable via recall()/recall_history() in this reference
        server, which is a silent dead end this tool refuses to create.

        `entity_detail` disambiguates two entities that would otherwise
        share a bare name (e.g. two different people both named "Anna"):
        pass a short distinguishing phrase (e.g. "coworker at Acme") and the
        fact is stored under its own key, distinct from any other entity
        using the same bare `entity` with a different `entity_detail`. A
        free-text recall() that can't tell the two apart will abstain with
        AMBIGUOUS and list both as candidates instead of guessing. Once a
        bare entity label has been disambiguated once, entity_detail becomes
        REQUIRED on every later write to that label -- omitting it raises an
        E_SCHEMA error naming the detail(s) already on record, rather than
        risking a silent collision with the wrong entity.

        If this workspace's orlog.toml declares [schema].known_types
        (non-empty), entity's type prefix (the "person" in "person:emma")
        must be one of them, and attribute must already be a known attribute
        for that type (any attribute previously used by a fact of that
        type) -- both raise E_SCHEMA naming the known set otherwise. Pass
        register_new_type=true / register_new_attribute=true to add a new
        one instead of hard-failing. Entities with no ":" in them, or a
        workspace with no known_types declared, are exempt from this check
        (but never from the entity_detail rule above).
        """
        return remember_tool(
            runtime, text, occurred_at=occurred_at, type=type, actor=actor,
            entity=entity, attribute=attribute, value=value, entity_detail=entity_detail,
            register_new_type=register_new_type, register_new_attribute=register_new_attribute,
        )

    @mcp.tool(
        description=(
            f"Recall a fact as of a point in time. Guarantee mode: {mode}. "
            "`query` has two valid forms. (1) The exact \"entity.attribute\" key "
            "used when the fact was remembered (e.g. \"user:42.email\") -- a fast, "
            "direct point-in-time key lookup. (2) Free text with no \".\" at all "
            "(e.g. \"what is user 42's email?\") -- this searches every "
            "remembered fact's own text by meaning to find which key the query is "
            "about, then still serves the deterministic, freshness-checked, cited "
            "value for that key (never a raw/unverified snippet). If more than one "
            "key matches about equally well (e.g. two same-named entities "
            "disambiguated only by entity_detail), it abstains with reasons: "
            "[\"AMBIGUOUS\"] and lists every tied candidate instead of guessing. A "
            "query with no matching entity.attribute (or no free-text match at all) "
            "abstains with NO_CANDIDATES, including for facts that were remembered "
            "as text only, without entity/attribute/value. Free-text search never "
            "hangs: if the semantic backend can't become ready in time (e.g. a cold "
            "model load with no network), it abstains with reasons: "
            "[\"EMBEDDER_UNAVAILABLE\"] instead of blocking. A fact whose own "
            "remembered text contradicts the value it was recorded with abstains "
            "with reasons: [\"UNSUPPORTED_BY_SOURCE\"] -- permanently, on every "
            "future read, and without ever blocking. Verification is otherwise "
            "bounded by a hard timeout: reasons: [\"VERIFY_TIMEOUT\"] if it can't "
            "complete in time, again instead of blocking."
        )
    )
    def recall(query: str, as_of: str = "now") -> dict:
        return recall_tool(runtime, query, as_of=as_of)

    @mcp.tool()
    def recall_history(query: str) -> dict:
        """The fact's full revision chain with validity windows.

        Same "entity.attribute" key lookup as recall() -- not free text.
        """
        return recall_history_tool(runtime, query)

    @mcp.tool()
    def list_entities(prefix: str | None = None, limit: int | None = None) -> dict:
        """Enumerate remembered entities: {entities: [{entity_label,
        entity_detail, attribute_count}]}. A deterministic scan of the log --
        zero tokens, no embedding call, no derive/verify -- for when you
        already know roughly what's stored and just need to see what's
        there, instead of a semantic recall() search. `prefix` filters by
        entity_label prefix; `limit` caps the count returned.
        """
        return list_entities_tool(runtime, prefix=prefix, limit=limit)

    @mcp.tool()
    def list_attributes(entity: str) -> dict:
        """Current attributes for one entity: {entity, attributes:
        [{attribute, value, valid_from, valid_to}]}. Same deterministic,
        zero-token, no-embedding guarantee as list_entities. `entity` may be
        a bare label or the exact "label#detail" form; if the bare label
        matches more than one disambiguated entity, returns {ambiguous:
        true, entity, candidates: [...]} instead of guessing which one.
        """
        return list_attributes_tool(runtime, entity)

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
