"""Configuration system: YAML loading with pydantic models and defaults."""

import logging
import os
import re
from pathlib import Path
from typing import Annotated, Any, Literal, Self
from urllib.parse import urlsplit

import yaml
from croniter import croniter
from pydantic import BaseModel, ConfigDict, Field, model_validator

from proctor.core.globs import is_strictly_broader
from proctor.workflow.spec import WorkflowSpec

logger = logging.getLogger(__name__)

# Hosts that never resolve back to the core from a remote docker fleet's
# host (loopback/docker-bridge addresses meaningful only on that box).
# Compared by exact hostname (via urlsplit), never by substring — a
# substring check would false-reject look-alikes like 172.17.0.10 or
# nats://[2001:db8::1]:4222 (which merely contains "::1").
_UNROUTABLE_NATS_HOSTS = frozenset(
    {"host.docker.internal", "localhost", "127.0.0.1", "::1", "172.17.0.1"}
)


class LLMConfig(BaseModel):
    """LLM provider configuration."""

    default_model: str = "claude-sonnet-4-20250514"
    fallback_model: str | None = None
    max_tokens: int = 4096
    temperature: float = 0.7
    request_timeout: float = 60.0
    max_retries: int = 1


class NATSConfig(BaseModel):
    """NATS client configuration."""

    model_config = ConfigDict(extra="forbid")

    servers: list[str] = ["nats://localhost:4222"]
    name: str = ""
    subject_prefix: str = "proctor"
    connect_timeout: float = 5.0
    reconnect_time_wait: float = 2.0
    reconnect_jitter: float = Field(default=0.5, ge=0.0, le=5.0)
    max_reconnect_attempts: int = -1
    user: str | None = None
    user_env: str | None = None
    password_env: str | None = None
    tls_ca: Path | None = None
    tls_client_cert: Path | None = None
    tls_client_key: Path | None = None

    @model_validator(mode="after")
    def _populate_name_fallback(self) -> Self:
        if not self.name:
            import socket

            self.name = f"proctor-{socket.gethostname()}"
        return self

    @model_validator(mode="after")
    def _user_exclusive(self) -> Self:
        if self.user and self.user_env:
            raise ValueError("Set either NATSConfig.user or user_env, not both")
        return self


class EventsConfig(BaseModel):
    """Shared events configuration across all EventTransport backends."""

    model_config = ConfigDict(extra="forbid")
    max_payload: int = 65_536  # bytes; shared with NATS server limits
    drain_timeout: float = 60.0  # seconds; LLM-heavy handlers need headroom


class ScheduleItemConfig(BaseModel):
    """A single scheduled task definition."""

    name: str
    cron: str | None = None
    interval_seconds: float | None = None
    payload: dict[str, Any] = Field(default_factory=dict)
    enabled: bool = True

    @model_validator(mode="after")
    def validate_schedule(self) -> "ScheduleItemConfig":
        """Validate schedule type, cron syntax, and interval value."""
        has_cron = self.cron is not None
        has_interval = self.interval_seconds is not None
        if has_cron == has_interval:
            raise ValueError(
                "Exactly one of 'cron' or 'interval_seconds' "
                "must be set, not both or neither."
            )
        if self.cron is not None and not croniter.is_valid(self.cron):
            raise ValueError(f"Invalid cron expression: {self.cron!r}")
        if self.interval_seconds is not None and self.interval_seconds <= 0:
            raise ValueError("interval_seconds must be greater than 0")
        return self


class RouteRule(BaseModel):
    """Declarative rule: event pattern → catalog workflow + prompt binding."""

    event_pattern: str
    workflow_id: str
    prompt: str | None = None
    prompt_from_payload: str | None = None

    @model_validator(mode="after")
    def _exactly_one_prompt_source(self) -> Self:
        sources = (self.prompt is not None, self.prompt_from_payload is not None)
        if sum(sources) != 1:
            raise ValueError(
                "RouteRule must specify exactly one of: prompt, prompt_from_payload"
            )
        return self


class SchedulerConfig(BaseModel):
    """Task scheduler configuration."""

    poll_interval_seconds: int = 30
    enabled: bool = True


class TelegramConfig(BaseModel):
    """Telegram trigger configuration."""

    bot_token: str
    allowed_chat_ids: list[int] = Field(default_factory=list)
    poll_timeout: int = 30


