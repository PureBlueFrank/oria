"""Import and Pydantic schema smoke tests for every currently shipped module."""

from __future__ import annotations

import importlib
import inspect
import pkgutil

import pytest
from pydantic import BaseModel

import oria

pytestmark = pytest.mark.contract


def _oria_module_names() -> list[str]:
    return [
        module_info.name for module_info in pkgutil.walk_packages(oria.__path__, prefix="oria.")
    ]


def test_every_oria_module_imports() -> None:
    imported = [importlib.import_module(name).__name__ for name in _oria_module_names()]

    assert imported == _oria_module_names()


def test_every_oria_pydantic_model_builds_json_schema() -> None:
    modules = [
        importlib.import_module(name)
        for name in [
            "oria.config.models",
            "oria.core.types",
            "oria.data",
            "oria.domain.models",
            "oria.migrations.runner",
        ]
    ]
    models = {
        value
        for module in modules
        for _, value in inspect.getmembers(module, inspect.isclass)
        if issubclass(value, BaseModel) and value.__module__.startswith("oria.")
    }

    schemas = {model.__name__: model.model_json_schema() for model in models}

    assert schemas
    assert all(schema.get("type") == "object" for schema in schemas.values())
