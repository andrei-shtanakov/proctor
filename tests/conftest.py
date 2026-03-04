"""Shared test fixtures."""

import pytest


@pytest.fixture(params=["asyncio", "trio"])
def anyio_backend(request: pytest.FixtureRequest) -> str:
    """Run async tests on both asyncio and trio."""
    return request.param
