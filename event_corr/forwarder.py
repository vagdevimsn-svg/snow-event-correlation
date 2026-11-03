"""Send incident payloads to ServiceNow through the Table API (stdlib only).

Dry-run by default. With send=True, each payload is checked against existing incidents by
correlation_id first, so replaying the same events never opens duplicates.
"""
from __future__ import annotations

import base64
import json
import logging
import os
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Callable

log = logging.getLogger(__name__)
LOCAL_HOSTS = {"localhost", "127.0.0.1", "::1"}
FIELDS = ("short_description", "cmdb_ci", "business_service", "priority", "assignment_group",
          "correlation_id", "correlation_display", "work_notes")


class ForwardError(RuntimeError):
    pass


class IncidentForwarder:
    def __init__(self, instance_url: str, user: str = "", password: str = "", token: str = "",
                 opener: Callable | None = None, timeout: int = 20):
        parsed = urllib.parse.urlparse(instance_url)
        if parsed.scheme != "https" and not (parsed.scheme == "http" and parsed.hostname in LOCAL_HOSTS):
            raise ForwardError("instance URL must use https (http only for a local mock)")
        if not token and not (user and password):
            raise ForwardError("provide SN_TOKEN or SN_USER and SN_PASSWORD")
        self.base = instance_url.rstrip("/")
        self.auth = f"Bearer {token}" if token else "Basic " + base64.b64encode(f"{user}:{password}".encode()).decode()
        self.open = opener or urllib.request.urlopen
        self.timeout = timeout

    @classmethod
    def from_env(cls, **kw) -> IncidentForwarder:
        url = os.environ.get("SN_INSTANCE_URL", "")
        if not url:
            raise ForwardError("missing environment variable: SN_INSTANCE_URL")
        return cls(url, os.environ.get("SN_USER", ""), os.environ.get("SN_PASSWORD", ""),
                   os.environ.get("SN_TOKEN", ""), **kw)

    def _request(self, method: str, path: str, body: dict | None = None) -> dict:
        req = urllib.request.Request(f"{self.base}{path}", method=method,
                                     data=json.dumps(body).encode() if body is not None else None,
                                     headers={"Accept": "application/json", "Content-Type": "application/json",
                                              "Authorization": self.auth})
        try:
            with self.open(req, timeout=self.timeout) as resp:
                return json.loads(resp.read().decode() or "{}")
        except urllib.error.HTTPError as exc:
            raise ForwardError(f"{method} {path}: HTTP {exc.code}") from exc
        except urllib.error.URLError as exc:
            raise ForwardError(f"cannot reach {self.base}: {exc.reason}") from exc

    def exists(self, correlation_id: str) -> str | None:
        q = urllib.parse.urlencode({"sysparm_query": f"correlation_id={correlation_id}",
                                    "sysparm_fields": "sys_id,number", "sysparm_limit": 1})
        rows = self._request("GET", f"/api/now/table/incident?{q}").get("result", [])
        return rows[0].get("number") or rows[0].get("sys_id") if rows else None

    def send(self, incidents: list[dict]) -> list[dict]:
        results = []
        for inc in incidents:
            existing = self.exists(inc["correlation_id"])
            if existing:
                results.append({"correlation_id": inc["correlation_id"], "status": "exists", "number": existing})
                continue
            created = self._request("POST", "/api/now/table/incident",
                                    {k: inc[k] for k in FIELDS if inc.get(k) not in (None, "")})
            number = created.get("result", {}).get("number", "")
            log.info("created incident %s for %s", number, inc["correlation_id"])
            results.append({"correlation_id": inc["correlation_id"], "status": "created", "number": number})
        return results
