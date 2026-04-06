"""Configuration system: YAML loading with pydantic models and defaults."""

import logging
from pathlib import Path

import yaml
from pydantic import BaseModel, model_validator

logger = logging.getLogger(__name__)


class LLMConfig(BaseModel):
    """LLM provider configuration."""

    default_model: str = "claude-sonnet-4-20250514"
    fallback_model: str = "ollama/llama3.2"
    max_tokens: int = 4096
    temperature: float = 0.7


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
    payload: dict = {}
    enabled: bool = True

    @model_validator(mode="after")
    def exactly_one_schedule_type(self) -> "ScheduleItemConfig":
        """Ensure exactly one of cron or interval_seconds is set."""
        has_cron = self.cron is not None
        has_interval = self.interval_seconds is not None
        if has_cron == has_interval:
            raise ValueError(
                "Exactly one of 'cron' or 'interval_seconds' "
                "must be set, not both or neither."
            )
        return self


class SchedulerConfig(BaseModel):
    """Task scheduler configuration."""

    poll_interval_seconds: int = 30
    enabled: bool = True


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
    schedules: list[ScheduleItemConfig] = []


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
