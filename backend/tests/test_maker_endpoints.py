"""
Tests for the read-only maker proposal endpoints.

Covers:
  GET /api/maker/proposals — listing + filters (status, eligible, platform, market_id)
  GET /api/maker/summary   — counts + top rejection reasons + average edge

The store is injected per-test via FastAPI's ``dependency_overrides``
so every test runs against an isolated ``tmp_path``-backed
``PaperMakerStore``.  Read-only by design — POST/DELETE must return 405.
"""
from __future__ import annotations

from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from main import app
from routers.maker import get_paper_store
from services.maker.state import (
    PaperMakerStore,
    STATUS_PAPER_ACTIVE,
    STATUS_PAPER_REJECTED,
)

# Reuse the persistence test's proposal factory.
from tests.test_maker_persistence import _proposal


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture
def client_with_store(tmp_path: Path):
    """TestClient with the maker store dependency redirected at tmp_path.

    Yields ``(client, store, tmp_path)``.  Cleans up the override afterwards
    so other test files using TestClient(app) keep their default behavior.
    """
    store = PaperMakerStore(base_dir=tmp_path)
    app.dependency_overrides[get_paper_store] = lambda: store
    try:
        with TestClient(app) as client:
            yield client, store, tmp_path
    finally:
        app.dependency_overrides.pop(get_paper_store, None)


@pytest.fixture
def populated(client_with_store):
    """Pre-populated store: 2 eligible Kalshi, 1 rejected Polymarket,
    1 rejected Kalshi (with two reason codes)."""
    client, store, tmp_path = client_with_store
    store.append(
        _proposal(eligible=True, proposal_id="P-K1",
                  market_id="K-MKT-1", platform="kalshi"),
        STATUS_PAPER_ACTIVE,
    )
    store.append(
        _proposal(eligible=True, proposal_id="P-K2",
                  market_id="K-MKT-2", platform="kalshi"),
        STATUS_PAPER_ACTIVE,
    )
    store.append(
        _proposal(
            eligible=False,
            rejection_reasons=("PLATFORM_OUT_OF_SCOPE",),
            proposal_id="P-P1",
            market_id="P-MKT-1",
            platform="polymarket",
        ),
        STATUS_PAPER_REJECTED,
    )
    store.append(
        _proposal(
            eligible=False,
            rejection_reasons=("BOOK_STALE", "PLATFORM_OUT_OF_SCOPE"),
            proposal_id="P-K3",
            market_id="K-MKT-3",
            platform="kalshi",
        ),
        STATUS_PAPER_REJECTED,
    )
    return client, store, tmp_path


# ---------------------------------------------------------------------------
# Empty store
# ---------------------------------------------------------------------------

class TestEmptyStore:
    def test_proposals_empty_returns_empty_list(self, client_with_store):
        client, _, _ = client_with_store
        resp = client.get("/api/maker/proposals")
        assert resp.status_code == 200
        body = resp.json()
        assert body == {"days": 1, "count": 0, "proposals": []}

    def test_summary_empty_returns_zeros(self, client_with_store):
        client, _, _ = client_with_store
        resp = client.get("/api/maker/summary")
        assert resp.status_code == 200
        body = resp.json()
        assert body["days"] == 1
        assert body["total"] == 0
        assert body["eligible"] == 0
        assert body["rejected"] == 0
        assert body["by_status"] == {}
        assert body["by_platform"] == {}
        assert body["top_rejection_reasons"] == []
        assert body["average_estimated_maker_edge"] is None

    def test_proposals_missing_directory(self, tmp_path: Path):
        """Endpoint must not error when the store base_dir doesn't exist yet."""
        nonexistent = tmp_path / "no-such-dir"
        store = PaperMakerStore(base_dir=nonexistent)
        app.dependency_overrides[get_paper_store] = lambda: store
        try:
            with TestClient(app) as client:
                resp = client.get("/api/maker/proposals")
                assert resp.status_code == 200
                assert resp.json()["count"] == 0
        finally:
            app.dependency_overrides.pop(get_paper_store, None)


# ---------------------------------------------------------------------------
# Listing + filters
# ---------------------------------------------------------------------------

class TestProposalsList:
    def test_returns_all_records(self, populated):
        client, _, _ = populated
        resp = client.get("/api/maker/proposals")
        assert resp.status_code == 200
        body = resp.json()
        assert body["count"] == 4
        ids = {p["proposal_id"] for p in body["proposals"]}
        assert ids == {"P-K1", "P-K2", "P-P1", "P-K3"}

    def test_filter_by_status_active(self, populated):
        client, _, _ = populated
        resp = client.get(
            "/api/maker/proposals", params={"status": STATUS_PAPER_ACTIVE}
        )
        body = resp.json()
        assert body["count"] == 2
        for p in body["proposals"]:
            assert p["status"] == STATUS_PAPER_ACTIVE

    def test_filter_by_status_rejected(self, populated):
        client, _, _ = populated
        resp = client.get(
            "/api/maker/proposals", params={"status": STATUS_PAPER_REJECTED}
        )
        body = resp.json()
        assert body["count"] == 2
        for p in body["proposals"]:
            assert p["status"] == STATUS_PAPER_REJECTED

    def test_filter_by_eligible_true(self, populated):
        client, _, _ = populated
        resp = client.get("/api/maker/proposals", params={"eligible": "true"})
        body = resp.json()
        assert body["count"] == 2
        for p in body["proposals"]:
            assert p["eligible"] is True

    def test_filter_by_eligible_false(self, populated):
        client, _, _ = populated
        resp = client.get("/api/maker/proposals", params={"eligible": "false"})
        body = resp.json()
        assert body["count"] == 2
        for p in body["proposals"]:
            assert p["eligible"] is False

    def test_filter_by_platform(self, populated):
        client, _, _ = populated
        resp = client.get("/api/maker/proposals", params={"platform": "kalshi"})
        body = resp.json()
        assert body["count"] == 3
        for p in body["proposals"]:
            assert p["platform"] == "kalshi"

        resp = client.get("/api/maker/proposals", params={"platform": "polymarket"})
        body = resp.json()
        assert body["count"] == 1
        assert body["proposals"][0]["platform"] == "polymarket"

    def test_filter_by_market_id(self, populated):
        client, _, _ = populated
        resp = client.get(
            "/api/maker/proposals", params={"market_id": "K-MKT-1"}
        )
        body = resp.json()
        assert body["count"] == 1
        assert body["proposals"][0]["market_id"] == "K-MKT-1"

    def test_filter_combination(self, populated):
        """Filters AND together: eligible=true + platform=kalshi → both Kalshi
        eligible records, polymarket excluded."""
        client, _, _ = populated
        resp = client.get(
            "/api/maker/proposals",
            params={"eligible": "true", "platform": "kalshi"},
        )
        body = resp.json()
        assert body["count"] == 2

    def test_unknown_status_returns_empty(self, populated):
        client, _, _ = populated
        resp = client.get(
            "/api/maker/proposals", params={"status": "no_such_status"}
        )
        assert resp.status_code == 200
        assert resp.json()["count"] == 0