class HMACAuthConfig(BaseModel):
    """HMAC-SHA256 auth for a webhook path."""

    model_config = ConfigDict(extra="forbid")
    type: Literal["hmac"] = "hmac"
    secret_env: str = Field(pattern=r"^[A-Z][A-Z0-9_]*$")
    header: str = "X-Hub-Signature-256"
    # Header-value prefix stripped before hex comparison. Configures
    # header SHAPE only, not the HMAC base string — Slack and Stripe
    # sign a constructed string (e.g. "v0:{timestamp}:{body}"), which
    # this implementation does NOT produce. Real Slack/Stripe support
    # needs dedicated auth.type: "slack" / "stripe" — see Risks in
    # docs/superpowers/specs/2026-04-15-webhook-trigger-design.md.
    prefix: str = "sha256="


class BearerAuthConfig(BaseModel):
    """Bearer-token auth for a webhook path (RFC 6750)."""

    model_config = ConfigDict(extra="forbid")
    type: Literal["bearer"] = "bearer"
    secret_env: str = Field(pattern=r"^[A-Z][A-Z0-9_]*$")
    header: str = "Authorization"


class NoneAuthConfig(BaseModel):
    """No authentication. Explicit opt-in. Triggers startup WARNING."""

    model_config = ConfigDict(extra="forbid")
    type: Literal["none"] = "none"


AuthConfig = Annotated[
    HMACAuthConfig | BearerAuthConfig | NoneAuthConfig,
    Field(discriminator="type"),
]


class WebhookPathConfig(BaseModel):
    """Per-path webhook configuration."""

    model_config = ConfigDict(extra="forbid")
    source_name: str | None = None
    auth: AuthConfig


_SOURCE_NAME_RE = re.compile(r"[a-z][a-z0-9_]*")


class WebhookConfig(BaseModel):
    """Webhook trigger configuration (aiohttp server)."""

    model_config = ConfigDict(extra="forbid")
    host: str = "127.0.0.1"
    port: int = Field(default=8080, ge=0, le=65535)
    paths: dict[str, WebhookPathConfig] = Field(min_length=1)
    max_in_flight: int = 20
    max_body_bytes: int = 1_048_576
    shutdown_timeout: float = 30.0
    keepalive_timeout: float = 75.0

    @model_validator(mode="after")
    def _validate_paths(self) -> Self:
        """Validate path format; derive + uniqueness-check source_name."""
        seen_names: dict[str, str] = {}
        for path, path_cfg in self.paths.items():
            if not path.startswith("/"):
                raise ValueError(f"webhook path {path!r} must start with '/'")
            if path == "/":
                raise ValueError(f"webhook path {path!r} is not allowed")
            if path.endswith("/"):
                raise ValueError(f"webhook path {path!r} has a trailing '/'")
            if any(c in path for c in "*?["):
                raise ValueError(
                    f"webhook path {path!r} contains a fnmatch metacharacter (*, ?, [)"
                )
            effective = path_cfg.source_name or path.rsplit("/", 1)[-1]
            if not _SOURCE_NAME_RE.fullmatch(effective):
                raise ValueError(
                    f"webhook path {path!r}: source_name "
                    f"{effective!r} must match "
                    f"^[a-z][a-z0-9_]*$"
                )
            if effective in {"", "*", "?"}:
                raise ValueError(
                    f"webhook path {path!r}: source_name {effective!r} is reserved"
                )
            if effective in seen_names:
                raise ValueError(
                    f"webhook source_name uniqueness: "
                    f"{effective!r} used by both "
                    f"{seen_names[effective]!r} and {path!r}"
                )
            seen_names[effective] = path
            path_cfg.source_name = effective
        return self


class RouterConfig(BaseModel):
    """TaskRouter admission settings."""

    max_concurrency: int = Field(default=4, ge=1)
    # 0 = reject immediately, never queue
    queue_ttl_seconds: float = Field(default=600.0, ge=0.0)
    # must stay > 0: asyncio.sleep(0) would spin the tick loop hot
    queue_tick_seconds: float = Field(default=30.0, gt=0.0)

    @model_validator(mode="before")
    @classmethod
    def _reject_legacy_agent(cls, data: object) -> object:
        if isinstance(data, dict) and "agent" in data:
            raise ValueError(
                "router.agent has been removed; configure worker.max_slots "
                "(top-level worker: section) instead"
            )
        return data


class WorkerConfig(BaseModel):
    """Identity and capacity of this node's executor."""

    id: str = Field(default="local", pattern=r"^[a-z][a-z0-9_]*$")
    capabilities: list[str] = Field(default_factory=list)
    max_slots: int = Field(default=4, ge=1)


