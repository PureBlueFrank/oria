"""Explicit-version prompt loading from installed package resources."""

from __future__ import annotations

import json
import re
from importlib import resources
from importlib.abc import Traversable

from jinja2 import Environment, StrictUndefined, meta

_NAME = re.compile(r"^[a-z][a-z0-9_]{0,63}$")
_VERSION_FILE = re.compile(r"^v([1-9][0-9]*)\.jinja$")
_META_PREFIX = "# meta: "


class PromptError(ValueError):
    """Raised when a prompt resource or render contract is invalid."""


class PromptManager:
    """Load a named prompt only when the caller supplies its exact version."""

    def __init__(self) -> None:
        self._environment = Environment(
            autoescape=False,
            undefined=StrictUndefined,
            keep_trailing_newline=True,
        )

    def list_versions(self, name: str) -> tuple[int, ...]:
        root = self._prompt_root(name)
        try:
            entries = tuple(root.iterdir())
        except (FileNotFoundError, ModuleNotFoundError) as exc:
            raise PromptError("prompt name is unavailable") from exc
        versions = sorted(
            int(match.group(1))
            for entry in entries
            if (match := _VERSION_FILE.fullmatch(entry.name)) is not None
        )
        if not versions:
            raise PromptError("prompt name is unavailable")
        return tuple(versions)

    def render(self, name: str, *, version: int, **variables: object) -> str:
        if isinstance(version, bool) or not isinstance(version, int) or version <= 0:
            raise PromptError("prompt version must be a positive integer")
        template_text, declared = self._load(name, version)
        syntax = self._environment.parse(template_text)
        observed = meta.find_undeclared_variables(syntax)
        if observed != declared:
            raise PromptError("prompt metadata variables do not match the template")
        if set(variables) != declared:
            raise PromptError("prompt variables do not match the declared contract")
        try:
            return self._environment.from_string(template_text).render(**variables)
        except Exception as exc:
            raise PromptError("prompt rendering failed") from exc

    def _load(self, name: str, version: int) -> tuple[str, set[str]]:
        target = self._prompt_root(name).joinpath(f"v{version}.jinja")
        try:
            raw = target.read_text(encoding="utf-8")
        except (FileNotFoundError, ModuleNotFoundError) as exc:
            raise PromptError("prompt version is unavailable") from exc
        first, separator, body = raw.partition("\n")
        if not separator or not first.startswith(_META_PREFIX):
            raise PromptError("prompt metadata is missing")
        try:
            metadata = json.loads(first.removeprefix(_META_PREFIX))
        except json.JSONDecodeError as exc:
            raise PromptError("prompt metadata is invalid") from exc
        if not isinstance(metadata, dict) or not isinstance(metadata.get("desc"), str):
            raise PromptError("prompt metadata is invalid")
        declared = metadata.get("vars")
        if (
            not isinstance(declared, list)
            or any(not isinstance(item, str) for item in declared)
            or len(set(declared)) != len(declared)
        ):
            raise PromptError("prompt metadata variables are invalid")
        return body, set(declared)

    @staticmethod
    def _prompt_root(name: str) -> Traversable:
        if _NAME.fullmatch(name) is None:
            raise PromptError("prompt name is invalid")
        return resources.files("oria.prompts").joinpath(name)