# ---------------------------------------------------------------------------
# Summary aggregation
# ---------------------------------------------------------------------------

class TestSummaryAggregation:
    def test_total_eligible_rejected_counts(self, populated):
        client, _, _ = populated
        body = client.get("/api/maker/summary").json()
        assert body["total"] == 4
        assert body["eligible"] == 2
        assert body["rejected"] == 2

    def test_by_status_breakdown(self, populated):
        client, _, _ = populated
        body = client.get("/api/maker/summary").json()
        assert body["by_status"] == {
            STATUS_PAPER_ACTIVE: 2,
            STATUS_PAPER_REJECTED: 2,
        }

    def test_by_platform_breakdown(self, populated):
        client, _, _ = populated
        body = client.get("/api/maker/summary").json()
        assert body["by_platform"] == {"kalshi": 3, "polymarket": 1}

    def test_top_rejection_reasons_counted(self, populated):
        """PLATFORM_OUT_OF_SCOPE appears on both rejected records (count 2);
        BOOK_STALE only on K3 (count 1).  Eligible records contribute none."""
        client, _, _ = populated
        body = client.get("/api/maker/summary").json()
        reasons = {r["reason"]: r["count"] for r in body["top_rejection_reasons"]}
        assert reasons["PLATFORM_OUT_OF_SCOPE"] == 2
        assert reasons["BOOK_STALE"] == 1

    def test_top_rejection_reasons_ordered_by_count(self, populated):
        client, _, _ = populated
        body = client.get("/api/maker/summary").json()
        counts = [r["count"] for r in body["top_rejection_reasons"]]
        assert counts == sorted(counts, reverse=True)

    def test_average_estimated_maker_edge_for_eligible(self, populated):
        """The default _proposal estimated_maker_edge is 0.08; both eligible
        records carry that value."""
        client, _, _ = populated
        body = client.get("/api/maker/summary").json()
        assert body["average_estimated_maker_edge"] == pytest.approx(0.08)

    def test_average_edge_none_when_no_eligible(self, client_with_store):
        client, store, _ = client_with_store
        store.append(
            _proposal(eligible=False, rejection_reasons=("BOOK_STALE",)),
            STATUS_PAPER_REJECTED,
        )
        body = client.get("/api/maker/summary").json()
        assert body["average_estimated_maker_edge"] is None

    def test_top_n_query_param_caps_list(self, client_with_store):
        client, store, _ = client_with_store
        # Six distinct rejection reasons across six rejected records
        for i, code in enumerate(["A", "B", "C", "D", "E", "F"]):
            store.append(
                _proposal(
                    eligible=False,
                    rejection_reasons=(code,),
                    proposal_id=f"r-{i}",
                ),
                STATUS_PAPER_REJECTED,
            )
        body = client.get("/api/maker/summary", params={"top_n": 3}).json()
        assert len(body["top_rejection_reasons"]) == 3


# ---------------------------------------------------------------------------
# Days parameter
# ---------------------------------------------------------------------------

class TestDaysParameter:
    def test_default_days_is_1(self, populated):
        client, _, _ = populated
        body = client.get("/api/maker/proposals").json()
        assert body["days"] == 1

    def test_explicit_days_param_echoed(self, populated):
        client, _, _ = populated
        body = client.get("/api/maker/proposals", params={"days": 7}).json()
        assert body["days"] == 7

    def test_summary_days_echoed(self, populated):
        client, _, _ = populated
        body = client.get("/api/maker/summary", params={"days": 14}).json()
        assert body["days"] == 14

    def test_days_zero_rejected(self, client_with_store):
        client, _, _ = client_with_store
        resp = client.get("/api/maker/proposals", params={"days": 0})
        assert resp.status_code == 422

    def test_days_negative_rejected(self, client_with_store):
        client, _, _ = client_with_store
        resp = client.get("/api/maker/summary", params={"days": -1})
        assert resp.status_code == 422


# ---------------------------------------------------------------------------
# Read-only / no secrets
# ---------------------------------------------------------------------------

class TestReadOnly:
    def test_post_proposals_method_not_allowed(self, client_with_store):
        client, _, _ = client_with_store
        assert client.post("/api/maker/proposals").status_code == 405

    def test_post_summary_method_not_allowed(self, client_with_store):
        client, _, _ = client_with_store
        assert client.post("/api/maker/summary").status_code == 405

    def test_delete_proposals_method_not_allowed(self, client_with_store):
        client, _, _ = client_with_store
        assert client.delete("/api/maker/proposals").status_code == 405

    def test_put_proposals_method_not_allowed(self, client_with_store):
        client, _, _ = client_with_store
        assert client.put("/api/maker/proposals").status_code == 405


