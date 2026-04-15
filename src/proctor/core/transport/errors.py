"""Transport exception hierarchy.

All transport-related exceptions inherit from TransportError so callers
can `except TransportError` and catch everything. Some subclasses
(EventSchemaError, HandlerTimeoutError) are internal control-flow
only — they NEVER propagate beyond transport.
"""

from __future__ import annotations


class TransportError(Exception):
    """Base for all event transport errors."""


class TransportConnectionError(TransportError):
    """Connect / start failures."""


class TransportLifecycleError(TransportError):
    """Double-start, stop-before-start, etc."""


class TransportUnavailableError(TransportError):
    """publish() attempted while not CONNECTED."""


class TransportDrainingError(TransportUnavailableError):
    """publish() attempted during drain phase."""


class InvalidSubjectError(TransportError, ValueError):
    """Subject violates charset or wildcard rules.

    Dual-inherits ValueError so callers that `except ValueError` (e.g.
    pydantic validators) still catch this.
    """


class EventTooLargeError(TransportError):
    """Serialized event exceeds events.max_payload."""


class EventSchemaError(TransportError):
    """Internal control-flow exception raised inside transport when a
    received message can't be decoded.

    NEVER propagates beyond transport — caught internally for log +
    drop behaviour. Kept as a class (not bare logger call) so internal
    handlers can use `except EventSchemaError` pattern matching cleanly.
    """


class HandlerTimeoutError(TransportError):
    """Internal: handler exceeded drain soft timeout.

    Logged, task cancelled; never propagates beyond transport.
    """
