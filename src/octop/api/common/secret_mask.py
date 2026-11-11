"""Mask stored secrets for callers who may see that a secret is set but not its value."""

from __future__ import annotations

from typing import Any

_SECRET_FIELD_HINTS = ("secret", "key", "token", "password")


def mask_secret(value: str | None) -> str | None:
    """First four characters plus asterisks (all asterisks when that short); empty stays."""
    if not value:
        return value
    return f"{value[:4]}{'*' * 8}" if len(value) > 4 else "*" * len(value)


def mask_secret_fields(extra: dict[str, Any]) -> dict[str, Any]:
    """Copy of *extra* with string values under secret-looking keys masked."""
    return {
        name: mask_secret(value)
        if isinstance(value, str) and any(hint in name.lower() for hint in _SECRET_FIELD_HINTS)
        else value
        for name, value in extra.items()
    }
