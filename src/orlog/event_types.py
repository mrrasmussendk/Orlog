"""event_types: the event-type registry (ORLOG-SPEC.md §B12.1).

Core owns a small, fixed vocabulary -- "fact", the outcome.* types skuld
writes, and x.orlog.forget. A *profile* (plugins.py) adds its own types
under the vendor-namespaced `x.<vendor>.*` prefix, each with a pydantic
model for its payload, through the `orlog.event_types` entry-point group.
That is the whole seam: a profile can put typed, validated events of its
own into the log without a line of core changing.

Design decisions:

1. The namespace rule is enforced, not merely documented. A registered name
   MUST match `x.<vendor>.<name>` and MUST NOT claim the `orlog` vendor.
   Without that, two profiles -- or a profile and a future core type --
   collide inside an append-only log, where the collision can never be
   taken back out.

2. A type's model validates the payload BEFORE the event is appended, for
   the same reason remember() rejects an unretrievable text-only fact: an
   append-only log cannot be fixed afterwards, so a malformed payload has
   to fail at write time or not at all.

3. Payload strings are scrubbed (scrub.py) before validation, not after.
   Scrubbing rewrites values, so validating first would certify a payload
   that is not the one stored; doing it in this order means the model sees
   exactly the bytes that land in the log. The whole payload goes through
   it, recursively, because a profile's field names are unknown here and
   "nothing downstream of scrub sees unscrubbed text" (scrub.py) does not
   get an exemption for third-party payloads.

4. The registry is immutable once built, and built once per Runtime. A log
   whose typing rules could change mid-process would make `orlog replay`
   non-reproducible, which is G2.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any, Iterable, Mapping

from pydantic import BaseModel, ValidationError

from orlog.errors import SchemaError
from orlog.plugins import EVENT_TYPES_GROUP, PluginError, load_group, resolve_mapping

#: Types core itself writes. Not registrable by a profile.
CORE_EVENT_TYPES = frozenset(
    {
        "fact",
        "outcome.abstention",
        "outcome.correction",
        "outcome.verification",
        "x.orlog.forget",
    }
)

#: Vendor prefixes a profile may not claim.
RESERVED_VENDORS = frozenset({"orlog"})

_TYPE_NAME = re.compile(r"^x\.(?P<vendor>[a-z0-9_]+)\.(?P<name>[a-z0-9_]+(?:\.[a-z0-9_]+)*)$")


@dataclass(frozen=True)
class EventTypeSpec:
    """One registered type: its name, its payload model, and the profile
    that shipped it (kept for error messages -- "which profile told me this
    type exists" is the first question when a payload is rejected)."""

    name: str
    model: type[BaseModel]
    distribution: str

    @property
    def vendor(self) -> str:
        match = _TYPE_NAME.match(self.name)
        return match.group("vendor") if match else ""


def _check_name(name: str, *, distribution: str) -> None:
    if name in CORE_EVENT_TYPES:
        raise PluginError(
            f"distribution {distribution!r} tried to register the core event type {name!r}; "
            "core types are not overridable"
        )
    match = _TYPE_NAME.match(name)
    if match is None:
        raise PluginError(
            f"distribution {distribution!r} registered event type {name!r}, which is not a "
            "vendor-namespaced type name -- expected 'x.<vendor>.<name>' "
            "(lowercase letters, digits and underscores)"
        )
    if match.group("vendor") in RESERVED_VENDORS:
        raise PluginError(
            f"distribution {distribution!r} registered event type {name!r}, but vendor "
            f"{match.group('vendor')!r} is reserved for orlog core"
        )


def _check_model(name: str, model: Any, *, distribution: str) -> type[BaseModel]:
    if not (isinstance(model, type) and issubclass(model, BaseModel)):
        raise PluginError(
            f"distribution {distribution!r} registered event type {name!r} with "
            f"{model!r}; expected a pydantic BaseModel subclass for its payload"
        )
    return model


class EventTypeRegistry:
    """Every event type this workspace knows: core's, plus each loaded
    profile's. Immutable (module docstring point 4)."""

    def __init__(self, specs: Iterable[EventTypeSpec] = ()) -> None:
        self._specs: dict[str, EventTypeSpec] = {}
        for spec in specs:
            if spec.name in self._specs:
                first = self._specs[spec.name].distribution
                raise PluginError(
                    f"event type {spec.name!r} is registered twice: by {first!r} and by "
                    f"{spec.distribution!r}. Two profiles cannot share a type name -- the log "
                    "they both write into has no way to tell the two payload shapes apart."
                )
            self._specs[spec.name] = spec

    @classmethod
    def load(
        cls, *, enabled: Iterable[str] | None = None, disabled: Iterable[str] | None = None
    ) -> "EventTypeRegistry":
        specs: list[EventTypeSpec] = []
        for loaded in load_group(EVENT_TYPES_GROUP, enabled=enabled, disabled=disabled):
            for name, model in resolve_mapping(loaded).items():
                _check_name(name, distribution=loaded.distribution)
                specs.append(
                    EventTypeSpec(
                        name=name,
                        model=_check_model(name, model, distribution=loaded.distribution),
                        distribution=loaded.distribution,
                    )
                )
        return cls(specs)

    def __contains__(self, name: object) -> bool:
        return name in self._specs

    def __len__(self) -> int:
        return len(self._specs)

    @property
    def names(self) -> frozenset[str]:
        """Registered (profile) type names only -- core's are not in here."""
        return frozenset(self._specs)

    def spec(self, name: str) -> EventTypeSpec | None:
        return self._specs.get(name)

    def known_event_types(self, declared: Iterable[str] = ()) -> frozenset[str]:
        """The merge §B12.1 defines: core's vocabulary, every type a loaded
        profile registered, and anything the workspace declared in
        `[schema] known_event_types`. This is what remember() checks a
        non-fact `type` argument against."""
        return CORE_EVENT_TYPES | self.names | frozenset(declared)

    def validate_payload(self, name: str, payload: Mapping[str, Any]) -> dict[str, Any]:
        """Validate `payload` against the type's model and return the
        canonical dump that gets appended. Raises SchemaError (E_SCHEMA, the
        same code every other write-time rejection uses) on a bad payload --
        a profile's typing mistake is not a different KIND of failure from
        core's, so it does not get a different error code."""
        spec = self._specs.get(name)
        if spec is None:
            raise SchemaError(f"{name!r} is not a registered event type")
        try:
            model = spec.model.model_validate(dict(payload))
        except ValidationError as exc:
            raise SchemaError(
                f"payload for event type {name!r} (registered by {spec.distribution!r}) is "
                f"invalid: {exc.error_count()} error(s): "
                + "; ".join(
                    f"{'.'.join(str(p) for p in e['loc']) or '<root>'}: {e['msg']}"
                    for e in exc.errors()
                )
            ) from exc
        return model.model_dump(mode="json")
