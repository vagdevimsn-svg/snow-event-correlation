import json
from datetime import timedelta
from pathlib import Path

import pytest

from event_corr.__main__ import main
from event_corr.engine import Correlator, EventError, normalize

DATA = Path(__file__).resolve().parents[1] / "sample_data"
TOPO = json.loads((DATA / "topology.json").read_text())
ALERTS = json.loads((DATA / "alerts.json").read_text())


def make(window=10):
    return Correlator(TOPO["ci_by_host"], TOPO["parent_of"], TOPO["service_tier"], window=timedelta(minutes=window))


def raw(host="lnx-pay-01", sev="Major", ts="2026-10-30T14:00:00Z", check="http_5xx"):
    return {"source": "test", "host": host, "check": check, "resource": "r", "severity": sev,
            "message": f"{check} on {host}", "timestamp": ts}


def test_normalize_maps_severity_and_lowercases_host():
    ev = normalize(raw(host="LNX-PAY-01", sev="Critical"))
    assert ev.severity == 1 and ev.node == "lnx-pay-01"


@pytest.mark.parametrize("bad", [{"host": "x"}, raw(sev="nope"), {**raw(), "timestamp": "yesterday"}])
def test_normalize_rejects_malformed(bad):
    with pytest.raises(EventError):
        normalize(bad)


def test_dedupe_counts_repeats_and_escalates_severity():
    c = make()
    c.ingest([raw(sev="Minor"), raw(sev="Critical", ts="2026-10-30T14:02:00Z")])
    (alert,) = c.alerts.values()
    assert alert.count == 2 and alert.severity == 1


def test_clear_closes_alert():
    c = make()
    c.ingest([raw(), raw(sev="Clear", ts="2026-10-30T14:01:00Z")])
    assert next(iter(c.alerts.values())).state == "Closed"
    assert c.incidents() == []


def test_service_walk_and_cycle_protection():
    c = make()
    assert c.service_for("db_paydb01") == "Online Payments"
    assert c.service_for(None) is None
    cyc = Correlator({}, {"a": "b", "b": "a"}, {"svc": 1})
    assert cyc.service_for("a") is None


def test_alerts_on_same_service_within_window_become_one_incident():
    c = make()
    c.ingest([raw(host="paydb01", sev="Critical", check="db"), raw(host="lnx-pay-02", ts="2026-10-30T14:03:00Z")])
    incs = c.incidents()
    assert len(incs) == 1
    assert incs[0]["cmdb_ci"] == "db_paydb01"  # highest severity, earliest = probable cause
    assert incs[0]["priority"] == 1


def test_alerts_outside_window_split():
    c = make(window=5)
    c.ingest([raw(host="paydb01", check="db"), raw(host="lnx-pay-02", ts="2026-10-30T14:30:00Z")])
    assert len(c.incidents()) == 2


def test_priority_matrix():
    c = make()
    c.ingest([raw(host="lnx-rpt-01", sev="Warning", check="disk")])
    assert c.incidents()[0]["priority"] == 4


def test_sample_end_to_end():
    c = make()
    c.ingest(ALERTS)
    incs = c.incidents()
    assert len(c.rejected) == 1
    assert [i["business_service"] for i in incs] == ["Online Payments", "Branch Reporting", ""]
    assert incs[0]["u_alert_count"] == 2


def test_cli(capsys):
    assert main(["--alerts", str(DATA / "alerts.json"), "--topology", str(DATA / "topology.json")]) == 0
    assert json.loads(capsys.readouterr().out)[0]["priority"] == 1
    assert main(["--alerts", "missing.json", "--topology", str(DATA / "topology.json")]) == 1
