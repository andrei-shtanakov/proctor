"""Configuration system: YAML loading with pydantic models and defaults."""

import logging
import re
from pathlib import Path
from typing import Annotated, Any, Literal, Self

import yaml
from croniter import croniter
from pydantic import BaseModel, ConfigDict, Field, model_validator

from proctor.core.globs import is_strictly_broader
from proctor.workflow.spec import WorkflowSpec

logger = logging.getLogger(__name__)


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


class RouterAgentConfig(BaseModel):
    """Slot budget of the single local agent (Phase 2)."""

    max_slots: int = Field(default=4, ge=1)


class RouterConfig(BaseModel):
    """TaskRouter admission settings."""

    max_concurrency: int = Field(default=4, ge=1)
    # 0 = reject immediately, never queue
    queue_ttl_seconds: float = Field(default=600.0, ge=0.0)
    # must stay > 0: asyncio.sleep(0) would spin the tick loop hot
    queue_tick_seconds: float = Field(default=30.0, gt=0.0)
    agent: RouterAgentConfig = RouterAgentConfig()


class WorkerConfig(BaseModel):
    """Identity and capacity of this node's executor."""

    id: str = Field(default="local", pattern=r"^[a-z][a-z0-9_]*$")
    capabilities: list[str] = Field(default_factory=list)
    max_slots: int = Field(default=4, ge=1)


class RegistryConfig(BaseModel):
    """Worker discovery/liveness settings (core/standalone only)."""

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


def _resolve_transport_mode_static(
    transport: Literal["auto", "local", "nats"],
    node_role: Literal["standalone", "core", "worker"],
) -> Literal["local", "nats"]:
    """Resolve effective transport mode: auto → role-based; else explicit."""
    if transport != "auto":
        return transport
    return "local" if node_role == "standalone" else "nats"


def load_config(path: Path | str | None = None) -> ProctorConfig:
    """Load config from YAML file, returning defaults if file missing."""
    if path is None:
        return ProctorConfig()

    config_path = Path(path)
    if not config_path.exists():
        logger.info("Config file %s not found, using defaults", config_path)
        return ProctorConfig()

    with open(config_path) as f:
        data = yaml.safe_load(f)

    if data is None:
        return ProctorConfig()

    return ProctorConfig.model_validate(data)
