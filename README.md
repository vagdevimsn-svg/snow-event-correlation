# snow-event-correlation

Turns a noisy stream of monitoring alerts into a few prioritized, service-aware incident payloads, following the same stages as ServiceNow Event Management: normalize → de-duplicate → bind to CI → correlate by service → create incident.

> **Portfolio project.** Independently written with synthetic alerts and a synthetic topology. It is not code from any employer or client and does not reproduce ServiceNow product code.

## Business problem

During an outage, monitoring tools fire dozens of alerts for one root cause: the database slows down, every app server behind it starts returning 5xx, and the service desk gets a ticket per alert. Engineers lose time triaging duplicates while the real cause waits. Correlating alerts against CMDB relationships collapses that noise into one incident on the right service, with the probable cause CI attached and priority driven by business impact.

## How it works

```
 SolarWinds / Prometheus JSON
            |
            v
 1. normalize()      vendor fields -> em_event fields (source, node, type, resource, severity 0-5)
            |
 2. ingest()         message_key = source|node|type|resource
            |        repeats increment count; Clear (sev 0) closes the alert
            v
 3. CI binding       host -> CI via ci_by_host (stands in for CMDB identification)
            |
 4. correlate()      walk parent_of up to an application service (cycle-safe);
            |        alerts on the same service inside the time window form one group
            v
 5. incidents()      root cause = earliest highest-severity alert
                     priority = impact (service tier) x urgency (severity) -> P1..P4
                     payload fields match the incident table (short_description, cmdb_ci,
                     business_service, priority, work_notes)
```

## Run it

```bash
git clone https://github.com/vagdevimsn-svg/snow-event-correlation.git
cd snow-event-correlation
python -m venv .venv && source .venv/bin/activate
pip install -r requirements-dev.txt

python -m event_corr --alerts sample_data/alerts.json --topology sample_data/topology.json
```

With the sample data: **8 raw alerts → 5 unique alerts → 3 incidents**, 1 malformed event rejected and logged, and the payments outage raised as a single P1 against the database CI. Output: [docs/sample_output.json](docs/sample_output.json)

## Tests

```bash
python -m pytest -q
```

Covers severity mapping, malformed input, de-duplication, Clear handling, dependency walking with cycle protection, time-window grouping, the priority matrix and the CLI.

## Mapping to the platform

| This repo | ServiceNow |
|---|---|
| `normalize()` | Event field mapping / event rules |
| `message_key` de-dup | Alert de-duplication on `message_key` |
| `ci_by_host` | CI binding through CMDB identification |
| `parent_of` walk | Service Mapping / CMDB relationships |
| time-window groups | Alert correlation rules / AIOps grouping |
| incident payload | Alert Management rule → incident (Table API `POST /api/now/table/incident`) |

## Security notes

No credentials are used; the tool only reads local files. If extended to post incidents, use an integration user with the minimum roles, read credentials from environment variables, and send only the fields shown above.

## Limitations and next steps

- Correlation is rule-based (topology + time window), not ML-based.
- Topology is a static file; next step is loading it from `cmdb_rel_ci` through the Table API.
- No flapping detection or maintenance-window suppression yet.

## License

MIT