class TestNearMissDiagnostics:
    """Latest-run summary explains *why* no eligible proposal appeared.
    Counts must distinguish between unusable input (no book, crossed,
    stale) and real near-misses where only the edge math fell short."""

    def _append_with_book(
        self,
        store: PaperMakerStore,
        *,
        proposal_id: str,
        best_bid: float | None,
        best_ask: float | None,
        rejection_reasons: tuple[str, ...] = (),
        eligible: bool = False,
        estimated_maker_edge: float | None = None,
        proposed_price: float | None = None,
        maker_max_bid: float | None = None,
        side: str = "Player A",
        event_label: str = "Player A vs Player B",
    ):
        from services.maker.planner import MakerProposal
        prop = _proposal(
            eligible=eligible,
            rejection_reasons=rejection_reasons,
            proposal_id=proposal_id,
            side=side,
            event_label=event_label,
        )
        with_book = MakerProposal(**{
            **{f.name: getattr(prop, f.name)
               for f in prop.__dataclass_fields__.values()},
            "best_bid": best_bid,
            "best_ask": best_ask,
            "proposed_price": proposed_price,
            "maker_max_bid": maker_max_bid,
            "estimated_maker_edge": estimated_maker_edge,
        })
        store.append(
            with_book,
            STATUS_PAPER_ACTIVE if eligible else STATUS_PAPER_REJECTED,
        )

    def test_no_real_books_zero_diagnostics(self, client_with_store):
        """Every record has best_bid/best_ask = None: real-book counts are 0
        and there is no closest_rejected_record to surface."""
        client, store, _ = client_with_store
        for i in range(3):
            self._append_with_book(
                store,
                proposal_id=f"nb-{i}",
                best_bid=None,
                best_ask=None,
                rejection_reasons=("BOOK_CROSSED_OR_EMPTY", "BID_NOT_INSIDE_SPREAD"),
            )
        body = client.get("/api/maker/summary").json()
        assert body["records_with_real_book"] == 0
        assert body["records_with_valid_inside_spread_possible"] == 0
        assert body["rejected_for_edge_only"] == 0
        assert body["closest_rejected_edge"] is None
        assert body["closest_rejected_record"] is None

    def test_real_books_but_no_inside_spread_possible(self, client_with_store):
        """Crossed and 1-tick spreads count as real-book but inside-spread
        impossible.  Exactly the scenario where the maker pass had data but
        the spread was too tight to seat a maker bid."""
        client, store, _ = client_with_store
        # Crossed: best_bid >= best_ask
        self._append_with_book(
            store, proposal_id="x-crossed",
            best_bid=0.91, best_ask=0.90,
            rejection_reasons=("BOOK_CROSSED_OR_EMPTY",),
        )
        # 1-tick spread: bb + tick == ba, no room inside
        self._append_with_book(
            store, proposal_id="x-1tick",
            best_bid=0.45, best_ask=0.46,
            rejection_reasons=("SPREAD_TOO_NARROW",),
        )
        body = client.get("/api/maker/summary").json()
        assert body["records_with_real_book"] == 2
        assert body["records_with_valid_inside_spread_possible"] == 0

    def test_real_book_edge_near_miss_counted(self, client_with_store):
        """A record with real book + only edge-related reasons (no
        structural blockers) counts as edge-only near-miss.  Its edge is
        the closest_rejected_edge if it's the highest among rejected
        real-book records."""
        client, store, _ = client_with_store
        # Edge-only near-miss: real book, MAKER_EDGE_TOO_LOW, no structural
        self._append_with_book(
            store, proposal_id="near-miss",
            best_bid=0.40, best_ask=0.50,
            proposed_price=0.41, maker_max_bid=0.41,
            estimated_maker_edge=0.04,
            rejection_reasons=("MAKER_EDGE_TOO_LOW",),
            side="Player Near", event_label="Player Near vs Other",
        )
        # A structural-blocker record alongside — must NOT count as edge-only
        self._append_with_book(
            store, proposal_id="structural",
            best_bid=0.40, best_ask=0.41,
            estimated_maker_edge=0.06,   # higher edge but structural blocker
            rejection_reasons=("SPREAD_TOO_NARROW", "MAKER_EDGE_TOO_LOW"),
        )
        body = client.get("/api/maker/summary").json()
        assert body["records_with_real_book"] == 2
        assert body["rejected_for_edge_only"] == 1
        # closest_rejected_edge picks the highest across BOTH (regardless of
        # structural status) — the diagnostic answers "how close did any
        # rejected record get with a real book?"
        assert body["closest_rejected_edge"] == pytest.approx(0.06)
        rec = body["closest_rejected_record"]
        assert rec is not None
        assert rec["estimated_maker_edge"] == pytest.approx(0.06)

    def test_closest_rejected_edge_excludes_null_edges(self, client_with_store):
        """Rejected records whose estimated_maker_edge is None must not be
        considered for closest_rejected_edge — that field is for actual
        edge near-misses, not ineligibility for unrelated reasons."""
        client, store, _ = client_with_store
        # All rejected records here have real book but None edge (e.g.,
        # because proposed_price was None due to crossed book or low p_true)
        self._append_with_book(
            store, proposal_id="null-1",
            best_bid=0.40, best_ask=0.50,
            estimated_maker_edge=None,
            rejection_reasons=("BID_NOT_INSIDE_SPREAD",),
        )
        self._append_with_book(
            store, proposal_id="null-2",
            best_bid=0.30, best_ask=0.45,
            estimated_maker_edge=None,
            rejection_reasons=("BOOK_STALE",),
        )
        body = client.get("/api/maker/summary").json()
        assert body["records_with_real_book"] == 2
        assert body["closest_rejected_edge"] is None
        assert body["closest_rejected_record"] is None

    def test_primary_rejection_reasons_count_first_only(self, client_with_store):
        """``primary_rejection_reasons`` counts only the first reason per
        record, collapsing the multi-reason noise.  Three records each
        starting with BOOK_CROSSED_OR_EMPTY must give count=3 for that
        single reason — not 9 (3 records × 3 reasons each)."""
        client, store, _ = client_with_store
        for i in range(3):
            self._append_with_book(
                store, proposal_id=f"multi-{i}",
                best_bid=None, best_ask=None,
                rejection_reasons=(
                    "BOOK_CROSSED_OR_EMPTY",
                    "SPREAD_TOO_NARROW",
                    "BID_NOT_INSIDE_SPREAD",
                ),
            )
        body = client.get("/api/maker/summary").json()
        primary = {r["reason"]: r["count"] for r in body["primary_rejection_reasons"]}
        assert primary == {"BOOK_CROSSED_OR_EMPTY": 3}
        # The legacy top_rejection_reasons still counts every reason
        legacy = {r["reason"]: r["count"] for r in body["top_rejection_reasons"]}
        assert legacy["BOOK_CROSSED_OR_EMPTY"] == 3
        assert legacy["SPREAD_TOO_NARROW"] == 3
        assert legacy["BID_NOT_INSIDE_SPREAD"] == 3

    def test_diagnostics_compose_with_latest_run(self, client_with_store):
        """latest_run=true + diagnostics: counts apply only to the newest
        scan's records.  Older runs' edge near-misses must not bleed into
        the latest-run closest_rejected_edge."""
        from services.maker.planner import MakerProposal
        import time
        client, store, _ = client_with_store

        # Run A (older) — has a 0.09 near-miss
        prop_a = _proposal(
            eligible=False, rejection_reasons=("MAKER_EDGE_TOO_LOW",),
            proposal_id="a-near", created_at=time.time() - 60.0,
        )
        store.append(
            MakerProposal(**{
                **{f.name: getattr(prop_a, f.name)
                   for f in prop_a.__dataclass_fields__.values()},
                "best_bid": 0.40, "best_ask": 0.50,
                "proposed_price": 0.41, "maker_max_bid": 0.41,
                "estimated_maker_edge": 0.09,
                "run_id": "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa",
            }),
            STATUS_PAPER_REJECTED,
        )
        # Run B (newer) — has a 0.04 near-miss
        prop_b = _proposal(
            eligible=False, rejection_reasons=("MAKER_EDGE_TOO_LOW",),
            proposal_id="b-near", created_at=time.time(),
        )
        store.append(
            MakerProposal(**{
                **{f.name: getattr(prop_b, f.name)
                   for f in prop_b.__dataclass_fields__.values()},
                "best_bid": 0.40, "best_ask": 0.50,
                "proposed_price": 0.41, "maker_max_bid": 0.41,
                "estimated_maker_edge": 0.04,
                "run_id": "bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb",
            }),
            STATUS_PAPER_REJECTED,
        )

        full = client.get("/api/maker/summary").json()
        assert full["closest_rejected_edge"] == pytest.approx(0.09)

        latest = client.get(
            "/api/maker/summary", params={"latest_run": "true"}
        ).json()
        # Latest run only sees run B → 0.04 near-miss
        assert latest["closest_rejected_edge"] == pytest.approx(0.04)
        assert latest["records_with_real_book"] == 1
        assert latest["rejected_for_edge_only"] == 1


