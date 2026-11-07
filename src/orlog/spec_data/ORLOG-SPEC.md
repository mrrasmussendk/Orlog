Orlog — Production Specification v1.1

Supersedes: ORLOG-SPEC.md v1.0 (Part A is unchanged; v1.1 adds Part B's
§B12 — profiles — and the §B1/§B2/§B5/§B9/§B10 changes that go with it,
and removes HTTP transport from §C4's non-goals).
Supersedes: ORLOG-SPEC.md v0.1-draft (protocol core carries over; this
document is the complete build target).
Deliverable: an installable MCP memory server (pip install orlog) with an
embeddable Python library underneath, a pluggable verifier system, and a
conformance suite. The product promise: memory for your agent that never
serves a stale fact.

RFC 2119 keywords (MUST/SHOULD/MAY) are normative. Anything not marked is
informative.


PART A — Protocol (normative core)

A1. Guarantees

An orlog v1.0 deployment in conformant mode guarantees:


G1 Immutability — events are never mutated or deleted after append;
erasure of identity is by pseudonym-key destruction only (§B7).
G2 Revisability — every projection can be rebuilt from the log,
deterministically, at any time.
G3 Verification — no assertion is served without passing the sovereign
verifier; this includes cached assertions.
G4 Visible failure — the only failure mode exposed to callers is explicit
abstention with machine-readable reasons; never a best guess.
G5 Outcome learning — all adaptation derives from recorded outcomes,
never from co-occurrence statistics.


Degraded mode (no verifier configured): G1, G2, G5 hold; recall responses
MUST carry "verified": false and the server MUST NOT be described as
conformant. The MCP tool descriptions MUST reflect the mode.

A2. Epistemic state machine

OBSERVED → INTERPRETED → RETRIEVED → ASSERTED → VERIFIED → REINFORCED
                                         │           ▲
                                         ▼ fail      │ pass
                                    RE-DERIVED ──────┤
                                         │ fail      │
                                         ▼           │
                                     ABSTAINED ◄─────┘ (no valid candidates)

Rules (MUST): assertions require non-empty citations before verification;
exactly one re-derivation per query, with failed citations excluded; abstention
carries reasons and no candidate answer; every terminal state emits an
outcome.* event; illegal transitions raise ProtocolViolation.

A3. Object schemas

All objects validate against spec/schemas/*.json (the normative wire format).
IDs are ULIDs. Timestamps are RFC 3339 UTC. Canonical serialization for hashing:
JSON with sorted keys, no whitespace, UTF-8.

Event

json{
  "id": "01J...ULID",
  "occurred_at": "2026-07-11T14:03:00Z",
  "recorded_at": "2026-07-11T14:03:02Z",
  "actor": "user | agent:<name> | tool:<name> | system",
  "type": "utterance | decision | tool_result | fact | outcome.verification | outcome.abstention | outcome.correction | x.<vendor>.*",
  "payload": {"text": "…", "...": "type-specific"},
  "entities": ["PERSON_7", "ORG_3"],
  "supersedes_hint": null,
  "schema_version": "1.0",
  "prev_hash": "sha256:…"
}

MUST: recorded_at and prev_hash assigned by the log; hash chain over
canonical form; append rejects schema-invalid events with E_SCHEMA.

Assertion / VerificationResult / CacheEntry / ValidityWindow /

ProjectionVersion / Candidate

As in spec v0.1 §3 (unchanged), plus on Assertion:
"tokens": {"in": int, "out": int} and "latency_ms": number for
observability.

Answer (the recall response, MCP-facing)

json{
  "verified": true,
  "claim": "80.000 kr",
  "citations": [
    {"event_id": "01J…", "excerpt": "Effective 2025-06-01: …", "valid_from": "2025-06-01", "valid_to": null}
  ],
  "as_of": "2026-07-11T00:00:00Z",
  "route": "fresh | cache | rederived",
  "truth_version": "pytest@a1b2c3 | karnov@2026-07-10 | windows@v14",
  "abstained": false,
  "reasons": [],
  "excluded": {"by_validity": 3, "by_k": 12}
}

Abstention: "abstained": true, "claim": null, "reasons": ["EXPIRED", …].

A4. The answer pipeline (normative)

As spec v0.1 §5, with production additions:


P1 Cache lookup → if hit, verify cached assertion at current as_of/truth
version. Pass → serve (route=cache), record outcome. Fail → evict, continue.
P2 Retrieve (as-of filtered, hybrid scoring §B3). Empty → abstain
NO_CANDIDATES.
P3 Derive (LLM, §B4). Deriver-level failures (timeout, malformed output after
repair, empty citations after retry) → abstain DERIVATION_FAILED — never a
protocol violation, never an unverified answer.
P4 Verify. Pass → serve fresh, cache, record. Fail → P5.
P5 Re-derive once against candidates minus failed citations, pre-filtered by
V2+V3. Verify. Pass → serve (route=rederived). Fail or no candidates →
abstain with accumulated reasons.
P6 Record outcome event; apply importance updates (§B6).


Latency budget (SHOULD, local deployment, warm): P1 ≤ 5 ms; P2 ≤ 50 ms at
100k events; verification ≤ 20 ms excluding external truth lookups; total
non-LLM overhead ≤ 100 ms. The LLM call dominates and is not budgeted here.

A5. Verifier contract (sovereign)

pythonclass Verifier(Protocol):
    truth_version: str                     # snapshot id of ground truth
    def verify(self, assertion: Assertion, as_of: datetime) -> VerificationResult: ...

Checks, all mandatory in order: V1 uncited → fail UNCITED; V2 every
citation resolves in the log (NOT_FOUND); V3 every citation valid at
as_of per the verifier's own ground truth (EXPIRED / NOT_YET_VALID);
V4 every citation supports the claim per domain rules (UNSUPPORTED).
MUST be deterministic for fixed (assertion, as_of, truth_version). MUST NOT
invoke a nondeterministic model. External truth lookups MUST be versioned
(truth_version changes when ground truth changes) and SHOULD be cached with
invalidation on version change.


PART B — Production runtime

B1. Package & processes


Distribution: PyPI orlog, Python ≥ 3.11, Apache-2.0. Core deps: pydantic
≥ 2, mcp (official SDK), fastembed (default embedder), anthropic and
openai as optional extras (orlog[anthropic], orlog[openai]).
Processes: one MCP server per workspace (a directory containing
orlog.toml + data). Single-writer: the server owns the log; concurrent
server instances on one workspace MUST be prevented by a lockfile
(data/orlog.lock, E_LOCKED on second start).
CLI: orlog init (scaffold workspace), orlog serve [--http [HOST]:PORT]
(MCP; stdio by default, streamable HTTP on request — §B12.5),
orlog conformance [--target self|<endpoint>], orlog replay (rebuild all
projections, verify hash chain), orlog inspect <event_id> (provenance
chain), orlog forget <pseudonym> (crypto-shred, §B7).
Profiles: a distribution MAY extend a deployment through the entry-point
groups of §B12 without forking core. Which installed profiles a workspace
loads is the workspace's decision ([profiles], §B10), not the
interpreter's.


B2. Storage

Layout under <workspace>/data/:

events/log-00001.jsonl      append-only segments, rotate at 64 MB
index.sqlite                all derived state (rebuildable)
vault.sqlite                pseudonym vault (encrypted, separate file)
cache/derivations/          replay cache for model-assisted projections

SQLite DDL (normative minimum):

sqlCREATE TABLE events_idx (id TEXT PRIMARY KEY, occurred_at TEXT, recorded_at TEXT,
  actor TEXT, type TEXT, segment TEXT, offset INTEGER);
CREATE INDEX ix_ev_time ON events_idx(occurred_at);
CREATE INDEX ix_ev_type ON events_idx(type);
CREATE TABLE entities_idx (event_id TEXT, entity TEXT);
CREATE TABLE windows (projection TEXT, version INTEGER, fact_id TEXT,
  valid_from TEXT, valid_to TEXT, chain TEXT,
  PRIMARY KEY (projection, version, fact_id));
CREATE TABLE embeddings (event_id TEXT PRIMARY KEY, model TEXT, dim INTEGER, vec BLOB);
CREATE TABLE importance (event_id TEXT PRIMARY KEY, weight REAL DEFAULT 1.0, updated_at TEXT);
CREATE TABLE route_cache (key TEXT PRIMARY KEY, assertion_json TEXT,
  created_at TEXT, last_verified_at TEXT, hits INTEGER);
CREATE TABLE projections (projection TEXT, version INTEGER, built_from TEXT,
  built_at TEXT, builder TEXT, quality REAL, PRIMARY KEY (projection, version));

The DDL above is a normative MINIMUM, not a closed set: an implementation
MAY add derived tables, and the reference implementation adds three for
§B12.4's query API — event_bodies (each event's canonical JSON, plus its
type, its timestamps normalized to UTC, and its segment/offset), facts_idx
(the grouping columns of a fact payload) and outcomes_idx (query_id and
status of each outcome.* event). Timestamps in added tables SHOULD be
normalized to UTC: ISO-8601 only orders lexicographically when every value
carries the same offset, and a range filter that compares them as written
silently excludes events recorded in another zone.
MUST: index.sqlite is entirely derivable — orlog replay drops and rebuilds
it from the log byte-identically (G2 test). An added table is bound by the
same MUST. A reader that finds the index inconsistent with the log MUST
rebuild it rather than serve from it. Appends are fsynced before the
append() call returns. Hash-chain verification runs on startup for the newest
segment and full-chain on orlog replay.

B3. Retrieval

Hybrid scorer (default): score = (0.5·lexical + 0.5·cosine) · importance,
where lexical is token-overlap/BM25-lite and cosine uses the configured
embedder. Default embedder: fastembed with BAAI/bge-small-en-v1.5
(local, no API); configurable per workspace. Embeddings are computed at append
time and stored (B2).

MUST: filter by validity window at as_of BEFORE ranking (a fact invalid at
as_of never reaches the deriver); report excluded.by_validity and
excluded.by_k; deterministic tie-break by event id. k default 8,
configurable 1–50. SHOULD: optional reranker hook (Reranker protocol) — v1.0
ships without a default reranker.

B4. Derivation (huginn, the LLM socket)

pythonclass Deriver(Protocol):
    def derive(self, query: str, as_of: datetime, candidates: list[Candidate]) -> Assertion: ...

LLMDeriver (production default): provider-agnostic over
anthropic/openai-compatible messages APIs.

Prompt contract (normative behavior, wording free):


System: answer ONLY from the numbered facts provided; cite fact ids; if the
facts do not contain the answer, output the literal token INSUFFICIENT.
Facts are rendered as [<event_id>] (<valid_from>) <text>.
Required output: JSON {"claim": str, "citations": [event_id, …]}.


MUST: temperature 0; max_tokens configurable (default 400); robust JSON
extraction (largest {…} span) before failing; on malformed output, ONE repair
retry with an error-explaining message; INSUFFICIENT → abstain
DERIVER_INSUFFICIENT; citations filtered to provided candidate ids (dropping
unknown ids; if none remain → treated as uncited); empty citations after retry
→ abstain DERIVATION_FAILED. Timeout default 30 s → abstain
DERIVER_TIMEOUT. API keys come ONLY from environment variables named in
config — never from config values, never logged, never appended to the event
log (§B7). Token counts and latency recorded on the Assertion.

ScriptedDeriver ships permanently (not a stub): powers conformance runs,
tests, and API-key-free evaluation.

B5. Projections (verdandi)

v1.0 ships two:


supersession_chains — groups fact-type events into chains, computes
validity windows. Grouping strategies (config): entity+type (deterministic:
same pseudonym entities + same event type ⇒ same chain; default) and llm
(model-proposed grouping; every model response stored in
cache/derivations/ keyed by input hash so rebuilds replay without calls —
purity via replay cache). supersedes_hint is used only to seed candidate
pairs; it never binds.
session_summaries — per-session digest views for retrieval context
(deterministic template in v1.0).


Both are registered through §B12.2's projection registry, as is every
projection a profile ships: core's own projections are not a special case
of the mechanism, they are its first consumers. orlog replay rebuilds all
of them.
Rebuild triggers: explicit (orlog replay), on builder version change, and
SHOULD on quality drop (verified-pass rate of answers citing this projection
falling 20% below its trailing mean). New projection versions do not delete old
ones until keep_versions (default 2) is exceeded.

B6. Outcomes & adaptation (skuld)

Every P6 writes an outcome.* event. Importance update (defaults, config-
tunable): pass → each cited fact weight += 0.5; correction event naming a
fact → weight += 1.0 to the corrected-to fact; nightly decay
weight *= 0.99 floored at 0.1. FORBIDDEN inputs to any adaptive behavior:
retrieval co-occurrence, query frequency without outcomes, embedding drift.
Cache: key = sha256(normalized query ‖ as_of class (instant vs current) ‖
projection versions ‖ truth_version); eviction on verification failure or
projection/truth version change; max_entries default 10 000, LRU beyond that
(storage bound, not trust — evicted ≠ invalidated).

B7. Security & privacy


Scrub pipeline (pre-append, MUST): configurable detectors — regex pack
(emails, phones, CPR/SSN-like ids) always on; optional local NER pack.
Detected spans → stable pseudonyms (PERSON_7) via the vault. Raw text of a
detected span MUST NOT reach the log.
Vault: vault.sqlite encrypted at rest (SQLCipher or field-level
AES-GCM; key from ORLOG_VAULT_KEY env or OS keychain). Re-identification
only via explicit resolve API, never automatic in recall output unless
resolve_entities = true in config AND the caller passes the workspace
capability token.
Erasure: orlog forget PERSON_7 deletes the vault row (crypto-shred) and
appends an x.orlog.forget event recording that erasure occurred (not what
was erased). GDPR Art. 17 compliance mode documented.
Secrets hygiene: API keys via env only; orlog.toml MUST be committable;
scrubber SHOULD detect and tokenize key-shaped strings in payloads.
MCP transport: stdio only in v1.0 (no network listener ⇒ no authn surface);
HTTP transport deferred to v1.1 behind token auth.


B8. Errors & observability

Error taxonomy (closed set): E_SCHEMA, E_LOCKED, E_CHAIN_BROKEN,
E_PROTOCOL (illegal state transition — always a bug), E_VERIFIER_UNAVAILABLE
(truth source unreachable → abstain TRUTH_UNAVAILABLE, never serve
unverified), E_STORAGE. Abstention reasons (caller-facing, distinct from
errors): UNCITED, NOT_FOUND, EXPIRED, NOT_YET_VALID, UNSUPPORTED, NO_CANDIDATES, DERIVATION_FAILED, DERIVER_INSUFFICIENT, DERIVER_TIMEOUT, TRUTH_UNAVAILABLE.

Observability: structured logs (JSON lines, no PII); counters exposed via
orlog stats and an MCP resource: appends, recalls, cache_hits, cache_evicts,
verifications{pass,fail by code}, abstentions{by reason}, rederivations,
tokens{in,out}, p50/p95 non-LLM latency. Provenance: orlog inspect <event_id>
prints the causal chain (event → projection version → assertions citing it →
outcomes).

B9. MCP server surface

Server name orlog. Tools (JSON Schema in spec/schemas/mcp/):


remember — args: {text, occurred_at?, type?="fact", actor?="user", entity,
attribute, value, entity_detail?, register_new_type?=false,
register_new_attribute?=false} → returns {event_id}. Runs scrub → append →
incremental projection update. entity, attribute, AND value are REQUIRED
together (not merely optional structured fields alongside text) — a
text-only write is durably loggable but permanently unretrievable via
recall()/recall_history(), so remember() raises E_SCHEMA rather than
silently create that dead end. When [schema].known_types (§B10) is
non-empty: entity's type prefix (the "person"
in "person:emma") MUST be a known type (declared in known_types, OR already
used by a prior fact -- register_new_type=true's effect is durable, not a
one-time bypass) unless register_new_type=true; once a type has at least one
attribute on record, a NEW attribute for it MUST already be known (a type's
first-ever attribute is always accepted, nothing to diverge from yet) unless
register_new_attribute=true. Either violation raises E_SCHEMA naming the
known set. Independent of schema
(and not gated by [schema].known_types): when a fact's bare entity label
already has one or more entity_detail values on record, entity_detail is
REQUIRED on this write — supplying one that matches an existing detail
continues that entity, a new one introduces another distinct entity under the
same label; omitting it raises E_SCHEMA naming the existing detail(s), so a
caller can never silently create an ambiguous second entity by accident.
Entities with no ":" in them, or workspaces with an empty/absent
[schema].known_types, are exempt from the type/attribute check (but never
from the entity_detail check, which needs no config).
recall — args: {query, as_of?="now"} → returns the Answer object (§A3).
Tool description MUST state the guarantee mode (conformant vs degraded).
recall_history — args: {query} → the fact's full chain with validity
windows (the "what did it used to be" tool).
list_entities — args: {prefix?, limit?} → {entities: [{entity_label,
entity_detail, attribute_count}]}. A deterministic scan over the log's own
fact events, grouped by (entity_label, entity_detail) — zero tokens, no
embedding call, no derive/verify. The scan/list primitive: lets a caller
enumerate what it already knows without a semantic search.
list_attributes — args: {entity} → {entity, attributes: [{attribute, value,
valid_from, valid_to}]}, one entry per attribute at its current (as-of-now)
value. entity may be the bare label or the exact "label#detail" form. If a
bare label matches more than one disambiguated entity, returns {ambiguous:
true, entity, candidates: [...]} instead of guessing which one — the same
abstain-don't-guess discipline as recall()'s AMBIGUOUS path. Same zero-token,
no-embedding guarantee as list_entities.
check_action — args: {action_description} → advisory {warnings:[…], prior_outcomes:[…]} from skuld (memory-as-governance).
stats — no args → counters (§B8).


The seven tools above are normative: their names, arguments and the
guarantees their descriptions state are fixed, and a profile MUST NOT
replace one (§B12.3). A profile MAY add tools alongside them; so MAY an
embedding application, through build_server(runtime, extra_tools).
remember additionally accepts {payload} in place of
{entity, attribute, value} when type names a registered event type
(§B12.1); the entity/attribute/value requirement above applies to type
"fact" only.
Resources: orlog://spec (this document), orlog://conformance-report (latest
run). MUST: tool errors map to abstentions or the error taxonomy — an MCP tool
call never returns an unverified claim as if verified.

B10. Configuration (orlog.toml, complete)

toml[workspace]
name = "my-project"
mode = "conformant"            # or "degraded"

[deriver]
backend = "anthropic"          # anthropic | openai | scripted
model = "claude-haiku-4-5"
api_key_env = "ANTHROPIC_API_KEY"
max_tokens = 400
timeout_s = 30

[retrieval]
k = 8
embedder = "BAAI/bge-small-en-v1.5"

[verifier]
backend = "windows"            # windows | pytest | module:path.to.Class
# [verifier.pytest] test_command = "pytest -q", map = "tests_map.json"

[projections.supersession_chains]
grouping = "entity+type"       # or "llm"
keep_versions = 2

[adaptation]
pass_bonus = 0.5
correction_bonus = 1.0
daily_decay = 0.99

[cache]
max_entries = 10000

[privacy]
detectors = ["regex"]          # +"ner"
resolve_entities = false

[profiles]                      # §B12. Which installed profiles this
# enabled = ["mimir"]            # workspace loads. `enabled` is THREE-
disabled = []                   # STATE: absent (the default) means every
                                 # installed profile, as entry points
                                 # normally work; an explicit empty list
                                 # means NONE, the kill switch; a
                                 # non-empty list is a strict allowlist of
                                 # distribution names. `disabled`
                                 # subtracts from the default and is
                                 # ignored when `enabled` is given.
                                 # Per-workspace, not per-interpreter: a
                                 # profile's code types, projects and
                                 # serves this workspace's memories, so
                                 # which ones may is a property of the
                                 # memories.

[schema]
known_event_types = []          # e.g. ["x.mimir.decision"] -- EVENT types
                                 # (the `type` field on the event) this
                                 # workspace accepts beyond core's
                                 # vocabulary and whatever loaded profiles
                                 # register. Distinct from known_types
                                 # below, which is about ENTITY types.
known_types = []                # e.g. ["person", "user"] -- entity-type
                                 # prefixes (the "person" in "person:emma")
                                 # remember() accepts without
                                 # register_new_type=true. Empty (default):
                                 # schema-on-write is off, every entity/
                                 # attribute string is accepted as in v1.0
                                 # pre-revision. Known attributes per type are
                                 # NOT declared here -- the log itself is the
                                 # attribute registry (any attribute already
                                 # used by a fact of a known type is known).

B11. Built-in verifiers


windows — validity-window + claim-support checker over a ground-truth
window table (the projection's own windows MAY seed it only in degraded
mode; conformant mode requires an external table or authority adapter).
pytest (the coding-agent wedge) — ground truth = the test suite.
Citations reference events of type fact carrying {claim_text, test_ids};
V3 = all mapped tests currently pass (truth_version = HEAD commit + test
collection hash); V4 = claim text matches the stored fact. Runs tests in a
subprocess with a timeout; unreachable/failing collection →
TRUTH_UNAVAILABLE.
module: — user-supplied class implementing the Verifier protocol; the
documented extension point (this is where a law-database verifier plugs in).



B12. Profiles (extension seams)

A profile is an installed distribution that extends a deployment through
the three entry-point groups below. The point of this section is that a
profile needs no change to orlog core: core's event types, projections and
tools are declared in code because they are normative, and a profile's are
discovered.

MUST: an entry point that fails to import is an error (E_PLUGIN), never a
silent skip — a workspace quietly missing the profile that was supposed to
type, project and serve its memories is indistinguishable from one that
has no memories. MUST: discovery is filtered by [profiles] (§B10) before
anything is imported, and a workspace MUST be able to refuse every profile
([profiles] enabled = []) whatever is installed alongside it.

B12.1 Event types — group orlog.event_types

An entry point resolves to a {type_name: PayloadModel} mapping, or a
zero-argument callable returning one; PayloadModel is a pydantic model for
that type's payload. MUST: a registered name matches x.<vendor>.<name>;
the vendor orlog and every core type name (fact, outcome.*, x.orlog.*) are
reserved; a name registered by two profiles is an error. Two profiles
sharing a type name would write two payload shapes into one append-only
log under one name, where the collision can never be taken back out.
remember() -- the §B9 tool, not merely the library call -- takes {payload}
for a registered type and validates it against that model BEFORE appending; the payload is scrubbed (§B7) before
validation, so the model certifies exactly the bytes that land in the log.
The entity/attribute/value requirement of §B9 stays for type "fact".
A non-fact write whose type is in none of (core's vocabulary, the
registry, [schema] known_event_types) MUST be refused at write time.

B12.2 Projections — group orlog.projections

An entry point resolves to a {name: Projection} mapping. A Projection has
`name`, `version` and build(events, *, built_at) -> (view,
ProjectionVersion), pure in the sense §4.2 already requires. It MAY have
selects(event) -> bool, the predicate choosing its source events from the
log, and update(view, version, new_events, *, built_at) for an incremental
rebuild — which MUST produce what build() would have produced over the
same widened set. An optimization is not permitted to be a second answer.
A profile MUST NOT register a name a core projection already uses.
Registered projections are rebuilt by orlog replay and updated
incrementally on append. Built views MAY be cached; a cache MUST be keyed
by the projection, its source set and the known_as_of horizon the view was
built under, and that horizon MUST be compared exactly rather than rounded
into a time bucket — recorded_at is not monotonic (§A3's backfill), so two
nearby horizons can select genuinely different event sets.

B12.3 Tools — group orlog.tools

An entry point resolves to a {tool_name: callable} mapping; a tool is
(runtime, **kwargs) -> dict, the same shape §B9's own tools have. MUST:
the seven names of §B9 are reserved. A profile that could shadow recall
could replace the one call carrying G3/G4, decided by entry-point
ordering. The tool's docstring is its MCP description.
build_server(runtime, extra_tools) is the same seam for an embedding
application with no distribution to install.

B12.4 Index query API

A read-only query surface over index.sqlite: events by exact type, by type
namespace prefix, by entity prefix, by valid-time range and by known_as_of
horizon; and the grouped scans §B9's list_entities / list_attributes /
recall_history / check_action need. MUST: queries return events identical
to the log's own, and comparisons on names and prefixes are
case-sensitive. SHOULD: list_entities answers at p95 under 50 ms on a
100k-event workspace.

B12.5 HTTP transport

orlog serve --http [HOST]:PORT runs the §B9 server over MCP streamable
HTTP; stdio remains the default. MUST: the daemon holds the workspace lock
of §B1 for its whole life, and connected clients are sessions on that one
writer — which is the reason the transport exists, since over stdio each
client spawns its own server and all but the first get E_LOCKED. MUST: the
listen host defaults to loopback; this transport carries no authentication
of its own in v1.1, so binding beyond loopback is an explicit operator
decision.

PART C — Conformance & release

C1. Conformance suite (frozen cells, orlog conformance)

CellPASS criteria (as v0.1 §6, unchanged)C1 supersession100% as-of resolution; 0 answers from destroyed historyC2 cache staleness0 stale served; invalidated routes re-derived/abstained; ≥1 valid cache serveC3 imperfect projection0 confidently-wrong at 30% corruption; acc ≥ 90% of clean baselineC4 citation disciplineuncited/hallucinated/expired citations never surface as answersC5 immutability & replayno mutation path; chain verifies; rebuilds byte-identical

Runs with ScriptedDeriver (no API key), emits conformance-report.json +
human-readable summary. CI MUST run the suite on every commit.

C2. Definition of done — v1.0 ships when


All conformance cells pass in CI on Linux + macOS, Python 3.11/3.12.
pip install orlog && orlog init && orlog serve connects from Claude Code
and remember/recall round-trip works with the anthropic backend.
The pytest verifier demo repo works end-to-end (cached knowledge invalidated
by a deliberately broken test).
orlog replay on a 100k-event synthetic log completes < 60 s and
reproduces index.sqlite byte-identically.
Crash-safety test passes: kill -9 during a burst of appends → restart →
chain verifies, no lost acknowledged events.
Security review checklist: no PII in logs/errors, vault encrypted, keys
env-only, orlog forget verified.
Docs: README (install, 5-minute pytest demo, guarantee statement), this
spec, and the two benchmark-cell writeups.


C3. Versioning & compatibility

Semver. schema_version on every event; v1.x readers MUST read all 1.x events.
A profile's x.<vendor>.* types version with the profile, not with orlog; a
v1.x reader that meets a type no loaded profile registers MUST still read
the event (§A1's G1 does not depend on knowing what a payload means).
Breaking schema change ⇒ v2.0 + a migration command (orlog migrate) that
re-emits the log in the new schema WITH a recorded lineage event — migrations
never edit in place (G1 survives version bumps).

C4. Explicit non-goals for v1.0

Distributed/multi-writer logs; remote/multi-tenant MCP transport beyond the
single-machine loopback daemon of §B12.5 (authentication, TLS and
cross-host access are still out of scope); TypeScript port;
multi-tenant server; graph-embedding retrieval; automatic LLM-judged
verification (contradiction of A5); UI. All deferred, none precluded by the
contracts.