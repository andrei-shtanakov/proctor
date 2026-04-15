"""Tests for _DedupCache across all handler types."""

import asyncio
import functools

import pytest

from proctor.core.models import Event
from proctor.core.transport.local import _DedupCache


def _make_event() -> Event:
    return Event(type="test.ok", source="x", payload={})


class TestDedupCache:
    def test_first_seen_returns_false(self) -> None:
        cache = _DedupCache(size=100, ttl=10.0)

        async def h(e: Event) -> None:
            pass

        assert cache.seen(h, "msg-1") is False

    def test_second_same_key_returns_true(self) -> None:
        cache = _DedupCache(size=100, ttl=10.0)

        async def h(e: Event) -> None:
            pass

        cache.seen(h, "msg-1")
        assert cache.seen(h, "msg-1") is True

    def test_different_msg_id_not_deduped(self) -> None:
        cache = _DedupCache(size=100, ttl=10.0)

        async def h(e: Event) -> None:
            pass

        cache.seen(h, "msg-1")
        assert cache.seen(h, "msg-2") is False

    def test_different_handler_not_deduped(self) -> None:
        cache = _DedupCache(size=100, ttl=10.0)

        async def h1(e: Event) -> None:
            pass

        async def h2(e: Event) -> None:
            pass

        cache.seen(h1, "msg-1")
        assert cache.seen(h2, "msg-1") is False

    def test_async_function(self) -> None:
        cache = _DedupCache(size=100, ttl=10.0)

        async def h(e: Event) -> None:
            pass

        cache.seen(h, "msg-1")
        assert cache.seen(h, "msg-1") is True

    def test_async_lambda(self) -> None:
        cache = _DedupCache(size=100, ttl=10.0)
        h = lambda e: asyncio.sleep(0)  # noqa: E731
        cache.seen(h, "msg-1")
        assert cache.seen(h, "msg-1") is True

    def test_bound_method(self) -> None:
        cache = _DedupCache(size=100, ttl=10.0)

        class C:
            async def m(self, e: Event) -> None:
                pass

        c = C()
        cache.seen(c.m, "msg-1")
        assert cache.seen(c.m, "msg-1") is True

    def test_partial(self) -> None:
        cache = _DedupCache(size=100, ttl=10.0)

        async def base(context: str, e: Event) -> None:
            pass

        h = functools.partial(base, "ctx-a")
        cache.seen(h, "msg-1")
        assert cache.seen(h, "msg-1") is True

    def test_callable_class(self) -> None:
        cache = _DedupCache(size=100, ttl=10.0)

        class Callable:
            async def __call__(self, e: Event) -> None:
                pass

        c = Callable()
        cache.seen(c, "msg-1")
        assert cache.seen(c, "msg-1") is True


class TestDedupTTL:
    def test_entry_expires(self, monkeypatch: pytest.MonkeyPatch) -> None:
        import time

        fake_time = [0.0]
        monkeypatch.setattr(time, "monotonic", lambda: fake_time[0])

        cache = _DedupCache(size=100, ttl=10.0)

        async def h(e: Event) -> None:
            pass

        cache.seen(h, "msg-1")
        fake_time[0] = 20.0  # past TTL
        assert cache.seen(h, "msg-1") is False


class TestDedupEviction:
    def test_lru_eviction_at_capacity(self) -> None:
        cache = _DedupCache(size=3, ttl=1000.0)

        async def h(e: Event) -> None:
            pass

        for i in range(5):
            cache.seen(h, f"msg-{i}")
        # First 2 evicted, last 3 still in cache
        assert cache.seen(h, "msg-0") is False  # evicted
        assert cache.seen(h, "msg-4") is True  # present
