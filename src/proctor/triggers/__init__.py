"""Triggers module — input adapters that produce events."""

from proctor.triggers.base import Trigger
from proctor.triggers.telegram import TelegramTrigger
from proctor.triggers.terminal import TerminalTrigger

__all__ = [
    "TelegramTrigger",
    "TerminalTrigger",
    "Trigger",
]
