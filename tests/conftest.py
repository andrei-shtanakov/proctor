"""Shared test fixtures."""

import pytest


@pytest.fixture(params=["asyncio", "trio"])
def anyio_backend(request: pytest.FixtureRequest) -> str:
    """Run async tests on both asyncio and trio."""
    return request.param


def pytest_collection_modifyitems(
    config: pytest.Config, items: list[pytest.Item]
) -> None:
    """Skip integration tests unless explicitly requested."""
    # Check if user explicitly asked for integration tests with -m
    has_marker_filter = bool(getattr(config.option, "markexpr", None))
    if not has_marker_filter:
        # No marker filter; skip integration tests from tests/integration/
        skip_integration = pytest.mark.skip(reason="use -m integration to run")
        for item in items:
            # Only skip tests from tests/integration/ package
            if "integration" in item.keywords and "/integration/" in str(item.fspath):
                item.add_marker(skip_integration)
