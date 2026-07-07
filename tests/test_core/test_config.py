"""Tests for configuration system: models, YAML loading, defaults."""

from pathlib import Path

import pytest
import yaml
from pydantic import ValidationError

from proctor.core.config import (
    LLMConfig,
    NATSConfig,
    ProctorConfig,
    RegistryConfig,
    RouteRule,
    ScheduleItemConfig,
    SchedulerConfig,
    TelegramConfig,
    WorkerConfig,
    load_config,
)
from proctor.workflow.spec import WorkflowMode, WorkflowSpec


class TestLLMConfig:
    def test_defaults(self) -> None:
        cfg = LLMConfig()
        assert cfg.default_model == "claude-sonnet-4-20250514"
        assert cfg.fallback_model is None
        assert cfg.max_tokens == 4096
        assert cfg.temperature == 0.7
        assert cfg.request_timeout == 60.0
        assert cfg.max_retries == 1

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


class TestLLMConfigExtended:
    def test_fallback_model_default_none(self) -> None:
        cfg = LLMConfig()
        assert cfg.fallback_model is None

    def test_request_timeout_default(self) -> None:
        cfg = LLMConfig()
        assert cfg.request_timeout == 60.0

    def test_max_retries_default(self) -> None:
        cfg = LLMConfig()
        assert cfg.max_retries == 1

    def test_overrides(self) -> None:
        cfg = LLMConfig(
            fallback_model="ollama/llama3.2",
            request_timeout=10.0,
            max_retries=3,
        )
        assert cfg.fallback_model == "ollama/llama3.2"
        assert cfg.request_timeout == 10.0
        assert cfg.max_retries == 3


