"""Tests for configuration system: models, YAML loading, defaults."""

from pathlib import Path

import pytest
import yaml

from proctor.core.config import (
    LLMConfig,
    NATSConfig,
    ProctorConfig,
    ScheduleItemConfig,
    SchedulerConfig,
    TelegramConfig,
    load_config,
)


class TestLLMConfig:
    def test_defaults(self) -> None:
        cfg = LLMConfig()
        assert cfg.default_model == "claude-sonnet-4-20250514"
        assert cfg.fallback_model == "ollama/llama3.2"
        assert cfg.max_tokens == 4096
        assert cfg.temperature == 0.7

    def test_custom_values(self) -> None:
        cfg = LLMConfig(
            default_model="gpt-4",
            fallback_model="local/model",
            max_tokens=2048,
            temperature=0.5,
        )
        assert cfg.default_model == "gpt-4"
        assert cfg.fallback_model == "local/model"
        assert cfg.max_tokens == 2048
        assert cfg.temperature == 0.5


class TestNATSConfig:
    def test_defaults(self) -> None:
        cfg = NATSConfig()
        assert cfg.url == "nats://localhost:4222"
        assert cfg.connect_timeout == 5.0
        assert cfg.reconnect_time_wait == 2.0
        assert cfg.max_reconnect_attempts == 60

    def test_custom_values(self) -> None:
        cfg = NATSConfig(
            url="nats://remote:4222",
            connect_timeout=10.0,
            max_reconnect_attempts=100,
        )
        assert cfg.url == "nats://remote:4222"
        assert cfg.connect_timeout == 10.0
        assert cfg.max_reconnect_attempts == 100


class TestSchedulerConfig:
    def test_defaults(self) -> None:
        cfg = SchedulerConfig()
        assert cfg.poll_interval_seconds == 30
        assert cfg.enabled is True

    def test_disabled(self) -> None:
        cfg = SchedulerConfig(enabled=False)
        assert cfg.enabled is False

    def test_custom_interval(self) -> None:
        cfg = SchedulerConfig(poll_interval_seconds=60)
        assert cfg.poll_interval_seconds == 60


class TestTelegramConfig:
    def test_required_fields(self) -> None:
        cfg = TelegramConfig(
            bot_token="123:ABC",
            allowed_chat_ids=[111, 222],
        )
        assert cfg.bot_token == "123:ABC"
        assert cfg.allowed_chat_ids == [111, 222]
        assert cfg.poll_timeout == 30

    def test_custom_poll_timeout(self) -> None:
        cfg = TelegramConfig(
            bot_token="tok",
            allowed_chat_ids=[1],
            poll_timeout=60,
        )
        assert cfg.poll_timeout == 60

    def test_empty_allowed_chat_ids(self) -> None:
        cfg = TelegramConfig(bot_token="tok", allowed_chat_ids=[])
        assert cfg.allowed_chat_ids == []

    def test_missing_bot_token_raises(self) -> None:
        with pytest.raises(ValueError):
            TelegramConfig(allowed_chat_ids=[1])  # type: ignore[call-arg]

    def test_missing_allowed_chat_ids_raises(self) -> None:
        with pytest.raises(ValueError):
            TelegramConfig(bot_token="tok")  # type: ignore[call-arg]


class TestScheduleItemConfig:
    def test_cron_schedule(self) -> None:
        item = ScheduleItemConfig(name="daily", cron="0 9 * * *")
        assert item.name == "daily"
        assert item.cron == "0 9 * * *"
        assert item.interval_seconds is None
        assert item.payload == {}
        assert item.enabled is True

    def test_interval_schedule(self) -> None:
        item = ScheduleItemConfig(name="heartbeat", interval_seconds=60.0)
        assert item.name == "heartbeat"
        assert item.cron is None
        assert item.interval_seconds == 60.0

    def test_with_payload(self) -> None:
        item = ScheduleItemConfig(
            name="check",
            cron="*/5 * * * *",
            payload={"target": "api", "timeout": 30},
        )
        assert item.payload == {"target": "api", "timeout": 30}

    def test_disabled(self) -> None:
        item = ScheduleItemConfig(name="off", cron="0 0 * * *", enabled=False)
        assert item.enabled is False

    def test_neither_cron_nor_interval_raises(self) -> None:
        with pytest.raises(ValueError, match="Exactly one"):
            ScheduleItemConfig(name="bad")

    def test_both_cron_and_interval_raises(self) -> None:
        with pytest.raises(ValueError, match="Exactly one"):
            ScheduleItemConfig(
                name="bad",
                cron="0 * * * *",
                interval_seconds=60.0,
            )

    def test_interval_float_precision(self) -> None:
        item = ScheduleItemConfig(name="precise", interval_seconds=0.5)
        assert item.interval_seconds == 0.5


