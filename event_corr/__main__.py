"""CLI: event-corr --alerts sample_data/alerts.json --topology sample_data/topology.json"""
from __future__ import annotations

import argparse
import json
import logging
import sys
from datetime import timedelta
from pathlib import Path

from . import __version__
from .engine import Correlator
from .forwarder import ForwardError, IncidentForwarder
from .topology import TopologyError, load_topology


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="event-corr", description="Correlate monitoring alerts into incidents")
    ap.add_argument("--alerts", required=True, help="JSON array of raw alerts")
    ap.add_argument("--topology", required=True, help="topology JSON or a CMDB export (cmdb_ci + cmdb_rel_ci)")
    ap.add_argument("--maintenance", help="JSON array of {ci, start, end} maintenance windows")
    ap.add_argument("--window-min", type=int, default=10, help="correlation window in minutes")
    ap.add_argument("--flap-threshold", type=int, default=3, help="re-opens within the flap window that mark flapping")
    ap.add_argument("--metrics", action="store_true", help="print noise-reduction metrics instead of incidents")
    ap.add_argument("--send", action="store_true",
                    help="create incidents in ServiceNow (SN_* env vars); default is dry-run")
    ap.add_argument("-v", "--verbose", action="store_true")
    ap.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    args = ap.parse_args(argv)
    logging.basicConfig(level=logging.DEBUG if args.verbose else logging.INFO,
                        format="%(levelname)s %(name)s: %(message)s", stream=sys.stderr)
    try:
        alerts = json.loads(Path(args.alerts).read_text(encoding="utf-8"))
        topo = load_topology(args.topology, args.maintenance)
    except (OSError, json.JSONDecodeError, TopologyError) as exc:
        logging.error("cannot read input: %s", exc)
        return 1
    if not isinstance(alerts, list):
        logging.error("alerts file must contain a JSON array")
        return 1

    c = Correlator(topo.ci_by_host, topo.parent_of, topo.service_tier, window=timedelta(minutes=args.window_min),
                   maintenance=topo.maintenance, flap_threshold=args.flap_threshold)
    c.ingest(alerts)
    incidents, m = c.incidents(), c.metrics()
    logging.info("%d raw alerts -> %d unique -> %d incidents (%d rejected, %d suppressed, %d flapping); "
                 "noise reduction %.1f%%", m["raw_events"], m["unique_alerts"], m["incidents"], m["rejected"],
                 m["suppressed_maintenance"], m["flapping"], m["noise_reduction_pct"])
    if args.metrics:
        print(json.dumps(m, indent=2))
        return 0
    if args.send:
        try:
            print(json.dumps(IncidentForwarder.from_env().send(incidents), indent=2))
        except ForwardError as exc:
            logging.error("%s", exc)
            return 1
        return 0
    print(json.dumps(incidents, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
