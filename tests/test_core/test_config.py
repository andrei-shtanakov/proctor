"""Tests for configuration system: models, YAML loading, defaults."""

from pathlib import Path

import pytest
import yaml

from proctor.core.config import (
    LLMConfig,
    NATSConfig,
    ProctorConfig,
    SchedulerConfig,
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

    def test_partial_nested_override(self) -> None:
        cfg = ProctorConfig(
            llm=LLMConfig(max_tokens=2048),
        )
        assert cfg.llm.max_tokens == 2048
        assert cfg.llm.default_model == "claude-sonnet-4-20250514"


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

    def test_nested_config_defaults_preserved(
        self, tmp_path: Path
    ) -> None:
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

    def test_invalid_yaml_raises(self, tmp_path: Path) -> None:
        config_file = tmp_path / "bad.yaml"
        config_file.write_text(":\n  :\n- {\n")

        with pytest.raises((yaml.YAMLError, Exception)):
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
            SchedulerConfig,
            load_config,
        )

        assert LLMConfig is not None
        assert NATSConfig is not None
        assert ProctorConfig is not None
        assert SchedulerConfig is not None
        assert load_config is not None
