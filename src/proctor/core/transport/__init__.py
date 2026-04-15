"""EventTransport abstraction and backends.

See docs/superpowers/specs/2026-04-15-nats-transport-design.md for
design rationale. All 21 ADRs in docs/superpowers/adr/.
"""

from proctor.core.transport.base import (
    ConnectionState,
    DisconnectCallback,
    EventTransport,
    Handler,
    ListenerHandle,
    SubscriptionHandle,
)
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
from proctor.core.transport.local import LocalEventTransport

__all__ = [
    "ConnectionState",
    "DisconnectCallback",
    "EventSchemaError",
    "EventTooLargeError",
    "EventTransport",
    "Handler",
    "HandlerTimeoutError",
    "InvalidSubjectError",
    "ListenerHandle",
    "LocalEventTransport",
    "SubscriptionHandle",
    "TransportConnectionError",
    "TransportDrainingError",
    "TransportError",
    "TransportLifecycleError",
    "TransportUnavailableError",
]

try:
    from proctor.core.transport.nats import (
        NATSEventTransport,
        register_decoder,
    )

    __all__ += ["NATSEventTransport", "register_decoder"]
except ImportError:  # pragma: no cover
    # nats-py not installed — standalone deployment
    pass
