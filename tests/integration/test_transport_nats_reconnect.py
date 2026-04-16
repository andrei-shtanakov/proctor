"""Toxiproxy-driven reconnect tests for NATSEventTransport.

Verifies connection_state transitions under injected network failures:
    CONNECTED → RECONNECTING (proxy disabled)
             → CONNECTED      (proxy re-enabled)

The test launches NATS + Toxiproxy as separate containers on a shared
network. Toxiproxy proxies `:7000 → nats:4222`; the transport connects
via `nats://<toxi_host>:<mapped_7000>`. Disabling the proxy simulates
a partition; re-enabling restores the link.

Gated by the `nats` marker and container availability.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import urllib.error
import urllib.request
from collections.abc import AsyncIterator, Iterator
from uuid import uuid4

import pytest

from proctor.core.config import EventsConfig, NATSConfig
from proctor.core.models import Event
from proctor.core.transport.base import ConnectionState
from proctor.core.transport.nats import NATSEventTransport

pytestmark = [pytest.mark.anyio, pytest.mark.nats]


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


@pytest.fixture(scope="module")
def toxi_network() -> Iterator[tuple[str, str, int, str]]:
    """Start NATS + Toxiproxy on a shared podman/docker network.

    Yields (toxi_api_url, nats_via_proxy_url, proxy_port, proxy_name).
    """
    from testcontainers.core.container import DockerContainer
    from testcontainers.core.network import Network
    from testcontainers.core.waiting_utils import wait_for_logs

    try:
        network = Network()
        network.create()
    except Exception as e:  # pragma: no cover
        pytest.skip(f"Network create failed: {e}")

    nats_alias = "nats"
    try:
        nats = (
            DockerContainer("nats:2-alpine")
            .with_network(network)
            .with_network_aliases(nats_alias)
            .with_exposed_ports(4222)
        )
        nats.start()
        wait_for_logs(nats, "Server is ready", timeout=30)

        toxi = (
            DockerContainer("ghcr.io/shopify/toxiproxy:2.9.0")
            .with_network(network)
            .with_exposed_ports(8474, 7000)
        )
        toxi.start()
        wait_for_logs(toxi, "Starting Toxiproxy HTTP server", timeout=30)

        toxi_host = toxi.get_container_host_ip()
        toxi_api_port = toxi.get_exposed_port(8474)
        toxi_proxy_port = int(toxi.get_exposed_port(7000))
        toxi_api = f"http://{toxi_host}:{toxi_api_port}"

        proxy_name = "nats_proxy"
        payload = json.dumps(
            {
                "name": proxy_name,
                "listen": "0.0.0.0:7000",
                "upstream": f"{nats_alias}:4222",
                "enabled": True,
            }
        ).encode("utf-8")
        req = urllib.request.Request(
            f"{toxi_api}/proxies",
            data=payload,
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        try:
            with urllib.request.urlopen(req, timeout=5) as resp:
                assert resp.status == 201, f"unexpected status {resp.status}"
        except urllib.error.HTTPError as e:
            pytest.skip(f"Toxiproxy proxy create failed: {e}")

        nats_url = f"nats://{toxi_host}:{toxi_proxy_port}"
        try:
            yield toxi_api, nats_url, toxi_proxy_port, proxy_name
        finally:
            with contextlib.suppress(Exception):
                toxi.stop()
            with contextlib.suppress(Exception):
                nats.stop()
    except Exception as e:  # pragma: no cover
        pytest.skip(f"Container setup failed: {e}")
    finally:
        with contextlib.suppress(Exception):
            network.remove()


def _toxi_set_enabled(api: str, proxy: str, enabled: bool) -> None:
    payload = json.dumps({"enabled": enabled}).encode("utf-8")
    req = urllib.request.Request(
        f"{api}/proxies/{proxy}",
        data=payload,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=5) as resp:
        assert resp.status == 200


@pytest.fixture
async def reconnecting_transport(
    toxi_network: tuple[str, str, int, str],
) -> AsyncIterator[tuple[NATSEventTransport, str, str]]:
    """NATSEventTransport connected via toxiproxy — yield (transport, api, proxy).

    Ensures the proxy is enabled before transport.start() so each test
    starts from a CONNECTED baseline regardless of what the previous
    test did.
    """
    api, nats_url, _, proxy_name = toxi_network
    # Reset proxy to enabled so each test gets a fresh CONNECTED start.
    _toxi_set_enabled(api, proxy_name, True)

    prefix = f"toxi_{uuid4().hex[:8]}"
    t = NATSEventTransport(
        NATSConfig(
            servers=[nats_url],
            subject_prefix=prefix,
            reconnect_time_wait=0.5,
            reconnect_jitter=0.1,
            max_reconnect_attempts=-1,
            connect_timeout=5.0,
        ),
        events_config=EventsConfig(),
    )
    await t.start()
    try:
        yield t, api, proxy_name
    finally:
        # Re-enable so downstream tests aren't starved by a disabled proxy.
        with contextlib.suppress(Exception):
            _toxi_set_enabled(api, proxy_name, True)
        await t.stop()


async def _wait_for_state(
    transport: NATSEventTransport,
    expected: ConnectionState,
    timeout: float = 10.0,
) -> None:
    """Poll transport.connection_state until it matches `expected`."""
    deadline = asyncio.get_event_loop().time() + timeout
    while asyncio.get_event_loop().time() < deadline:
        if transport.connection_state == expected:
            return
        await asyncio.sleep(0.1)
    raise AssertionError(
        f"state did not reach {expected}: current={transport.connection_state}"
    )


async def test_disconnect_transitions_to_reconnecting(
    reconnecting_transport: tuple[NATSEventTransport, str, str],
) -> None:
    t, api, proxy = reconnecting_transport
    assert t.connection_state == ConnectionState.CONNECTED

    _toxi_set_enabled(api, proxy, False)
    await _wait_for_state(t, ConnectionState.RECONNECTING, timeout=15.0)


async def test_reconnect_transitions_back_to_connected(
    reconnecting_transport: tuple[NATSEventTransport, str, str],
) -> None:
    t, api, proxy = reconnecting_transport

    _toxi_set_enabled(api, proxy, False)
    await _wait_for_state(t, ConnectionState.RECONNECTING, timeout=15.0)

    _toxi_set_enabled(api, proxy, True)
    await _wait_for_state(t, ConnectionState.CONNECTED, timeout=15.0)


async def test_delivery_resumes_after_reconnect(
    reconnecting_transport: tuple[NATSEventTransport, str, str],
) -> None:
    t, api, proxy = reconnecting_transport
    received: list[Event] = []

    async def h(e: Event) -> None:
        received.append(e)

    t.subscribe("reconn.>", h)
    await t.flush(timeout=2.0)
    await t.publish(Event(type="reconn.before", source="x", payload={}))
    for _ in range(50):
        if received:
            break
        await asyncio.sleep(0.05)
    assert len(received) == 1

    _toxi_set_enabled(api, proxy, False)
    await _wait_for_state(t, ConnectionState.RECONNECTING, timeout=15.0)

    _toxi_set_enabled(api, proxy, True)
    await _wait_for_state(t, ConnectionState.CONNECTED, timeout=15.0)
    # Allow NATS to re-establish subscription on the new connection
    await asyncio.sleep(0.5)

    await t.publish(Event(type="reconn.after", source="x", payload={}))
    for _ in range(100):
        if len(received) >= 2:
            break
        await asyncio.sleep(0.05)
    assert len(received) == 2
    assert received[1].type == "reconn.after"
