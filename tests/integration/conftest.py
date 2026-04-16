"""Fixtures for NATS-backed integration tests.

The `nats_container` fixture starts a real NATS server via testcontainers
(docker/podman). Requires the nats extra: `uv sync --extra nats`.

To run locally against podman:

    export DOCKER_HOST=unix:///var/folders/.../podman-machine-default-api.sock
    export DOCKER_CONFIG=/tmp/empty-docker-config  # if credsStore=desktop
    export TESTCONTAINERS_RYUK_DISABLED=true
    uv run pytest -m nats

The CI workflow (`integration-nats` job) uses GHA `services:` for NATS
directly and sets `NATS_URL` via env, bypassing testcontainers entirely.
"""

from __future__ import annotations

import os
from collections.abc import AsyncIterator, Iterator
from typing import TYPE_CHECKING

import pytest

if TYPE_CHECKING:  # pragma: no cover
    from proctor.core.transport.nats import NATSEventTransport


@pytest.fixture(scope="session")
def nats_url() -> Iterator[str]:
    """Return a reachable NATS server URL.

    If `NATS_URL` is set in the environment (CI), use it directly.
    Otherwise start a NatsContainer via testcontainers (podman/docker).
    """
    env_url = os.environ.get("NATS_URL")
    if env_url:
        yield env_url
        return

    try:
        from testcontainers.nats import NatsContainer
    except ImportError:  # pragma: no cover
        pytest.skip("testcontainers not installed; NATS_URL not set")

    try:
        with NatsContainer() as container:
            yield container.nats_uri()
    except Exception as e:  # pragma: no cover
        pytest.skip(f"Failed to start NATS container: {e}")


@pytest.fixture
async def nats_transport(nats_url: str) -> AsyncIterator[NATSEventTransport]:
    """Started NATSEventTransport pointing at an isolated subject prefix.

    Subject prefix is randomised per test so parallel tests don't see each
    other's events (NATS is broadcast; no namespaces).
    """
    from uuid import uuid4

    from proctor.core.config import EventsConfig, NATSConfig
    from proctor.core.transport.nats import NATSEventTransport

    prefix = f"proctor_test_{uuid4().hex[:8]}"
    config = NATSConfig(servers=[nats_url], subject_prefix=prefix)
    transport = NATSEventTransport(
        config,
        events_config=EventsConfig(max_payload=65_536),
    )
    await transport.start()
    try:
        yield transport
    finally:
        await transport.stop()
