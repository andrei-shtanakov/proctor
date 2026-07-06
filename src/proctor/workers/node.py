"""WorkerNode — the worker-role runtime.

Startup order matters: subscribe → bus.flush() (the transport registers
subscriptions via a background task; flush completes them) → publish
worker.registered. Shutdown order matters more: stop accepting → cancel
AND await the heartbeat loop (a heartbeat escaping after offline would
re-register the worker) → drain executions → worker.offline(shutdown)
→ flush.
"""

import asyncio
import contextlib
import logging
from typing import Any, Protocol
from uuid import uuid4

from proctor.core.bus import EventBus
from proctor.core.config import WorkerConfig
from proctor.core.models import Event
from proctor.workflow.spec import WorkflowSpec

logger = logging.getLogger(__name__)


class _Engine(Protocol):
    async def execute(self, spec: WorkflowSpec) -> Any: ...


class WorkerNode:
    """Register, heartbeat, execute assignments, publish results."""

    def __init__(
        self,
        bus: EventBus,
        config: WorkerConfig,
        engine: _Engine,
        *,
        heartbeat_interval: float,
        drain_timeout: float = 10.0,
    ) -> None:
        self._bus = bus
        self._config = config
        self._engine = engine
        self._interval = heartbeat_interval
        self._drain_timeout = drain_timeout
        self.instance_id = str(uuid4())
        self._source = f"worker:{config.id}"
        self._accepting = False
        self._heartbeat_task: asyncio.Task[None] | None = None
        self._exec_tasks: set[asyncio.Task[None]] = set()

    def _profile_payload(self) -> dict[str, Any]:
        return {
            "worker_id": self._config.id,
            "instance_id": self.instance_id,
            "capabilities": self._config.capabilities,
            "max_slots": self._config.max_slots,
        }

    async def start(self) -> None:
        """Subscribe, wait for subscription readiness, then announce."""
        self._bus.subscribe(f"task.assign.{self._config.id}", self._handle_assign)
        await self._bus.flush()  # readiness barrier — see module docstring
        self._accepting = True
        await self._bus.publish(
            Event(
                type="worker.registered",
                source=self._source,
                payload=self._profile_payload(),
            )
        )
        self._heartbeat_task = asyncio.create_task(self._heartbeat_loop())
        logger.info(
            "WorkerNode %s started (instance %s)",
            self._config.id,
            self.instance_id,
        )

    async def stop(self) -> None:
        """Tear down in the safe order (no post-offline heartbeats)."""
        self._accepting = False
        if self._heartbeat_task is not None:
            self._heartbeat_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                try:
                    await self._heartbeat_task
                except Exception:
                    logger.exception("Heartbeat loop exited with error")
            self._heartbeat_task = None
        if self._exec_tasks:
            _, pending = await asyncio.wait(
                self._exec_tasks, timeout=self._drain_timeout
            )
            for exec_task in pending:
                exec_task.cancel()
            if pending:
                await asyncio.gather(*pending, return_exceptions=True)
        await self._bus.publish(
            Event(
                type="worker.offline",
                source=self._source,
                payload={
                    "worker_id": self._config.id,
                    "instance_id": self.instance_id,
                    "reason": "shutdown",
                },
            )
        )
        await self._bus.flush()
        logger.info("WorkerNode %s stopped", self._config.id)

    async def _heartbeat_loop(self) -> None:
        while True:
            await asyncio.sleep(self._interval)
            try:
                await self._bus.publish(
                    Event(
                        type="worker.heartbeat",
                        source=self._source,
                        payload=self._profile_payload(),
                    )
                )
            except Exception:
                logger.exception("Heartbeat publish failed")

    async def _handle_assign(self, event: Event) -> None:
        if not self._accepting:
            return
        payload = event.payload
        if payload.get("target_instance_id") != self.instance_id:
            logger.warning(
                "Dropping assignment for foreign instance %s",
                payload.get("target_instance_id"),
            )
            return
        dispatch_id = payload.get("dispatch_id")
        task_payload = payload.get("task") or {}
        task_id = task_payload.get("id")
        if not isinstance(dispatch_id, str) or not isinstance(task_id, str):
            logger.warning("Malformed task.assign payload: %s", payload)
            return
        if len(self._exec_tasks) >= self._config.max_slots:
            await self._publish_result(
                task_id, dispatch_id, ok=False, error="worker_busy"
            )
            return
        spec = WorkflowSpec.model_validate(payload.get("spec"))
        exec_task = asyncio.create_task(self._execute(task_id, dispatch_id, spec))
        self._exec_tasks.add(exec_task)
        exec_task.add_done_callback(self._exec_tasks.discard)

    async def _execute(
        self, task_id: str, dispatch_id: str, spec: WorkflowSpec
    ) -> None:
        try:
            result = await self._engine.execute(spec)
            if result.error:
                await self._publish_result(
                    task_id, dispatch_id, ok=False, error=result.error
                )
            else:
                await self._publish_result(
                    task_id, dispatch_id, ok=True, output=result.output
                )
        except Exception as exc:
            logger.exception("Execution of task %s crashed", task_id)
            with contextlib.suppress(Exception):
                await self._publish_result(
                    task_id, dispatch_id, ok=False, error=str(exc)
                )

    async def _publish_result(
        self,
        task_id: str,
        dispatch_id: str,
        *,
        ok: bool,
        output: str | None = None,
        error: str | None = None,
    ) -> None:
        await self._bus.publish(
            Event(
                type="task.result",
                source=self._source,
                payload={
                    "task_id": task_id,
                    "dispatch_id": dispatch_id,
                    "worker_id": self._config.id,
                    "instance_id": self.instance_id,
                    "ok": ok,
                    "output": output,
                    "error": error,
                },
            )
        )