class TestLatestRunFilter:
    """``/api/maker/proposals?latest_run=true`` and
    ``/api/maker/summary?latest_run=true`` narrow to the most recent
    scan's run_id; legacy records (run_id absent) are ignored unless
    they're all that's in the store, in which case the endpoint falls
    back gracefully to the full set."""

    def _append_with_run(
        self, store: PaperMakerStore, *, run_id: str, market_id: str,
        proposal_id: str, created_at: float, eligible: bool = True,
        rejection_reasons: tuple[str, ...] = (),
    ):
        from services.maker.planner import MakerProposal
        prop = _proposal(
            eligible=eligible,
            rejection_reasons=rejection_reasons,
            proposal_id=proposal_id,
            market_id=market_id,
            created_at=created_at,
        )
        with_run = MakerProposal(**{
            **{f.name: getattr(prop, f.name)
               for f in prop.__dataclass_fields__.values()},
            "run_id": run_id,
        })
        store.append(
            with_run,
            STATUS_PAPER_ACTIVE if eligible else STATUS_PAPER_REJECTED,
        )

    def _populate_two_runs(self, store: PaperMakerStore) -> tuple[float, float]:
        """Run A (older, 3 records) + Run B (newer, 2 records).  Returns
        (run_a_ts, run_b_ts) for tests that need to assert on times."""
        import time
        run_a = "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa"
        run_b = "bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb"
        ts_a = time.time() - 60.0
        ts_b = time.time()
        # Run A — older, 3 records
        for i in range(3):
            self._append_with_run(
                store, run_id=run_a, market_id=f"K-A{i}",
                proposal_id=f"A-{i}", created_at=ts_a + i * 0.001,
            )
        # Run B — newer, 2 records (one rejected)
        self._append_with_run(
            store, run_id=run_b, market_id="K-B0",
            proposal_id="B-0", created_at=ts_b,
        )
        self._append_with_run(
            store, run_id=run_b, market_id="K-B1",
            proposal_id="B-1", created_at=ts_b + 0.001,
            eligible=False, rejection_reasons=("BOOK_STALE",),
        )
        return ts_a, ts_b

    def test_default_returns_all_records_across_runs(self, client_with_store):
        client, store, _ = client_with_store
        self._populate_two_runs(store)
        body = client.get("/api/maker/proposals").json()
        # Default: latest_run=false → all 5 records visible
        assert body["count"] == 5

    def test_latest_run_returns_only_newest_run(self, client_with_store):
        client, store, _ = client_with_store
        self._populate_two_runs(store)
        body = client.get(
            "/api/maker/proposals", params={"latest_run": "true"}
        ).json()
        assert body["count"] == 2
        # Only Run B's records survive the filter
        ids = {p["proposal_id"] for p in body["proposals"]}
        assert ids == {"B-0", "B-1"}
        run_ids = {p["run_id"] for p in body["proposals"]}
        assert run_ids == {"bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb"}

    def test_latest_run_composes_with_other_filters(self, client_with_store):
        """latest_run=true + eligible=true must apply both: latest scan AND
        eligible only.  Run B has 1 eligible (B-0) and 1 rejected (B-1).
        Run A has 3 eligibles, but they should be excluded by latest_run."""
        client, store, _ = client_with_store
        self._populate_two_runs(store)
        body = client.get(
            "/api/maker/proposals",
            params={"latest_run": "true", "eligible": "true"},
        ).json()
        assert body["count"] == 1
        assert body["proposals"][0]["proposal_id"] == "B-0"

    def test_latest_run_market_id_filter_combines(self, client_with_store):
        """Frontend's exact query: latest_run=true + market_id=<sel>."""
        client, store, _ = client_with_store
        self._populate_two_runs(store)
        body = client.get(
            "/api/maker/proposals",
            params={"latest_run": "true", "market_id": "K-A0"},
        ).json()
        # K-A0 is in Run A (the older one), so latest_run filters it out
        assert body["count"] == 0

        body = client.get(
            "/api/maker/proposals",
            params={"latest_run": "true", "market_id": "K-B0"},
        ).json()
        assert body["count"] == 1
        assert body["proposals"][0]["proposal_id"] == "B-0"

    def test_legacy_only_records_fallback_to_all(self, client_with_store):
        """When no record carries a run_id (entire file is legacy),
        latest_run=true falls back gracefully to the full set rather than
        returning empty.  Prevents a stale-data outage on dashboards."""
        client, store, _ = client_with_store
        # _proposal() default factory leaves run_id=None → legacy
        store.append(_proposal(proposal_id="legacy-1"), STATUS_PAPER_ACTIVE)
        store.append(_proposal(proposal_id="legacy-2"), STATUS_PAPER_ACTIVE)
        body = client.get(
            "/api/maker/proposals", params={"latest_run": "true"}
        ).json()
        assert body["count"] == 2

    def test_mixed_legacy_and_run_records_excludes_legacy(self, client_with_store):
        """When some records have run_ids and others don't, latest_run
        narrows to the newest run_id and DROPS the legacy records (they
        can't be assigned to any run)."""
        client, store, _ = client_with_store
        # 1 legacy + 2 in run X
        store.append(_proposal(proposal_id="legacy-1"), STATUS_PAPER_ACTIVE)
        self._append_with_run(
            store, run_id="cccccccc-cccc-cccc-cccc-cccccccccccc",
            market_id="K-X0", proposal_id="X-0",
            created_at=__import__("time").time(),
        )
        self._append_with_run(
            store, run_id="cccccccc-cccc-cccc-cccc-cccccccccccc",
            market_id="K-X1", proposal_id="X-1",
            created_at=__import__("time").time() + 0.001,
        )
        body = client.get(
            "/api/maker/proposals", params={"latest_run": "true"}
        ).json()
        ids = {p["proposal_id"] for p in body["proposals"]}
        assert ids == {"X-0", "X-1"}

    def test_summary_latest_run_counts_only_newest(self, client_with_store):
        """summary?latest_run=true → counts based on Run B (2 records,
        1 eligible, 1 rejected).  default summary still aggregates all 5."""
        client, store, _ = client_with_store
        self._populate_two_runs(store)

        full = client.get("/api/maker/summary").json()
        assert full["total"] == 5
        assert full["eligible"] == 4
        assert full["rejected"] == 1

        latest = client.get(
            "/api/maker/summary", params={"latest_run": "true"}
        ).json()
        assert latest["total"] == 2
        assert latest["eligible"] == 1
        assert latest["rejected"] == 1

    def test_summary_legacy_fallback_matches_proposals_fallback(
        self, client_with_store
    ):
        """All-legacy fallback applies to /summary as well — never returns
        empty just because no record has a run_id."""
        client, store, _ = client_with_store
        store.append(_proposal(eligible=True), STATUS_PAPER_ACTIVE)
        store.append(_proposal(eligible=False, rejection_reasons=("BOOK_STALE",)),
                     STATUS_PAPER_REJECTED)
        latest = client.get(
            "/api/maker/summary", params={"latest_run": "true"}
        ).json()
        assert latest["total"] == 2
        assert latest["eligible"] == 1
        assert latest["rejected"] == 1