class DockerWorkerConfig(BaseModel):
    """One declared fleet of container-based workers."""

    image: str
    base_worker_id: str = Field(pattern=r"^[a-z][a-z0-9_]*$")
    capabilities: list[str] = Field(default_factory=list)
    replicas: int = Field(default=1, ge=1)
    runtime: Literal["docker", "podman"] = "docker"
    nats_servers: list[str] = Field(
        default_factory=lambda: ["nats://host.docker.internal:4222"]
    )
    env: dict[str, str] = Field(default_factory=dict)
    secret_env: list[str] = Field(default_factory=list)
    network: str | None = None
    ssh_host: str | None = None
    op_timeout: float = Field(default=30.0, gt=0.0)
    op_margin: float = Field(default=10.0, gt=0.0)
    max_unreachable_duration: float = Field(default=120.0, gt=0.0)
    poll_interval: float = Field(default=2.0, gt=0.0)
    stop_timeout: float = Field(default=30.0, gt=0.0)
    base_backoff: float = Field(default=1.0, gt=0.0)
    max_backoff: float = Field(default=60.0, gt=0.0)
    max_restarts: int = Field(default=5, ge=1)
    stability_window: float = Field(default=60.0, gt=0.0)
    log_tail: int = Field(default=50, ge=1)

    @model_validator(mode="after")
    def _validate_ssh_host(self) -> Self:
        # Reject ANY scheme (not just ssh://): the prefix is added
        # automatically, so http://box would become ssh://http://box.
        if self.ssh_host is not None and "://" in self.ssh_host:
            raise ValueError(
                "ssh_host must be [user@]host[:port] without a scheme; "
                "the 'ssh://' prefix is added automatically"
            )
        return self

    @model_validator(mode="after")
    def _validate_remote_nats_reachable(self) -> Self:
        if self.ssh_host is None:
            return self
        for server in self.nats_servers:
            # A scheme-less `host:port` misparses (hostname None); retry
            # with a `//` netloc marker so the check is never silently
            # skipped for a remote fleet.
            hostname = urlsplit(server).hostname or urlsplit(f"//{server}").hostname
            if hostname is not None and hostname.lower() in _UNROUTABLE_NATS_HOSTS:
                raise ValueError(
                    f"remote fleet nats_servers {server!r} is unroutable from "
                    "the remote host; set nats_servers to a core address "
                    "reachable from there"
                )
        return self


def docker_ssh_env(fleet: DockerWorkerConfig) -> dict[str, str]:
    """Env that points a fleet's container client at its remote socket.

    docker → DOCKER_HOST, podman → CONTAINER_HOST; local (no ssh_host) → {}.
    """
    if fleet.ssh_host is None:
        return {}
    url = f"ssh://{fleet.ssh_host}"
    if fleet.runtime == "podman":
        return {"CONTAINER_HOST": url}
    return {"DOCKER_HOST": url}


class RegistryConfig(BaseModel):
    """Worker discovery/liveness settings.

    ``liveness_timeout`` is core/standalone-only — it drives the
    registry's sweep for silent workers. ``heartbeat_interval`` is
    read by *both* roles: the core uses it for the registry sweep
    cadence, and worker nodes read it (bootstrap's worker branch) to
    set their own heartbeat loop's interval. A worker's
    ``heartbeat_interval`` must stay comfortably under the core's
    ``liveness_timeout`` — this is a cross-node coupling that per-node
    config validation cannot check; a mismatch causes liveness
    flapping (workers appearing to go offline and back) with task
    casualties on the affected dispatches.
    """

    heartbeat_interval: float = Field(default=30.0, gt=0.0)
    liveness_timeout: float = Field(default=90.0, gt=0.0)

    @model_validator(mode="after")
    def _liveness_exceeds_heartbeat(self) -> Self:
        if self.liveness_timeout <= self.heartbeat_interval:
            raise ValueError("registry.liveness_timeout must exceed heartbeat_interval")
        return self


