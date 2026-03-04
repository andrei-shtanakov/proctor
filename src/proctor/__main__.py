"""Proctor entry point — `python -m proctor`."""

import sys

import anyio

from proctor import __version__


async def main() -> None:
    """Start Proctor kernel."""
    print(f"Proctor v{__version__} starting...")
    # TODO: parse args, load config, bootstrap kernel
    print("No config provided. Exiting.")


if __name__ == "__main__":
    try:
        anyio.run(main)
    except KeyboardInterrupt:
        sys.exit(0)
