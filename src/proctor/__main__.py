"""Proctor entry point — ``python -m proctor``."""

import argparse
import logging
import signal
import sys

import anyio

from proctor import __version__
from proctor.core.bootstrap import Application
from proctor.core.config import load_config

logger = logging.getLogger(__name__)


async def main() -> None:
    """Start Proctor kernel with signal handling."""
    parser = argparse.ArgumentParser(description="Proctor agent system")
    parser.add_argument(
        "--config",
        type=str,
        default=None,
        help="Path to proctor.yaml config file",
    )
    args = parser.parse_args()

    config = load_config(args.config)
    logging.basicConfig(
        level=getattr(logging, config.log_level.upper(), logging.INFO),
        format="%(asctime)s %(name)s %(levelname)s %(message)s",
    )

    print(f"Proctor v{__version__} starting...", flush=True)
    app = Application(config)
    await app.start()

    try:
        with anyio.open_signal_receiver(signal.SIGINT, signal.SIGTERM) as signals:
            async for sig in signals:
                logger.info("Received signal %s, shutting down", sig)
                break
    finally:
        await app.stop()


if __name__ == "__main__":
    try:
        anyio.run(main)
    except KeyboardInterrupt:
        sys.exit(0)
