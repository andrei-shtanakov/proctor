"""Tests for EventTransport ABC contract."""

import pytest

from proctor.core.transport import (
    ConnectionState,
    EventTransport,
    ListenerHandle,
    SubscriptionHandle,
)


class TestConnectionState:
    """Test ConnectionState enum."""

    def test_states(self) -> None:
        """Test all ConnectionState values."""
        assert ConnectionState.CONNECTED.value == "connected"
        assert ConnectionState.RECONNECTING.value == "reconnecting"
        assert ConnectionState.DISCONNECTED.value == "disconnected"


class TestEventTransportAbstract:
    """Test EventTransport ABC enforcement."""

    def test_cannot_instantiate(self) -> None:
        """Cannot directly instantiate EventTransport."""
        with pytest.raises(TypeError):
            EventTransport()  # type: ignore[abstract]

    def test_required_methods(self) -> None:
        """Check that required methods are declared."""
        required = {
            "start",
            "stop",
            "drain",
            "flush",
            "publish",
            "subscribe",
            "add_disconnect_listener",
            "add_reconnect_listener",
        }
        for name in required:
            assert hasattr(EventTransport, name), f"Missing: {name}"
        assert hasattr(EventTransport, "connection_state")


class TestProtocols:
    """Test Protocol implementations."""

    def test_subscription_handle_protocol(self) -> None:
        """A minimal impl satisfies the SubscriptionHandle Protocol."""

        class _Sub:
            @property
            def subject(self) -> str:
                return "test"

            async def unsubscribe(self) -> None:
                pass

        sub: SubscriptionHandle = _Sub()
        assert sub.subject == "test"

    def test_listener_handle_protocol(self) -> None:
        """A minimal impl satisfies the ListenerHandle Protocol."""

        class _L:
            def remove(self) -> None:
                pass

        lh: ListenerHandle = _L()
        lh.remove()
