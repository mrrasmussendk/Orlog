"""runtime: wires an open Workspace + Config into a live Pipeline
(shared by cli.py and server.py -- ORLOG-SPEC.md §B1/§B9).

Design decisions:

1. `Runtime.build_pipeline()` re-projects from the CURRENT log on every
   call rather than caching a projection across calls -- "commit as late as
   possible" (DESIGN-PRINCIPLES.md principle 2). This reference
   implementation re-projects the whole log every time, which is fine at
   demo scale and honestly not a production strategy; a real deployment
   would rebuild on the triggers spec §B5 lists (explicit, builder version
   change, quality drop), not on every call.

2. This reference server's queryable domain is still the "fact" convention
   established since the very first milestone: payload {entity, attribute,
   value}. `remember()` accepts entity/attribute/value as optional
   structured fields alongside free `text` -- when given, the event is a
   "fact" the Pipeline (point-in-time entity.attribute lookups) can answer
   about; free-text-only memories are still durably appended (G1/G2 hold),
   but are not retrievable through recall() in this reference server. A
   genuinely free-text recall would mean wiring retrieval_hybrid's
   embedding search into Pipeline as a pluggable retriever -- a real
   capability this project has (see retrieval_hybrid.py), just not wired
   into the live server this round. `recall`'s `query` argument is
   therefore expected in "entity.attribute" form (e.g. "user:42.email"),
   matching the same convention Pipeline.answer() already builds
   internally.
"""

from __future__ import annotations

from datetime import datetime, timezone

from orlog.config import OrlogConfig
from orlog.heimdall import Heimdall, build_ground_truth
from orlog.huginn import ScriptedDeriver
from orlog.models.event import EventDraft
from orlog.muninn import RouteCache
from orlog.observability import Stats
from orlog.pipeline import Pipeline
from orlog.scrub import scrub
from orlog.skuld import OutcomeLedger
from orlog.storage import EventIndex, SegmentedLog
from orlog.vault import Vault
from orlog.verdandi import build_supersession_chains
from orlog.workspace import Workspace


def _build_deriver(config: OrlogConfig):
    if config.deriver.backend == "scripted":
        return ScriptedDeriver()
    if config.deriver.backend == "anthropic":
        from orlog.huginn_llm import AnthropicCompletion, LLMDeriver

        completion = AnthropicCompletion(model=config.deriver.model, api_key_env=config.deriver.api_key_env, timeout_s=config.deriver.timeout_s)
        return LLMDeriver(completion, model=config.deriver.model, max_tokens=config.deriver.max_tokens, timeout_s=config.deriver.timeout_s)
    if config.deriver.backend == "openai":
        from orlog.huginn_llm import LLMDeriver, OpenAICompletion

        completion = OpenAICompletion(model=config.deriver.model, api_key_env=config.deriver.api_key_env, timeout_s=config.deriver.timeout_s)
        return LLMDeriver(completion, model=config.deriver.model, max_tokens=config.deriver.max_tokens, timeout_s=config.deriver.timeout_s)
    raise ValueError(f"unknown deriver backend {config.deriver.backend!r}")


class Runtime:
    """One open workspace: log, index, vault, cache, ledger, stats."""

    def __init__(self, workspace: Workspace, config: OrlogConfig) -> None:
        workspace.scaffold()
        self.workspace = workspace
        self.config = config
        self.log = SegmentedLog(workspace.events_dir)
        self.index = EventIndex(workspace.index_path)
        self.vault = Vault(workspace.vault_path)
        self.cache = RouteCache(max_entries=config.cache.max_entries)
        self.ledger = OutcomeLedger(self.log, index=self.index)
        self.stats = Stats()
        self.deriver = _build_deriver(config)

    def remember(
        self,
        text: str,
        *,
        occurred_at: datetime | None = None,
        event_type: str = "fact",
        actor: str = "user",
        entity: str | None = None,
        attribute: str | None = None,
        value: str | None = None,
    ):
        occurred_at = occurred_at or datetime.now(timezone.utc)
        scrubbed_text = scrub(text, self.vault, detectors=self.config.privacy.detectors)
        payload: dict = {"text": scrubbed_text}
        if entity is not None and attribute is not None and value is not None:
            payload.update(entity=entity, attribute=attribute, value=value)
        draft = EventDraft(occurred_at=occurred_at, actor=actor, type=event_type, payload=payload)
        event = self.log.append(draft)
        self.index.index_event(event, segment=self._current_segment_name(), offset=0)
        self.stats.record_append()
        return event

    def _current_segment_name(self) -> str:
        return self.log._current.path.name

    def build_pipeline(self, *, now: datetime | None = None) -> Pipeline | None:
        """Returns None if the log has no "fact" events yet -- there is
        nothing to project (verdandi raises ValueError on zero events).
        """
        now = now or datetime.now(timezone.utc)
        events = self.log.read_all()
        fact_events = [
            e for e in events
            if e.type == "fact" and e.payload.get("entity") is not None and e.payload.get("attribute") is not None
        ]
        if not fact_events:
            return None

        view, pv = build_supersession_chains(fact_events, builder="orlog-runtime", built_at=now)
        truth = build_ground_truth(fact_events)
        verifier = Heimdall(truth, truth_version=f"windows@{pv.built_from}")
        return Pipeline(
            view=view,
            events_by_id={e.id: e for e in fact_events},
            projection_version=pv,
            cache=self.cache,
            deriver=self.deriver,
            verifier=verifier,
            ledger=self.ledger,
            pass_bonus=self.config.adaptation.pass_bonus,
            stats=self.stats,
        )

    def close(self) -> None:
        self.index.close()
        self.vault.close()