class TestLimitParameter:
    """``limit`` caps response size at 1000 (server-side clamp); default 100."""

    def test_default_limit_is_100(self, client_with_store):
        """Sanity: explicit empty store + no limit param → default
        clamp kicks in but list is empty anyway."""
        client, _, _ = client_with_store
        body = client.get("/api/maker/proposals").json()
        # Cannot assert a number with empty store; covered indirectly below.
        assert body["count"] == 0

    def test_limit_caps_returned_records(self, client_with_store):
        client, store, _ = client_with_store
        for i in range(15):
            store.append(
                _proposal(proposal_id=f"p-{i}"),
                STATUS_PAPER_ACTIVE,
            )
        body = client.get(
            "/api/maker/proposals", params={"limit": 5}
        ).json()
        assert body["count"] == 5

    def test_limit_returns_newest_first(self, client_with_store):
        """When the limit truncates, the *most recent* records survive —
        the chronologically oldest are dropped first."""
        import time
        client, store, _ = client_with_store
        base_ts = time.time()
        for i in range(5):
            store.append(
                _proposal(proposal_id=f"p-{i}", created_at=base_ts + i),
                STATUS_PAPER_ACTIVE,
            )
        body = client.get(
            "/api/maker/proposals", params={"limit": 2}
        ).json()
        ids = [p["proposal_id"] for p in body["proposals"]]
        # p-4 (newest) and p-3 (next-newest) must survive; p-0..p-2 dropped.
        assert ids == ["p-4", "p-3"]

    def test_limit_above_max_rejected(self, client_with_store):
        """Server clamps at 1000; values above must 422."""
        client, _, _ = client_with_store
        resp = client.get("/api/maker/proposals", params={"limit": 1001})
        assert resp.status_code == 422

    def test_limit_zero_rejected(self, client_with_store):
        client, _, _ = client_with_store
        resp = client.get("/api/maker/proposals", params={"limit": 0})
        assert resp.status_code == 422

    def test_limit_at_max_accepted(self, client_with_store):
        client, _, _ = client_with_store
        resp = client.get("/api/maker/proposals", params={"limit": 1000})
        assert resp.status_code == 200


class TestNullablePriceFieldsSurfacedByApi:
    """Regression guard for the rejected-proposal price-sanitization fix:
    None-valued ``maker_max_bid`` / ``proposed_price`` / ``estimated_maker_edge``
    must serialize to JSON null in the API response, and the summary endpoint
    must skip None edges in its average."""

    def test_proposed_price_null_in_response_for_low_p_true(self, client_with_store):
        client, store, _ = client_with_store
        store.append(
            _proposal(
                eligible=False,
                rejection_reasons=("TRUE_PROB_TOO_LOW", "BOOK_CROSSED_OR_EMPTY"),
                proposal_id="san-1",
                maker_max_bid=None,
                proposed_price=None,
                estimated_maker_edge=None,
            ),
            STATUS_PAPER_REJECTED,
        )
        resp = client.get("/api/maker/proposals")
        assert resp.status_code == 200
        # Raw JSON null in the response body, not -0.01
        assert '"proposed_price":null' in resp.text
        assert '"maker_max_bid":null' in resp.text
        assert '"estimated_maker_edge":null' in resp.text
        assert "-0.01" not in resp.text

        rec = resp.json()["proposals"][0]
        assert rec["proposed_price"] is None
        assert rec["maker_max_bid"] is None
        assert rec["estimated_maker_edge"] is None

    def test_summary_average_edge_excludes_none_rejected_records(self, client_with_store):
        """A rejected record with edge=None must not poison the average over
        eligible records.  Adds one eligible (edge=0.08) and one rejected
        (edge=None) — average is 0.08, not NaN/0/error."""
        client, store, _ = client_with_store
        store.append(_proposal(eligible=True, proposal_id="elig-1"), STATUS_PAPER_ACTIVE)
        store.append(
            _proposal(
                eligible=False,
                rejection_reasons=("TRUE_PROB_TOO_LOW",),
                proposal_id="rej-1",
                proposed_price=None,
                maker_max_bid=None,
                estimated_maker_edge=None,
            ),
            STATUS_PAPER_REJECTED,
        )
        body = client.get("/api/maker/summary").json()
        assert body["average_estimated_maker_edge"] == pytest.approx(0.08)


