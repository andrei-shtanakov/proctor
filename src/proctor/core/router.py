"""Router — maps trigger events to catalog workflows.

Emits ``routing.unmatched`` and ``routing.binding_failed`` events on the
bus for observability. Subscribers on ``routing.*`` should treat these
as read-only signals — Router does not listen to its own namespace.
"""

from __future__ import annotations

from typing import Any


def _resolve_path(
    payload: dict[str, Any], path: str
) -> tuple[str | None, str | None]:
    """Walk a dotted path through nested dicts.

    On success: ``(value, None)``.
    On failure: ``(None, <reason>)`` where ``reason`` identifies the
    specific failure class:

    - top-level key missing: ``"top-level key 'X' missing"``
    - intermediate key missing: ``"key 'X' missing under 'A.B'"``
    - intermediate value is not a dict: ``"intermediate value at 'A' is not a dict"``
    - terminal value is not a string: ``"terminal value at 'A.B' is <type>,``
      ``expected str"``
    """
    current: Any = payload
    traversed: list[str] = []
    for key in path.split("."):
        if not isinstance(current, dict):
            prefix = ".".join(traversed) if traversed else "<root>"
            return None, f"intermediate value at '{prefix}' is not a dict"
        if key not in current:
            if not traversed:
                return None, f"top-level key '{key}' missing"
            prefix = ".".join(traversed)
            return None, f"key '{key}' missing under '{prefix}'"
        current = current[key]
        traversed.append(key)
    if not isinstance(current, str):
        return (
            None,
            f"terminal value at '{path}' is {type(current).__name__}, expected str",
        )
    return current, None