class TestProctorConfig:
    def test_defaults(self) -> None:
        cfg = ProctorConfig()
        assert cfg.node_role == "standalone"
        assert cfg.node_id == "node-1"
        assert cfg.nats_url == "nats://localhost:4222"
        assert cfg.data_dir == Path("data")
        assert cfg.log_level == "INFO"

    def test_nested_llm_defaults(self) -> None:
        cfg = ProctorConfig()
        assert cfg.llm.default_model == "claude-sonnet-4-20250514"
        assert cfg.llm.fallback_model == "ollama/llama3.2"
        assert cfg.llm.max_tokens == 4096
        assert cfg.llm.temperature == 0.7

    def test_nested_nats_defaults(self) -> None:
        cfg = ProctorConfig()
        assert cfg.nats.url == "nats://localhost:4222"
        assert cfg.nats.connect_timeout == 5.0

    def test_nested_scheduler_defaults(self) -> None:
        cfg = ProctorConfig()
        assert cfg.scheduler.poll_interval_seconds == 30
        assert cfg.scheduler.enabled is True

    def test_custom_top_level(self) -> None:
        cfg = ProctorConfig(
            node_role="core",
            node_id="node-2",
            log_level="DEBUG",
        )
        assert cfg.node_role == "core"
        assert cfg.node_id == "node-2"
        assert cfg.log_level == "DEBUG"

    def test_data_dir_as_string(self) -> None:
        cfg = ProctorConfig(data_dir="/tmp/proctor")
        assert cfg.data_dir == Path("/tmp/proctor")

    def test_telegram_none_by_default(self) -> None:
        cfg = ProctorConfig()
        assert cfg.telegram is None

    def test_telegram_config_set(self) -> None:
        cfg = ProctorConfig(
            telegram=TelegramConfig(
                bot_token="123:ABC",
                allowed_chat_ids=[111],
            ),
        )
        assert cfg.telegram is not None
        assert cfg.telegram.bot_token == "123:ABC"
        assert cfg.telegram.allowed_chat_ids == [111]
        assert cfg.telegram.poll_timeout == 30

    def test_partial_nested_override(self) -> None:
        cfg = ProctorConfig(
            llm=LLMConfig(max_tokens=2048),
        )
        assert cfg.llm.max_tokens == 2048
        assert cfg.llm.default_model == "claude-sonnet-4-20250514"

    def test_schedules_default_empty(self) -> None:
        cfg = ProctorConfig()
        assert cfg.schedules == []

    def test_schedules_with_items(self) -> None:
        items = [
            ScheduleItemConfig(name="a", cron="0 * * * *"),
            ScheduleItemConfig(name="b", interval_seconds=120),
        ]
        cfg = ProctorConfig(schedules=items)
        assert len(cfg.schedules) == 2
        assert cfg.schedules[0].name == "a"
        assert cfg.schedules[1].interval_seconds == 120