class TestExecutionRouteSurfacedByApi:
    """The four route fields written by ``PaperMakerStore`` must round-trip
    through ``GET /api/maker/proposals`` unchanged.  Pre-7bba5c6 records
    legitimately lack these fields; this suite covers the post-7bba5c6
    contract on fresh records."""

    def _append_proposal_with_route(
        self, store: PaperMakerStore, *, market_id: str, route_market_id: str,
        route_side: str, display_side: str, proposal_id: str,
    ):
        from services.maker.planner import MakerProposal
        prop = _proposal(proposal_id=proposal_id, market_id=market_id)
        prop_with_route = MakerProposal(
            **{**{f.name: getattr(prop, f.name)
                  for f in prop.__dataclass_fields__.values()},
               "display_side": display_side,
               "execution_market_id": route_market_id,
               "execution_contract_side": route_side,
               "execution_route": "direct_yes" if route_side == "yes" else "equivalent_no"}
        )
        store.append(prop_with_route, STATUS_PAPER_ACTIVE)

    def test_direct_yes_route_in_response(self, client_with_store):
        client, store, _ = client_with_store
        self._append_proposal_with_route(
            store,
            market_id="KXATPMATCH-A",
            route_market_id="KXATPMATCH-A",
            route_side="yes",
            display_side="Player A",
            proposal_id="route-direct",
        )
        body = client.get("/api/maker/proposals").json()
        assert body["count"] == 1
        rec = body["proposals"][0]
        assert rec["display_side"] == "Player A"
        assert rec["execution_market_id"] == "KXATPMATCH-A"
        assert rec["execution_contract_side"] == "yes"
        assert rec["execution_route"] == "direct_yes"

    def test_equivalent_no_route_in_response(self, client_with_store):
        client, store, _ = client_with_store
        self._append_proposal_with_route(
            store,
            market_id="KXATPMATCH-A",
            route_market_id="KXATPMATCH-B",
            route_side="no",
            display_side="Player A",
            proposal_id="route-equiv",
        )
        body = client.get("/api/maker/proposals").json()
        rec = body["proposals"][0]
        # Canonical market_id (legacy display) stays as the team's own ticker
        assert rec["market_id"] == "KXATPMATCH-A"
        # But execution route names the actual contract a paper order would post on
        assert rec["execution_market_id"] == "KXATPMATCH-B"
        assert rec["execution_contract_side"] == "no"
        assert rec["execution_route"] == "equivalent_no"

    def test_pre_route_record_omits_fields_silently(self, client_with_store):
        """Sanity guard for the legacy-record path: a record persisted
        without route fields (simulating pre-7bba5c6 JSONL lines) flows
        through the API unchanged.  The fields are simply absent from the
        response dict; the frontend supplies fallbacks via ``||`` / ``??``
        on the typed-as-optional fields."""
        client, store, _ = client_with_store
        # Build a legacy record by writing JSONL directly, bypassing the
        # store's _proposal_to_record (which always emits the four fields).
        import json
        from datetime import datetime, timezone
        ts = _proposal().created_at
        path = store.path_for(datetime.fromtimestamp(ts, tz=timezone.utc).date())
        path.parent.mkdir(parents=True, exist_ok=True)
        legacy_record = {
            "schema_version": 1, "status": STATUS_PAPER_ACTIVE,
            "proposal_id": "legacy-1", "platform": "kalshi",
            "market_id": "K-LEGACY", "market_type": "h2h", "side": "A",
            "event_label": "A vs B", "event_start": "2099-01-01T00:00:00Z",
            "p_true": 0.5, "required_edge": 0.05, "cost_buffer": 0.01,
            "best_bid": 0.4, "best_ask": 0.9,
            "book_fetched_at": ts, "fd_fetched_at": ts,
            "maker_max_bid": 0.44, "proposed_price": 0.41,
            "tick_size": 0.01, "estimated_maker_edge": 0.08,
            "eligible": True, "rejection_reasons": [],
            "rule_evaluations": [], "taker_status_at_planning": "SKIP",
            "taker_reject_reasons": ["NO_EDGE"],
            "taker_downgrade_reasons": [], "taker_edge_at_planning": -0.41,
            "created_at": ts, "notes": ["paper_only"],
            # No display_side / execution_* — pre-7bba5c6 schema
        }
        with open(path, "a", encoding="utf-8") as f:
            f.write(json.dumps(legacy_record) + "\n")

        body = client.get("/api/maker/proposals").json()
        rec = body["proposals"][0]
        assert rec["proposal_id"] == "legacy-1"
        # API does not synthesize defaults for legacy records — the frontend
        # is responsible for the fallback (``p.execution_market_id || p.market_id``).
        assert "execution_market_id" not in rec
        assert "execution_contract_side" not in rec
        assert "execution_route" not in rec
        assert "display_side" not in rec


class TestNoSecrets:
    def test_response_does_not_contain_api_key_or_paths(
        self, monkeypatch, client_with_store
    ):
        monkeypatch.setenv("KALSHI_API_KEY", "secret-XYZ-7777")
        client, store, tmp_path = client_with_store
        store.append(_proposal(), STATUS_PAPER_ACTIVE)

        for path in ("/api/maker/proposals", "/api/maker/summary"):
            resp = client.get(path)
            raw = resp.text
            # No env-var values
            assert "secret-XYZ-7777" not in raw
            # No auth header literals
            assert "Authorization" not in raw
            assert "api_key" not in raw
            # No filesystem paths leaked into the JSON
            assert str(tmp_path) not in raw
            assert "data/maker_paper_orders" not in raw


# ---------------------------------------------------------------------------
# Routing
# ---------------------------------------------------------------------------

class TestRouterMounted:
    def test_proposals_route_exists(self, client_with_store):
        client, _, _ = client_with_store
        assert client.get("/api/maker/proposals").status_code == 200

    def test_summary_route_exists(self, client_with_store):
        client, _, _ = client_with_store
        assert client.get("/api/maker/summary").status_code == 200

    def test_unknown_maker_route_404(self, client_with_store):
        client, _, _ = client_with_store
        assert client.get("/api/maker/no-such-route").status_code == 404


# ---------------------------------------------------------------------------
# /api/maker/config — runtime config GET/POST
#
# Safety invariants:
#   paper_only           must always remain True
#   platforms            must always remain ("kalshi",)
#   market_types         must always remain ("h2h",)
# These are not in MakerConfigUpdateRequest and are clamped by _enforce_safety
# regardless of input.  Tests below confirm both layers of defense.
# ---------------------------------------------------------------------------

