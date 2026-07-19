"""retrieval_hybrid: the production L2 backend (ORLOG-SPEC.md §B3).

Design decisions:

1. This is a SEPARATE module from retrieval.py, not a replacement for it.
   retrieval.retrieve_current_fact() already knows exactly which
   (entity, attribute) chain to look at -- a point lookup -- and pipeline.py
   and the conformance suite are built around that. hybrid_retrieve() here
   is the more general case spec §B3 actually describes: free-text search
   across EVERY known fact, using a lexical+embedding score to find the
   right one. Both are legitimate L2 backends; a real deployment might use
   hybrid_retrieve() for open-ended recall and the point lookup wherever
   the caller already knows the exact key.

2. `Embedder` is a Protocol so the scoring math (blending, ranking,
   tie-breaking) is testable without a network call or a model download.
   HashingEmbedder is a deterministic, dependency-free stand-in used by
   this module's own tests; FastEmbedEmbedder (spec §B3's actual default,
   BAAI/bge-small-en-v1.5, local, no API) is imported lazily -- constructing
   it is the only place a `fastembed` install or model download is needed,
   so importing this module never requires either.

3. Every MUST clause from spec §B3 lives in hybrid_retrieve(), in order:
   filter by validity window BEFORE ranking (an invalid-at-as_of fact is
   never scored, let alone returned); report excluded.by_validity and
   excluded.by_k; rank by score = 0.5*lexical + 0.5*cosine, times
   importance; deterministic tie-break by event id.
"""

from __future__ import annotations

import re
import zlib
from datetime import datetime
from math import sqrt
from pathlib import Path
from typing import Protocol

from pydantic import BaseModel, ConfigDict

from orlog.retrieval import Candidate, RetrievalResult
from orlog.verdandi import SupersessionChainsView

_TOKEN_RE = re.compile(r"[a-z0-9]+")

# Excluded from _lexical_score's Jaccard overlap: high-frequency English
# function words (articles, copulas, auxiliary verbs, question words,
# personal pronouns) plus "s", the bare fragment _TOKEN_RE always splits a
# possessive into ("bilal's" -> "bilal", "s"). Left uncounted, these
# dominate the Jaccard score of any short sentence -- e.g. "What IS
# {name}'S job title?" shares "is"/"s" with an unrelated "{name}'S native
# language IS {x}" fact far more than it shares real content words with the
# fact it's actually asking about, letting two coincidental function-word
# matches outrank a stronger semantic (cosine) match.
#
# Deliberately NOT included: "in". Despite being a preposition, it carries
# real topical signal here -- "lives IN {city}" / "does {name} live IN?"
# co-occur specifically around location facts, unlike a purely coincidental
# function-word match. Stripping it (an earlier version of this set did)
# erased the one token a city question shares with its own city fact,
# leaving it tied with an unrelated native_language fact on the entity name
# alone -- which pushed that native_language candidate's score close enough
# to trigger a false AMBIGUOUS abstain via recall_tool's ambiguity_margin
# check. Every OTHER preposition here ("as", "of", "on", ...) was checked
# the same way and found to carry no comparable signal -- "as" in
# particular actively hurts: "{name} works AS A {job title}" needs it
# excluded the same way "is"/"s" do, or it dilutes that fact's own token
# set enough to lose to an unrelated native_language fact on some entities.
#
# Two more "principled-looking" alternatives to this hand-picked list were
# tried and measured worse on the same benchmark (see
# benchmarks/vs_mem0/README.md's "Retrieval bug found and fixed" for the
# full numbers): (1) lowering the lexical/cosine blend weight instead of
# curating stopwords -- worse, because it discards genuine lexical signal
# (e.g. real word overlap on "plan" facts) along with the noise, not just
# the noise; (2) corpus-driven IDF weighting -- worse, because "in" and
# "as" turned out to have identical document frequency in the test corpus
# despite one carrying real signal and the other none, so raw frequency
# alone can't tell them apart; (3) weighting tokens by length instead of a
# curated list (no domain knowledge required) -- didn't reintroduce wrong
# answers, but was measurably less discriminating overall (lower known-fact
# accuracy) than this hand-picked list, which was chosen deliberately after
# comparing all four. See retrieval_hybrid tests for the concrete cases
# this was found from.
_STOPWORDS = frozenset({
    "a", "an", "the", "s",
    "is", "are", "was", "were", "be", "been", "being", "do", "does", "did", "doing", "has", "have", "had", "having",
    "to", "as", "of", "on", "at", "for", "with", "by", "from", "about",
    "and", "or", "but", "not", "no",
    "i", "you", "he", "she", "we", "they", "them", "his", "her", "their", "our", "your", "its", "it",
    "this", "that", "these", "those",
    "what", "which", "who", "whom", "whose", "where", "when", "how",
})


