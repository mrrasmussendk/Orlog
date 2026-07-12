# orlog

> Odin feared for Huginn, but worried more for Muninn.
> — *Grímnismál*

**orlog** is a spec-with-a-runtime for trustworthy AI agent memory. The name is
the Norse *ørlǫg*: the immutable layers of what has been laid down, from which
the Norns weave the present. That is the architecture — an append-only,
hash-chained event log, with interpretation continuously re-woven on top of
it, and a deterministic gatekeeper checking every claim before it reaches the
world. Agent knowledge moves through an explicit state machine instead of an
opaque "memory store," so a system built on orlog can always say *why* it
believes something, and can say "I don't know" instead of guessing.

## The state machine

```
OBSERVED ──► INTERPRETED ──► RETRIEVED ──► ASSERTED ──► VERIFIED ──► REINFORCED
(logged)     (projected)     (as-of)       (derived,     (sovereign     (outcome
                                            must cite)     check)         weights)
                                                 │             │
                                                 │             └─ fail ─► RE-DERIVE ─► ABSTAINED
                                                 │                            │
                                                 │                     (retry once,
                                                 │                      then give up)
                                                 └──────────────────────────────────────
                                                    every claim ends VERIFIED or ABSTAINED
```

Every claim a consuming agent can act on is either **VERIFIED** (it passed a
deterministic check, against something outside the memory's own opinion of
itself) or **ABSTAINED** (verification failed twice — once on the original
derivation, once after a single re-derivation attempt — so the system says "I
can't verify this" instead of guessing).

[`docs/DESIGN-PRINCIPLES.md`](docs/DESIGN-PRINCIPLES.md) is the design
rationale (the twelve principles this architecture is built to satisfy).
[`docs/ORLOG-SPEC.md`](docs/ORLOG-SPEC.md) is the normative protocol (v1.0) —
Part A (protocol core) and Part B (the production runtime: MCP server, SQLite
storage, a real LLM `Deriver`, security/PII vault, CLI) are both built and
tested. See "Known deviations" below for the handful of places the code
doesn't match the spec exactly.

## The Norse module map

| Module                | Norse role                              | Layer | What it does |
|------------------------|-----------------------------------------|-------|---------------|
| `urd.py`               | the fixed past — the event log           | L0    | append-only, hash-chained JSONL, ULID ids |
| `storage.py`           | —                                        | L0    | segment rotation (64MB) + the SQLite index, both derivable from the log |
| `verdandi.py`          | what-is-becoming — the projection layer  | L1    | `supersession_chains`: validity windows per fact |
| `verdandi_sessions.py` | —                                        | L1    | `session_summaries`: deterministic per-session digest |
| `retrieval.py`         | —                                        | L2    | point lookup: `retrieve_current_fact` |
| `retrieval_hybrid.py`  | —                                        | L2    | free-text search: lexical + cosine, pluggable `Embedder` |
| `huginn.py`            | thought, sent out for answers — derivation | L3  | `Deriver` protocol, `ScriptedDeriver` (no LLM, deterministic) |
| `huginn_llm.py`        | —                                        | L3    | `LLMDeriver`: real anthropic/openai calls, prompt contract, JSON repair |
| `heimdall.py`          | the watchman at the gate — the verifier  | L4    | checks V1–V4, own ground-truth table (the "windows" backend) |
| `heimdall_pytest.py`   | —                                        | L4    | the "pytest" backend: ground truth = the test suite |
| `verifiers.py`         | —                                        | L4    | the "module:" dynamic-loader extension point |
| `skuld.py`             | what is owed — the outcome ledger        | L5    | records outcomes + importance weighting/decay |
| `muninn.py`            | memory — the route cache Heimdall never trusts on return | cross-cutting | LRU-bounded, gated on re-verification |
| `pipeline.py`          | —                                        | —     | `answer()`: wires cache → verify → retrieve → derive → verify → retry → abstain |
| `states.py`            | the epistemic state machine itself       | —     | legal transitions |
| `vault.py` / `scrub.py`| —                                        | —     | PII tokenization, AES-GCM-encrypted pseudonym registry |
| `config.py`            | —                                        | —     | `orlog.toml` |
| `workspace.py`         | —                                        | —     | directory layout + single-writer lock |
| `runtime.py`           | —                                        | —     | wires a Workspace + Config into a live Pipeline |
| `cli.py`               | —                                        | —     | `orlog init/serve/conformance/replay/inspect/forget` |
| `server.py` / `server_tools.py` | —                               | —     | the MCP server: `remember`/`recall`/`recall_history`/`check_action`/`stats` |
| `observability.py`     | —                                        | —     | structured JSON logs + stats counters |
| `errors.py`            | —                                        | —     | the closed error taxonomy (E_SCHEMA, E_LOCKED, ...) |

The reference domain throughout is point-in-time facts shaped
`{"entity", "attribute", "value"}`. `ScriptedDeriver` is extractive
(deterministic, no LLM call) and ships permanently, per spec, for
API-key-free conformance runs; `LLMDeriver` is the real thing, swapped in via
`orlog.toml`'s `[deriver] backend = "anthropic" | "openai" | "scripted"`.

## Layout

```
docs/DESIGN-PRINCIPLES.md   design rationale
docs/ORLOG-SPEC.md          normative protocol v1.0 — source of truth for src/orlog/
spec/schemas/                JSON Schema contracts for the six layers
src/orlog/models/            pydantic models mirroring the schemas
src/orlog/                   see the module map above
tests/                       one test per contract, plus a unit-test file per module
tests/conformance/           C1 supersession, C2 cache staleness, C3 imperfect projection, C4 citation discipline, C5 immutability & replay
```

## Running the tests

```
python -m pip install -e ".[dev,anthropic,openai]"
pytest
```

## Using the CLI

```
orlog init myworkspace          # scaffold a workspace, prints a vault key to export
export ORLOG_VAULT_KEY=...      # from the line above
cd myworkspace
orlog serve                     # MCP stdio server
orlog replay                    # verify the hash chain, rebuild index.sqlite
orlog inspect <event_id>        # provenance chain
orlog forget <PSEUDONYM_TOKEN>  # crypto-shred
orlog conformance               # run tests/conformance/ against this install
```

## Known deviations from ORLOG-SPEC.md v1.0

- `pipeline.py` is 266 lines, over this project's own ~200-line-per-file
  guideline — it's the central orchestrator wiring every layer together
  plus the optional stats hooks, and splitting it felt riskier than leaving
  it, but it's a candidate for a future cleanup pass.
- `recall`'s `query` argument is "entity.attribute" form, not genuinely free
  text — `retrieval_hybrid.py`'s embedding search is a real, tested library
  capability but isn't wired into the live MCP server this round (see
  `runtime.py`'s module docstring).
- `FastEmbedEmbedder` (the spec-default embedder) is never exercised by the
  test suite — constructing it downloads a model over the network, which
  automated tests deliberately avoid. `HashingEmbedder` (deterministic,
  offline) is what `retrieval_hybrid.py`'s tests actually run against.
- The workspace lock (`workspace.py`) is a plain "does the lock file exist"
  check, not a real OS-level advisory lock — enough to stop a second `orlog
  serve` on the same machine, not a distributed locking primitive.
- `orlog conformance --target self` only supports `self` (shells out to this
  install's own `tests/conformance/`); a remote `--target=<endpoint>` mode
  isn't built, since spec's MCP transport is stdio-only in v1.0.
