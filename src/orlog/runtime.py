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
   value}. `remember()` REQUIRES entity, attribute, AND value together
   (alongside free `text`) -- every event this reference server appends is
   a "fact" the Pipeline (point-in-time entity.attribute lookups) can
   answer about. A bare-text write (any of the three omitted) used to be
   silently accepted and durably appended (G1/G2 still held) but was
   permanently unretrievable through recall()/recall_history() -- a
   dead-end memory with no error. `remember()` now raises SchemaError
   instead, at write time, per the same "fail visible, not plausible"
   discipline as `_enforce_schema`/`_enforce_entity_detail` below.

   A non-dotted `recall()` query DOES now fall back to genuinely free-text
   recall, via retrieval_hybrid's resolve_key() (see server_tools.py):
   the embedding search only ever resolves which (entity, attribute) key a
   query is about, never the answer itself -- Pipeline.answer() is still
   what serves the deterministic, freshness-checked, cited value for that
   resolved key. `recall`'s `query` argument therefore has two valid forms:
   an exact "entity.attribute" key (e.g. "user:42.email", matching the
   convention Pipeline.answer() builds internally -- unchanged, fast path,
   no semantic search involved) or free text with no "." at all.

3. `Runtime.embedder` is built lazily (see `_build_embedder()` below) --
   only the first time the free-text fallback path actually runs -- so a
   workspace/test that never exercises it never pays for constructing one,
   matching retrieval_hybrid.FastEmbedEmbedder's own lazy `fastembed` import.

   That first build can be network-bound (a cold FastEmbedEmbedder without
   a warm model cache has to download it), so it runs on a worker thread
   with a hard wall-clock timeout (`config.retrieval.embedder_build_timeout_s`)
   rather than directly on the caller's thread: a stdio MCP server handles
   one call at a time, so an unbounded hang here blocks every other tool
   call behind it, not just this one recall(). Exceeding the timeout raises
   RetrieverUnavailableError -- server_tools.recall_tool turns that into an
   honest EMBEDDER_UNAVAILABLE abstention, never a silent multi-minute stall.

4. `remember()` computes and stores a `self_supported` verdict on any fact
   payload (does this fact's own remembered text support the value it's
   being recorded with?) -- checked ONCE, here, at write time. A read that
   later hits a self-contradictory fact (Pipeline.answer(), via
   heimdall.GroundTruthFact.self_supported) fails fast with
   UNSUPPORTED_BY_SOURCE and never even attempts a derive() call for it --
   a poisoned fact is a permanent, instant verification failure for every
   future reader, not a fresh (and for a real LLM deriver, potentially
   network-bound) derive+verify round trip repeated on every recall().
   Pipeline.answer() additionally bounds every derive() attempt it DOES
   make with its own hard wall-clock ceiling (`config.verifier.answer_timeout_s`,
   VERIFY_TIMEOUT on expiry) -- a backstop for any OTHER failure mode this
   write-time check doesn't cover, so a read can never block indefinitely.
