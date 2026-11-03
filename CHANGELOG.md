# Changelog

## 1.1.0 — 2026-11-03
- Webhook service (`event-corr-serve`): `POST /events`, `GET /incidents`, `/metrics`, `/health`; optional bearer token; 1 MB body limit.
- Maintenance-window suppression, inherited from parent CIs (e.g. an app in a change window covers its servers).
- Flap detection: alerts that re-open N times within a window are held back instead of paging.
- ServiceNow forwarder (`--send`): creates incidents through the Table API, idempotent on `correlation_id`; dry-run by default.
- Topology can be loaded straight from a CMDB export (`cmdb_ci` + `cmdb_rel_ci`), the same format used by snow-cmdb-data-quality.
- Noise-reduction metrics (`--metrics`); stable correlation IDs.
- Packaging (`event-corr`, `event-corr-serve`), Dockerfile with healthcheck, ruff, coverage gate, Docker smoke test in CI.

## 1.0.0 — 2026-11-02
- First release: normalize, de-duplicate on message_key, CI binding, service correlation, priority matrix.
