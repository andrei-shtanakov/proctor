"""EventTransport abstraction and backends.

See docs/superpowers/specs/2026-04-15-nats-transport-design.md for
design rationale. All 21 ADRs in docs/superpowers/adr/.
"""

from proctor.core.transport.errors import (
    EventSchemaError,
    EventTooLargeError,
    HandlerTimeoutError,
    InvalidSubjectError,
    TransportConnectionError,
    TransportDrainingError,
    TransportError,
    TransportLifecycleError,
    TransportUnavailableError,
)

__all__ = [
    "EventSchemaError",
    "EventTooLargeError",
    "HandlerTimeoutError",
    "InvalidSubjectError",
    "TransportConnectionError",
    "TransportDrainingError",
    "TransportError",
    "TransportLifecycleError",
    "TransportUnavailableError",
]