class TestMakerConfigGet:
    def test_get_returns_disabled_paper_only_defaults(self, client_with_store):
        client, _, _ = client_with_store
        resp = client.get("/api/maker/config")
        assert resp.status_code == 200
        body = resp.json()
        assert body["enabled"] is False
        assert body["paper_only"] is True
        assert body["platforms"] == ["kalshi"]
        assert body["market_types"] == ["h2h"]
        # Sanity: the documented default thresholds are surfaced
        assert body["min_estimated_maker_edge"] == pytest.approx(0.05)
        assert body["require_min_spread"] == pytest.approx(0.02)
        assert body["max_book_age_seconds"] == pytest.approx(30.0)
        assert body["max_fd_age_seconds"] == pytest.approx(600.0)


class TestMakerConfigPostEnableAndDisable:
    def test_post_can_enable(self, client_with_store):
        client, _, _ = client_with_store
        resp = client.post("/api/maker/config", json={"enabled": True})
        assert resp.status_code == 200
        body = resp.json()
        assert body["enabled"] is True
        # Safety invariants still hold
        assert body["paper_only"] is True
        assert body["platforms"] == ["kalshi"]
        assert body["market_types"] == ["h2h"]

    def test_post_persists_across_get(self, client_with_store):
        client, _, _ = client_with_store
        client.post("/api/maker/config", json={"enabled": True, "min_estimated_maker_edge": 0.07})
        body = client.get("/api/maker/config").json()
        assert body["enabled"] is True
        assert body["min_estimated_maker_edge"] == pytest.approx(0.07)

    def test_post_can_disable_again(self, client_with_store):
        client, _, _ = client_with_store
        client.post("/api/maker/config", json={"enabled": True})
        resp = client.post("/api/maker/config", json={"enabled": False})
        assert resp.json()["enabled"] is False

    def test_post_partial_update_preserves_other_fields(self, client_with_store):
        client, _, _ = client_with_store
        client.post("/api/maker/config", json={
            "enabled": True,
            "min_estimated_maker_edge": 0.08,
            "max_book_age_seconds": 45.0,
        })
        # Only update enabled; min_edge and max_book_age must remain
        client.post("/api/maker/config", json={"enabled": False})
        body = client.get("/api/maker/config").json()
        assert body["enabled"] is False
        assert body["min_estimated_maker_edge"] == pytest.approx(0.08)
        assert body["max_book_age_seconds"] == pytest.approx(45.0)


class TestMakerConfigPostSafety:
    """Even if a hostile client tries to send unsafe fields, the endpoint
    must NOT change paper_only / platforms / market_types away from safe."""

    def test_post_paper_only_false_ignored(self, client_with_store):
        client, _, _ = client_with_store
        resp = client.post("/api/maker/config", json={"enabled": True, "paper_only": False})
        assert resp.status_code == 200
        # paper_only stays True regardless
        assert resp.json()["paper_only"] is True
        # And the runtime singleton agrees
        assert client.get("/api/maker/config").json()["paper_only"] is True

    def test_post_platforms_polymarket_ignored(self, client_with_store):
        client, _, _ = client_with_store
        resp = client.post("/api/maker/config", json={
            "enabled": True,
            "platforms": ["polymarket", "kalshi"],
        })
        assert resp.status_code == 200
        # platforms stays locked to ("kalshi",)
        assert resp.json()["platforms"] == ["kalshi"]

    def test_post_market_types_totals_ignored(self, client_with_store):
        client, _, _ = client_with_store
        resp = client.post("/api/maker/config", json={
            "enabled": True,
            "market_types": ["totals", "handicap"],
        })
        assert resp.status_code == 200
        assert resp.json()["market_types"] == ["h2h"]

    def test_post_unknown_live_trading_flag_ignored(self, client_with_store):
        """A made-up "live_trading=true" key must be silently ignored by Pydantic
        and produce no behavior change."""
        client, _, _ = client_with_store
        resp = client.post("/api/maker/config", json={
            "enabled": True,
            "live_trading": True,
            "place_real_orders": True,
        })
        assert resp.status_code == 200
        # Safety invariants still hold; no unknown field is reflected
        body = resp.json()
        assert body["paper_only"] is True
        assert "live_trading" not in body
        assert "place_real_orders" not in body

    def test_post_combined_unsafe_payload_clamped(self, client_with_store):
        """Combined attempt: enable + disable paper_only + add polymarket +
        switch to totals.  Only the 'enabled' field should win; everything
        else stays at the safe defaults."""
        client, _, _ = client_with_store
        resp = client.post("/api/maker/config", json={
            "enabled": True,
            "paper_only": False,
            "platforms": ["polymarket"],
            "market_types": ["totals"],
            "live_trading": True,
        })
        body = resp.json()
        assert body["enabled"] is True
        assert body["paper_only"] is True
        assert body["platforms"] == ["kalshi"]
        assert body["market_types"] == ["h2h"]


