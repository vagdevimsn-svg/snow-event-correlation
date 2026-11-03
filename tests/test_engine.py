import json
from datetime import timedelta
from pathlib import Path

import pytest

from event_corr.engine import Correlator, EventError, MaintenanceWindow, normalize, parse_time
from event_corr.topology import TopologyError, load_topology

DATA = Path(__file__).resolve().parents[1] / "sample_data"
TOPO = load_topology(DATA / "topology.json")
ALERTS = json.loads((DATA / "alerts.json").read_text())


def make(window=10, maintenance=None, **kw):
    return Correlator(TOPO.ci_by_host, TOPO.parent_of, TOPO.service_tier, window=timedelta(minutes=window),
                      maintenance=TOPO.maintenance if maintenance is None else maintenance, **kw)


def raw(host="lnx-pay-01", sev="Major", ts="2026-10-30T14:00:00Z", check="http_5xx"):
    return {"source": "test", "host": host, "check": check, "resource": "r", "severity": sev,
            "message": f"{check} on {host}", "timestamp": ts}


def test_normalize_maps_severity_and_lowercases_host():
    ev = normalize(raw(host="LNX-PAY-01", sev="Critical"))
    assert ev.severity == 1 and ev.node == "lnx-pay-01"


@pytest.mark.parametrize("bad", [{"host": "x"}, raw(sev="nope"), {**raw(), "timestamp": "yesterday"},
                                 {**raw(), "host": "  "}, "not an object", {**raw(), "host": 5}])
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
    assert Correlator({}, {"a": "b", "b": "a"}, {"svc": 1}).service_for("a") is None


def test_alerts_on_same_service_within_window_become_one_incident():
    c = make()
    c.ingest([raw(host="paydb01", sev="Critical", check="db"), raw(host="lnx-pay-02", ts="2026-10-30T14:03:00Z")])
    (inc,) = c.incidents()
    assert inc["cmdb_ci"] == "db_paydb01" and inc["priority"] == 1  # highest severity, earliest = probable cause


def test_alerts_outside_window_split():
    c = make(window=5)
    c.ingest([raw(host="paydb01", check="db"), raw(host="lnx-pay-02", ts="2026-10-30T14:30:00Z")])
    assert len(c.incidents()) == 2


def test_priority_matrix():
    c = make()
    c.ingest([raw(host="lnx-rpt-01", sev="Warning", check="disk")])
    assert c.incidents()[0]["priority"] == 4


def test_maintenance_window_suppresses_child_and_expires():
    window = [MaintenanceWindow("app_payments_api", parse_time("2026-10-30T14:00:00Z"),
                                parse_time("2026-10-30T14:30:00Z"))]
    c = make(maintenance=window)
    c.ingest([raw(host="lnx-pay-01", ts="2026-10-30T14:10:00Z"),             # covered via parent app
              raw(host="paydb01", check="db", ts="2026-10-30T15:00:00Z")])   # after the window
    states = {a.message_key.split("|")[1]: a.state for a in c.alerts.values()}
    assert states == {"lnx-pay-01": "Suppressed", "paydb01": "Open"}
    assert len(c.incidents()) == 1


def test_flapping_alert_is_held_back():
    c = make(flap_threshold=3)
    seq = []
    for i, sev in enumerate(["Minor", "Clear"] * 3 + ["Minor"]):
        seq.append(raw(host="lnx-rpt-01", sev=sev, check="restart", ts=f"2026-10-30T14:{i * 2:02d}:00Z"))
    c.ingest(seq)
    (alert,) = c.alerts.values()
    assert alert.flapping and c.incidents() == [] and c.metrics()["flapping"] == 1


def test_two_reopens_is_not_flapping():
    c = make(flap_threshold=3)
    c.ingest([raw(sev=s, ts=f"2026-10-30T14:0{i}:00Z") for i, s in enumerate(["Major", "Clear", "Major"])])
    assert not next(iter(c.alerts.values())).flapping and len(c.incidents()) == 1


def test_correlation_id_is_stable():
    a, b = make(), make()
    a.ingest(ALERTS)
    b.ingest(list(reversed(ALERTS)))
    assert [i["correlation_id"] for i in a.incidents()] == [i["correlation_id"] for i in b.incidents()]


def test_sample_end_to_end_and_metrics():
    c = make()
    c.ingest(ALERTS)
    incs = c.incidents()
    assert [i["business_service"] for i in incs] == ["Online Payments", "Branch Reporting", ""]
    assert incs[0]["u_alert_count"] == 2
    m = c.metrics()
    assert m == {"raw_events": 16, "rejected": 1, "unique_alerts": 7, "open": 5, "closed": 1,
                 "suppressed_maintenance": 1, "flapping": 1, "incidents": 3, "noise_reduction_pct": 80.0}


def test_topology_from_cmdb_export():
    topo = load_topology(DATA / "cmdb_export.json")
    assert topo.ci_by_host["paydb01"] == "db001"
    assert topo.parent_of["db001"] == "app001" and topo.parent_of["app001"] == "svc001"
    assert topo.service_tier["svc001"] == 1 and topo.service_tier["svc002"] == 3
    assert "svc001" not in topo.parent_of  # business app is not walked into


def test_topology_errors(tmp_path):
    with pytest.raises(TopologyError):
        load_topology(tmp_path / "missing.json")
    bad = tmp_path / "bad.json"
    bad.write_text('{"something": 1}')
    with pytest.raises(TopologyError):
        load_topology(bad)
    bad.write_text('{"ci_by_host": {}, "maintenance": [{"ci": "x"}]}')
    with pytest.raises(TopologyError):
        load_topology(bad)
