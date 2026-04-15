"""Triggers module — input adapters that produce events."""

from proctor.triggers.base import Trigger
from proctor.triggers.scheduler import SchedulerTrigger
from proctor.triggers.telegram import TelegramTrigger
from proctor.triggers.terminal import TerminalTrigger
from proctor.triggers.webhook import WebhookTrigger

__all__ = [
    "SchedulerTrigger",
    "TelegramTrigger",
    "TerminalTrigger",
    "Trigger",
    "WebhookTrigger",
]