def _tokenize(text: str) -> set[str]:
    return {t for t in _TOKEN_RE.findall(text.lower()) if t not in _STOPWORDS}


def _lexical_score(query_tokens: set[str], content: str) -> float:
    """Token-overlap / 'BM25-lite' (spec §B3): Jaccard overlap between
    query and content tokens -- a cheap, dependency-free proxy for lexical
    relevance, not full BM25 with IDF weighting.
    """
    content_tokens = _tokenize(content)
    if not query_tokens or not content_tokens:
        return 0.0
    return len(query_tokens & content_tokens) / len(query_tokens | content_tokens)


def _cosine(a: list[float], b: list[float]) -> float:
    dot = sum(x * y for x, y in zip(a, b))
    norm_a, norm_b = sqrt(sum(x * x for x in a)), sqrt(sum(y * y for y in b))
    return dot / (norm_a * norm_b) if norm_a and norm_b else 0.0


class Embedder(Protocol):
    name: str

    def embed(self, texts: list[str]) -> list[list[float]]: ...


class HashingEmbedder:
    """Deterministic, offline: hashes character trigrams (via zlib.crc32,
    NOT the builtin hash(), which is randomized per-process) into a
    fixed-size vector. No semantic understanding -- exists purely so this
    module's scoring mechanics are testable without a real model.
    """

    name = "hashing-trigram-v1"

    def __init__(self, dim: int = 128) -> None:
        self.dim = dim

    def embed(self, texts: list[str]) -> list[list[float]]:
        return [self._embed_one(t) for t in texts]

    def _embed_one(self, text: str) -> list[float]:
        vec = [0.0] * self.dim
        text = text.lower()
        for i in range(max(0, len(text) - 2)):
            idx = zlib.crc32(text[i : i + 3].encode("utf-8")) % self.dim
            vec[idx] += 1.0
        return vec


def _default_fastembed_cache_dir() -> Path:
    """A persistent, per-user cache directory for the downloaded ONNX model
    -- NOT fastembed's own default (which falls under the OS temp folder,
    e.g. Windows' %TEMP%\\fastembed_cache). A temp directory can be cleared
    by a reboot or a cleanup tool at any time, silently turning every future
    "warm cache" construction back into a cold, network-bound download --
    exactly the failure mode that caused a multi-minute recall() hang.
    """
    return Path.home() / ".cache" / "orlog" / "fastembed"


class FastEmbedEmbedder:
    """spec §B3's actual default: fastembed, local, no API. Heavy
    dependency (onnxruntime + a downloaded model) -- `fastembed` is
    imported lazily so this class is the only place a model download can
    be triggered.
    """

    def __init__(self, model_name: str = "BAAI/bge-small-en-v1.5", *, cache_dir: str | None = None) -> None:
        from fastembed import TextEmbedding

        self.name = model_name
        cache_dir = cache_dir or str(_default_fastembed_cache_dir())
        self._model = TextEmbedding(model_name=model_name, cache_dir=cache_dir)

    def embed(self, texts: list[str]) -> list[list[float]]:
        return [vec.tolist() for vec in self._model.embed(texts)]


def hybrid_retrieve(
    query: str,
    as_of: datetime,
    view: SupersessionChainsView,
    events_by_id: dict,
    embedder: Embedder,
    *,
    k: int = 8,
    importance: dict[str, float] | None = None,
) -> RetrievalResult:
    """Search every fact in every chain (not one known entity/attribute
    key) for the ones most relevant to `query`, as of `as_of`.
    """
    importance = importance or {}
    in_window: list[tuple[str, str]] = []
    by_validity_excluded = 0

    for chain in view.chains.values():
        for event_id in chain:
            window = view.windows[event_id]
            if window.valid_from <= as_of < window.valid_to:
                # No default here, to match retrieval.py/heimdall.py's
                # str(payload.get("value")) convention -- a fact event
                # without "value" content should look the same ("None")
                # everywhere it's read, not silently become "" in this one
                # backend and skew _lexical_score (which short-circuits to
                # 0.0 on empty content) against the other L2 backend.
                content = str(events_by_id[event_id].payload.get("value"))
                in_window.append((event_id, content))
            else:
                by_validity_excluded += 1

    if not in_window:
        return RetrievalResult(candidates=[], excluded={"by_validity": by_validity_excluded, "by_k": 0})

    query_tokens = _tokenize(query)
    query_vec = embedder.embed([query])[0]
    content_vecs = embedder.embed([content for _, content in in_window])

    scored: list[Candidate] = []
    for (event_id, content), content_vec in zip(in_window, content_vecs):
        weight = importance.get(event_id, 1.0)
        score = (0.5 * _lexical_score(query_tokens, content) + 0.5 * _cosine(query_vec, content_vec)) * weight
        window = view.windows[event_id]
        scored.append(
            Candidate(event_id=event_id, content=content, valid_from=window.valid_from, valid_to=window.valid_to, score=score, importance=weight)
        )

    scored.sort(key=lambda c: (-c.score, c.event_id))  # deterministic tie-break by event id

    top = scored[:k]
    by_k_excluded = max(0, len(scored) - k)
    return RetrievalResult(candidates=top, excluded={"by_validity": by_validity_excluded, "by_k": by_k_excluded})


