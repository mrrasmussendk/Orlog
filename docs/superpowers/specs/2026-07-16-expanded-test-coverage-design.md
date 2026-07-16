# Expanded test coverage: remember/recall edge cases, server surface, and live LLM tool-calling

Status: approved for planning
Date: 2026-07-16

## Motivation

Real-world use of orlog as a live MCP memory server has shown two symptoms:

1. Facts don't always get durably remembered the way they should.
2. Tool responses/descriptions don't always give the calling LLM enough to
   act on correctly (what to do next on an abstention, what a schema
   violation actually requires).

`tests/test_remember_contract.py`'s own docstring documents a real incident
of the first kind: a claim that "`remember()` rejects incomplete writes" was
made without observing the live running system, which kept silently
accepting incomplete writes for over an hour after the fix had already
landed on disk.

Investigation (this design's prerequisite) found the underlying logic is
believed correct as designed — this is a test-coverage gap, not a behavior
bug. Two concrete, previously-**untested** surfaces were found that map
directly onto "sending back actual instructions to the LLM":

- `server.py` (the real `FastMCP` tool registration: names, JSON schemas,
  and docstrings/descriptions — literally what a calling LLM reads to learn
  how to use `remember`/`recall`) has **zero** test coverage. Only
  `server_tools.py`'s plain functions are tested.
- `AnthropicCompletion`/`OpenAICompletion` in `huginn_llm.py` (the adapters
  that actually build the request sent to the model) have **zero** test
  coverage. `test_huginn_llm.py` only exercises `LLMDeriver` against a fake
  `CompletionFn`.

A third gap, raised directly by the project owner: whether fuzzy natural
language (e.g. "Anna lived in Copenhagen") reliably becomes a well-formed
`remember(entity=, attribute=, value=)` call is a question about the
**calling LLM's** tool-use behavior against `server.py`'s tool descriptions
— not about orlog's own code, since orlog never parses free text into a
schema itself (confirmed: no extraction/NER/inference logic exists in
`src/orlog/`; the only two AI-usage points, per
`docs/WHAT-IS-ORLOG.md`, are drafting an answer's wording and resolving a
free-text *query* to a key on the **read** side). This needs a genuinely
different kind of test: one that puts a real model in the loop.

## Goals

- Add substantially more test coverage across `remember`/`recall` edge
  cases, the real MCP server surface, and the LLM prompt/adapter layer.
- Add a small, clearly-separated tier of integration tests that prove a
  real model, given only `server.py`'s existing tool descriptions, turns
  fuzzy natural language into correctly-structured `remember`/`recall`
  calls.
- Zero changes to `src/orlog/` — this is test-only work. If a new test
  reveals an actual behavior bug, stop and report it rather than silently
  patching production code as a side effect.

## Non-goals

- No new "instructions to the LLM" content (docstrings, error messages) is
  authored by this work unless a test proves one is actually missing or
  wrong — in which case that becomes a separate, explicitly-flagged
  follow-up, not a silent scope-creep fix bundled into a test PR.
- No CI workflow changes. The live-model integration tests must work
  locally for anyone with an `ANTHROPIC_API_KEY` exported; wiring them into
  `.github/workflows/ci.yml` (which would require a repo secret) is a
  separate decision for the project owner to make later.

## Part A — deterministic test expansion (no network, no API key)

Matches the existing suite's house style throughout: one narratively-named
test function per scenario with a docstring/comment explaining *why*, not
table-driven `@pytest.mark.parametrize`, except where a handful of cases
are genuinely symmetric enumeration.

### A1. `tests/test_server.py` (new)

Drives the real `orlog.server.build_server()` FastMCP instance — not
`server_tools.py`'s plain functions — via `asyncio.run(mcp.list_tools())`
and `asyncio.run(mcp.call_tool(name, args))`. Confirmed both are async
methods on `FastMCP` but callable from a plain `def test_...():` via
`asyncio.run(...)`, so no new test dependency (e.g. `pytest-asyncio`) is
needed.

Cases:

- Every tool name from spec §B9 is registered exactly once: `remember`,
  `recall`, `recall_history`, `list_entities`, `list_attributes`,
  `check_action`, `stats`.
- `recall`'s description string mentions every abstention reason code the
  pipeline can actually produce (`NO_CANDIDATES`, `AMBIGUOUS`,
  `EMBEDDER_UNAVAILABLE`, `UNSUPPORTED_BY_SOURCE`, `VERIFY_TIMEOUT`) — a
  regression guard against a new reason code shipping without ever being
  documented back to the calling LLM.
- `recall`'s description states the current `workspace.mode`
  (conformant/degraded) from the actual runtime config, not a hardcoded
  string.
- `remember`'s docstring states the entity+attribute+value coupling
  explicitly, since the JSON schema's own `required` list only names
  `text` (confirmed live: FastMCP derives `required` from Python parameter
  defaults, so the "required together" rule is invisible to a client that
  only reads the schema, not the prose).
- A full `call_tool()` round trip: `remember` then `recall` over the real
  MCP surface, parsing the returned `TextContent` as JSON.
- A `call_tool()` error path: an incomplete `remember` call (missing
  entity/attribute/value) raises `mcp.server.fastmcp.exceptions.ToolError`,
  and its message contains the real `SchemaError` text (i.e. the guidance
  survives FastMCP's own error-wrapping layer intact).
- Both `orlog://spec` and `orlog://conformance-report` resources are
  registered and return non-empty text.

### A2. `tests/test_huginn_llm_adapters.py` (new)

Tests `AnthropicCompletion`/`OpenAICompletion` by monkeypatching the
`anthropic.Anthropic`/`openai.OpenAI` client classes with a fake that
returns a scripted response object shaped like the real SDK's — no network
call, no API key, same no-live-call guarantee as the rest of the suite.

Cases:

- Missing API key env var raises `RuntimeError` before any client is built
  (both providers).
- Text is correctly joined across multiple `content` blocks (Anthropic) /
  read from `choices[0].message.content` (OpenAI).
- A non-text content block (Anthropic) is skipped, not concatenated as
  garbage.
- `usage` is converted to the `{"in":, "out":}` shape `LLMDeriver` expects,
  for both providers' differently-named usage fields.
- The provider's own timeout exception (`anthropic.APITimeoutError` /
  `openai.APITimeoutError`) is caught and re-raised as the plain stdlib
  `TimeoutError` `LLMDeriver._call` expects — proving the "provider-neutral
  signal" the module docstring promises actually holds.
- `temperature=0` and the configured `model`/`max_tokens` are passed
  through unchanged to the underlying client call.

### A3. `tests/test_remember_contract.py` (expand)

- The other two partial-write combinations, alongside the existing
  "entity+attribute, no value" case: entity+value/no attribute,
  attribute+value/no entity (parametrized — genuinely symmetric).
- Empty-string (not `None`) entity/attribute/value is rejected the same
  way `None` is (confirms the check isn't an `is None`-only check that a
  client could route around with `""`).

### A4. `tests/test_server_tools.py` (expand)

- `entity_detail` passed on a *first* (not-yet-disambiguated) write to a
  label — confirms it's accepted/stored and doesn't spuriously require
  disambiguation before any collision exists.
- `register_new_type=True` and `register_new_attribute=True` combined in
  one call, for a genuinely new type *and* new attribute simultaneously.
- `register_new_attribute=True` for an already-known type but a new
  attribute name, and the reverse (known attribute, new type).
- Invalid `occurred_at` string (unparseable) raises a clear error rather
  than an obscure one from deep in `datetime.fromisoformat`.
- A `recall` query with more than one `.` (e.g.
  `"user:1.settings.theme"`) — confirms `rpartition(".")`'s
  right-to-left split behavior is intentional, not accidental.
- Case sensitivity: an entity/attribute recalled with different case than
  it was written abstains (or matches) — whichever the current retrieval
  logic actually does, pinned down explicitly rather than left implicit.
- The ambiguity margin's exact boundary (a runner-up score precisely at
  `top.score * ambiguity_margin`) — confirmed as either tied or not, not
  left to floating-point luck.

