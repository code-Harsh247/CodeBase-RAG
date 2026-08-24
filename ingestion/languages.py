"""Tree-sitter grammar registry.

Adding a language means registering its grammar here and writing a mapper that
emits the shared schema — the schema itself does not change.
"""

from __future__ import annotations

from dataclasses import dataclass
from functools import cache
from pathlib import Path

import tree_sitter_python
from tree_sitter import Language, Parser


@dataclass(frozen=True)
class LanguageSpec:
    name: str
    extensions: tuple[str, ...]


PYTHON = LanguageSpec(name="python", extensions=(".py",))

_REGISTRY: dict[str, LanguageSpec] = {PYTHON.name: PYTHON}

_GRAMMARS = {PYTHON.name: tree_sitter_python.language}


def supported_extensions() -> tuple[str, ...]:
    return tuple(ext for spec in _REGISTRY.values() for ext in spec.extensions)


def language_for_path(path: Path) -> LanguageSpec | None:
    suffix = path.suffix.lower()
    for spec in _REGISTRY.values():
        if suffix in spec.extensions:
            return spec
    return None


@cache
def get_parser(language_name: str) -> Parser:
    """Return a cached parser for ``language_name``."""
    if language_name not in _GRAMMARS:
        raise ValueError(f"Unsupported language: {language_name}")
    return Parser(Language(_GRAMMARS[language_name]()))