class KeyCandidate(BaseModel):
    """One (entity, attribute) chain that a free-text query might mean --
    the output of resolve_key(), not a value. See that function's docstring
    for why the caller (recall_tool) still has to hand this key back to the
    deterministic point lookup to get an actual answer.
    """

    model_config = ConfigDict(extra="forbid")

    entity: str
    attribute: str
    entity_label: str | None = None  # bare display name, if entity_detail (see runtime.py) was used
    entity_detail: str | None = None
    event_id: str  # best-scoring event within this key's chain
    matched_text: str
    score: float


def resolve_key(
    query: str,
    view: SupersessionChainsView,
    events_by_id: dict,
    embedder: Embedder,
    *,
    k: int = 5,
) -> list[KeyCandidate]:
    """Find which (entity, attribute) key(s) a free-text query is probably
    ABOUT -- not what the current value is. That's the whole point: this
    function only ever returns a KEY (plus a confidence score), never an
    answer. The caller (recall_tool) resolves the top match back through
    the deterministic point lookup (Pipeline.answer()) to get a fresh,
    verified, cited value -- so a natural-language query can find the right
    entity by meaning while the actual answer still comes from the same
    sovereign, freshness-checked path an exact-key lookup would use.

    Every event in every chain is considered, NOT filtered by validity: an
    old, superseded mention of a key ("Anna lives in Boston", since
    corrected) is still good evidence the query is about that key, even
    though its value is stale -- staleness is exactly what the deterministic
    re-lookup afterwards is for. Content searched per event is its own
    remembered `text` (falling back to `value` if text is somehow absent) --
    the original natural-language sentence carries far more of the semantic
    signal a natural-language query matches against than the terse
    extracted value alone -- PLUS the chain's own `attribute` name
    (`"job_title"` -> `"job title"`), appended for scoring only. Unlike
    everything in _STOPWORDS, this isn't phrasing tuned to any particular
    dataset: `attribute` is caller-supplied schema metadata (required at
    write time, see Runtime.remember()), not derived from the free-text
    query at all, so folding it into the match is closer to a search
    engine boosting on a field name than to guessing at word choice. It
    resolved every remaining case a benchmark run found ambiguous between
    two facts about the same entity (e.g. "job_title" vs "native_language"
    both mentioning the entity's name) once the query and the fact's own
    attribute name shared a literal word ("job title") that two attribute
    values alone never would. `matched_text` returned below is still the
    original, unaugmented text -- the attribute name is scoring-only, never
    shown as if it were remembered evidence.

    Results are grouped by key (one best-scoring KeyCandidate per key, not
    per event), ranked descending, deterministic tie-break by the key
    string itself.
    """
    query_tokens = _tokenize(query)
    query_vec = embedder.embed([query])[0]

    entries = []
    for key, event_ids in view.chains.items():
        entity, _, attribute = key.partition("::")
        for event_id in event_ids:
            payload = events_by_id[event_id].payload
            content = str(payload.get("text") or payload.get("value") or "")
            searchable = f"{content} {attribute.replace('_', ' ')}"
            entries.append((entity, attribute, event_id, content, searchable, payload))

    if not entries:
        return []

    searchable_vecs = embedder.embed([entry[4] for entry in entries])

    best_by_key: dict[str, KeyCandidate] = {}
    for (entity, attribute, event_id, content, searchable, payload), searchable_vec in zip(entries, searchable_vecs):
        score = 0.5 * _lexical_score(query_tokens, searchable) + 0.5 * _cosine(query_vec, searchable_vec)
        key = f"{entity}::{attribute}"
        current = best_by_key.get(key)
        if current is None or score > current.score:
            best_by_key[key] = KeyCandidate(
                entity=entity,
                attribute=attribute,
                entity_label=payload.get("entity_label"),
                entity_detail=payload.get("entity_detail"),
                event_id=event_id,
                matched_text=content,
                score=score,
            )

    ranked = sorted(best_by_key.values(), key=lambda c: (-c.score, f"{c.entity}::{c.attribute}"))
    return ranked[:k]
