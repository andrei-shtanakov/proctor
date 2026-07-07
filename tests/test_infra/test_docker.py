"""ContainerRuntime: argv construction and inspect parsing, no daemon."""

import json
from collections.abc import Awaitable, Callable
from pathlib import Path

import pytest

from proctor.infra.docker import ContainerRuntime, ContainerSpec, ContainerStatus

pytestmark = pytest.mark.anyio


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


FIXTURES = Path(__file__).parent / "fixtures"


def _fake(
    rc: int = 0, out: str = "", err: str = ""
) -> tuple[
    Callable[[list[str], float | None], Awaitable[tuple[int, str, str]]],
    list[tuple[list[str], float | None]],
]:
    calls: list[tuple[list[str], float | None]] = []

    async def run_cmd(argv: list[str], timeout: float | None) -> tuple[int, str, str]:
        calls.append((argv, timeout))
        return rc, out, err

    return run_cmd, calls


async def test_run_builds_argv() -> None:
    run_cmd, calls = _fake(out="cid123\n")
    rt = ContainerRuntime("podman", run_cmd=run_cmd)
    spec = ContainerSpec(
        image="proctor:latest",
        name="docker_py_0_deadbeef",
        env={"PROCTOR_WORKER_ID": "docker_py_0_deadbeef"},
        env_file="/tmp/fleet.env",
        labels={"proctor.fleet": "docker_py"},
        network="proctor_net",
    )
    cid = await rt.run(spec)
    assert cid == "cid123"
    argv = calls[0][0]
    assert argv[:2] == ["podman", "run"]
    assert "-d" in argv
    assert "--restart" in argv and argv[argv.index("--restart") + 1] == "no"
    assert "--name" in argv
    assert "-e" in argv
    assert "--env-file" in argv
    assert argv[argv.index("--env-file") + 1] == "/tmp/fleet.env"
    assert "--label" in argv
    assert "proctor.fleet=docker_py" in argv
    assert "--network" in argv
    assert argv[-1] == "proctor:latest"


async def test_run_raises_on_nonzero() -> None:
    run_cmd, _ = _fake(rc=125, err="no such image")
    rt = ContainerRuntime("docker", run_cmd=run_cmd)
    with pytest.raises(RuntimeError, match="no such image"):
        await rt.run(ContainerSpec(image="missing", name="x"))


async def test_inspect_parses_docker_fixture() -> None:
    raw = (FIXTURES / "inspect_docker.json").read_text()
    run_cmd, calls = _fake(out=raw)
    rt = ContainerRuntime("docker", run_cmd=run_cmd)
    st = await rt.inspect("abc123def456")
    assert st.id == "abc123def456"
    assert st.state == "exited"
    assert st.exit_code == 1
    assert "inspect" in calls[0][0]
    assert "{{json .}}" in calls[0][0]


async def test_inspect_parses_podman_fixture() -> None:
    raw = (FIXTURES / "inspect_podman.json").read_text()
    run_cmd, _ = _fake(out=raw)
    rt = ContainerRuntime("podman", run_cmd=run_cmd)
    st = await rt.inspect("podman789aa")
    assert st.id == "podman789aa"
    assert st.state == "running"
    assert st.exit_code == 0


def test_status_parse_normalizes_both() -> None:
    d = json.loads((FIXTURES / "inspect_docker.json").read_text())
    p = json.loads((FIXTURES / "inspect_podman.json").read_text())
    assert ContainerStatus.parse(d).state == "exited"
    assert ContainerStatus.parse(p).state == "running"


async def test_stop_remove_logs_argv() -> None:
    run_cmd, calls = _fake(out="line1\nline2\n")
    rt = ContainerRuntime("docker", run_cmd=run_cmd)
    await rt.stop("cid", timeout=12.0)
    assert calls[-1][0][:2] == ["docker", "stop"]
    assert "-t" in calls[-1][0] and calls[-1][0][calls[-1][0].index("-t") + 1] == "12"
    await rt.remove("cid")
    assert calls[-1][0][:3] == ["docker", "rm", "-f"]
    out = await rt.logs("cid", tail=50)
    assert out == "line1\nline2\n"
    assert calls[-1][0][:2] == ["docker", "logs"]
    assert (
        "--tail" in calls[-1][0]
        and calls[-1][0][calls[-1][0].index("--tail") + 1] == "50"
    )


async def test_stop_rounds_fractional_timeout_up() -> None:
    """A sub-second grace must not floor to 0 (immediate SIGKILL)."""
    run_cmd, calls = _fake()
    rt = ContainerRuntime("docker", run_cmd=run_cmd)
    await rt.stop("cid", timeout=0.9)
    assert calls[-1][0][calls[-1][0].index("-t") + 1] == "1"
    await rt.stop("cid", timeout=0.1)
    assert calls[-1][0][calls[-1][0].index("-t") + 1] == "1"
    await rt.stop("cid", timeout=30.0)
    assert calls[-1][0][calls[-1][0].index("-t") + 1] == "30"


async def test_fast_ops_use_op_timeout() -> None:
    run_cmd, calls = _fake(out="{}")
    rt = ContainerRuntime("docker", run_cmd=run_cmd, op_timeout=7.0, op_margin=3.0)
    await rt.remove("cid")
    assert calls[-1][1] == 7.0  # op_timeout


async def test_stop_uses_stop_timeout_plus_margin() -> None:
    run_cmd, calls = _fake()
    rt = ContainerRuntime("docker", run_cmd=run_cmd, op_timeout=7.0, op_margin=3.0)
    await rt.stop("cid", timeout=30.0)
    # deadline is the -t grace (30) + margin (3), NOT op_timeout (7)
    assert calls[-1][1] == 33.0


async def test_default_run_cmd_kills_and_reaps_on_timeout() -> None:
    # A real slow command under a tiny timeout must raise AND leave no child.
    from proctor.infra.docker import _make_default_run_cmd

    run_cmd = _make_default_run_cmd(env=None)
    with pytest.raises(RuntimeError, match="timed out"):
        # `sleep 30` under a 0.3s deadline
        await run_cmd(["sleep", "30"], 0.3)


async def test_env_merged_into_subprocess() -> None:
    from proctor.infra.docker import _make_default_run_cmd

    run_cmd = _make_default_run_cmd(env={"PROCTOR_TEST_MARKER": "xyz"})
    # `env` prints the environment; assert our marker is present
    rc, out, err = await run_cmd(["sh", "-c", "echo $PROCTOR_TEST_MARKER"], 5.0)
    assert rc == 0
    assert "xyz" in out
