"""plugins: entry-point discovery, shared by all three registries (ORLOG-SPEC.md §B12).

A *profile* is an installed distribution that extends orlog through entry
points -- event types (§B12.1), projections (§B12.2) and tools (§B12.3) --
without forking core. This module is the single mechanism all three use, so
discovery, allowlisting, error reporting and test overrides are written
once rather than three times.

Design decisions:

1. Discovery is `importlib.metadata.entry_points(group=...)`, the same
   mechanism pytest and setuptools use. An entry point that fails to import
   is NOT swallowed: `load_group()` raises PluginError naming the
   distribution, because a workspace whose profile silently failed to load
   is a workspace quietly missing the memories that profile was supposed to
   type, project and serve. "Fail visible, not plausible" applies to the
   plugin boundary too.

2. Loading is *filtered by the workspace*, not global. `[profiles] enabled`
   (orlog.toml §B10) is three-state: absent means "everything installed",
   an empty list means "nothing", and a non-empty list is a strict
   allowlist of distribution names. `disabled` subtracts from the default
   instead. A workspace therefore pins exactly which third-party code gets
   to see its log, which matters rather more here than it does for a test
   runner -- and can refuse all of it, which is why the empty list had to
   mean "none" rather than "all".

3. `register()` exists alongside entry points so this package's own tests
   (and an embedding application that never installs a distribution at all)
   can supply a plugin programmatically. It is additive and process-local;
   entry points remain the distribution mechanism. `clear_registered()`
   makes it safe to use from a fixture.
"""

from __future__ import annotations

from dataclasses import dataclass
from importlib.metadata import EntryPoint, entry_points
from typing import Any, Iterable, Iterator

from orlog.errors import OrlogError

EVENT_TYPES_GROUP = "orlog.event_types"
PROJECTIONS_GROUP = "orlog.projections"
TOOLS_GROUP = "orlog.tools"

GROUPS = (EVENT_TYPES_GROUP, PROJECTIONS_GROUP, TOOLS_GROUP)

#: Distribution name used for plugins supplied through `register()` rather
#: than through an installed distribution's entry points.
LOCAL_DISTRIBUTION = "<local>"


class PluginError(OrlogError):
    """A profile's entry point could not be imported, or declared something
    this registry refuses to accept."""

    code = "E_PLUGIN"


@dataclass(frozen=True)
class LoadedEntryPoint:
    """One resolved entry point: what it was called, who shipped it, and
    the object it resolved to."""

    group: str
    name: str
    distribution: str
    value: Any


_REGISTERED: dict[str, list[LoadedEntryPoint]] = {group: [] for group in GROUPS}


def register(group: str, name: str, value: Any, *, distribution: str = LOCAL_DISTRIBUTION) -> None:
    """Add a plugin programmatically, as if an installed distribution had
    declared it under `group`. Process-local and additive -- see module
    docstring point 3."""
    if group not in _REGISTERED:
        raise PluginError(f"unknown plugin group {group!r} -- expected one of {', '.join(GROUPS)}")
    _REGISTERED[group].append(
        LoadedEntryPoint(group=group, name=name, distribution=distribution, value=value)
    )


def clear_registered(group: str | None = None) -> None:
    """Drop programmatically registered plugins (all groups, or one)."""
    for key in _REGISTERED if group is None else [group]:
        _REGISTERED[key] = []


def registered(group: str) -> list[LoadedEntryPoint]:
    return list(_REGISTERED.get(group, []))


def _distribution_of(ep: EntryPoint) -> str:
    # EntryPoint.dist is populated by the selectable entry_points() API, but
    # stays Optional in the stdlib's own typing and is absent on an
    # EntryPoint constructed by hand -- so it is read defensively rather
    # than assumed.
    dist = getattr(ep, "dist", None)
    name = getattr(dist, "name", None) if dist is not None else None
    return name or "<unknown>"


def _installed(group: str) -> Iterator[EntryPoint]:
    yield from entry_points(group=group)


def _allowed(distribution: str, enabled: Iterable[str] | None, disabled: Iterable[str] | None) -> bool:
    if enabled is not None:
        # An allowlist is strict, including when it is empty: `<local>`
        # plugins registered by an embedding application are only loaded if
        # named, same as any installed distribution. Anything else would
        # make the allowlist a suggestion.
        return distribution in list(enabled)
    return distribution not in list(disabled or [])


def load_group(
    group: str,
    *,
    enabled: Iterable[str] | None = None,
    disabled: Iterable[str] | None = None,
) -> list[LoadedEntryPoint]:
    """Every entry point in `group` this workspace is allowed to load,
    resolved. Entry points come first in distribution+name order (so a
    build is deterministic), programmatically registered plugins last.

    Raises PluginError if an allowed entry point cannot be imported --
    never a partial, silent load (module docstring point 1).
    """
    if group not in GROUPS:
        raise PluginError(f"unknown plugin group {group!r} -- expected one of {', '.join(GROUPS)}")

    found: list[LoadedEntryPoint] = []
    for ep in sorted(_installed(group), key=lambda e: (_distribution_of(e), e.name)):
        distribution = _distribution_of(ep)
        if not _allowed(distribution, enabled, disabled):
            continue
        try:
            value = ep.load()
        except Exception as exc:  # noqa: BLE001 -- re-raised in the taxonomy below
            raise PluginError(
                f"entry point {ep.name!r} in group {group!r} (from distribution {distribution!r}) "
                f"could not be imported: {type(exc).__name__}: {exc}"
            ) from exc
        found.append(LoadedEntryPoint(group=group, name=ep.name, distribution=distribution, value=value))

    for local in _REGISTERED.get(group, []):
        if _allowed(local.distribution, enabled, disabled):
            found.append(local)
    return found


def resolve_mapping(loaded: LoadedEntryPoint) -> dict[str, Any]:
    """Normalize what an entry point resolved to into {name: object}.

    A profile may declare either a mapping directly, or a zero-argument
    callable returning one -- the callable form lets a profile build its
    registry at load time (e.g. from its own config) without executing at
    import. Anything else is a PluginError naming the distribution, rather
    than a TypeError from somewhere deep in a registry.
    """
    value = loaded.value
    if callable(value) and not isinstance(value, dict):
        try:
            value = value()
        except Exception as exc:  # noqa: BLE001
            raise PluginError(
                f"entry point {loaded.name!r} in group {loaded.group!r} (from distribution "
                f"{loaded.distribution!r}) raised while building its registry: "
                f"{type(exc).__name__}: {exc}"
            ) from exc
    if not isinstance(value, dict):
        raise PluginError(
            f"entry point {loaded.name!r} in group {loaded.group!r} (from distribution "
            f"{loaded.distribution!r}) resolved to {type(value).__name__}; expected a dict of "
            "{name: object} or a zero-argument callable returning one"
        )
    return dict(value)
