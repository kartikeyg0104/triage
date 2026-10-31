"""Public API: use tomlette.parse(text); the implementation lives in tomlette.impl."""

from .impl import parse

__all__ = ["parse"]