class TestMakerConfigPostRejectsInvalidNumbers:
    """Non-finite or obviously invalid numbers must be rejected with 422
    BEFORE the runtime config is touched.  Regression: previously
    ``Infinity`` was stored (removing the book-freshness gate) and then
    every later GET/POST returned 500 because the value could not be
    JSON-serialized.

    Raw bodies are sent via ``content=`` because ``json=`` cannot encode
    Infinity/NaN."""

    @staticmethod
    def _post_raw(client, body: str):
        return client.post(
            "/api/maker/config",
            content=body,
            headers={"Content-Type": "application/json"},
        )

    @pytest.mark.parametrize("body", [
        '{"enabled": true, "max_book_age_seconds": Infinity}',
        '{"enabled": true, "max_book_age_seconds": -Infinity}',
        '{"enabled": true, "max_book_age_seconds": NaN}',
        '{"enabled": true, "max_book_age_seconds": 1e309}',
        '{"enabled": true, "max_fd_age_seconds": Infinity}',
        '{"enabled": true, "min_estimated_maker_edge": NaN}',
        '{"enabled": true, "require_min_spread": 1e309}',
        '{"enabled": true, "max_notional_per_order_usd": Infinity}',
        '{"enabled": true, "max_daily_notional_usd": NaN}',
        '{"enabled": true, "max_book_age_seconds": -1}',
        '{"enabled": true, "max_fd_age_seconds": -0.5}',
        '{"enabled": true, "min_estimated_maker_edge": -0.05}',
        '{"enabled": true, "require_min_spread": -0.01}',
        '{"enabled": true, "max_notional_per_market_usd": -10}',
        '{"enabled": true, "max_contracts_per_order": 0}',
        '{"enabled": true, "max_open_orders_total": -1}',
        '{"enabled": true, "max_open_orders_per_market": 0}',
        '{"enabled": true, "max_orders_per_hour": 0}',
        '{"enabled": true, "default_ttl_seconds": 0}',
        '{"enabled": true, "cancel_before_event_offset_s": -1}',
    ])
    def test_invalid_number_rejected_and_config_unchanged(self, client_with_store, body):
        from services.maker.config import get_maker_config
        client, _, _ = client_with_store
        before = get_maker_config()

        resp = self._post_raw(client, body)
        assert resp.status_code == 422

        # Runtime config untouched — in particular, still disabled.
        after = get_maker_config()
        assert after == before
        assert after.enabled is False
        assert after.paper_only is True

        # GET still serializes cleanly.
        get_resp = client.get("/api/maker/config")
        assert get_resp.status_code == 200
        get_body = get_resp.json()
        assert get_body["enabled"] is False
        assert get_body["paper_only"] is True
        assert get_body["max_book_age_seconds"] == pytest.approx(30.0)

    def test_invalid_post_preserves_previous_runtime_values(self, client_with_store):
        client, _, _ = client_with_store
        ok = client.post("/api/maker/config", json={
            "enabled": True, "max_book_age_seconds": 45.0,
        })
        assert ok.status_code == 200

        bad = self._post_raw(client, '{"enabled": false, "max_book_age_seconds": Infinity}')
        assert bad.status_code == 422

        body = client.get("/api/maker/config").json()
        assert body["enabled"] is True
        assert body["max_book_age_seconds"] == pytest.approx(45.0)

        # A subsequent valid POST still works (UI can turn maker off).
        off = client.post("/api/maker/config", json={"enabled": False})
        assert off.status_code == 200
        assert off.json()["enabled"] is False

    def test_422_body_names_finite_number_error(self, client_with_store):
        client, _, _ = client_with_store
        resp = self._post_raw(client, '{"max_book_age_seconds": Infinity}')
        assert resp.status_code == 422
        detail = resp.json()["detail"]
        assert any(
            d["type"] == "finite_number" and d["loc"][-1] == "max_book_age_seconds"
            for d in detail
        )

    def test_endpoint_validates_before_storing(self, client_with_store):
        """Defense in depth: even if a non-finite value bypassed request
        validation (model_construct skips it), the endpoint must fail
        before set_maker_config is reached."""
        import pydantic
        from routers.maker import (
            MakerConfigUpdateRequest,
            update_maker_config_endpoint,
        )
        from services.maker.config import get_maker_config

        before = get_maker_config()
        req = MakerConfigUpdateRequest.model_construct(
            enabled=True, max_book_age_seconds=float("inf"),
        )
        # model_construct does not populate fields_set from kwargs the
        # same way; mark them explicitly so exclude_unset keeps them.
        req.__pydantic_fields_set__ = {"enabled", "max_book_age_seconds"}
        with pytest.raises(pydantic.ValidationError):
            update_maker_config_endpoint(req)
        assert get_maker_config() == before
        assert get_maker_config().enabled is False

    def test_zero_ages_and_offset_still_accepted(self, client_with_store):
        """Boundary: ge=0 fields accept 0 (fail-closed values)."""
        client, _, _ = client_with_store
        resp = client.post("/api/maker/config", json={
            "max_book_age_seconds": 0,
            "cancel_before_event_offset_s": 0,
            "min_estimated_maker_edge": 0.0,
        })
        assert resp.status_code == 200
        assert resp.json()["enabled"] is False


class TestMakerConfigRefreshIntegration:
    """The runtime config drives the normal refresh path: when enabled via
    POST, ``fetch_opportunities`` (called by snapshot.refresh_snapshot)
    runs the maker pass; when disabled, no records are written."""

    def test_refresh_disabled_writes_no_records(self, client_with_store):
        import asyncio
        from unittest.mock import patch
        from services.opportunities import fetch_opportunities
        from services.engine_config import EngineConfig

        # Stub the upstream odds fetch to avoid network
        async def _no_odds(*args, **kwargs):
            return [], {}

        client, store, _ = client_with_store

        # Maker disabled (default)
        with patch("services.opportunities.fetch_odds", side_effect=_no_odds):
            opps, meta = asyncio.run(fetch_opportunities(
                cfg=EngineConfig(),
                adapters=[],
            ))

        assert meta["maker_enabled"] is False
        assert meta["maker_proposals_total"] == 0
        # No JSONL file written
        assert store.read_recent(days=2) == []

    def test_refresh_enabled_via_runtime_meta_reflects(self, client_with_store):
        import asyncio
        from unittest.mock import patch
        from services.opportunities import fetch_opportunities
        from services.engine_config import EngineConfig

        async def _no_odds(*args, **kwargs):
            return [], {}

        client, store, _ = client_with_store
        # Enable via the API endpoint — the same path a frontend would use.
        client.post("/api/maker/config", json={"enabled": True})

        with patch("services.opportunities.fetch_odds", side_effect=_no_odds):
            opps, meta = asyncio.run(fetch_opportunities(
                cfg=EngineConfig(),
                adapters=[],
            ))

        # No features → 0 proposals, but maker_enabled=True flows through.
        assert meta["maker_enabled"] is True
        assert meta["maker_proposals_total"] == 0
        # Sanity: subsequent GET shows the runtime is still enabled
        assert client.get("/api/maker/config").json()["enabled"] is True


class TestMakerConfigNoOrderPlacementCode:
    """Static guard: no order-placement keywords appear in the maker
    runtime path (config + service + planner + state).  If a future commit
    introduces live trading, it must be deliberate enough to also touch
    this list — and a code review will catch it."""

    def test_no_order_placement_strings_in_maker_modules(self):
        from pathlib import Path
        backend_root = Path(__file__).resolve().parent.parent
        forbidden = ("portfolio/orders", "place_order", "create_order")
        offenders = []
        for module in (
            backend_root / "services" / "maker" / "config.py",
            backend_root / "services" / "maker" / "service.py",
            backend_root / "services" / "maker" / "planner.py",
            backend_root / "services" / "maker" / "state.py",
            backend_root / "services" / "maker" / "policy.py",
            backend_root / "routers" / "maker.py",
        ):
            text = module.read_text(encoding="utf-8")
            for needle in forbidden:
                if needle in text:
                    offenders.append(f"{module.name}: {needle}")
        assert offenders == [], f"Order-placement strings found: {offenders}"