class TestLoadConfig:
    def test_none_path_returns_defaults(self) -> None:
        cfg = load_config(None)
        assert cfg == ProctorConfig()

    def test_missing_file_returns_defaults(self, tmp_path: Path) -> None:
        cfg = load_config(tmp_path / "nonexistent.yaml")
        assert cfg == ProctorConfig()

    def test_load_from_yaml(self, tmp_path: Path) -> None:
        config_file = tmp_path / "test.yaml"
        data = {
            "node_role": "core",
            "node_id": "test-node",
            "log_level": "DEBUG",
            "llm": {"default_model": "gpt-4", "max_tokens": 2048},
            "scheduler": {"enabled": False},
        }
        config_file.write_text(yaml.dump(data))

        cfg = load_config(config_file)
        assert cfg.node_role == "core"
        assert cfg.node_id == "test-node"
        assert cfg.log_level == "DEBUG"
        assert cfg.llm.default_model == "gpt-4"
        assert cfg.llm.max_tokens == 2048
        assert cfg.llm.fallback_model == "ollama/llama3.2"
        assert cfg.scheduler.enabled is False

    def test_load_from_string_path(self, tmp_path: Path) -> None:
        config_file = tmp_path / "test.yaml"
        config_file.write_text(yaml.dump({"node_id": "str-path"}))

        cfg = load_config(str(config_file))
        assert cfg.node_id == "str-path"

    def test_empty_yaml_returns_defaults(self, tmp_path: Path) -> None:
        config_file = tmp_path / "empty.yaml"
        config_file.write_text("")

        cfg = load_config(config_file)
        assert cfg == ProctorConfig()

    def test_nested_config_defaults_preserved(self, tmp_path: Path) -> None:
        """Unspecified nested configs get defaults."""
        config_file = tmp_path / "minimal.yaml"
        config_file.write_text(yaml.dump({"node_id": "minimal"}))

        cfg = load_config(config_file)
        assert cfg.llm.default_model == "claude-sonnet-4-20250514"
        assert cfg.nats.url == "nats://localhost:4222"
        assert cfg.scheduler.enabled is True

    def test_full_yaml_roundtrip(self, tmp_path: Path) -> None:
        """Full config loads all nested values correctly."""
        data = {
            "node_role": "worker",
            "node_id": "w-1",
            "nats_url": "nats://remote:4222",
            "data_dir": "/var/proctor",
            "log_level": "WARNING",
            "llm": {
                "default_model": "custom-model",
                "fallback_model": "local/tiny",
                "max_tokens": 1024,
                "temperature": 0.3,
            },
            "nats": {
                "url": "nats://remote:4222",
                "connect_timeout": 10.0,
                "reconnect_time_wait": 5.0,
                "max_reconnect_attempts": 120,
            },
            "scheduler": {
                "poll_interval_seconds": 60,
                "enabled": False,
            },
        }
        config_file = tmp_path / "full.yaml"
        config_file.write_text(yaml.dump(data))

        cfg = load_config(config_file)
        assert cfg.node_role == "worker"
        assert cfg.node_id == "w-1"
        assert cfg.data_dir == Path("/var/proctor")
        assert cfg.llm.temperature == 0.3
        assert cfg.nats.connect_timeout == 10.0
        assert cfg.nats.max_reconnect_attempts == 120
        assert cfg.scheduler.poll_interval_seconds == 60

    def test_load_telegram_from_yaml(self, tmp_path: Path) -> None:
        config_file = tmp_path / "tg.yaml"
        data = {
            "telegram": {
                "bot_token": "123:ABC",
                "allowed_chat_ids": [111, 222],
                "poll_timeout": 45,
            },
        }
        config_file.write_text(yaml.dump(data))
        cfg = load_config(config_file)
        assert cfg.telegram is not None
        assert cfg.telegram.bot_token == "123:ABC"
        assert cfg.telegram.allowed_chat_ids == [111, 222]
        assert cfg.telegram.poll_timeout == 45

    def test_load_without_telegram_yaml(self, tmp_path: Path) -> None:
        config_file = tmp_path / "no_tg.yaml"
        config_file.write_text(yaml.dump({"node_id": "no-tg"}))
        cfg = load_config(config_file)
        assert cfg.telegram is None

    def test_load_with_schedules(self, tmp_path: Path) -> None:
        data = {
            "node_id": "sched-node",
            "schedules": [
                {"name": "backup", "cron": "0 2 * * *"},
                {
                    "name": "poll",
                    "interval_seconds": 300,
                    "payload": {"url": "https://example.com"},
                    "enabled": False,
                },
            ],
        }
        config_file = tmp_path / "sched.yaml"
        config_file.write_text(yaml.dump(data))

        cfg = load_config(config_file)
        assert len(cfg.schedules) == 2
        assert cfg.schedules[0].name == "backup"
        assert cfg.schedules[0].cron == "0 2 * * *"
        assert cfg.schedules[1].interval_seconds == 300
        assert cfg.schedules[1].enabled is False
        assert cfg.schedules[1].payload == {"url": "https://example.com"}

    def test_load_no_schedules_key(self, tmp_path: Path) -> None:
        """Config without schedules key still works."""
        config_file = tmp_path / "no_sched.yaml"
        config_file.write_text(yaml.dump({"node_id": "test"}))

        cfg = load_config(config_file)
        assert cfg.schedules == []

    def test_invalid_yaml_raises(self, tmp_path: Path) -> None:
        config_file = tmp_path / "bad.yaml"
        config_file.write_text(":\n  :\n- {\n")

        with pytest.raises(yaml.YAMLError):
            load_config(config_file)

    def test_example_config_loads(self) -> None:
        """The shipped example config loads successfully."""
        example = Path(__file__).parents[2] / "config" / "proctor.yaml"
        if example.exists():
            cfg = load_config(example)
            assert cfg.node_id == "node-local"
            assert cfg.llm.default_model == "claude-sonnet-4-20250514"


class TestPublicExports:
    def test_import_from_core(self) -> None:
        from proctor.core import (
            LLMConfig,
            NATSConfig,
            ProctorConfig,
            ScheduleItemConfig,
            SchedulerConfig,
            TelegramConfig,
            load_config,
        )

        assert LLMConfig is not None
        assert NATSConfig is not None
        assert ProctorConfig is not None
        assert ScheduleItemConfig is not None
        assert SchedulerConfig is not None
        assert TelegramConfig is not None
        assert load_config is not None
