"""
Test-suite-wide fixtures.

The runtime maker config (``services.maker.config._runtime_maker_config``)
is mutable module-level state.  Without a reset hook, a test that calls
``set_maker_config`` could leak its setting into unrelated tests that rely
on the disabled default — flaky failures in the worst case, false greens in
the best.  This autouse fixture resets the runtime config to ``None``
around every test so each one starts from the pristine
``DEFAULT_MAKER_CONFIG`` (disabled, paper-only, Kalshi+H2H).

``_isolate_alert_state_path`` redirects the default ``AlertStateStore``
path (``services.monitor.state._DEFAULT_PATH``, normally
``backend/data/alert_state.json``) to a per-test ``tmp_path`` so no test
reads the developer's live cooldown / hourly-cap history or overwrites it
via ``save()``.

``_isolate_maker_paper_store`` does the same for the paper-maker proposal
store (``services.maker.state._DEFAULT_BASE_DIR``).
"""
from __future__ import annotations

import pytest


@pytest.fixture(autouse=True)
def _reset_maker_runtime_config():
    from services.maker.config import reset_maker_config
    reset_maker_config()
    yield
    reset_maker_config()


@pytest.fixture(autouse=True)
def _isolate_alert_state_path(tmp_path, monkeypatch):
    import services.monitor.state as alert_state
    monkeypatch.setattr(
        alert_state, "_DEFAULT_PATH", str(tmp_path / "alert_state.json")
    )
    yield


@pytest.fixture(autouse=True)
def _isolate_maker_paper_store(tmp_path, monkeypatch):
    """Point the default paper-maker JSONL store at ``tmp_path`` so no test
    writes proposal records into ``backend/data/maker_paper_orders``."""
    import services.maker.state as maker_state
    monkeypatch.setattr(
        maker_state, "_DEFAULT_BASE_DIR", tmp_path / "maker_paper_orders"
    )
    yield
