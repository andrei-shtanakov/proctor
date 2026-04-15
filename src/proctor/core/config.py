"""Configuration system: YAML loading with pydantic models and defaults."""

import logging
from pathlib import Path
from typing import Any, Self

import yaml
from croniter import croniter
from pydantic import BaseModel, Field, model_validator

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
    """NATS messaging configuration."""

    url: str = "nats://localhost:4222"
    connect_timeout: float = 5.0
    reconnect_time_wait: float = 2.0
    max_reconnect_attempts: int = 60


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


class ProctorConfig(BaseModel):
    """Root configuration model with nested configs."""

    node_role: str = "standalone"
    node_id: str = "node-1"
    nats_url: str = "nats://localhost:4222"
    data_dir: Path = Path("data")
    log_level: str = "INFO"
    llm: LLMConfig = LLMConfig()
    nats: NATSConfig = NATSConfig()
    scheduler: SchedulerConfig = SchedulerConfig()
    telegram: TelegramConfig | None = None
    schedules: list[ScheduleItemConfig] = []
    workflows: dict[str, WorkflowSpec] = Field(default_factory=dict)
    routes: list[RouteRule] = Field(default_factory=list)

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
