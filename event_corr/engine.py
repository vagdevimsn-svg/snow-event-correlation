"""Alert-to-incident correlation, modelled on the ServiceNow Event Management pipeline:

    raw monitoring alert -> normalized event (em_event fields) -> de-duplicated alert (message_key)
    -> CI binding -> maintenance suppression / flap detection -> service-level correlation group
    -> one prioritized incident payload per group

The CMDB topology is supplied as plain maps (see topology.py) so the logic can be tested offline.
"""
from __future__ import annotations

import hashlib
import logging
from dataclasses import dataclass, field
from datetime import datetime, timedelta

log = logging.getLogger(__name__)

# em_event severity: 1 Critical, 2 Major, 3 Minor, 4 Warning, 5 Info, 0 Clear
SEVERITY_MAP = {"critical": 1, "major": 2, "error": 2, "minor": 3, "warning": 4, "info": 5, "ok": 0, "clear": 0}
UNMAPPED = "UNMAPPED"


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
    state: str = "Open"          # Open | Closed | Suppressed
    reopen_times: list[datetime] = field(default_factory=list)
    flapping: bool = False


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


@dataclass(frozen=True)
class MaintenanceWindow:
    ci: str
    start: datetime
    end: datetime

    def covers(self, ci: str, when: datetime) -> bool:
        return self.ci == ci and self.start <= when <= self.end


