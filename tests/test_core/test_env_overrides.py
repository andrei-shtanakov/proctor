"""Env-var overrides applied by load_config (container injection)."""

from pathlib import Path

import pytest
import yaml

from proctor.core.config import load_config


@pytest.fixture
def _yaml(tmp_path: Path) -> Path:
    p = tmp_path / "c.yaml"
    p.write_text(
        yaml.dump(
            {
                "node_role": "worker",
                "transport": "nats",
                "worker": {"id": "from_yaml", "capabilities": ["yaml_cap"]},
                "nats": {"servers": ["nats://yaml:4222"]},
            }
        )
    )
    return p


def test_no_env_keeps_yaml(_yaml: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    for k in (
        "PROCTOR_WORKER_ID",
        "PROCTOR_WORKER_CAPABILITIES",
        "PROCTOR_NATS_SERVERS",
    ):
        monkeypatch.delenv(k, raising=False)
    cfg = load_config(_yaml)
    assert cfg.worker.id == "from_yaml"
    assert cfg.worker.capabilities == ["yaml_cap"]
    assert cfg.nats.servers == ["nats://yaml:4222"]


def test_worker_id_override(_yaml: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("PROCTOR_WORKER_ID", "from_env")
    assert load_config(_yaml).worker.id == "from_env"


def test_nats_servers_override(_yaml: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("PROCTOR_NATS_SERVERS", "nats://a:4222,nats://b:4222")
    assert load_config(_yaml).nats.servers == ["nats://a:4222", "nats://b:4222"]


@pytest.mark.parametrize(
    "raw,expected",
    [
        ("", []),
        ("python", ["python"]),
        ("shell,python", ["shell", "python"]),
        (" shell , python ", ["shell", "python"]),
    ],
)
def test_capabilities_csv(
    _yaml: Path,
    monkeypatch: pytest.MonkeyPatch,
    raw: str,
    expected: list[str],
) -> None:
    monkeypatch.setenv("PROCTOR_WORKER_CAPABILITIES", raw)
    assert load_config(_yaml).worker.capabilities == expected
