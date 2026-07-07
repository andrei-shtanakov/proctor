"""Real container worker managed by the core (podman/docker + NATS).

Gated by the `docker` marker: a container runtime binary (podman or
docker), a reachable NATS server (mirrors test_distribution_nats.py's
NATS_URL/testcontainers conventions via the shared `nats_url` fixture),
and a pre-built `proctor:latest` image must all be present. This test
does not build the image — it skips cleanly if it is missing.

Shape: a core Application (transport=nats) declares one docker_workers
fleet (replicas=1, image proctor:latest). The manager launches the
container, the container's own worker process self-registers over
NATS, the core dispatches a requires-gated task to it, and the test
kills the container to prove the poll loop relaunches it under a fresh
worker_id (fresh-id fencing — see infra/docker.py and workers/docker.py
docstrings). One test is enough.
"""

from __future__ import annotations

import asyncio
import os
import shutil
from pathlib import Path
from uuid import uuid4

import anyio
import pytest

from proctor.core.bootstrap import Application
from proctor.core.config import (
    DockerWorkerConfig,
    EventsConfig,
    NATSConfig,
    ProctorConfig,
    RegistryConfig,
    RouterConfig,
    RouteRule,
    WorkerConfig,
    docker_ssh_env,
)
from proctor.core.models import Event
from proctor.workflow.spec import WorkflowMode, WorkflowPolicies, WorkflowSpec

pytestmark = [pytest.mark.anyio, pytest.mark.docker]

IMAGE = "proctor:latest"


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


def _runtime_name() -> str | None:
    """Prefer podman, then docker — mirrors DockerWorkerConfig.runtime."""
    if shutil.which("podman"):
        return "podman"
    if shutil.which("docker"):
        return "docker"
    return None


async def _image_present(binary: str, image: str) -> bool:
    proc = await asyncio.create_subprocess_exec(
        binary,
        "image",
        "inspect",
        image,
        stdout=asyncio.subprocess.DEVNULL,
        stderr=asyncio.subprocess.DEVNULL,
    )
    return await proc.wait() == 0


def _container_facing_nats_url(nats_url: str) -> str:
    """Rewrite a host-reachable NATS URL for reachability from a container.

    `nats_url` (env `NATS_URL` or testcontainers) is valid from the test
    process's network namespace. A podman/docker container sees a
    different loopback; `host.docker.internal` is the conventional
    bridge both runtimes provide.
    """
    return nats_url.replace("localhost", "host.docker.internal").replace(
        "127.0.0.1", "host.docker.internal"
    )


def _core_config(
    tmp_path: Path,
    nats_url: str,
    prefix: str,
    runtime_name: str,
    ssh_host: str | None = None,
) -> ProctorConfig:
    policies = WorkflowPolicies(max_runtime_seconds=900)
    return ProctorConfig(
        data_dir=tmp_path / "core",
        transport="nats",
        nats=NATSConfig(servers=[nats_url], subject_prefix=prefix, name="core"),
        events=EventsConfig(max_payload=65_536),
        router=RouterConfig(max_concurrency=4, queue_tick_seconds=0.05),
        registry=RegistryConfig(heartbeat_interval=0.5, liveness_timeout=90.0),
        worker=WorkerConfig(id="local", max_slots=1),
        workflows={
            "job": WorkflowSpec(
                workflow_id="job",
                mode=WorkflowMode.SIMPLE,
                requires=["python"],
                policies=policies,
            ),
        },
        routes=[
            RouteRule(
                event_pattern="trigger.terminal",
                workflow_id="job",
                prompt_from_payload="text",
            ),
        ],
        docker_workers=[
            DockerWorkerConfig(
                image=IMAGE,
                base_worker_id="docker_it",
                capabilities=["python"],
                replicas=1,
                runtime=runtime_name,  # type: ignore[arg-type]
                nats_servers=[_container_facing_nats_url(nats_url)],
                ssh_host=ssh_host,
                poll_interval=0.5,
                stop_timeout=5.0,
                base_backoff=0.2,
                max_backoff=1.0,
                stability_window=1.0,
            ),
        ],
    )


async def _wait_for(
    collected: list[Event], event_type: str, timeout: float = 30.0
) -> Event:
    with anyio.fail_after(timeout):
        while True:
            for e in collected:
                if e.type == event_type:
                    return e
            await anyio.sleep(0.05)


