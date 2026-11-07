"""tools: the MCP tool registry (ORLOG-SPEC.md §B12.3).

§B9 fixes core's seven tools. This module is the seam a *profile* uses to
add its own: the `orlog.tools` entry-point group, and a tool shape --
`(runtime, **kwargs) -> dict` -- deliberately identical to the one
server_tools.py has always used, so a profile's tools are testable by
direct call exactly like core's, with no FastMCP machinery involved.

Design decisions:

1. Core's tool names are reserved. A profile that could shadow `recall`
   could quietly replace the one call in this system that carries the
   verify-or-abstain guarantee, which is the guarantee (G3/G4). Shadowing
   is a PluginError at load, not a last-writer-wins race decided by
   entry-point ordering.

2. `bind()` builds the callable FastMCP actually registers: the profile's
   own function with `runtime` already supplied, and a `__signature__`
   that is the original's minus that first parameter. The signature is
   computed with `eval_str=True` against the PROFILE's module globals
   before being attached, so a tool annotated with its own types still
   produces a correct JSON schema -- FastMCP would otherwise try to
   resolve those names in this module, where they do not exist.

3. A tool declares its MCP description in its docstring, like core's do.
   No second description field to drift out of sync with the code.
"""

from __future__ import annotations

import functools
import inspect
from dataclasses import dataclass
from typing import Any, Callable, Iterable

from orlog.plugins import TOOLS_GROUP, PluginError, load_group, resolve_mapping

#: The tools §B9 fixes. Not registrable by a profile (docstring point 1).
CORE_TOOL_NAMES = frozenset(
    {
        "remember",
        "recall",
        "recall_history",
        "list_entities",
        "list_attributes",
        "check_action",
        "stats",
    }
)


@dataclass(frozen=True)
class ToolSpec:
    """One registered tool: its MCP name, the `(runtime, ...) -> dict`
    function behind it, and the profile that shipped it."""

    name: str
    func: Callable[..., dict]
    distribution: str

    @property
    def description(self) -> str | None:
        doc = inspect.getdoc(self.func)
        return doc or None


def _check_tool(name: str, func: Any, *, distribution: str) -> Callable[..., dict]:
    if name in CORE_TOOL_NAMES:
        raise PluginError(
            f"distribution {distribution!r} tried to register the core tool {name!r}; core "
            "tools are not overridable -- they carry orlog's verify-or-abstain guarantee"
        )
    if not name.isidentifier():
        raise PluginError(
            f"distribution {distribution!r} registered tool {name!r}, which is not a valid "
            "identifier -- an MCP tool name has to be callable by name from a client"
        )
    if not callable(func):
        raise PluginError(
            f"distribution {distribution!r} registered tool {name!r} with {func!r}, "
            "which is not callable"
        )
    parameters = list(inspect.signature(func).parameters.values())
    if not parameters or parameters[0].kind in (
        inspect.Parameter.VAR_POSITIONAL,
        inspect.Parameter.VAR_KEYWORD,
    ):
        raise PluginError(
            f"distribution {distribution!r} registered tool {name!r} with signature "
            f"{inspect.signature(func)}; a tool's first parameter must be the Runtime "
            "(the shape server_tools.py uses: (runtime, **kwargs) -> dict)"
        )
    return func


class ToolRegistry:
    """Every profile tool this workspace loads. Core's seven are not in
    here -- server.py wires those directly, because their descriptions and
    arguments are normative (§B9), not pluggable."""

    def __init__(self, specs: Iterable[ToolSpec] = ()) -> None:
        self._specs: dict[str, ToolSpec] = {}
        for spec in specs:
            if spec.name in self._specs:
                raise PluginError(
                    f"tool {spec.name!r} is registered twice: by "
                    f"{self._specs[spec.name].distribution!r} and by {spec.distribution!r}"
                )
            self._specs[spec.name] = spec

    @classmethod
    def load(
        cls, *, enabled: Iterable[str] | None = None, disabled: Iterable[str] | None = None
    ) -> "ToolRegistry":
        specs: list[ToolSpec] = []
        for loaded in load_group(TOOLS_GROUP, enabled=enabled, disabled=disabled):
            for name, func in resolve_mapping(loaded).items():
                specs.append(
                    ToolSpec(
                        name=name,
                        func=_check_tool(name, func, distribution=loaded.distribution),
                        distribution=loaded.distribution,
                    )
                )
        return cls(specs)

    def __contains__(self, name: object) -> bool:
        return name in self._specs

    def __iter__(self):
        return iter(self._specs.values())

    def __len__(self) -> int:
        return len(self._specs)

    @property
    def names(self) -> tuple[str, ...]:
        return tuple(self._specs)

    def get(self, name: str) -> ToolSpec:
        try:
            return self._specs[name]
        except KeyError:
            raise PluginError(
                f"no tool named {name!r} -- registered: {', '.join(self.names) or '(none)'}"
            ) from None

    def extend(self, extra: Iterable[ToolSpec]) -> "ToolRegistry":
        """A new registry with `extra` added -- how build_server()'s
        `extra_tools` argument joins entry-point discovery without an
        embedding application having to install a distribution."""
        return ToolRegistry(list(self._specs.values()) + list(extra))


def as_specs(
    tools: Any, *, distribution: str = "<embedded>"
) -> list[ToolSpec]:
    """Normalize build_server()'s `extra_tools` -- a {name: func} mapping, an
    iterable of ToolSpec, or None -- into ToolSpecs."""
    if tools is None:
        return []
    if isinstance(tools, dict):
        return [
            ToolSpec(name=name, func=_check_tool(name, func, distribution=distribution), distribution=distribution)
            for name, func in tools.items()
        ]
    specs: list[ToolSpec] = []
    for tool in tools:
        if isinstance(tool, ToolSpec):
            _check_tool(tool.name, tool.func, distribution=tool.distribution)
            specs.append(tool)
        else:
            raise PluginError(
                f"extra_tools entries must be ToolSpec objects or a {{name: func}} mapping, "
                f"got {tool!r}"
            )
    return specs


def bind(spec: ToolSpec, runtime: Any) -> Callable[..., dict]:
    """The callable FastMCP registers: `spec.func` with `runtime` applied
    and that parameter removed from the advertised signature (docstring
    point 2)."""
    func = spec.func
    try:
        signature = inspect.signature(func, eval_str=True)
    except NameError as exc:
        raise PluginError(
            f"tool {spec.name!r} (from distribution {spec.distribution!r}) has type annotations "
            f"that cannot be resolved in its own module: {exc}"
        ) from exc
    parameters = list(signature.parameters.values())
    runtime_param = parameters[0].name

    @functools.wraps(func)
    def bound(*args: Any, **kwargs: Any) -> dict:
        return func(runtime, *args, **kwargs)

    bound.__signature__ = signature.replace(parameters=parameters[1:])  # type: ignore[attr-defined]
    # functools.wraps copied the original's annotations, which still name the
    # runtime parameter; FastMCP reads the signature, but anything else
    # introspecting this callable should see the same shape.
    bound.__annotations__ = {k: v for k, v in getattr(func, "__annotations__", {}).items() if k != runtime_param}
    bound.__name__ = spec.name
    bound.__qualname__ = spec.name
    return bound
