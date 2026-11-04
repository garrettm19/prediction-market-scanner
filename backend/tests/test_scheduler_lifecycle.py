"""
snap2 / moniL0: MonitorScheduler start/stop concurrency.

- Concurrent start() calls (or overlapping POST /monitor/start) must not
  create duplicate odds-poller / WS tasks.
- stop() issued while start() is awaiting its startup pipeline must not
  leave orphaned background tasks.

The startup pipeline and every source run_loop are stubbed; nothing here
touches the network, and alert state lives under tmp_path (conftest).
"""
import asyncio

from routers import monitor as monitor_router
from services.monitor.config import MonitorConfig
from services.monitor.scheduler import MonitorScheduler


def _stubbed_scheduler(pipeline_gate: asyncio.Event | None = None):
    cfg = MonitorConfig(enabled=True, dry_run=True, platforms=["kalshi", "polymarket"])
    s = MonitorScheduler(monitor_config=cfg)
    calls = {"pipeline": 0, "odds_poller": 0, "kalshi_ws": 0, "pm_ws": 0}

    async def _pipeline(trigger: str = "scheduled"):
        calls["pipeline"] += 1
        if pipeline_gate is not None:
            await pipeline_gate.wait()
        else:
            await asyncio.sleep(0)
        return None

    def _loop(name):
        async def _run(_callback):
            calls[name] += 1
            await asyncio.Event().wait()  # run until cancelled
        return _run

    s._run_pipeline_and_alert = _pipeline  # type: ignore[method-assign]
    s.odds_poller.run_loop = _loop("odds_poller")  # type: ignore[method-assign]
    s.kalshi_ws.run_loop = _loop("kalshi_ws")  # type: ignore[method-assign]
    s.pm_ws.run_loop = _loop("pm_ws")  # type: ignore[method-assign]
    return s, calls


def _task_names(s: MonitorScheduler) -> list[str]:
    return sorted(t.get_name() for t in s._tasks)


def test_concurrent_start_creates_single_set_of_tasks():
    async def _run():
        s, calls = _stubbed_scheduler()
        await asyncio.gather(s.start(), s.start(), s.start())
        await asyncio.sleep(0)
        assert _task_names(s) == ["kalshi_ws", "odds_poller", "pm_ws"]
        assert calls["pipeline"] == 1
        assert calls["odds_poller"] == 1
        assert calls["kalshi_ws"] == 1
        await s.stop()
        assert s._tasks == []

    asyncio.run(_run())


def test_stop_during_startup_pipeline_leaves_no_tasks():
    async def _run():
        gate = asyncio.Event()
        s, calls = _stubbed_scheduler(pipeline_gate=gate)

        start_task = asyncio.create_task(s.start())
        await asyncio.sleep(0)  # start() is now blocked in the startup pipeline
        assert calls["pipeline"] == 1

        stop_task = asyncio.create_task(s.stop())
        await asyncio.sleep(0)
        gate.set()  # let the startup pipeline finish
        await asyncio.gather(start_task, stop_task)
        await asyncio.sleep(0)

        assert s.status().running is False
        assert s._tasks == []
        assert calls["odds_poller"] == 0
        assert calls["kalshi_ws"] == 0
        assert calls["pm_ws"] == 0
        # No background source task survives anywhere in the loop.
        others = [
            t for t in asyncio.all_tasks()
            if t is not asyncio.current_task()
            and t.get_name() in ("odds_poller", "kalshi_ws", "pm_ws")
        ]
        assert others == []

    asyncio.run(_run())


def test_stop_after_start_cancels_all_tasks():
    async def _run():
        s, _ = _stubbed_scheduler()
        await s.start()
        await asyncio.sleep(0)
        tasks = list(s._tasks)
        assert len(tasks) == 3
        await s.stop()
        assert s._tasks == []
        assert all(t.done() for t in tasks)
        assert s.status().running is False

    asyncio.run(_run())


def test_restart_after_stop_during_startup_works():
    async def _run():
        gate = asyncio.Event()
        s, calls = _stubbed_scheduler(pipeline_gate=gate)
        start_task = asyncio.create_task(s.start())
        await asyncio.sleep(0)
        stop_task = asyncio.create_task(s.stop())
        await asyncio.sleep(0)
        gate.set()
        await asyncio.gather(start_task, stop_task)

        await s.start()
        await asyncio.sleep(0)
        assert _task_names(s) == ["kalshi_ws", "odds_poller", "pm_ws"]
        await s.stop()
        assert s._tasks == []

    asyncio.run(_run())


def test_concurrent_router_start_monitoring_no_duplicate_tasks(monkeypatch):
    """Two overlapping POST /monitor/start calls leave exactly one poller."""
    async def _run():
        s, _ = _stubbed_scheduler()
        cfg = s.monitor_config
        monkeypatch.setattr(monitor_router, "_config", cfg)
        monkeypatch.setattr(monitor_router, "_scheduler", s)

        await asyncio.gather(
            monitor_router.start_monitoring(),
            monitor_router.start_monitoring(),
        )
        await asyncio.sleep(0)
        names = _task_names(s)
        assert names.count("odds_poller") == 1
        assert names.count("kalshi_ws") == 1
        live = [
            t for t in asyncio.all_tasks()
            if t.get_name() == "odds_poller" and not t.done()
        ]
        assert len(live) == 1

        await monitor_router.stop_monitoring()
        assert s._tasks == []
        assert s.status().running is False

    asyncio.run(_run())