### A5. `tests/test_huginn_llm.py` (expand)

- Duplicate citation ids in the model's JSON response are deduplicated (or
  whatever the actual current behavior is) rather than silently
  double-counted.
- A `citations` field that isn't a list (e.g. a bare string) is handled
  without crashing — either coerced or rejected with `UncitedAssertion`.
- An empty-string `claim` (present but falsy) is treated the same as a
  missing claim.
- JSON wrapped in a markdown code fence, and JSON containing nested
  `{...}` inside a string value, both still extract correctly via the
  largest-span heuristic.

### A6. `tests/conformance/` (expand each cell by 1-2 scenarios)

- **C1**: two facts about the same key with the exact same `occurred_at`
  instant — confirms the tie-break rule is deterministic, not
  insertion-order-dependent by accident.
- **C2**: a third query interleaved between an existing entry's cache put
  and its eviction, to confirm the unrelated entry's cache route is
  unaffected by a neighboring key's invalidation.
- **C3**: corruption rate exactly at the documented 30%/90% boundary
  (rather than only the comfortably-inside-the-margin case already
  covered).
- **C5**: replay correctness across a segment rotation boundary (`storage.py`'s
  64MB rotation), not just a single-segment log.

## Part B — live LLM tool-calling integration tests

### Location and gating