def parse_time(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


def normalize(raw: dict) -> Event:
    """Map a vendor alert (SolarWinds/Prometheus-style JSON) to em_event fields."""
    if not isinstance(raw, dict):
        raise EventError("event must be a JSON object")
    try:
        sev_txt = str(raw["severity"]).strip().lower()
        ts = parse_time(raw["timestamp"])
        node = raw["host"].strip().lower()
    except (KeyError, AttributeError, ValueError, TypeError) as exc:
        raise EventError(f"malformed alert: {exc!r}") from exc
    if sev_txt not in SEVERITY_MAP:
        raise EventError(f"unknown severity {raw['severity']!r}")
    if not node:
        raise EventError("empty host")
    return Event(source=str(raw.get("source", "unknown")), node=node, type=str(raw.get("check", "generic")),
                 resource=str(raw.get("resource", "")), severity=SEVERITY_MAP[sev_txt],
                 description=str(raw.get("message", ""))[:500], time=ts)


class Correlator:
    def __init__(self, ci_by_host: dict[str, str], parent_of: dict[str, str], service_tier: dict[str, int],
                 window: timedelta = timedelta(minutes=10), maintenance: list[MaintenanceWindow] | None = None,
                 flap_threshold: int = 3, flap_window: timedelta = timedelta(minutes=30)):
        self.ci_by_host = {k.lower(): v for k, v in ci_by_host.items()}
        self.parent_of = parent_of
        self.service_tier = service_tier
        self.window = window
        self.maintenance = maintenance or []
        self.flap_threshold, self.flap_window = flap_threshold, flap_window
        self.alerts: dict[str, Alert] = {}
        self.rejected: list[tuple[object, str]] = []
        self.raw_count = 0

    # --- stage 1: dedupe on message_key, suppression, flapping -----------------------------------
    def _in_maintenance(self, ci: str | None, when: datetime) -> bool:
        node, seen = ci, set()
        while node and node not in seen:  # a window on a parent (e.g. the app) covers its children
            if any(w.covers(node, when) for w in self.maintenance):
                return True
            seen.add(node)
            node = self.parent_of.get(node)
        return False

    def ingest(self, raws: list[dict]) -> None:
        self.raw_count += len(raws)
        for raw in sorted(raws, key=lambda r: str(r.get("timestamp", "")) if isinstance(r, dict) else ""):
            try:
                ev = normalize(raw)
            except EventError as exc:
                log.warning("rejected event: %s", exc)
                self.rejected.append((raw, str(exc)))
                continue
            existing = self.alerts.get(ev.message_key)
            if ev.severity == 0:
                if existing and existing.state == "Open":
                    existing.state = "Closed"
                    existing.last_seen = ev.time
                continue
            if existing and existing.state in ("Open", "Suppressed"):
                existing.count += 1
                existing.last_seen = ev.time
                existing.severity = min(existing.severity, ev.severity)
                continue
            ci = self.ci_by_host.get(ev.node)
            if ci is None and not existing:
                log.info("no CI bound for node %s", ev.node)
            if existing and existing.state == "Closed":   # re-opened after a clear -> flap candidate
                existing.reopen_times = [t for t in existing.reopen_times if ev.time - t <= self.flap_window]
                existing.reopen_times.append(ev.time)
                existing.flapping = existing.flapping or len(existing.reopen_times) >= self.flap_threshold
                existing.state, existing.last_seen, existing.count = "Open", ev.time, existing.count + 1
                existing.severity = ev.severity
                alert = existing
            else:
                alert = Alert(ev.message_key, ci, ev.severity, ev.time, ev.time, description=ev.description)
                self.alerts[ev.message_key] = alert
            if self._in_maintenance(ci, ev.time):
                alert.state = "Suppressed"
                log.info("suppressed %s: CI %s in maintenance", ev.message_key, ci)

    # --- stage 2: walk the dependency map up to the application service --------------------------
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
        actionable = sorted((a for a in self.alerts.values() if a.state == "Open" and not a.flapping),
                            key=lambda a: a.first_seen)
        for alert in actionable:
            svc = self.service_for(alert.ci) or UNMAPPED
            bucket = groups.setdefault(svc, [])
            if bucket and alert.first_seen - bucket[-1].alerts[0].first_seen <= self.window:
                bucket[-1].alerts.append(alert)
            else:
                bucket.append(Group(svc, [alert]))
        return [g for gs in groups.values() for g in gs]

    # --- stage 3: incident payloads --------------------------------------------------------------
    def priority(self, group: Group) -> int:
        """Impact from service tier (1 = most critical) and urgency from alert severity -> P1..P4."""
        impact = self.service_tier.get(group.service, 3)
        urgency = 1 if group.severity <= 1 else 2 if group.severity == 2 else 3
        return min(4, max(1, impact + urgency - 1))

    @staticmethod
    def correlation_id(group: Group) -> str:
        """Stable id so re-sending the same group never opens a duplicate incident."""
        rc = group.root_cause
        key = f"{group.service}|{rc.message_key}|{rc.first_seen.isoformat()}"
        return "evc-" + hashlib.sha256(key.encode()).hexdigest()[:20]

    def incidents(self) -> list[dict]:
        out = []
        for g in self.correlate():
            rc = g.root_cause
            out.append({
                "short_description": f"[{g.service}] {rc.description}"[:160],
                "cmdb_ci": rc.ci or "",
                "business_service": "" if g.service == UNMAPPED else g.service,
                "priority": self.priority(g),
                "assignment_group": "",  # left to assignment rules on the instance
                "correlation_id": self.correlation_id(g),
                "correlation_display": "event-corr",
                "work_notes": "Correlated alerts:\n" + "\n".join(
                    f"- {a.message_key} sev={a.severity} count={a.count}" for a in g.alerts),
                "u_alert_count": len(g.alerts),
            })
        return sorted(out, key=lambda i: i["priority"])

    def metrics(self) -> dict:
        states = [a.state for a in self.alerts.values()]
        incidents = len(self.correlate())
        valid = self.raw_count - len(self.rejected)
        return {
            "raw_events": self.raw_count,
            "rejected": len(self.rejected),
            "unique_alerts": len(self.alerts),
            "open": states.count("Open"),
            "closed": states.count("Closed"),
            "suppressed_maintenance": states.count("Suppressed"),
            "flapping": sum(a.flapping for a in self.alerts.values()),
            "incidents": incidents,
            "noise_reduction_pct": round(100 * (1 - incidents / valid), 1) if valid else 0.0,
        }
