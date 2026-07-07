"""Runtime-agnostic async wrapper over the container CLI (docker|podman).

All operations shell out via an injected exec function so tests need no
daemon. inspect() reads structured `--format '{{json .}}'` output only —
never scraped human text — and ContainerStatus.parse normalizes the
docker-vs-podman JSON shape into one model.
"""

import asyncio
import json
import math
from collections.abc import Awaitable, Callable
from typing import Any

from pydantic import BaseModel, Field

RunCmd = Callable[[list[str]], Awaitable[tuple[int, str, str]]]


async def _default_run_cmd(argv: list[str]) -> tuple[int, str, str]:
    proc = await asyncio.create_subprocess_exec(
        *argv,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    out, err = await proc.communicate()
    return (
        proc.returncode or 0,
        out.decode(errors="replace"),
        err.decode(errors="replace"),
    )


class ContainerSpec(BaseModel):
    """Declarative inputs for `run`."""

    image: str
    name: str
    env: dict[str, str] = Field(default_factory=dict)
    env_file: str | None = None
    labels: dict[str, str] = Field(default_factory=dict)
    network: str | None = None
    restart_policy: str = "no"


class ContainerStatus(BaseModel):
    """Normalized subset of `inspect` across docker and podman."""

    id: str
    state: str
    exit_code: int
    started_at: str

    @classmethod
    def parse(cls, raw: dict[str, Any]) -> "ContainerStatus":
        state = raw.get("State") or {}
        return cls(
            id=str(raw.get("Id", "")),
            state=str(state.get("Status", "unknown")),
            exit_code=int(state.get("ExitCode", 0) or 0),
            started_at=str(state.get("StartedAt", "")),
        )


class ContainerRuntime:
    """Async CLI wrapper; `binary` is `docker` or `podman`."""

    def __init__(self, binary: str, run_cmd: RunCmd | None = None) -> None:
        self._binary = binary
        self._run = run_cmd or _default_run_cmd

    async def _exec(self, args: list[str]) -> str:
        argv = [self._binary, *args]
        rc, out, err = await self._run(argv)
        if rc != 0:
            raise RuntimeError(
                f"{' '.join(argv)} exited {rc}: {err.strip() or out.strip()}"
            )
        return out

    async def run(self, spec: ContainerSpec) -> str:
        args = [
            "run",
            "-d",
            "--name",
            spec.name,
            "--restart",
            spec.restart_policy,
        ]
        for key, value in spec.env.items():
            args += ["-e", f"{key}={value}"]
        if spec.env_file is not None:
            args += ["--env-file", spec.env_file]
        for key, value in spec.labels.items():
            args += ["--label", f"{key}={value}"]
        if spec.network is not None:
            args += ["--network", spec.network]
        args.append(spec.image)
        return (await self._exec(args)).strip()

    async def inspect(self, container_id: str) -> ContainerStatus:
        out = await self._exec(["inspect", "--format", "{{json .}}", container_id])
        return ContainerStatus.parse(json.loads(out))

    async def stop(self, container_id: str, timeout: float) -> None:
        # `-t` takes whole seconds; round UP so a fractional grace period
        # never floors to 0 (which would SIGKILL immediately, defeating
        # the drain window).
        secs = max(1, math.ceil(timeout)) if timeout > 0 else 0
        await self._exec(["stop", "-t", str(secs), container_id])

    async def remove(self, container_id: str) -> None:
        await self._exec(["rm", "-f", container_id])

    async def logs(self, container_id: str, tail: int) -> str:
        return await self._exec(["logs", "--tail", str(tail), container_id])