- New directory `tests/integration/`, new file
  `tests/integration/test_remember_extraction.py`.
- `pytestmark = pytest.mark.skipif(not os.environ.get("ANTHROPIC_API_KEY"), reason="requires a live ANTHROPIC_API_KEY")`
  at module level, so a normal `pytest` run with no key reports these as
  **skipped**, never failed or even collected-as-error.
- Register `integration` as a known marker in `pyproject.toml` so
  `pytest -m "not integration"` can explicitly exclude them, and so pytest
  doesn't warn about an unregistered marker.
- Model: `claude-haiku-4-5-20251001` by default, overridable via an
  `ORLOG_TEST_MODEL` env var.
- Provider scope: Anthropic only (per project owner's call — keeps cost
  and surface area minimal; the thing under test is prompt/tool-description
  quality, not provider-specific tool-calling quirks).

### Mechanics (shared helper, one real model call per scenario)

1. Build a real `Runtime` (temp workspace, `embedder="hashing"`) and
   `build_server(runtime)`.
2. `asyncio.run(mcp.list_tools())`, convert each `MCPTool` to Anthropic's
   tool-use shape: `{"name": t.name, "description": t.description,
   "input_schema": t.inputSchema}` — the exact descriptions already
   shipped in `server.py`, not a specially-crafted test-only prompt. This
   is the point: proving the existing docstrings are sufficient on their
   own.
3. Send the scenario's fuzzy sentence as the user message, with a minimal
   system prompt only establishing the assistant has memory tools
   available (no schema hints beyond what `server.py` already provides).
4. Extract the resulting `tool_use` block's `name` + `input`.
5. Execute that exact tool call for real via `asyncio.run(mcp.call_tool(...))`
   against the same runtime (full round trip, not just inspecting the
   arguments).
6. For write scenarios, `recall` it back (via the exact key the model used,
   and/or a fresh fuzzy query) and assert `verified` + the claim's content.

Assertions throughout are deliberately fuzzy — case-insensitive substring
checks on `entity`/`value`, not exact-string equality — since the subject
under test is a model's judgment call, not deterministic code.

### Scenarios (5)

1. **Location fact**: "Anna lived in Copenhagen." → `remember` call whose
   `entity` mentions "anna" and `value` mentions "Copenhagen"; recall round
   trip confirms `verified=True` and the claim mentions Copenhagen.
2. **Plan/preference fact**: "Remember that Marc's subscription plan is
   Pro." → similar assertions for `entity`≈"marc", `value`≈"Pro".
3. **Allergy-style fact**: "Just so you know, Emma is allergic to
   shellfish." → `entity`≈"emma", `value`≈"shellfish".
4. **Disambiguation needed**: two sequential fuzzy statements about
   different people who share a first name (e.g. a coworker Anna and a
   roommate Anna, each with distinguishing context in the sentence) →
   confirms the model supplies distinct `entity_detail` (or otherwise
   keeps them separately keyed) so both remain independently recallable —
   mirrors the existing entity_detail conformance tests, but proves the
   *extraction* side rather than only the retrieval side.
5. **Reverse direction**: given facts already stored, a natural-language
   question → confirms the model calls `recall` (not `remember`) with a
   query that resolves correctly, exercising the calling LLM's half of
   "guessing which stored fact a plain-English question is about" (the
   other half — orlog's own `resolve_key()` — is already covered by
   existing unit tests).

## Verification

- `.venv` already set up in this workspace (Python 3.14, `pip install -e
  ".[dev,anthropic,openai]"`); the full existing suite passes (211/211)
  before this work starts.
- Every new deterministic test (Part A) runs via plain `pytest` with no
  network access and no API key, alongside the existing suite.
- Part B tests are run manually with `ANTHROPIC_API_KEY` exported; not
  required for the rest of the suite to pass.
- Where practical, confirm a new test actually tests something by checking
  it fails against a deliberately-reverted/broken version of the relevant
  behavior at least once, per TDD practice.
- No changes to `src/orlog/` unless a genuine behavior bug is found during
  this work, in which case: stop, report the finding, and get explicit
  sign-off before fixing it (out of scope for a "tests only" pass
  otherwise).
