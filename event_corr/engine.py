"""Alert-to-incident correlation, modelled on the ServiceNow Event Management pipeline:

    raw monitoring alert -> normalized event (em_event fields) -> de-duplicated alert (message_key)
    -> CI binding -> service-level correlation group -> one prioritized incident payload per service

The CMDB topology is supplied as a simple parent/child map so the logic can be tested offline.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime, timedelta

log = logging.getLogger(__name__)

# em_event severity: 1 Critical, 2 Major, 3 Minor, 4 Warning, 5 Info, 0 Clear
SEVERITY_MAP = {"critical": 1, "major": 2, "error": 2, "minor": 3, "warning": 4, "info": 5, "ok": 0, "clear": 0}


class EventError(ValueError):
    pass


@dataclass
class Event:
    source: str
    node: str
    type: str
    resource: str
    severity: int
    description: str
    time: datetime

    @property
    def message_key(self) -> str:
        return f"{self.source}|{self.node}|{self.type}|{self.resource}"


@dataclass
class Alert:
    message_key: str
    ci: str | None
    severity: int
    first_seen: datetime
    last_seen: datetime
    count: int = 1
    description: str = ""
    state: str = "Open"


@dataclass
class Group:
    service: str
    alerts: list[Alert] = field(default_factory=list)

    @property
    def severity(self) -> int:
        return min(a.severity for a in self.alerts)

    @property
    def root_cause(self) -> Alert:
        """Earliest highest-severity alert in the group is the probable cause."""
        return sorted(self.alerts, key=lambda a: (a.severity, a.first_seen))[0]


def normalize(raw: dict) -> Event:
    """Map a vendor alert (SolarWinds/Prometheus-style JSON) to em_event fields."""
    try:
        sev_txt = str(raw["severity"]).strip().lower()
        ts = datetime.fromisoformat(raw["timestamp"].replace("Z", "+00:00"))
        node = raw["host"].strip().lower()
    except (KeyError, AttributeError, ValueError) as exc:
        raise EventError(f"malformed alert: {exc!r}") from exc
    if sev_txt not in SEVERITY_MAP:
        raise EventError(f"unknown severity {raw['severity']!r}")
    return Event(source=raw.get("source", "unknown"), node=node, type=raw.get("check", "generic"),
                 resource=raw.get("resource", ""), severity=SEVERITY_MAP[sev_txt],
                 description=raw.get("message", ""), time=ts)


class Correlator:
    def __init__(self, ci_by_host: dict[str, str], parent_of: dict[str, str], service_tier: dict[str, int],
                 window: timedelta = timedelta(minutes=10)):
        self.ci_by_host = {k.lower(): v for k, v in ci_by_host.items()}
        self.parent_of = parent_of
        self.service_tier = service_tier
        self.window = window
        self.alerts: dict[str, Alert] = {}
        self.rejected: list[tuple[dict, str]] = []

    # --- stage 1: dedupe on message_key -------------------------------------------------
    def ingest(self, raws: list[dict]) -> None:
        for raw in sorted(raws, key=lambda r: r.get("timestamp", "")):
            try:
                ev = normalize(raw)
            except EventError as exc:
                log.warning("rejected event: %s", exc)
                self.rejected.append((raw, str(exc)))
                continue
            existing = self.alerts.get(ev.message_key)
            if ev.severity == 0:
                if existing:
                    existing.state = "Closed"
                    existing.last_seen = ev.time
                continue
            if existing and existing.state == "Open":
                existing.count += 1
                existing.last_seen = ev.time
                existing.severity = min(existing.severity, ev.severity)
            else:
                ci = self.ci_by_host.get(ev.node)
                if ci is None:
                    log.info("no CI bound for node %s", ev.node)
                self.alerts[ev.message_key] = Alert(ev.message_key, ci, ev.severity, ev.time, ev.time,
                                                    description=ev.description)

    # --- stage 2: walk the dependency map up to the business/application service -------------
    def service_for(self, ci: str | None) -> str | None:
        seen = set()
        while ci and ci not in self.service_tier:
            if ci in seen:  # defensive: cyclic relationship in CMDB
                log.warning("cycle detected at %s", ci)
                return None
            seen.add(ci)
            ci = self.parent_of.get(ci)
        return ci

    def correlate(self) -> list[Group]:
        groups: dict[str, list[Group]] = {}
        open_alerts = sorted((a for a in self.alerts.values() if a.state == "Open"), key=lambda a: a.first_seen)
        for alert in open_alerts:
            svc = self.service_for(alert.ci) or "UNMAPPED"
            bucket = groups.setdefault(svc, [])
            if bucket and alert.first_seen - bucket[-1].alerts[0].first_seen <= self.window:
                bucket[-1].alerts.append(alert)
            else:
                bucket.append(Group(svc, [alert]))
        return [g for gs in groups.values() for g in gs]

    # --- stage 3: incident payloads ---------------------------------------------------------
    def priority(self, group: Group) -> int:
        """Impact from service tier (1 = most critical) and urgency from alert severity -> P1..P4."""
        impact = self.service_tier.get(group.service, 3)
        urgency = 1 if group.severity <= 1 else 2 if group.severity == 2 else 3
        return min(4, max(1, impact + urgency - 1))

    def incidents(self) -> list[dict]:
        out = []
        for g in self.correlate():
            rc = g.root_cause
            out.append({
                "short_description": f"[{g.service}] {rc.description}"[:160],
                "cmdb_ci": rc.ci or "",
                "business_service": "" if g.service == "UNMAPPED" else g.service,
                "priority": self.priority(g),
                "assignment_group": "",  # left to assignment rules on the instance
                "work_notes": "Correlated alerts:\n" + "\n".join(
                    f"- {a.message_key} sev={a.severity} count={a.count}" for a in g.alerts),
                "u_alert_count": len(g.alerts),
            })
        return sorted(out, key=lambda i: i["priority"])
