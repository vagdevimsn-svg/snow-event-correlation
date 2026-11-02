"""CLI: python -m event_corr --alerts sample_data/alerts.json --topology sample_data/topology.json"""
from __future__ import annotations

import argparse
import json
import logging
import sys
from datetime import timedelta
from pathlib import Path

from .engine import Correlator


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="event_corr", description="Correlate monitoring alerts into incidents")
    ap.add_argument("--alerts", required=True)
    ap.add_argument("--topology", required=True)
    ap.add_argument("--window-min", type=int, default=10)
    ap.add_argument("-v", "--verbose", action="store_true")
    args = ap.parse_args(argv)
    logging.basicConfig(level=logging.DEBUG if args.verbose else logging.INFO,
                        format="%(levelname)s %(name)s: %(message)s", stream=sys.stderr)
    try:
        alerts = json.loads(Path(args.alerts).read_text())
        topo = json.loads(Path(args.topology).read_text())
    except (OSError, json.JSONDecodeError) as exc:
        logging.error("cannot read input: %s", exc)
        return 1
    c = Correlator(topo["ci_by_host"], topo["parent_of"], topo["service_tier"],
                   window=timedelta(minutes=args.window_min))
    c.ingest(alerts)
    incidents = c.incidents()
    logging.info("%d raw alerts -> %d de-duplicated -> %d incidents (%d rejected)",
                 len(alerts), len(c.alerts), len(incidents), len(c.rejected))
    print(json.dumps(incidents, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