"""

from __future__ import annotations

import threading
from datetime import datetime, timezone

from orlog.config import OrlogConfig
from orlog.errors import (
    ChainBrokenError,
    RetrieverUnavailableError,
    SchemaError,
    VerifierUnavailableError,
)
from orlog.heimdall import Heimdall, build_ground_truth
from orlog.huginn import ScriptedDeriver
from orlog.models.event import EventDraft
from orlog.muninn import RouteCache
from orlog.observability import Stats
from orlog.pipeline import Pipeline
from orlog.scrub import detect_pii_kinds, scrub
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


def normalize_ws(s: str) -> str:
    """Collapse all whitespace runs to single spaces and strip the ends, so
    an evidence_span that differs from its source text only in incidental
    formatting (a line break, doubled spaces) isn't rejected as ungrounded.
    """
    return " ".join(s.split())


def _build_embedder(config: OrlogConfig):
    """"hashing" selects the dependency-free HashingEmbedder (an explicit,
    offline opt-out); anything else -- including this project's own default,
    "BAAI/bge-small-en-v1.5" (spec §B3) -- is treated as a fastembed model
    name, lazily importing FastEmbedEmbedder (the only place a real model
    download can be triggered).
    """
    if config.retrieval.embedder == "hashing":
        from orlog.retrieval_hybrid import HashingEmbedder

        return HashingEmbedder()
    from orlog.retrieval_hybrid import FastEmbedEmbedder

    return FastEmbedEmbedder(model_name=config.retrieval.embedder)


class Runtime:
    """One open workspace: log, index, vault, cache, ledger, stats."""

    def __init__(self, workspace: Workspace, config: OrlogConfig) -> None:
        workspace.scaffold()
        self.workspace = workspace
        self.config = config
        self.log = SegmentedLog(workspace.events_dir)
        # spec §B2's startup check. verify_chain(full=False) documented
        # itself as the startup check but had no caller anywhere except
        # `orlog replay`, so a workspace whose chain was broken -- by
        # tampering, or by the unlocked-CLI-writer race -- was served
        # normally, answering every recall with verified=True citations
        # drawn from a log nobody had checked. Only the newest segment is
        # walked, to keep startup O(one segment) rather than O(whole log);
        # `orlog replay` remains the full-chain check.
        if not self.log.verify_chain():
            raise ChainBrokenError(
                f"hash chain is broken in {workspace.events_dir} -- refusing to serve from a log "
                "whose integrity cannot be established. Run `orlog replay` to see the full extent."
            )
        self.index = EventIndex(workspace.index_path)
        self.vault = Vault(workspace.vault_path)
        self.cache = RouteCache(max_entries=config.cache.max_entries)
        self.ledger = OutcomeLedger(self.log, index=self.index)
        self.stats = Stats()
        self.deriver = _build_deriver(config)
        self._embedder = None

    @property
    def embedder(self):
        """Lazily built, on a worker thread with a hard timeout -- see
        module docstring point 3. Raises RetrieverUnavailableError (never
        hangs past config.retrieval.embedder_build_timeout_s) if the build
        doesn't finish in time; a later call tries again from scratch (the
        first attempt is left to finish or die on its own thread -- Python
        can't forcibly kill it, but it's harmless once abandoned).
        """
        if self._embedder is None:
            # A plain daemon thread, not concurrent.futures.ThreadPoolExecutor:
            # an Executor's shutdown(wait=True) -- including via its own
            # __exit__ -- blocks on the very thread we're trying to time out
            # on, and even shutdown(wait=False) still gets joined by
            # concurrent.futures' own atexit hook before the process can
            # exit. A daemon thread is abandoned outright at interpreter
            # exit, so a genuinely hung build never blocks server shutdown.
            outcome: dict = {}

            def _build() -> None:
                try:
                    outcome["embedder"] = _build_embedder(self.config)
                except Exception as exc:  # re-raised on the caller's thread below
                    outcome["error"] = exc

            thread = threading.Thread(target=_build, daemon=True)
            thread.start()
            thread.join(timeout=self.config.retrieval.embedder_build_timeout_s)
            if thread.is_alive():
                raise RetrieverUnavailableError(
                    f"embedder {self.config.retrieval.embedder!r} did not become ready within "
                    f"{self.config.retrieval.embedder_build_timeout_s}s"
                )
            if "error" in outcome:
                error = outcome["error"]
                # Every caller (cli.cmd_serve, server_tools' free-text path)
                # catches RetrieverUnavailableError and degrades to
                # exact-key recall. They caught only that, though, while a
                # build can fail as well as hang: a cold fastembed cache
                # with no network RAISES (download error), and a missing
                # extra raises ModuleNotFoundError. Either one escaped as
                # itself, so the server failed to start at all instead of
                # serving exact-key recalls, and free-text recall crashed
                # rather than abstaining EMBEDDER_UNAVAILABLE.
                if not isinstance(error, RetrieverUnavailableError):
                    raise RetrieverUnavailableError(
                        f"embedder {self.config.retrieval.embedder!r} could not be built: "
                        f"{type(error).__name__}: {error}"
                    ) from error
                raise error
            self._embedder = outcome["embedder"]
        return self._embedder

    def remember(
        self,
        text: str,
        *,
        occurred_at: datetime | None = None,
        recorded_at: datetime | None = None,
        event_type: str = "fact",
        actor: str = "user",
        entity: str | None = None,
        attribute: str | None = None,
        value: str | None = None,
        entity_detail: str | None = None,
        evidence_span: str | None = None,
        paraphrased_value: bool = False,
        register_new_type: bool = False,
        register_new_attribute: bool = False,
    ):
        # entity/attribute/value are now REQUIRED together, not optional: a
        # bare-text write (any of the three missing) used to be silently
        # accepted and durably appended, but was permanently unretrievable
        # through recall()/recall_history() in this reference server -- a
        # dead-end memory with no error and no way to notice it happened.
        # Rejecting it at write time is the same "fail visible, not
        # plausible" discipline as _enforce_schema/_enforce_entity_detail
        # below: an actionable error now, instead of a silent, permanent gap
        # discovered only much later at read time.
        if not entity or not attribute or not value:
            raise SchemaError(
                "remember() requires entity, attribute, and value together to store a queryable "
                "fact -- text-only memories are not retrievable via recall()/recall_history() in "
                f"this reference server (got entity={entity!r}, attribute={attribute!r}, value={value!r})."
            )
        # entity/attribute are structured lookup keys and are deliberately
        # NOT scrubbed -- tokenizing them would break exact-match
        # recall("entity.attribute"). That makes them the one way PII can
        # reach the immutable log in cleartext, where vault.forget()'s
        # crypto-shredding can never reach it, and from there into every
        # LLM prompt (pipeline builds the query as "entity.attribute" and
        # huginn_llm sends it verbatim). Since it cannot be scrubbed, it is
        # refused: an actionable error at write time beats an unerasable
        # identifier in an append-only log.
        self._reject_pii_in_key("entity", entity)
        self._reject_pii_in_key("attribute", attribute)
        occurred_at = occurred_at or datetime.now(timezone.utc)
        scrubbed_text = scrub(text, self.vault, detectors=self.config.privacy.detectors)
        payload: dict = {"text": scrubbed_text}
        self._enforce_schema(entity, attribute, register_new_type=register_new_type, register_new_attribute=register_new_attribute)
        self._enforce_entity_detail(entity, entity_detail)
        # `value` is caller-supplied free-form fact content and can carry
        # the same PII shapes `text` can (e.g. value="alice@example.com")
        # -- it MUST go through the same scrub() pass, or it lands in the
        # immutable log in cleartext, un-tokenized and therefore
        # unreachable by vault.forget()'s crypto-shredding (scrub.py's
        # own module docstring: "nothing downstream of this module
        # should see unscrubbed text"). entity/attribute are structured
        # lookup keys, not prose, and stay as given -- scrubbing them
        # would break exact-match recall("entity.attribute") queries.
        scrubbed_value = scrub(value, self.vault, detectors=self.config.privacy.detectors)
        # `evidence_span` is the caller's literal quote from `text` that
        # grounds `value` -- a SEPARATE string from `value` itself, so
        # `value` is free to be a clean/normalized/paraphrased extraction
        # (e.g. value="loves Porto" from text "...adores the city of
        # Porto...") without that paraphrase itself having to appear
        # verbatim in text. It goes through the same scrub() pass as text
        # for the same reason value does (vault.tokenize is stable per
        # input, so a PII span scrubbed independently here still matches
        # the identically-tokenized occurrence inside scrubbed_text).
        # Ungrounded per spec: fail visible, at write time, not a fact that
        # silently poisons every future recall() with UNSUPPORTED_BY_SOURCE.
        scrubbed_span = scrub(evidence_span, self.vault, detectors=self.config.privacy.detectors) if evidence_span is not None else None
        if scrubbed_span is not None and normalize_ws(scrubbed_span) not in normalize_ws(scrubbed_text):
            raise SchemaError(
                f"SPAN_NOT_IN_SOURCE: evidence_span {evidence_span!r} does not appear in text {text!r} -- "
                "quote the source text verbatim (whitespace differences are ok), don't paraphrase or fabricate it."
            )
        # `entity_detail` disambiguates two entities that share a bare
        # name (the "which Anna" problem) -- it becomes PART OF the
        # chain-grouping key itself (verdandi/heimdall need no changes,
        # since both already group by whatever literal string lands in
        # payload["entity"]), while the bare name and detail are also
        # kept separately for display/disambiguation output. Like
        # entity/attribute, this is a structured qualifier, not prose,
        # so it is NOT scrubbed -- same reasoning as above.
        stored_entity = f"{entity}#{entity_detail}" if entity_detail else entity
        payload.update(entity=stored_entity, attribute=attribute, value=scrubbed_value)
        # Write-time self-consistency check (heimdall.GroundTruthFact.self_supported):
        # does this fact's own remembered text actually support the value
        # it's being recorded with? When the caller supplies evidence_span,
        # the span's presence in text has already been validated above (a
        # fabricated span raised SchemaError instead of ever reaching here)
        # and the span itself -- not `value` -- is what a citation excerpt
        # is built from (see pipeline._citations_from); what remains to
        # check here is whether the span supports the value.
        # Without evidence_span, this falls back to the legacy check (value
        # itself must appear verbatim in text) for callers that haven't
        # migrated yet. A caller-contradicted legacy fact (e.g. text says
        # "Paris", value says "Tokyo") is then a fast, permanent,
        # correctly-labeled verification failure for every future reader
        # instead of a fresh derive+verify round trip on every recall().
        if scrubbed_span is not None:
            payload["evidence_span"] = scrubbed_span
            # A valid span proves the QUOTE is real. It does not prove the
            # quote supports `value` -- and those came apart badly: a span
            # is only checked against `text`, so
            #   text="Anna hates Porto...", value="loves Porto",
            #   evidence_span="Anna hates Porto"
            # recorded self_supported=True and made recall() serve
            # verified=True with a citation excerpt contradicting its own
            # claim. Supplying a span was, in effect, an opt-out of the
            # write-time grounding check that a span-less write must pass.
            #
            # Literal grounding (the value appears in its own quote) is
            # checkable, so it is checked. A genuine paraphrase
            # ("adores the city of Porto" -> "loves Porto") is NOT
            # deterministically checkable, so it is not guessed at: the
            # caller must say so, and that assertion is recorded on the
            # event rather than being indistinguishable from real grounding.
            grounded = normalize_ws(scrubbed_value) in normalize_ws(scrubbed_span)
            if not grounded and not paraphrased_value:
                raise SchemaError(
                    f"VALUE_NOT_IN_SPAN: value {value!r} does not appear in evidence_span "
                    f"{evidence_span!r}, so the span does not show where the value came from. "
                    "Quote a span that contains the value, or pass paraphrased_value=True to "
                    "record on the event that this is a caller-asserted paraphrase."
                )
            payload["self_supported"] = True
            if not grounded:
                payload["value_paraphrased"] = True
        else:
            payload["self_supported"] = scrubbed_value in scrubbed_text
        if entity_detail:
            payload["entity_label"] = entity
            payload["entity_detail"] = entity_detail
        draft = EventDraft(occurred_at=occurred_at, actor=actor, type=event_type, payload=payload)
        event = self.log.append(draft, recorded_at=recorded_at)
        self.index.index_event(event, segment=self._current_segment_name(), offset=self.log.current_segment_offset())
        self.stats.record_append()
        return event

    def _reject_pii_in_key(self, field: str, value: str) -> None:
        """Refuse an entity/attribute that carries detectable PII.

        Deliberately narrow: only the unambiguous shapes (email, SSN/CPR,
        credential) the regex pack already recognizes. A bare name is not
        detectable and is not what this is for -- the target is
        `entity="user:alice@example.com"`, which puts an email address
        beyond the reach of erasure forever.
        """
        detectors = self.config.privacy.detectors
        if detectors is not None and "regex" not in detectors:
            return  # scrubbing is off entirely; respect that
        detected = detect_pii_kinds(value)
        if detected:
            raise SchemaError(
                f"PII_IN_KEY: {field}={value!r} looks like it contains {', '.join(sorted(detected))}. "
                f"{field} is a lookup key, so it is never scrubbed -- storing it would put that "
                "identifier in the append-only log in cleartext, unreachable by `orlog forget`. "
                "Use a stable opaque id instead (e.g. entity='user:1'), and put the identifying "
                "detail in text/value, where it is tokenized, or in entity_detail."
            )

    def _enforce_schema(
        self, entity: str, attribute: str, *, register_new_type: bool, register_new_attribute: bool
    ) -> None:
        """spec §B9/§B10: schema-on-write, opt-in via config.schema_.known_types.

        A no-op (nothing to enforce) when the entity has no "type:label"
        convention at all, or when known_types is empty -- the feature's
        default-off state, required so every entity string this codebase's
        own test suite and any pre-existing workspace already use keeps
        working unmodified. Once a workspace DOES declare known_types,
        register_new_type/register_new_attribute are the caller's explicit
        opt-in to grow the registry, rather than a silent auto-accept that
        risks a typo becoming a permanent, undetected divergent key.

        A type is "known" if it's declared in config OR already used by a
        prior fact in the log -- register_new_type=true's effect is durable
        (this write's own append is what makes the type show up in the log
        scan on every later call), not a one-time bypass that would have to
        be repeated on every future write of that type. A type's first-ever
        attribute is always accepted without register_new_attribute=true --
        there is nothing yet on record for that type to diverge FROM; only
        once at least one attribute exists does a genuinely new one need the
        explicit flag.
        """
        entity_type, sep, _ = entity.partition(":")
        declared_types = self.config.schema_.known_types
        if not sep or not declared_types:
            return
        known_types = set(declared_types) | self._known_types_in_log()
        if entity_type not in known_types:
            if register_new_type:
                return  # this write's own append registers the type (and its attribute)
            raise SchemaError(
                f"{entity_type!r} is not a known entity type. Known: {', '.join(sorted(known_types))}. "
                "Pass register_new_type=true to register it."
            )
        known_attributes = self._known_attributes_for_type(entity_type)
        if known_attributes and attribute not in known_attributes and not register_new_attribute:
            raise SchemaError(
                f"{attribute!r} is not a known attribute for type {entity_type!r}. "
                f"Known: {', '.join(sorted(known_attributes))}. "
                "Pass register_new_attribute=true to register it."
            )

    def _known_types_in_log(self) -> set[str]:
        types: set[str] = set()
        for event in self.log.read_all():
            if event.type != "fact":
                continue
            ent = event.payload.get("entity")
            if ent is None:
                continue
            entity_type, sep, _ = ent.partition(":")
            if sep:
                types.add(entity_type)
        return types

    def _known_attributes_for_type(self, entity_type: str) -> set[str]:
        """The log IS the attribute registry: any attribute already used by a
        fact whose entity's type prefix matches is "known" -- no separate
        persisted list to fall out of sync with what was actually written.
        Mirrors server_tools.recall_history_tool's own raw-log-scan pattern;
        consistent with this reference implementation's existing "re-derive
        from the log every time" posture (module docstring point 1).
        """
        attributes: set[str] = set()
        for event in self.log.read_all():
            if event.type != "fact":
                continue
            ent = event.payload.get("entity")
            attr = event.payload.get("attribute")
            if ent is None or attr is None:
                continue
            if ent.split(":", 1)[0] == entity_type:
                attributes.add(attr)
        return attributes

    def _enforce_entity_detail(self, entity: str, entity_detail: str | None) -> None:
        """spec §B9: entity_detail is required once a bare label has already
        been disambiguated at least once. No config gate -- this only ever
        activates once a caller has itself supplied entity_detail for this
        label before, so a workspace that never uses entity_detail sees zero
        behavior change (existing_details stays empty forever for it).

        Supplying a detail that matches one on record is a continuation of
        that same entity (e.g. adding another attribute to "anna#my
        sister"); a detail that doesn't match any on record is a legitimate,
        distinct entity newly introduced under the same label. Both are
        allowed -- only an *absent* detail is rejected, since that's the one
        case this codebase cannot tell apart from silently colliding with
        whichever disambiguated entity happens to come back first.
        """
        existing = self._known_details_for_label(entity)
        if not existing or entity_detail is not None:
            return
        first = sorted(existing)[0]
        extra = f" (and {len(existing) - 1} more)" if len(existing) > 1 else ""
        raise SchemaError(f"{entity!r} exists with detail={first!r}{extra}. Supply a distinguishing entity_detail.")

    def _known_details_for_label(self, label: str) -> set[str]:
        details: set[str] = set()
        for event in self.log.read_all():
            if event.type != "fact":
                continue
            if event.payload.get("entity_label") == label and event.payload.get("entity_detail"):
                details.add(event.payload["entity_detail"])
        return details

    def _current_segment_name(self) -> str:
        return self.log._current.path.name

    def _build_verifier(self, truth: dict, *, truth_version: str):
        """spec §B11's three backends: "windows", "pytest", "module:...".

        This dispatch did not exist -- Heimdall was hardcoded here and
        `config.verifier.backend` was read by nothing in the entire package,
        so a workspace configuring `[verifier] backend = "pytest"` (or a
        custom law-database verifier, spec §B11's own worked example)
        validated, warned nothing, and was silently gated by the windows
        verifier instead. An operator believing their own verifier was the
        gate, when it was not, is the worst possible way for this setting to
        fail.
        """
        backend = self.config.verifier.backend
        if backend == "windows":
            return Heimdall(truth, truth_version=truth_version)
        if backend == "pytest":
            from orlog.heimdall_pytest import PytestVerifier

            return PytestVerifier(
                {eid: fact.model_dump() for eid, fact in truth.items()},
                cwd=self.workspace.root,
            )
        if backend.startswith("module:"):
            from orlog.verifiers import load_verifier_from_module

            return load_verifier_from_module(backend, truth=truth, truth_version=truth_version)
        raise VerifierUnavailableError(
            f"unknown verifier backend {backend!r} -- expected 'windows', 'pytest', "
            "or 'module:package.module.ClassName'"
        )

    def build_pipeline(
        self, *, now: datetime | None = None, known_as_of: datetime | None = None
    ) -> Pipeline | None:
        """Returns None if the log has no "fact" events yet -- there is
        nothing to project (verdandi raises ValueError on zero events).

        `known_as_of` is the TRANSACTION-time horizon: only facts already
        recorded by that instant are projected. It is the second time axis,
        independent of the valid-time `as_of` a caller passes to
        Pipeline.answer(). Together they answer "what did this system
        believe on day X about day Y" -- as_of alone cannot, because a
        correction appended later supersedes on the valid-time axis and so
        silently rewrites the answer to a question about the past.

        This is a filter, not a prefix slice: a backfilled recorded_at need
        not rise with append order (see EventLog.append).

        The horizon is folded into truth_version because that string is
        part of the answer cache key (pipeline.answer -> cache_key). Two
        horizons can otherwise project different views yet collide on
        built_from -- the last event in log order can be the same event for
        both -- and the second would then be served the first's cached
        answer. It also makes every Answer self-describing about the
        horizon it was computed under.
        """
        now = now or datetime.now(timezone.utc)
        events = self.log.read_all()
        fact_events = [
            e for e in events
            if e.type == "fact" and e.payload.get("entity") is not None and e.payload.get("attribute") is not None
            and (known_as_of is None or e.recorded_at <= known_as_of)
        ]
        if not fact_events:
            return None

        view, pv = build_supersession_chains(fact_events, builder="orlog-runtime", built_at=now)
        truth = build_ground_truth(fact_events)
        truth_version = f"windows@{pv.built_from}"
        if known_as_of is not None:
            truth_version += f"|known@{known_as_of.isoformat()}"
        verifier = self._build_verifier(truth, truth_version=truth_version)
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
            answer_timeout_s=self.config.verifier.answer_timeout_s,
        )

    def close(self) -> None:
        self.index.close()
        self.vault.close()