class ProctorConfig(BaseModel):
    """Root configuration model with nested configs."""

    node_role: Literal["standalone", "core", "worker"] = "standalone"
    node_id: str = "node-1"
    transport: Literal["auto", "local", "nats"] = "auto"
    nats_url: str = "nats://localhost:4222"
    data_dir: Path = Path("data")
    log_level: str = "INFO"
    llm: LLMConfig = LLMConfig()
    nats: NATSConfig = NATSConfig()
    events: EventsConfig = EventsConfig()
    router: RouterConfig = RouterConfig()
    worker: WorkerConfig = WorkerConfig()
    registry: RegistryConfig = RegistryConfig()
    scheduler: SchedulerConfig = SchedulerConfig()
    telegram: TelegramConfig | None = None
    webhook: WebhookConfig | None = None
    schedules: list[ScheduleItemConfig] = []
    workflows: dict[str, WorkflowSpec] = Field(default_factory=dict)
    routes: list[RouteRule] = Field(default_factory=list)
    docker_workers: list[DockerWorkerConfig] = Field(default_factory=list)

    @model_validator(mode="after")
    def _validate_transport_consistency(self) -> Self:
        """Cross-field validation: transport mode vs nats config."""
        mode = _resolve_transport_mode_static(self.transport, self.node_role)
        if mode == "nats" and not self.nats.servers:
            raise ValueError(
                "transport resolves to 'nats' but nats.servers is empty. "
                "Set nats.servers or transport='local'."
            )
        if mode == "local" and self.nats != NATSConfig():
            logger.warning(
                "transport resolved to 'local'; nats config is set "
                "but will be ignored. Use transport='nats' to enforce."
            )
        return self

    @model_validator(mode="after")
    def _worker_role_requires_explicit_id(self) -> Self:
        if self.node_role == "worker" and self.worker.id == "local":
            raise ValueError(
                "node_role 'worker' requires an explicit worker.id; "
                "'local' is reserved for the core's inline executor"
            )
        return self

    @model_validator(mode="after")
    def _validate_catalog_keys(self) -> Self:
        """Ensure catalog key matches spec.workflow_id."""
        for key, spec in self.workflows.items():
            if spec.workflow_id != key:
                raise ValueError(
                    f"workflow catalog key {key!r} does not match "
                    f"spec.workflow_id {spec.workflow_id!r}"
                )
        return self

    @model_validator(mode="after")
    def _validate_route_refs(self) -> Self:
        for i, rule in enumerate(self.routes):
            if rule.workflow_id not in self.workflows:
                raise ValueError(
                    f"route #{i} pattern={rule.event_pattern!r} references "
                    f"unknown workflow_id {rule.workflow_id!r}. "
                    f"Known workflows: {sorted(self.workflows)}"
                )
        return self

    @model_validator(mode="after")
    def _no_shadowed_routes(self) -> Self:
        for i, earlier in enumerate(self.routes):
            for j_offset, later in enumerate(self.routes[i + 1 :]):
                j = i + 1 + j_offset
                if is_strictly_broader(earlier.event_pattern, later.event_pattern):
                    raise ValueError(
                        f"route #{i} pattern={earlier.event_pattern!r} "
                        f"shadows route #{j} pattern={later.event_pattern!r}. "
                        "Put specific rules before catch-all rules."
                    )
        return self

    @model_validator(mode="after")
    def _unique_docker_base_ids(self) -> Self:
        seen: set[str] = set()
        for fleet in self.docker_workers:
            if fleet.base_worker_id in seen:
                raise ValueError(
                    f"duplicate docker_workers base_worker_id "
                    f"{fleet.base_worker_id!r}; each fleet needs a unique base"
                )
            seen.add(fleet.base_worker_id)
        return self


def _resolve_transport_mode_static(
    transport: Literal["auto", "local", "nats"],
    node_role: Literal["standalone", "core", "worker"],
) -> Literal["local", "nats"]:
    """Resolve effective transport mode: auto → role-based; else explicit."""
    if transport != "auto":
        return transport
    return "local" if node_role == "standalone" else "nats"


def _apply_env_overrides(config: ProctorConfig) -> ProctorConfig:
    """Apply the container-injection env overrides after YAML load.

    Only the three fields a containerized worker needs; deliberately not
    a general env layer (see the design's Approach A rationale).
    """
    updates: dict[str, object] = {}
    worker_updates: dict[str, object] = {}
    if (servers := os.environ.get("PROCTOR_NATS_SERVERS")) is not None:
        nats_data = config.nats.model_dump() | {
            "servers": [s.strip() for s in servers.split(",") if s.strip()]
        }
        updates["nats"] = NATSConfig.model_validate(nats_data)
    if (wid := os.environ.get("PROCTOR_WORKER_ID")) is not None:
        worker_updates["id"] = wid
    if (caps := os.environ.get("PROCTOR_WORKER_CAPABILITIES")) is not None:
        worker_updates["capabilities"] = [
            c.strip() for c in caps.split(",") if c.strip()
        ]
    if worker_updates:
        worker_data = config.worker.model_dump() | worker_updates
        updates["worker"] = WorkerConfig.model_validate(worker_data)
    if not updates:
        return config
    return ProctorConfig.model_validate(config.model_dump() | updates)


def load_config(path: Path | str | None = None) -> ProctorConfig:
    """Load config from YAML file, returning defaults if file missing."""
    if path is None:
        return _apply_env_overrides(ProctorConfig())

    config_path = Path(path)
    if not config_path.exists():
        logger.info("Config file %s not found, using defaults", config_path)
        return _apply_env_overrides(ProctorConfig())

    with open(config_path) as f:
        data = yaml.safe_load(f)

    if data is None:
        return _apply_env_overrides(ProctorConfig())

    config = ProctorConfig.model_validate(data)
    return _apply_env_overrides(config)
