"""
moni1: WS consumers must back off when the server closes the connection
cleanly (``async for`` over the socket ends without raising), not only on
exceptions.  Backoff is exponential with a cap and resets only after a
healthy connection (at least one message received).

``websockets.connect`` is replaced by an in-process fake and
``asyncio.sleep`` is patched, so nothing touches the network or waits.
"""
import asyncio

import pytest
import websockets

from services.monitor.ws_kalshi import KalshiWsConsumer
from services.monitor.ws_polymarket import PolymarketWsConsumer


class _FakeWs:
    def __init__(self, messages):
        self._messages = list(messages)
        self.sent = []

    async def send(self, data):
        self.sent.append(data)

    def __aiter__(self):
        return self

    async def __anext__(self):
        if self._messages:
            return self._messages.pop(0)
        raise StopAsyncIteration  # clean close


class _FakeConnect:
    """Async context manager returned by the fake websockets.connect."""

    def __init__(self, messages):
        self._ws = _FakeWs(messages)

    async def __aenter__(self):
        return self._ws

    async def __aexit__(self, *exc):
        return False


def _install_fakes(monkeypatch, consumer, scripts, max_sleeps):
    """scripts[i] = messages delivered on connection i before a clean close."""
    connects = {"n": 0}
    sleeps: list[float] = []

    def _connect(*args, **kwargs):
        i = connects["n"]
        connects["n"] += 1
        msgs = scripts[i] if i < len(scripts) else []
        return _FakeConnect(msgs)

    async def _sleep(delay, *args, **kwargs):
        sleeps.append(delay)
        if len(sleeps) >= max_sleeps:
            consumer.stop()

    monkeypatch.setattr(websockets, "connect", _connect)
    monkeypatch.setattr(asyncio, "sleep", _sleep)
    return connects, sleeps


def _noop_cb():
    async def _cb(*_a):
        return None
    return _cb


CONSUMERS = [
    pytest.param(lambda: KalshiWsConsumer(api_key="test-key"), id="kalshi"),
    pytest.param(lambda: PolymarketWsConsumer(), id="polymarket"),
]


@pytest.mark.parametrize("make_consumer", CONSUMERS)
def test_clean_close_backs_off_exponentially_with_cap(monkeypatch, make_consumer):
    consumer = make_consumer()
    connects, sleeps = _install_fakes(monkeypatch, consumer, scripts=[], max_sleeps=7)

    asyncio.run(consumer.run_loop(_noop_cb()))

    assert sleeps == [5, 10, 20, 40, 60, 60, 60]
    assert connects["n"] == 7  # one connect per backoff, no tight loop
    assert consumer.status()["reconnect_count"] == 7
    assert consumer.status()["connected"] is False


@pytest.mark.parametrize("make_consumer", CONSUMERS)
def test_backoff_resets_after_healthy_connection(monkeypatch, make_consumer):
    consumer = make_consumer()
    # Connections 0-2 close immediately; connection 3 delivers a message
    # (healthy) and then closes; connections after that close immediately.
    scripts = [[], [], [], ['{"type": "noop"}']]
    _, sleeps = _install_fakes(monkeypatch, consumer, scripts=scripts, max_sleeps=6)

    asyncio.run(consumer.run_loop(_noop_cb()))

    assert sleeps == [5, 10, 20, 5, 10, 20]


@pytest.mark.parametrize("make_consumer", CONSUMERS)
def test_exception_path_uses_same_backoff(monkeypatch, make_consumer):
    consumer = make_consumer()
    sleeps: list[float] = []

    def _connect(*args, **kwargs):
        raise OSError("connection refused")

    async def _sleep(delay, *args, **kwargs):
        sleeps.append(delay)
        if len(sleeps) >= 6:
            consumer.stop()

    monkeypatch.setattr(websockets, "connect", _connect)
    monkeypatch.setattr(asyncio, "sleep", _sleep)

    asyncio.run(consumer.run_loop(_noop_cb()))

    assert sleeps == [5, 10, 20, 40, 60, 60]


def test_stop_during_connection_exits_without_sleep(monkeypatch):
    consumer = KalshiWsConsumer(api_key="test-key")
    sleeps: list[float] = []

    class _StopOnIter(_FakeWs):
        async def __anext__(self):
            consumer.stop()
            raise StopAsyncIteration

    class _Ctx(_FakeConnect):
        def __init__(self):
            self._ws = _StopOnIter([])

    async def _sleep(delay, *a, **k):
        sleeps.append(delay)

    monkeypatch.setattr(websockets, "connect", lambda *a, **k: _Ctx())
    monkeypatch.setattr(asyncio, "sleep", _sleep)

    asyncio.run(consumer.run_loop(_noop_cb()))
    assert sleeps == []


def test_kalshi_auth_rejection_still_stops_without_retry(monkeypatch):
    consumer = KalshiWsConsumer(api_key="test-key")
    sleeps: list[float] = []

    def _connect(*args, **kwargs):
        raise Exception("server rejected WebSocket connection: HTTP 401")

    async def _sleep(delay, *a, **k):
        sleeps.append(delay)

    monkeypatch.setattr(websockets, "connect", _connect)
    monkeypatch.setattr(asyncio, "sleep", _sleep)

    asyncio.run(consumer.run_loop(_noop_cb()))
    assert sleeps == []
    assert consumer.status()["running"] is False
