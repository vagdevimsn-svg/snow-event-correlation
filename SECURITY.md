# Security

- The webhook binds to 127.0.0.1 by default. When exposing it, set `EVENT_API_TOKEN` so `POST /events` requires a bearer token (compared in constant time), and put it behind TLS.
- Request bodies are capped at 1 MB; malformed events are rejected and counted, never executed.
- `--send` is opt-in. It needs an integration user that can create incidents (e.g. `itil` or a custom role). Credentials come from environment variables only and are never logged.
- `https` is enforced for the instance URL (plain http is allowed only for localhost mocks).
- All data in this repository is synthetic.

Report a security issue by opening a private GitHub security advisory on this repository.
