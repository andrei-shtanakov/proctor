"""Tests for transport error hierarchy."""

import pytest

from proctor.core.transport import (
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


class TestExceptionHierarchy:
    def test_all_inherit_transport_error(self) -> None:
        for exc_cls in [
            TransportConnectionError,
            TransportLifecycleError,
            TransportUnavailableError,
            TransportDrainingError,
            InvalidSubjectError,
            EventTooLargeError,
            EventSchemaError,
            HandlerTimeoutError,
        ]:
            assert issubclass(exc_cls, TransportError)

    def test_draining_is_unavailable(self) -> None:
        assert issubclass(TransportDrainingError, TransportUnavailableError)

    def test_invalid_subject_is_also_value_error(self) -> None:
        # Dual inheritance — callers catching ValueError still catch it
        with pytest.raises(ValueError):
            raise InvalidSubjectError("bad pattern")
        with pytest.raises(TransportError):
            raise InvalidSubjectError("bad pattern")
