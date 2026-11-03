"""Topology and maintenance-window loading.

Two topology formats are accepted:
1. A topology file: {"ci_by_host": {...}, "parent_of": {...}, "service_tier": {...}, "maintenance": [...]}
2. A CMDB export with "cmdb_ci" and "cmdb_rel_ci" arrays (the same format snow-cmdb-data-quality reads),
   from which host bindings, the upward dependency map and service tiers are derived.
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path

from .engine import MaintenanceWindow, parse_time

SERVICE_CLASSES = {"cmdb_ci_service_auto", "cmdb_ci_service", "cmdb_ci_service_discovered"}


class TopologyError(ValueError):
    pass


@dataclass
class Topology:
    ci_by_host: dict[str, str] = field(default_factory=dict)
    parent_of: dict[str, str] = field(default_factory=dict)
    service_tier: dict[str, int] = field(default_factory=dict)
    maintenance: list[MaintenanceWindow] = field(default_factory=list)


def _tier(criticality: str) -> int:
    """'1 - most critical' -> 1 ... '4 - not critical' -> 4; default 3."""
    head = str(criticality or "").strip()[:1]
    return int(head) if head.isdigit() and 1 <= int(head) <= 4 else 3


def _windows(items: list[dict]) -> list[MaintenanceWindow]:
    try:
        return [MaintenanceWindow(w["ci"], parse_time(w["start"]), parse_time(w["end"])) for w in items]
    except (KeyError, ValueError, TypeError) as exc:
        raise TopologyError(f"invalid maintenance window: {exc!r}") from exc


def from_cmdb_export(data: dict) -> Topology:
    cis = {c["sys_id"]: c for c in data.get("cmdb_ci", [])}
    topo = Topology()
    for c in cis.values():
        if c.get("name"):
            topo.ci_by_host[c["name"].lower()] = c["sys_id"]
        if c.get("sys_class_name") in SERVICE_CLASSES:
            topo.service_tier[c["sys_id"]] = _tier(c.get("busines_criticality", ""))
    for r in data.get("cmdb_rel_ci", []):
        parent, child = r.get("parent"), r.get("child")
        # cmdb_rel_ci is parent -> child (service -> app -> server); we walk child -> parent.
        if parent in cis and child in cis and cis[parent].get("sys_class_name") != "cmdb_ci_business_app":
            topo.parent_of.setdefault(child, parent)
    return topo


def load_topology(path: str | Path, maintenance_path: str | Path | None = None) -> Topology:
    try:
        data = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise TopologyError(f"cannot read topology {path}: {exc}") from exc
    if "cmdb_ci" in data:
        topo = from_cmdb_export(data)
    elif "ci_by_host" in data:
        topo = Topology(dict(data["ci_by_host"]), dict(data.get("parent_of", {})),
                        {k: int(v) for k, v in data.get("service_tier", {}).items()},
                        _windows(data.get("maintenance", [])))
    else:
        raise TopologyError("topology must contain ci_by_host or cmdb_ci/cmdb_rel_ci")
    if maintenance_path:
        try:
            topo.maintenance += _windows(json.loads(Path(maintenance_path).read_text(encoding="utf-8")))
        except (OSError, json.JSONDecodeError) as exc:
            raise TopologyError(f"cannot read maintenance file: {exc}") from exc
    return topo
