# Oracle: known differences

`scripts/test-client-gateway-oracle.py` runs one ACP scenario against `hermes acp` and against
the gateway, and fails on any observation that differs unless its key is listed here. Each entry
says which side is right and links the upstream issue. Remove an entry when the test reports that
it no longer differs.

- `initialize.session_close` — `hermes acp` doesn't advertise `sessionCapabilities.close` at
  v0.23.1, though it answers a `session/close` call without an error; the gateway advertises it.
