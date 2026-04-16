"""Tests for NATS subject wildcard matcher."""

import pytest

from proctor.core.transport.errors import InvalidSubjectError
from proctor.core.transport.local import _match_subject


class TestWildcardMatcher:
    @pytest.mark.parametrize(
        "subject,pattern,expected",
        [
            ("trigger.terminal", "trigger.terminal", True),  # literal
            ("trigger.terminal", "trigger.*", True),  # single-token
            (
                "trigger.webhook.github",
                "trigger.*",
                False,
            ),  # NATS * ≠ >
            ("trigger.webhook.github", "trigger.>", True),  # multi-token tail
            ("trigger.webhook.github", "trigger.webhook.*", True),
            ("trigger.webhook.github", "trigger.webhook.github", True),
            (
                "trigger.webhook.github.v2",
                "trigger.webhook.*",
                False,
            ),
            ("trigger.webhook.github.v2", "trigger.webhook.>", True),
            (
                "trigger.webhook.github",
                "trigger.*.github",
                True,
            ),  # * mid-path
            ("other.foo", "trigger.>", False),
            ("trigger", "trigger.>", False),  # > needs ≥1 token
            ("trigger.a", ">", True),  # bare > matches any ≥1-token subject
        ],
    )
    def test_match(self, subject: str, pattern: str, expected: bool) -> None:
        assert _match_subject(subject, pattern) is expected


class TestSubjectValidation:
    def test_wildcards_in_middle_are_ok(self) -> None:
        assert _match_subject("a.b.c", "a.*.c") is True

    def test_angle_only_at_end(self) -> None:
        with pytest.raises(InvalidSubjectError, match=">"):
            _match_subject("a.b", "a.>.b")

    def test_empty_subject_rejected(self) -> None:
        with pytest.raises(InvalidSubjectError):
            _match_subject("", "a")

    def test_fnmatch_meta_rejected(self) -> None:
        with pytest.raises(InvalidSubjectError):
            _match_subject("a.b", "a.?")
        with pytest.raises(InvalidSubjectError):
            _match_subject("a.b", "a.[abc]")
