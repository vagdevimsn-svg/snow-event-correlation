"""CLI, webhook service and ServiceNow forwarder (against a local mock incident table)."""
import base64
import json
import socket
import threading
import urllib.error
import urllib.parse
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

from event_corr.__main__ import main
from event_corr.forwarder import ForwardError, IncidentForwarder
from event_corr.server import build_server

DATA = Path(__file__).resolve().parents[1] / "sample_data"
ALERTS = json.loads((DATA / "alerts.json").read_text())


def free_port():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def call(method, url, body=None, headers=None):
    req = urllib.request.Request(url, method=method, data=json.dumps(body).encode() if body is not None else None,
                                 headers={"Content-Type": "application/json", **(headers or {})})
    try:
        with urllib.request.urlopen(req, timeout=5) as r:
            return r.status, json.loads(r.read())
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read() or b"{}")


# --- CLI ---------------------------------------------------------------------------------------
def test_cli_outputs(capsys):
    args = ["--alerts", str(DATA / "alerts.json"), "--topology", str(DATA / "topology.json")]
    assert main(args) == 0
    assert json.loads(capsys.readouterr().out)[0]["priority"] == 1
    assert main(args + ["--metrics"]) == 0
    assert json.loads(capsys.readouterr().out)["noise_reduction_pct"] == 80.0
    assert main(["--alerts", str(DATA / "alerts.json"), "--topology", str(DATA / "cmdb_export.json")]) == 0
    assert main(["--alerts", "missing.json", "--topology", str(DATA / "topology.json")]) == 1


def test_cli_rejects_non_array(tmp_path):
    p = tmp_path / "a.json"
    p.write_text('{"host": "x"}')
    assert main(["--alerts", str(p), "--topology", str(DATA / "topology.json")]) == 1


def test_cli_send_without_env_fails(monkeypatch):
    monkeypatch.delenv("SN_INSTANCE_URL", raising=False)
    assert main(["--alerts", str(DATA / "alerts.json"), "--topology", str(DATA / "topology.json"), "--send"]) == 1


# --- webhook service ---------------------------------------------------------------------------
@pytest.fixture
def service():
    port = free_port()
    server = build_server(str(DATA / "topology.json"), port=port, token="s3cret")
    threading.Thread(target=server.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{port}"
    server.shutdown()


def test_service_flow(service):
    auth = {"Authorization": "Bearer s3cret"}
    assert call("GET", f"{service}/health") == (200, {"status": "ok"})
    assert call("POST", f"{service}/events", ALERTS)[0] == 401
    status, body = call("POST", f"{service}/events", ALERTS, auth)
    assert status == 202 and body == {"accepted": 15, "rejected": 1}
    status, incidents = call("GET", f"{service}/incidents")
    assert status == 200 and len(incidents) == 3
    assert call("GET", f"{service}/metrics")[1]["incidents"] == 3
    assert call("POST", f"{service}/events", ALERTS[0], auth)[0] == 202   # single object accepted
    assert call("GET", f"{service}/nope")[0] == 404
    assert call("POST", f"{service}/other", {}, auth)[0] == 404


def test_service_rejects_bad_json(service):
    req = urllib.request.Request(f"{service}/events", method="POST", data=b"{bad",
                                 headers={"Authorization": "Bearer s3cret"})
    with pytest.raises(urllib.error.HTTPError) as err:
        urllib.request.urlopen(req, timeout=5)
    assert err.value.code == 400


# --- forwarder against a mock incident table -----------------------------------------------------
class MockIncidentTable:
    def __init__(self):
        self.rows, self.port = [], free_port()
        expected = "Basic " + base64.b64encode(b"demo:demo").decode()
        table = self

        class H(BaseHTTPRequestHandler):
            def _send(self, code, body):
                data = json.dumps(body).encode()
                self.send_response(code)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

            def do_GET(self):  # noqa: N802
                if self.headers.get("Authorization") != expected:
                    return self._send(401, {})
                cid = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)["sysparm_query"][0].split("=", 1)[1]
                return self._send(200, {"result": [r for r in table.rows if r["correlation_id"] == cid][:1]})

            def do_POST(self):  # noqa: N802
                body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
                body["number"] = f"INC{1000 + len(table.rows)}"
                table.rows.append(body)
                return self._send(201, {"result": body})

            def log_message(self, *a):
                pass

        self.server = ThreadingHTTPServer(("127.0.0.1", self.port), H)
        threading.Thread(target=self.server.serve_forever, daemon=True).start()


def test_forwarder_creates_then_skips_duplicates(monkeypatch, capsys):
    mock = MockIncidentTable()
    try:
        monkeypatch.setenv("SN_INSTANCE_URL", f"http://127.0.0.1:{mock.port}")
        monkeypatch.setenv("SN_USER", "demo")
        monkeypatch.setenv("SN_PASSWORD", "demo")
        args = ["--alerts", str(DATA / "alerts.json"), "--topology", str(DATA / "topology.json"), "--send"]
        assert main(args) == 0
        first = json.loads(capsys.readouterr().out)
        assert [r["status"] for r in first] == ["created"] * 3 and len(mock.rows) == 3
        assert "u_alert_count" not in mock.rows[0]  # only standard incident fields are sent
        assert main(args) == 0
        assert [r["status"] for r in json.loads(capsys.readouterr().out)] == ["exists"] * 3
        assert len(mock.rows) == 3
        monkeypatch.setenv("SN_PASSWORD", "wrong")
        assert main(args) == 1
    finally:
        mock.server.shutdown()


def test_forwarder_validation():
    with pytest.raises(ForwardError, match="https"):
        IncidentForwarder("http://example.com", "u", "p")
    with pytest.raises(ForwardError, match="SN_TOKEN"):
        IncidentForwarder("https://example.service-now.com")
    fwd = IncidentForwarder("https://example.service-now.com", token="t")
    assert fwd.auth == "Bearer t"
    with pytest.raises(ForwardError, match="cannot reach"):
        IncidentForwarder(f"http://127.0.0.1:{free_port()}", "u", "p").exists("x")
