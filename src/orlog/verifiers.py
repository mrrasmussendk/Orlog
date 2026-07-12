"""verifiers: the 'module:' dynamic loader (ORLOG-SPEC.md §B11).

spec §B11 names three built-in verifier backends: "windows"
(orlog.heimdall.Heimdall), "pytest" (orlog.heimdall_pytest.PytestVerifier),
and "module:path.to.Class" -- a user-supplied class implementing the
Verifier protocol, "the documented extension point (this is where a
law-database verifier plugs in)". The first two are built where they
belong (their own modules); this module is only the third: dynamically
importing and instantiating whatever class a config.verifier.backend value
of "module:..." names.
"""

from __future__ import annotations

import importlib
from typing import Any


def load_verifier_from_module(backend: str, **kwargs: Any) -> Any:
    """backend must look like "module:package.module.ClassName". Imports
    the module and instantiates the class with `**kwargs`.
    """
    prefix = "module:"
    if not backend.startswith(prefix):
        raise ValueError(f"expected a 'module:dotted.path.ClassName' string, got {backend!r}")

    dotted = backend[len(prefix):]
    module_name, sep, class_name = dotted.rpartition(".")
    if not sep:
        raise ValueError(f"{backend!r} must be 'module:package.module.ClassName' (missing a class name)")

    module = importlib.import_module(module_name)
    try:
        cls = getattr(module, class_name)
    except AttributeError as exc:
        raise ValueError(f"module {module_name!r} has no attribute {class_name!r}") from exc

    if not isinstance(cls, type):
        raise ValueError(f"{backend!r} does not name a class (got {type(cls).__name__}) -- likely missing the class name at the end")

    return cls(**kwargs)
