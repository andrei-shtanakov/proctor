"""Scaffold smoke tests."""

import importlib
import subprocess
import sys

import pytest

import proctor


def test_version_exists() -> None:
    """Package exposes a valid semver __version__."""
    import re

    assert hasattr(proctor, "__version__")
    assert isinstance(proctor.__version__, str)
    assert re.match(r"^\d+\.\d+\.\d+", proctor.__version__)


def test_package_importable() -> None:
    """Package can be imported cleanly."""
    mod = importlib.import_module("proctor")
    assert mod.__name__ == "proctor"


def test_main_module_exists() -> None:
    """__main__ module is importable."""
    mod = importlib.import_module("proctor.__main__")
    assert hasattr(mod, "main")


def test_main_is_coroutine() -> None:
    """main() is an async function."""
    import inspect

    from proctor.__main__ import main

    assert inspect.iscoroutinefunction(main)


def test_entry_point_runs() -> None:
    """python -m proctor runs without crashing."""
    result = subprocess.run(
        [sys.executable, "-m", "proctor"],
        capture_output=True,
        text=True,
        timeout=10,
    )
    assert result.returncode == 0
    assert f"Proctor v{proctor.__version__}" in result.stdout


@pytest.mark.asyncio
async def test_async_anyio_backend(anyio_backend: str) -> None:
    """Async tests run on both asyncio and trio via anyio."""
    import anyio

    result: list[str] = []

    async def append_value() -> None:
        result.append(anyio_backend)

    async with anyio.create_task_group() as tg:
        tg.start_soon(append_value)

    assert len(result) == 1
    assert result[0] in ("asyncio", "trio")
