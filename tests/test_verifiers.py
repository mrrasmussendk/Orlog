"""Contract: the 'module:' dynamic loader (ORLOG-SPEC.md §B11) imports and
instantiates a user-supplied class from a "module:path.to.Class" string.
"""

import pytest

from orlog.verifiers import load_verifier_from_module


def test_loads_and_instantiates_a_real_class():
    # orlog.errors.SchemaError is a convenient real class already in this
    # codebase to prove the import+construct mechanism works end to end.
    instance = load_verifier_from_module("module:orlog.errors.SchemaError", detail="test detail")
    assert instance.detail == "test detail"
    assert instance.code == "E_SCHEMA"


def test_rejects_a_backend_string_without_the_module_prefix():
    with pytest.raises(ValueError):
        load_verifier_from_module("windows")


def test_rejects_a_backend_string_with_no_class_name():
    with pytest.raises(ValueError):
        load_verifier_from_module("module:orlog.errors")


def test_unknown_attribute_raises_a_clear_error():
    with pytest.raises(ValueError):
        load_verifier_from_module("module:orlog.errors.NotARealClass")


def test_unknown_module_raises_import_error():
    with pytest.raises(ImportError):
        load_verifier_from_module("module:not_a_real_package.Thing")
