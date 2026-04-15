"""Router — maps trigger events to catalog workflows.

Emits ``routing.unmatched`` and ``routing.binding_failed`` events on the
bus for observability. Subscribers on ``routing.*`` should treat these
as read-only signals — Router does not listen to its own namespace.
"""

from __future__ import annotations

import logging
from fnmatch import fnmatchcase
from typing import Any

from proctor.core.bus import EventBus
from proctor.core.config import RouteRule
from proctor.core.models import Event
from proctor.workflow.spec import WorkflowSpec

logger = logging.getLogger(__name__)


def _resolve_path(payload: dict[str, Any], path: str) -> tuple[str | None, str | None]:
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


class Router:
    """Map trigger events to catalog workflows via declarative rules.

    Iterates routes in order (first match wins). Clones the matched
    catalog WorkflowSpec with ``model_copy(update={"prompt": resolved})``
    so catalog entries stay pristine. On unmatched events or binding
    failures, publishes ``routing.unmatched`` / ``routing.binding_failed``
    on the bus and returns ``None``.
    """

    def __init__(
        self,
        bus: EventBus,
        routes: list[RouteRule],
        workflows: dict[str, WorkflowSpec],
    ) -> None:
        self._bus = bus
        self._routes = routes
        self._workflows = workflows

    async def route(self, event: Event) -> WorkflowSpec | None:
        for rule in self._routes:
            if not fnmatchcase(event.type, rule.event_pattern):
                continue
            # rule matched — resolve the prompt
            if rule.prompt is not None:
                prompt: str | None = rule.prompt
            else:
                assert rule.prompt_from_payload is not None  # validator guarantees
                prompt, _reason = _resolve_path(event.payload, rule.prompt_from_payload)
                if prompt is None:
                    return None  # binding-failed handling comes in Task 9
            base = self._workflows[rule.workflow_id]
            logger.debug(
                "Routed event type=%s to workflow_id=%s",
                event.type,
                rule.workflow_id,
            )
            return base.model_copy(update={"prompt": prompt})

        return None  # unmatched handling comes in Task 8
