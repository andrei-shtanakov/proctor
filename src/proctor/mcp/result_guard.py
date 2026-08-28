"""Deterministic guard for MCP tool results (issue #52).

Scans tool output BEFORE it reaches agent context for two threat classes:
prompt-injection indicators and leaked credentials. The guard only detects
and reports; the enforcement policy (block/redact/log) belongs to the
future mcp/proxy that will call it.
"""

import re
from enum import StrEnum

from pydantic import BaseModel, Field

_SNIPPET_MAX_LEN = 120
_MASK_PREFIX_LEN = 6

_INJECTION_RULES: tuple[tuple[str, re.Pattern[str]], ...] = (
    (
        "ignore_instructions",
        re.compile(
            r"\bignore\s+(?:all\s+|any\s+)?(?:previous|prior|above|earlier)"
            r"\s+(?:instructions|directions|prompts?)\b",
            re.IGNORECASE,
        ),
    ),
    ("new_instructions", re.compile(r"\bnew\s+instructions?\s*:", re.IGNORECASE)),
    (
        "disregard_above",
        re.compile(
            r"\bdisregard\s+(?:the\s+)?(?:above|previous|prior|earlier)\b",
            re.IGNORECASE,
        ),
    ),
    (
        "hidden_html_comment",
        re.compile(
            r"<!--(?:(?!-->)[\s\S]){0,400}?"
            r"(?:instruction|ignore\b|system\s+prompt)"
            r"(?:(?!-->)[\s\S]){0,400}?-->",
            re.IGNORECASE,
        ),
    ),
    (
        "role_marker",
        re.compile(r"^\s*(?:system|assistant|developer)\s*:", re.IGNORECASE | re.M),
    ),
)

_CREDENTIAL_RULES: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("aws_access_key_id", re.compile(r"\bAKIA[0-9A-Z]{16}\b")),
    (
        "github_token",
        re.compile(
            r"\b(?:gh[pousr]_[A-Za-z0-9]{36}"
            r"|github_pat_[A-Za-z0-9]{22}_[A-Za-z0-9]{59})\b"
        ),
    ),
    ("anthropic_api_key", re.compile(r"\bsk-ant-[A-Za-z0-9_-]{20,}")),
    ("openai_api_key", re.compile(r"\bsk-(?!ant-)[A-Za-z0-9_-]{20,}")),
    ("slack_token", re.compile(r"\bxox[abprs]-[A-Za-z0-9-]{10,}")),
    ("private_key", re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----")),
)


class GuardFindingKind(StrEnum):
    """Threat class of a single guard finding."""

    PROMPT_INJECTION = "prompt_injection"
    CREDENTIAL = "credential"


class GuardFinding(BaseModel):
    """One detected threat in a tool result."""

    kind: GuardFindingKind
    rule: str
    snippet: str


class GuardReport(BaseModel):
    """Outcome of scanning one tool result."""

    findings: list[GuardFinding] = Field(default_factory=list)

    @property
    def is_clean(self) -> bool:
        """True when the scan produced no findings."""
        return not self.findings


def _truncate(text: str) -> str:
    if len(text) <= _SNIPPET_MAX_LEN:
        return text
    return text[: _SNIPPET_MAX_LEN - 1] + "…"


def _mask(secret: str) -> str:
    return secret[:_MASK_PREFIX_LEN] + "…"


def scan_tool_result(text: str) -> GuardReport:
    """Scan a tool result for prompt-injection indicators and credentials.

    Deterministic and side-effect free. Credential snippets are masked so
    the report itself never carries the full secret.
    """
    findings: list[GuardFinding] = []
    for rule, pattern in _INJECTION_RULES:
        for match in pattern.finditer(text):
            findings.append(
                GuardFinding(
                    kind=GuardFindingKind.PROMPT_INJECTION,
                    rule=rule,
                    snippet=_truncate(match.group(0)),
                )
            )
    for rule, pattern in _CREDENTIAL_RULES:
        for match in pattern.finditer(text):
            findings.append(
                GuardFinding(
                    kind=GuardFindingKind.CREDENTIAL,
                    rule=rule,
                    snippet=_mask(match.group(0)),
                )
            )
    return GuardReport(findings=findings)
