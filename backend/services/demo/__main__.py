"""
Run the offline demo pipeline once and print a compact table.

Usage (from backend/, no API keys, no network, no Node needed):

    python -m services.demo            # BUY + WATCH (what the dashboard lists)
    python -m services.demo --all      # also SKIP rows with their reject reasons

The synthetic Kalshi + Polymarket fixtures (services/demo/fixtures.py) go
through the real pipeline -- Pass A extract_features, series dedup,
ambiguity enrichment, Pass B evaluate_rules, Kelly -- exactly as
fetch_opportunities runs it.  Every status shown comes from the rule engine.
"""
from __future__ import annotations

import argparse
import asyncio
import logging

from services.demo.fixtures import demo_adapters, make_demo_odds_fetcher
from services.maker.config import MakerConfig
from services.opportunities import DEFAULT_CONFIG, diagnose_pipeline, fetch_opportunities


def _fmt_row(cols: list[str], widths: list[int]) -> str:
    return "  ".join(c.ljust(w) for c, w in zip(cols, widths)).rstrip()


def _market(platform: str, market_type: str, line: float | None) -> str:
    return f"{platform} {market_type}" + (f" {line:g}" if line is not None else "")


def _table(rows: list[list[str]]) -> str:
    header = ["STATUS", "MARKET", "EVENT", "SIDE", "PRICE", "P_MODEL", "EDGE", "REASONS"]
    widths = [max(len(r[i]) for r in [header, *rows]) for i in range(len(header))]
    lines = [_fmt_row(header, widths), _fmt_row(["-" * w for w in widths], widths)]
    lines += [_fmt_row(r, widths) for r in rows]
    return "\n".join(lines)


async def _run(show_all: bool) -> str:
    adapters = demo_adapters()
    fetcher = make_demo_odds_fetcher()
    opps, meta = await fetch_opportunities(
        cfg=DEFAULT_CONFIG, adapters=adapters, odds_fetcher=fetcher, maker_cfg=MakerConfig(),
    )
    rows = [
        [o.status, _market(o.platform, o.market_type, o.line), o.event, o.side,
         f"{o.pm_price:.3f}", f"{o.p_true:.3f}", f"{o.edge:+.3f}",
         ", ".join(o.downgrade_reasons) or "-"]
        for o in opps
    ]
    out = [
        "Prediction Market Scanner -- OFFLINE DEMO MODE (synthetic example data; no external requests)",
        f"Pass B status counts (all evaluated candidates): {meta['status_counts']}",
        "",
        _table(rows),
    ]
    if show_all:
        # SKIPs are not returned by fetch_opportunities (active list only);
        # the diagnostic pipeline exposes every evaluated candidate.  Note it
        # does not run series dedup, so cross-game pairings appear here too.
        diag = await diagnose_pipeline(cfg=DEFAULT_CONFIG, adapters=adapters, odds_fetcher=fetcher)
        skips = [
            [e["status"], _market(e["platform"], e["market_type"], None), e["event"],
             e["side"] or "-", f"{e['pm_price']:.3f}", f"{e['p_true']:.3f}",
             f"{e['edge']:+.3f}", ", ".join(e["reject_reasons"])]
            for e in diag["pass_b_classification"]["evaluated"]
            if e["status"] == "SKIP"
        ]
        out += ["", "SKIP candidates (diagnose_pipeline, no series dedup):", "", _table(skips)]
    return "\n".join(out)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--all", action="store_true", help="also list SKIP candidates")
    args = parser.parse_args()
    logging.basicConfig(level=logging.WARNING)
    print(asyncio.run(_run(args.all)))


if __name__ == "__main__":
    main()