async def _assert_register_dispatch_restart_teardown(
    core_config: ProctorConfig, runtime_name: str
) -> None:
    """Register, dispatch, crash-restart under a fresh id, then teardown.

    Shared by `test_docker_worker_lifecycle` and the ssh_host=None
    regression guard below — both must observe identical local-path
    behavior, so they run the exact same assertions against their own
    (independently built) config.
    """
    core = Application(core_config)

    async def core_llm(prompt: str) -> str:
        return "core should never run this workflow"

    core.set_llm_call(core_llm)

    collected: list[Event] = []

    async def collect(event: Event) -> None:
        collected.append(event)

    core.bus.subscribe("task.>", collect)
    core.bus.subscribe("worker.registered", collect)
    core.bus.subscribe("docker_worker.>", collect)
    await core.start()
    manager = core._docker_managers[0]
    try:
        registered = await _wait_for(collected, "worker.registered", timeout=60.0)
        first_worker_id = registered.payload["worker_id"]

        await core.bus.publish(
            Event(type="trigger.terminal", source="terminal", payload={"text": "go"})
        )
        done = await _wait_for(collected, "task.completed", timeout=30.0)
        assert done.payload.get("output")

        container_id = manager.slots[0].container_id
        kill = await asyncio.create_subprocess_exec(runtime_name, "kill", container_id)
        assert await kill.wait() == 0

        await _wait_for(collected, "docker_worker.restarted", timeout=30.0)
        collected.clear()
        re_registered = await _wait_for(collected, "worker.registered", timeout=60.0)
        assert re_registered.payload["worker_id"] != first_worker_id
    finally:
        stopped_container_ids = [s.container_id for s in manager.slots.values()]
        await core.stop()

    for cid in stopped_container_ids:
        inspect = await asyncio.create_subprocess_exec(
            runtime_name,
            "inspect",
            cid,
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.DEVNULL,
        )
        assert await inspect.wait() != 0, f"container {cid} still present after stop()"


async def test_docker_worker_lifecycle(tmp_path: Path, nats_url: str) -> None:
    """Register, dispatch, crash-restart under a fresh id, then teardown."""
    runtime_name = _runtime_name()
    if runtime_name is None:
        pytest.skip("neither podman nor docker found on PATH")
    if not await _image_present(runtime_name, IMAGE):
        pytest.skip(f"{IMAGE} image not present locally; this test does not build it")

    prefix = f"proctor_test_{uuid4().hex[:8]}"
    config = _core_config(tmp_path, nats_url, prefix, runtime_name)
    await _assert_register_dispatch_restart_teardown(config, runtime_name)


async def test_docker_worker_local_regression_ssh_host_none(
    tmp_path: Path, nats_url: str
) -> None:
    """Regression guard: ssh_host=None must behave exactly as before.

    Task 2/3 added `ssh_host`, `op_timeout`/`op_margin`, and
    `max_unreachable_duration` to `DockerWorkerConfig`, plus the
    `docker_ssh_env()` helper that injects `DOCKER_HOST`/`CONTAINER_HOST`
    for remote fleets. This test pins down that an explicit
    `ssh_host=None` fleet (i) gets no such env injected and (ii) still
    registers, dispatches, restarts on crash under a fresh worker_id, and
    tears down cleanly — the same local-path contract as before those
    changes landed.
    """
    runtime_name = _runtime_name()
    if runtime_name is None:
        pytest.skip("neither podman nor docker found on PATH")
    if not await _image_present(runtime_name, IMAGE):
        pytest.skip(f"{IMAGE} image not present locally; this test does not build it")

    prefix = f"proctor_test_{uuid4().hex[:8]}"
    config = _core_config(tmp_path, nats_url, prefix, runtime_name, ssh_host=None)
    fleet = config.docker_workers[0]
    assert fleet.ssh_host is None
    assert docker_ssh_env(fleet) == {}

    await _assert_register_dispatch_restart_teardown(config, runtime_name)


@pytest.mark.skipif(
    not os.environ.get("PROCTOR_TEST_SSH_HOST"),
    reason=(
        "set PROCTOR_TEST_SSH_HOST=[user@]host[:port] to run the live "
        "remote-docker flow (needs a reachable remote runtime + key)"
    ),
)
async def test_docker_worker_remote_lifecycle_sketch(
    tmp_path: Path, nats_url: str
) -> None:
    """Sketch of the live remote flow — not implemented, skip-gated.

    Exercising this for real needs a remote host reachable over SSH with
    a container runtime and a pre-built `proctor:latest` image, which
    this test suite/CI does not provision. The shape of a live version:

    # ssh_host = os.environ["PROCTOR_TEST_SSH_HOST"]
    # runtime_name = _runtime_name()
    # prefix = f"proctor_test_{uuid4().hex[:8]}"
    # config = _core_config(
    #     tmp_path, nats_url, prefix, runtime_name, ssh_host=ssh_host
    # )
    # core = Application(config)
    # core.set_llm_call(core_llm)  # as in the local tests above
    # core.bus.subscribe("task.>", collect)
    # core.bus.subscribe("worker.registered", collect)
    # core.bus.subscribe("docker_worker.>", collect)
    # await core.start()
    # registered = await _wait_for(collected, "worker.registered", 60.0)
    # first_worker_id = registered.payload["worker_id"]
    #
    # await core.bus.publish(Event(type="trigger.terminal", ...))
    # await _wait_for(collected, "task.completed", 30.0)
    #
    # # Kill the container on the *remote* host — DOCKER_HOST=ssh://...
    # # only redirects the manager's own docker CLI calls, so killing it
    # # from here needs an explicit ssh hop:
    # #   ssh {ssh_host} docker kill {container_id}
    #
    # await _wait_for(collected, "docker_worker.restarted", 30.0)
    # collected.clear()
    # re_registered = await _wait_for(collected, "worker.registered", 60.0)
    # assert re_registered.payload["worker_id"] != first_worker_id
    # await core.stop()
    """
    pytest.skip("remote sketch — fill in against a real PROCTOR_TEST_SSH_HOST target")