class TestNATSConfig:
    def test_defaults(self) -> None:
        cfg = NATSConfig()
        assert cfg.servers == ["nats://localhost:4222"]
        assert cfg.subject_prefix == "proctor"
        assert cfg.connect_timeout == 5.0
        assert cfg.reconnect_time_wait == 2.0
        assert cfg.reconnect_jitter == 0.5
        assert cfg.max_reconnect_attempts == -1
        assert cfg.name.startswith("proctor-")

    def test_custom_values(self) -> None:
        cfg = NATSConfig(
            servers=["nats://remote:4222"],
            connect_timeout=10.0,
            max_reconnect_attempts=100,
            name="my-node",
        )
        assert cfg.servers == ["nats://remote:4222"]
        assert cfg.connect_timeout == 10.0
        assert cfg.max_reconnect_attempts == 100
        assert cfg.name == "my-node"

    def test_user_user_env_mutually_exclusive(self) -> None:
        with pytest.raises(ValueError, match="user or user_env, not both"):
            NATSConfig(user="alice", user_env="NATS_USER")


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

    def test_allowed_chat_ids_defaults_to_empty(self) -> None:
        config = TelegramConfig(bot_token="tok")
        assert config.allowed_chat_ids == []


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
        assert cfg.llm.fallback_model is None
        assert cfg.llm.max_tokens == 4096
        assert cfg.llm.temperature == 0.7

    def test_nested_nats_defaults(self) -> None:
        cfg = ProctorConfig()
        assert cfg.nats.servers == ["nats://localhost:4222"]
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

    def test_workflow_scope_and_branch_from_yaml(self, tmp_path: Path) -> None:
        config_file = tmp_path / "test.yaml"
        data = {
            "workflows": {
                "deploy": {
                    "workflow_id": "deploy",
                    "mode": "simple",
                    "prompt": "deploy it",
                    "scope": ["src/**", "config/*.yaml"],
                    "branch": "release",
                }
            },
        }
        config_file.write_text(yaml.dump(data))
        cfg = load_config(config_file)
        spec = cfg.workflows["deploy"]
        assert spec.scope == ["src/**", "config/*.yaml"]
        assert spec.branch == "release"

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
        assert cfg.llm.fallback_model is None
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
        assert cfg.nats.servers == ["nats://localhost:4222"]
        assert cfg.scheduler.enabled is True

    def test_full_yaml_roundtrip(self, tmp_path: Path) -> None:
        """Full config loads all nested values correctly."""
        data = {
            "node_role": "worker",
            "node_id": "w-1",
            "nats_url": "nats://remote:4222",
            "data_dir": "/var/proctor",
            "log_level": "WARNING",
            "worker": {
                "id": "worker_1",
            },
            "llm": {
                "default_model": "custom-model",
                "fallback_model": "local/tiny",
                "max_tokens": 1024,
                "temperature": 0.3,
            },
            "nats": {
                "servers": ["nats://remote:4222"],
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


class TestRouteRule:
    def test_literal_prompt(self) -> None:
        from proctor.core.config import RouteRule

        rule = RouteRule(
            event_pattern="trigger.terminal",
            workflow_id="chat",
            prompt="hi",
        )
        assert rule.prompt == "hi"
        assert rule.prompt_from_payload is None

    def test_payload_prompt(self) -> None:
        from proctor.core.config import RouteRule

        rule = RouteRule(
            event_pattern="trigger.terminal",
            workflow_id="chat",
            prompt_from_payload="text",
        )
        assert rule.prompt is None
        assert rule.prompt_from_payload == "text"

    def test_both_sources_raises(self) -> None:
        from proctor.core.config import RouteRule

        with pytest.raises(ValueError, match="exactly one"):
            RouteRule(
                event_pattern="trigger.terminal",
                workflow_id="chat",
                prompt="hi",
                prompt_from_payload="text",
            )

    def test_neither_source_raises(self) -> None:
        from proctor.core.config import RouteRule

        with pytest.raises(ValueError, match="exactly one"):
            RouteRule(
                event_pattern="trigger.terminal",
                workflow_id="chat",
            )


class TestWorkflowCatalog:
    def test_empty_catalog_valid(self) -> None:
        cfg = ProctorConfig()
        assert cfg.workflows == {}

    def test_catalog_key_matches_field(self) -> None:
        cfg = ProctorConfig(
            workflows={
                "chat": WorkflowSpec(workflow_id="chat", mode=WorkflowMode.SIMPLE),
            }
        )
        assert "chat" in cfg.workflows

    def test_catalog_key_mismatch_raises(self) -> None:
        with pytest.raises(ValueError, match="workflow_id"):
            ProctorConfig(
                workflows={
                    "chat": WorkflowSpec(
                        workflow_id="other",
                        mode=WorkflowMode.SIMPLE,
                    ),
                }
            )


class TestRoutes:
    def test_empty_routes_valid(self) -> None:
        cfg = ProctorConfig()
        assert cfg.routes == []

    def test_routes_referencing_existing_workflow(self) -> None:
        cfg = ProctorConfig(
            workflows={
                "chat": WorkflowSpec(workflow_id="chat", mode=WorkflowMode.SIMPLE),
            },
            routes=[
                RouteRule(
                    event_pattern="trigger.terminal",
                    workflow_id="chat",
                    prompt_from_payload="text",
                ),
            ],
        )
        assert len(cfg.routes) == 1

    def test_orphan_workflow_id_raises(self) -> None:
        with pytest.raises(ValueError, match="unknown workflow_id"):
            ProctorConfig(
                workflows={
                    "chat": WorkflowSpec(workflow_id="chat", mode=WorkflowMode.SIMPLE),
                },
                routes=[
                    RouteRule(
                        event_pattern="trigger.terminal",
                        workflow_id="missing",
                        prompt_from_payload="text",
                    ),
                ],
            )


class TestShadowDetection:
    def test_narrow_before_broad_passes(self) -> None:
        cfg = ProctorConfig(
            workflows={
                "chat": WorkflowSpec(workflow_id="chat", mode=WorkflowMode.SIMPLE),
            },
            routes=[
                RouteRule(
                    event_pattern="trigger.telegram",
                    workflow_id="chat",
                    prompt_from_payload="text",
                ),
                RouteRule(
                    event_pattern="trigger.*",
                    workflow_id="chat",
                    prompt_from_payload="text",
                ),
            ],
        )
        assert len(cfg.routes) == 2

    def test_broad_before_narrow_raises(self) -> None:
        with pytest.raises(ValueError, match="shadows"):
            ProctorConfig(
                workflows={
                    "chat": WorkflowSpec(workflow_id="chat", mode=WorkflowMode.SIMPLE),
                },
                routes=[
                    RouteRule(
                        event_pattern="trigger.*",
                        workflow_id="chat",
                        prompt_from_payload="text",
                    ),
                    RouteRule(
                        event_pattern="trigger.telegram",
                        workflow_id="chat",
                        prompt_from_payload="text",
                    ),
                ],
            )

    def test_intersecting_but_not_subsuming_passes(self) -> None:
        """Patterns that overlap without one subsuming the other."""
        cfg = ProctorConfig(
            workflows={
                "chat": WorkflowSpec(workflow_id="chat", mode=WorkflowMode.SIMPLE),
            },
            routes=[
                RouteRule(
                    event_pattern="trigger.a.*",
                    workflow_id="chat",
                    prompt="x",
                ),
                RouteRule(
                    event_pattern="trigger.*.b",
                    workflow_id="chat",
                    prompt="x",
                ),
            ],
        )
        assert len(cfg.routes) == 2


def test_is_strictly_broader_direct() -> None:
    from proctor.core.globs import is_strictly_broader

    assert is_strictly_broader("trigger.*", "trigger.telegram") is True
    assert is_strictly_broader("trigger.telegram", "trigger.*") is False
    assert is_strictly_broader("trigger.telegram", "trigger.telegram") is False
    assert is_strictly_broader("trigger.a.*", "trigger.*.b") is False
    assert is_strictly_broader("trigger.*.b", "trigger.a.*") is False


class TestPublicExports:
    def test_import_from_core(self) -> None:
        from proctor.core import (
            EventsConfig,
            LLMConfig,
            NATSConfig,
            ProctorConfig,
            ScheduleItemConfig,
            SchedulerConfig,
            TelegramConfig,
            load_config,
        )

        assert EventsConfig is not None
        assert LLMConfig is not None
        assert NATSConfig is not None
        assert ProctorConfig is not None
        assert ScheduleItemConfig is not None
        assert SchedulerConfig is not None
        assert TelegramConfig is not None
        assert load_config is not None


class TestAuthConfig:
    def test_hmac_defaults(self) -> None:
        from proctor.core.config import HMACAuthConfig

        cfg = HMACAuthConfig(secret_env="TEST_SECRET")
        assert cfg.type == "hmac"
        assert cfg.header == "X-Hub-Signature-256"
        assert cfg.prefix == "sha256="

    def test_bearer_defaults(self) -> None:
        from proctor.core.config import BearerAuthConfig

        cfg = BearerAuthConfig(secret_env="TEST_TOKEN")
        assert cfg.type == "bearer"
        assert cfg.header == "Authorization"

    def test_none_has_no_fields(self) -> None:
        from proctor.core.config import NoneAuthConfig

        cfg = NoneAuthConfig()
        assert cfg.type == "none"

    def test_secret_env_format_rejected(self) -> None:
        from proctor.core.config import BearerAuthConfig, HMACAuthConfig

        with pytest.raises(ValueError):
            HMACAuthConfig(secret_env="lowercase")
        with pytest.raises(ValueError):
            BearerAuthConfig(secret_env="")
        with pytest.raises(ValueError):
            HMACAuthConfig(secret_env="123_BAD_START")

    def test_hmac_extra_field_forbidden(self) -> None:
        from proctor.core.config import HMACAuthConfig

        with pytest.raises(ValueError):
            HMACAuthConfig(secret_env="X", extra_field="bad")  # type: ignore[call-arg]

    def test_bearer_extra_field_forbidden(self) -> None:
        from proctor.core.config import BearerAuthConfig

        with pytest.raises(ValueError):
            BearerAuthConfig(secret_env="X", header_wrong="bad")  # type: ignore[call-arg]

    def test_none_extra_field_forbidden(self) -> None:
        from proctor.core.config import NoneAuthConfig

        with pytest.raises(ValueError):
            NoneAuthConfig(secret_env="X")  # type: ignore[call-arg]


class TestEventsConfig:
    def test_defaults(self) -> None:
        from proctor.core.config import EventsConfig

        cfg = EventsConfig()
        assert cfg.max_payload == 65_536
        assert cfg.drain_timeout == 60.0

    def test_in_proctor_config(self) -> None:
        from proctor.core.config import ProctorConfig

        cfg = ProctorConfig()
        assert cfg.events.max_payload == 65_536

    def test_extra_forbid(self) -> None:
        from proctor.core.config import EventsConfig

        with pytest.raises(ValueError):
            EventsConfig(unknown_field=1)  # type: ignore[call-arg]


class TestSourceNameTightened:
    def test_dash_rejected(self) -> None:
        from proctor.core.config import HMACAuthConfig, WebhookConfig, WebhookPathConfig

        with pytest.raises(ValueError, match="source_name"):
            WebhookConfig(
                paths={
                    "/webhook/test": WebhookPathConfig(
                        source_name="my-service",
                        auth=HMACAuthConfig(secret_env="X"),
                    ),
                }
            )

    def test_underscore_ok(self) -> None:
        from proctor.core.config import HMACAuthConfig, WebhookConfig, WebhookPathConfig

        cfg = WebhookConfig(
            paths={
                "/webhook/test": WebhookPathConfig(
                    source_name="my_service",
                    auth=HMACAuthConfig(secret_env="X"),
                ),
            }
        )
        assert cfg.paths["/webhook/test"].source_name == "my_service"

    def test_existing_examples_still_pass(self) -> None:
        from proctor.core.config import HMACAuthConfig, WebhookConfig, WebhookPathConfig

        for name in ["github", "ci", "heartbeat", "gitlab_push"]:
            WebhookConfig(
                paths={
                    f"/webhook/{name}": WebhookPathConfig(
                        source_name=name,
                        auth=HMACAuthConfig(secret_env="X"),
                    ),
                }
            )


class TestWebhookConfigValidation:
    def test_empty_paths_raises(self) -> None:
        from proctor.core.config import WebhookConfig

        with pytest.raises(ValueError):
            WebhookConfig(paths={})

    def test_valid_single_path(self) -> None:
        from proctor.core.config import (
            HMACAuthConfig,
            WebhookConfig,
            WebhookPathConfig,
        )

        cfg = WebhookConfig(
            paths={
                "/webhook/github": WebhookPathConfig(
                    source_name="github",
                    auth=HMACAuthConfig(secret_env="X"),
                ),
            }
        )
        assert "/webhook/github" in cfg.paths
        assert cfg.port == 8080
        assert cfg.max_in_flight == 20
        assert cfg.max_body_bytes == 1_048_576
        assert cfg.shutdown_timeout == 30.0
        assert cfg.keepalive_timeout == 75.0
        assert cfg.host == "127.0.0.1"

    def test_path_must_start_with_slash(self) -> None:
        from proctor.core.config import HMACAuthConfig, WebhookConfig, WebhookPathConfig

        with pytest.raises(ValueError, match="must start with"):
            WebhookConfig(
                paths={
                    "webhook/github": WebhookPathConfig(
                        auth=HMACAuthConfig(secret_env="X"),
                    ),
                }
            )

    def test_path_fnmatch_meta_rejected(self) -> None:
        from proctor.core.config import HMACAuthConfig, WebhookConfig, WebhookPathConfig

        with pytest.raises(ValueError, match="metacharacter"):
            WebhookConfig(
                paths={
                    "/webhook/*": WebhookPathConfig(
                        auth=HMACAuthConfig(secret_env="X"),
                    ),
                }
            )

    def test_trailing_slash_rejected(self) -> None:
        from proctor.core.config import HMACAuthConfig, WebhookConfig, WebhookPathConfig

        with pytest.raises(ValueError, match="trailing"):
            WebhookConfig(
                paths={
                    "/webhook/github/": WebhookPathConfig(
                        auth=HMACAuthConfig(secret_env="X"),
                    ),
                }
            )

    def test_source_name_uniqueness_enforced(self) -> None:
        from proctor.core.config import HMACAuthConfig, WebhookConfig, WebhookPathConfig

        with pytest.raises(ValueError, match="uniqueness"):
            WebhookConfig(
                paths={
                    "/webhook/gh-prod": WebhookPathConfig(
                        source_name="github",
                        auth=HMACAuthConfig(secret_env="X"),
                    ),
                    "/webhook/gh-stage": WebhookPathConfig(
                        source_name="github",
                        auth=HMACAuthConfig(secret_env="X"),
                    ),
                }
            )

    def test_source_name_format_rejected(self) -> None:
        from proctor.core.config import HMACAuthConfig, WebhookConfig, WebhookPathConfig

        with pytest.raises(ValueError, match="source_name"):
            WebhookConfig(
                paths={
                    "/webhook/GH": WebhookPathConfig(
                        source_name="GitHub",  # uppercase
                        auth=HMACAuthConfig(secret_env="X"),
                    ),
                }
            )

    def test_source_name_derived_from_basename(self) -> None:
        from proctor.core.config import (
            BearerAuthConfig,
            WebhookConfig,
            WebhookPathConfig,
        )

        cfg = WebhookConfig(
            paths={
                "/webhook/ci": WebhookPathConfig(
                    auth=BearerAuthConfig(secret_env="T"),
                ),
            }
        )
        assert cfg.paths["/webhook/ci"].source_name == "ci"

    def test_port_range_enforced(self) -> None:
        from proctor.core.config import HMACAuthConfig, WebhookConfig, WebhookPathConfig

        WebhookConfig(
            paths={
                "/webhook/x": WebhookPathConfig(
                    auth=HMACAuthConfig(secret_env="X"),
                ),
            },
            port=0,  # ephemeral — test allowance
        )
        with pytest.raises(ValueError):
            WebhookConfig(
                paths={
                    "/webhook/x": WebhookPathConfig(
                        auth=HMACAuthConfig(secret_env="X"),
                    ),
                },
                port=70000,  # type: ignore[arg-type]
            )

    def test_proctor_config_webhook_optional(self) -> None:
        cfg = ProctorConfig()
        assert cfg.webhook is None


class TestTransportResolution:
    @pytest.mark.parametrize(
        "transport,node_role,nats_servers,expected",
        [
            ("auto", "standalone", None, "local"),
            ("auto", "standalone", ["nats://x:4222"], "local"),
            ("auto", "core", ["nats://x:4222"], "nats"),
            ("auto", "worker", ["nats://x:4222"], "nats"),
            ("auto", "core", [], "ValueError"),
            ("local", "standalone", None, "local"),
            ("local", "core", ["nats://x:4222"], "local"),
            ("nats", "standalone", ["nats://x:4222"], "nats"),
            ("nats", "worker", [], "ValueError"),
        ],
    )
    def test_transport_mode_resolution_matrix(
        self,
        transport: str,
        node_role: str,
        nats_servers: list[str] | None,
        expected: str,
    ) -> None:
        from proctor.core.bootstrap import _resolve_transport_mode
        from proctor.core.config import NATSConfig, ProctorConfig, WorkerConfig

        kwargs: dict[str, object] = {
            "transport": transport,
            "node_role": node_role,
        }
        if node_role == "worker":
            kwargs["worker"] = WorkerConfig(id="worker_1")
        if nats_servers is not None:
            kwargs["nats"] = NATSConfig(servers=nats_servers)
        if expected == "ValueError":
            with pytest.raises(ValueError):
                ProctorConfig(**kwargs)  # type: ignore[arg-type]
            return
        cfg = ProctorConfig(**kwargs)  # type: ignore[arg-type]
        mode = _resolve_transport_mode(cfg)
        assert mode == expected


class TestWorkerAndRegistryConfig:
    def test_defaults(self) -> None:
        cfg = ProctorConfig()
        assert cfg.worker.id == "local"
        assert cfg.worker.max_slots == 4
        assert cfg.registry.heartbeat_interval == 30.0
        assert cfg.registry.liveness_timeout == 90.0

    def test_worker_id_charset(self) -> None:
        with pytest.raises(ValueError):
            WorkerConfig(id="worker-1")  # hyphen not subject-safe

    def test_worker_role_requires_explicit_id(self) -> None:
        with pytest.raises(ValueError, match="worker.id"):
            ProctorConfig(node_role="worker", worker=WorkerConfig(id="local"))

    def test_worker_role_with_explicit_id_ok(self) -> None:
        cfg = ProctorConfig(
            node_role="worker",
            worker=WorkerConfig(id="worker_a"),
            transport="local",
        )
        assert cfg.worker.id == "worker_a"

    def test_liveness_must_exceed_heartbeat(self) -> None:
        with pytest.raises(ValueError):
            RegistryConfig(heartbeat_interval=30.0, liveness_timeout=30.0)


class TestDockerWorkerConfig:
    def test_defaults(self) -> None:
        from proctor.core.config import DockerWorkerConfig

        fleet = DockerWorkerConfig(image="proctor:latest", base_worker_id="docker_py")
        assert fleet.replicas == 1
        assert fleet.runtime == "docker"
        assert fleet.capabilities == []
        assert fleet.max_restarts == 5
        assert fleet.stop_timeout == 30.0
        assert fleet.nats_servers == ["nats://host.docker.internal:4222"]

    def test_base_worker_id_charset(self) -> None:
        from proctor.core.config import DockerWorkerConfig

        with pytest.raises(ValidationError):
            DockerWorkerConfig(
                image="i",
                base_worker_id="docker-py",  # hyphen illegal
            )

    def test_duplicate_base_worker_id_rejected(self) -> None:
        from proctor.core.config import DockerWorkerConfig

        with pytest.raises(ValidationError, match="base_worker_id"):
            ProctorConfig(
                docker_workers=[
                    DockerWorkerConfig(image="a", base_worker_id="dup"),
                    DockerWorkerConfig(image="b", base_worker_id="dup"),
                ]
            )

    def test_empty_docker_workers_default(self) -> None:
        assert ProctorConfig().docker_workers == []


class TestRemoteDockerConfig:
    def test_ssh_host_defaults_none(self) -> None:
        from proctor.core.config import DockerWorkerConfig

        fleet = DockerWorkerConfig(image="i", base_worker_id="d")
        assert fleet.ssh_host is None
        assert fleet.op_timeout == 30.0
        assert fleet.op_margin == 10.0
        assert fleet.max_unreachable_duration == 120.0

    def test_ssh_host_with_scheme_rejected(self) -> None:
        from proctor.core.config import DockerWorkerConfig

        with pytest.raises(ValidationError, match="ssh://"):
            DockerWorkerConfig(image="i", base_worker_id="d", ssh_host="ssh://box")

    def test_ssh_host_any_scheme_rejected(self) -> None:
        from proctor.core.config import DockerWorkerConfig

        for bad in ("http://box", "tcp://box", "ssh://box"):
            with pytest.raises(ValidationError, match="without a scheme"):
                DockerWorkerConfig(image="i", base_worker_id="d", ssh_host=bad)

    def test_schemeless_unroutable_nats_still_rejected(self) -> None:
        from proctor.core.config import DockerWorkerConfig

        # `localhost:4222` (no scheme) misparses under urlsplit; the
        # validator must still catch it for a remote fleet.
        with pytest.raises(ValidationError, match="nats_servers"):
            DockerWorkerConfig(
                image="i",
                base_worker_id="d",
                ssh_host="box",
                nats_servers=["localhost:4222"],
            )

    def test_schemeless_routable_nats_ok(self) -> None:
        from proctor.core.config import DockerWorkerConfig

        fleet = DockerWorkerConfig(
            image="i",
            base_worker_id="d",
            ssh_host="box",
            nats_servers=["10.0.0.1:4222"],
        )
        assert fleet.ssh_host == "box"

    def test_ssh_env_docker(self) -> None:
        from proctor.core.config import DockerWorkerConfig, docker_ssh_env

        fleet = DockerWorkerConfig(
            image="i",
            base_worker_id="d",
            runtime="docker",
            ssh_host="user@box:2222",
            nats_servers=["nats://10.0.0.1:4222"],
        )
        assert docker_ssh_env(fleet) == {"DOCKER_HOST": "ssh://user@box:2222"}

    def test_ssh_env_podman(self) -> None:
        from proctor.core.config import DockerWorkerConfig, docker_ssh_env

        fleet = DockerWorkerConfig(
            image="i",
            base_worker_id="d",
            runtime="podman",
            ssh_host="box",
            nats_servers=["nats://10.0.0.1:4222"],
        )
        assert docker_ssh_env(fleet) == {"CONTAINER_HOST": "ssh://box"}

    def test_ssh_env_local_empty(self) -> None:
        from proctor.core.config import DockerWorkerConfig, docker_ssh_env

        fleet = DockerWorkerConfig(image="i", base_worker_id="d")
        assert docker_ssh_env(fleet) == {}

    @pytest.mark.parametrize(
        "server",
        [
            "nats://host.docker.internal:4222",
            "nats://localhost:4222",
            "nats://127.0.0.1:4222",
            "nats://[::1]:4222",
            "nats://172.17.0.1:4222",
        ],
    )
    def test_remote_fleet_rejects_unroutable_nats(self, server: str) -> None:
        from proctor.core.config import DockerWorkerConfig

        with pytest.raises(ValidationError, match="nats_servers"):
            DockerWorkerConfig(
                image="i",
                base_worker_id="d",
                ssh_host="box",
                nats_servers=[server],
            )

    def test_remote_fleet_routable_nats_ok(self) -> None:
        from proctor.core.config import DockerWorkerConfig

        fleet = DockerWorkerConfig(
            image="i",
            base_worker_id="d",
            ssh_host="box",
            nats_servers=["nats://10.0.0.1:4222"],
        )
        assert fleet.ssh_host == "box"

    def test_local_fleet_keeps_default_nats(self) -> None:
        from proctor.core.config import DockerWorkerConfig

        # no ssh_host → the host.docker.internal default is fine
        fleet = DockerWorkerConfig(image="i", base_worker_id="d")
        assert "host.docker.internal" in fleet.nats_servers[0]

    @pytest.mark.parametrize(
        "server",
        [
            "nats://172.17.0.10:4222",  # substring "172.17.0.1" of blocked host
            "nats://[2001:db8::1]:4222",  # substring "::1" of blocked host
        ],
    )
    def test_remote_fleet_accepts_lookalike_routable_nats(self, server: str) -> None:
        """A host-equality check must not false-reject look-alike hosts.

        These merely contain an unroutable address as a substring
        (172.17.0.1 / ::1) but are themselves distinct, routable hosts.
        """
        from proctor.core.config import DockerWorkerConfig

        fleet = DockerWorkerConfig(
            image="i",
            base_worker_id="d",
            ssh_host="box",
            nats_servers=[server],
        )
        assert fleet.nats_servers == [server]
