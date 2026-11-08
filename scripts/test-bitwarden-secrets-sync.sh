#!/bin/sh
# compose/bitwarden-broker/secrets_sync.py against a fake broker socket that
# serves synthetic items, with a temp tree in place of the target folders.
# Checks every mapping row, unknown and duplicate names, mode 600, untouched
# mtimes for unchanged values, fail-closed responses, and that no value shows
# in any output. Needs host python3 only.
set -eu

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
tmp="$(mktemp -d)"
trap 'rm -rf "$tmp"' EXIT

python3 - "$ROOT" "$tmp" <<'PY'
import http.server
import json
import os
import secrets
import socketserver
import subprocess
import sys
import threading
from pathlib import Path
from urllib.parse import quote

ROOT, TMP = Path(sys.argv[1]), Path(sys.argv[2])
SCRIPT = ROOT / "compose/bitwarden-broker/secrets_sync.py"
SOCK = TMP / "s.sock"
TARGETS = TMP / "targets"
HOST_SECRETS = "/srv/example/twenty-secrets"
failures = []
outputs = []
VALUES = set()


def fail(msg):
    failures.append(msg)


def val(tag):
    v = f"syn-{tag}-{secrets.token_hex(8)}"
    VALUES.add(v)
    return v


class Handler(http.server.BaseHTTPRequestHandler):
    def do_GET(self):
        body = self.server.payload if self.path == "/secrets" else b'{"error": "not_found"}'
        self.send_response(200 if self.path == "/secrets" else 404)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args):
        pass


class Server(socketserver.ThreadingUnixStreamServer):
    daemon_threads = True


server = Server(str(SOCK), Handler)
server.payload = b"[]"
threading.Thread(target=server.serve_forever, daemon=True).start()


def serve(items):
    server.payload = items if isinstance(items, bytes) else json.dumps(items).encode()
    for item in items if isinstance(items, list) else []:
        if isinstance(item, dict) and isinstance(item.get("value"), str):
            VALUES.add(item["value"])


def run(extra_env=None, sock=SOCK):
    env = {"PATH": os.environ["PATH"], "BITWARDEN_SYNC_SOCKET": str(sock),
           "BITWARDEN_SYNC_TARGETS": str(TARGETS),
           "TWENTY_SECRETS_DIR": HOST_SECRETS,
           "TWENTY_ENCRYPTION_KEY_FILE": f"{HOST_SECRETS}/encryption_key",
           "TWENTY_DB_PASSWORD_FILE": f"{HOST_SECRETS}/db_password"}
    env.update(extra_env or {})
    for k in [k for k, v in env.items() if v is None]:
        del env[k]
    proc = subprocess.run([sys.executable, str(SCRIPT)], env=env, capture_output=True, text=True)
    outputs.append(proc.stdout + proc.stderr)
    return proc.returncode, proc.stdout, proc.stderr


def write(rel, text, mode=0o600):
    path = TARGETS / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(text.encode())
    os.chmod(path, mode)
    os.utime(path, (1_000_000_000, 1_000_000_000))
    return path


def read(rel):
    return (TARGETS / rel).read_bytes().decode()


def check_modes(case):
    for path in TARGETS.rglob("*"):
        if path.is_file() and path.name.startswith("."):
            fail(f"{case}: temp file left behind: {path.name}")
        elif path.is_file() and path.stat().st_mode & 0o777 != 0o600:
            fail(f"{case}: {path.relative_to(TARGETS)} mode is {oct(path.stat().st_mode & 0o777)}, want 0o600")


def seed():
    old = {k: val(f"old-{k}") for k in ("ro", "rw", "enc", "db", "pg", "work", "home")}
    write("twenty-workspaces/main.env",
          "# Written by make crm-add.\nTWENTY_URL=https://crm.example.com\n"
          f"TWENTY_ACCESS=write\nTWENTY_KEY_RW=\"{old['rw']}\"\nTWENTY_KEY_RO={old['ro']}\n")
    write("twenty-secrets/encryption_key", old["enc"])
    write("twenty-secrets/db_password", old["db"] + "\n")
    write("postgres-services/vikunja.env",
          "# Shared Postgres registration for vikunja.\n"
          f"DB_SERVICE_PASSWORD={old['pg']}\n"
          f"DATABASE_URL=postgresql://svc_vikunja:{old['pg']}@postgres-db:5432/assistant\n"
          f"VIKUNJA_DATABASE_PASSWORD={old['pg']}\n")
    write("caldav-accounts/work.env",
          "CALDAV_URL=https://caldav.example.com/\r\nCALDAV_USERNAME=sam@example.com\r\n"
          f"CALDAV_PASSWORD={old['work']}\r\n# trailing comment\r\n")
    write("caldav-accounts/home.env",
          "CALDAV_URL=https://caldav.example.com/\nCALDAV_USERNAME=kim@example.com")
    return old


# --- Every row ---------------------------------------------------------------
old = seed()
new = {k: val(f"new-{k}") for k in ("ro", "rw", "enc", "db", "pg", "work", "home", "nofile")}
new["pg"] = "p@ss/w:rd%" + secrets.token_hex(4)
VALUES.add(new["pg"])
items = [
    {"name": "twenty/main/key-ro", "value": new["ro"]},
    {"name": "twenty/main/key-rw", "value": new["rw"]},
    {"name": "twenty/encryption-key", "value": new["enc"]},
    {"name": "twenty/db-password", "value": new["db"]},
    {"name": "postgres/vikunja", "value": new["pg"]},
    {"name": "caldav/work", "value": new["work"]},
    {"name": "caldav/home", "value": new["home"]},
    {"name": "caldav/nofile", "value": new["nofile"]},
    {"name": "github/example-token", "value": val("unknown")},
    {"name": "llm-proxy/chat", "value": val("llm")},
]
serve(items)
code, out, err = run()
if code != 3:
    fail(f"all rows: exit {code}, want 3 (Hermes-side files changed); stderr={err.strip()!r}")
want_changed = {
    "changed compose/twenty/workspaces/main.env",
    f"changed {HOST_SECRETS}/encryption_key",
    f"changed {HOST_SECRETS}/db_password",
    "changed compose/postgres/services/vikunja.env",
    "changed compose/caldav-mcp/accounts/work.env",
    "changed compose/caldav-mcp/accounts/home.env",
}
got_changed = {line for line in out.splitlines() if line.startswith("changed ")}
if got_changed != want_changed:
    fail(f"all rows: changed lines {sorted(got_changed)}, want {sorted(want_changed)}")
if read("twenty-workspaces/main.env") != ("# Written by make crm-add.\nTWENTY_URL=https://crm.example.com\n"
                                          f"TWENTY_ACCESS=write\nTWENTY_KEY_RW={new['rw']}\n"
                                          f"TWENTY_KEY_RO={new['ro']}\n"):
    fail("twenty workspace: key lines not replaced in place")
if read("twenty-secrets/encryption_key") != new["enc"]:
    fail("twenty/encryption-key: want the value with no trailing newline")
if read("twenty-secrets/db_password") != new["db"] + "\n":
    fail("twenty/db-password: want the value with the existing trailing newline kept")
if read("postgres-services/vikunja.env") != (
        "# Shared Postgres registration for vikunja.\n"
        f"DB_SERVICE_PASSWORD={new['pg']}\n"
        f"DATABASE_URL=postgresql://svc_vikunja:{quote(new['pg'], safe='')}@postgres-db:5432/assistant\n"
        f"VIKUNJA_DATABASE_PASSWORD={new['pg']}\n"):
    fail("postgres/vikunja: DB_SERVICE_PASSWORD and the lines carrying the same password not updated")
if read("caldav-accounts/work.env") != ("CALDAV_URL=https://caldav.example.com/\r\nCALDAV_USERNAME=sam@example.com\r\n"
                                        f"CALDAV_PASSWORD={new['work']}\r\n# trailing comment\r\n"):
    fail("caldav/work: CALDAV_PASSWORD not replaced in place with CRLF kept")
if read("caldav-accounts/home.env") != ("CALDAV_URL=https://caldav.example.com/\nCALDAV_USERNAME=kim@example.com\n"
                                        f"CALDAV_PASSWORD={new['home']}\n"):
    fail("caldav/home: CALDAV_PASSWORD not appended")
if (TARGETS / "caldav-accounts/nofile.env").exists():
    fail("caldav/nofile: the sync created an account file")
if "compose/caldav-mcp/accounts/nofile.env" not in out:
    fail("caldav/nofile: no note for the missing file")
for name in ("github/example-token", "llm-proxy/chat"):
    if name not in out:
        fail(f"unknown item {name} not reported")
check_modes("all rows")

# --- Unchanged values leave files and mtimes alone ---------------------------
mtimes = {p: p.stat().st_mtime_ns for p in TARGETS.rglob("*") if p.is_file()}
for p in mtimes:
    os.utime(p, ns=(1_000_000_000_000_000_000, 1_000_000_000_000_000_000))
code, out, err = run()
if code != 0:
    fail(f"unchanged: exit {code}, want 0")
if "changed " in out:
    fail(f"unchanged: reported changes: {out.splitlines()}")
for p in mtimes:
    if p.stat().st_mtime_ns != 1_000_000_000_000_000_000:
        fail(f"unchanged: {p.relative_to(TARGETS)} was rewritten")

# --- Only a non-Hermes file changes: exit 0 ----------------------------------
serve([{"name": "postgres/vikunja", "value": val("pg2")}])
code, out, err = run()
if code != 0 or out.splitlines().count("changed compose/postgres/services/vikunja.env") != 1:
    fail(f"postgres only: exit {code}, out={out.splitlines()}, want 0 and one changed line")

# --- A name served twice is refused ------------------------------------------
before = read("caldav-accounts/work.env")
serve([{"name": "caldav/work", "value": val("dup-a")}, {"name": "caldav/work", "value": val("dup-b")}])
code, out, err = run()
if read("caldav-accounts/work.env") != before:
    fail("duplicate: the file was written")
if "caldav/work" not in out + err or "changed " in out:
    fail(f"duplicate: want caldav/work named as refused and no change, got {(out + err).splitlines()}")
if code != 0:
    fail(f"duplicate: exit {code}, want 0")

# --- A value with a line break cannot go in a KEY=value line -----------------
serve([{"name": "caldav/work", "value": val("line") + "\nINJECTED=1"}])
code, out, err = run()
if read("caldav-accounts/work.env") != before or "INJECTED" in read("caldav-accounts/work.env"):
    fail("line break: the file was written")
if "caldav/work" not in out + err:
    fail("line break: caldav/work not reported")

# --- Twenty file variables unset or outside TWENTY_SECRETS_DIR ---------------
enc_before = read("twenty-secrets/encryption_key")
serve([{"name": "twenty/encryption-key", "value": val("enc2")}])
code, out, err = run({"TWENTY_ENCRYPTION_KEY_FILE": None})
if code != 0 or read("twenty-secrets/encryption_key") != enc_before or "TWENTY_ENCRYPTION_KEY_FILE" not in out:
    fail(f"unset file variable: exit {code}, out={out.splitlines()}, want 0, a note and no write")
code, out, err = run({"TWENTY_ENCRYPTION_KEY_FILE": "/elsewhere/encryption_key"})
if code != 0 or read("twenty-secrets/encryption_key") != enc_before or "TWENTY_SECRETS_DIR" not in out:
    fail(f"file outside TWENTY_SECRETS_DIR: exit {code}, out={out.splitlines()}, want 0, a note and no write")

# --- Malformed responses fail closed -----------------------------------------
snapshot = {p: p.read_bytes() for p in TARGETS.rglob("*") if p.is_file()}
good = {"name": "caldav/work", "value": val("good")}
for label, payload in (("not a list", {"items": [good]}),
                       ("value not a string", [good, {"name": "caldav/home", "value": 5}]),
                       ("missing name", [good, {"value": val("noname")}]),
                       ("extra field", [good, {"name": "caldav/home", "value": val("x"), "extra": 1}]),
                       ("not JSON", b"[{\"name\": \"caldav/work\", \"value\": \"" + val("trunc").encode())):
    serve(payload)
    code, out, err = run()
    if code != 1:
        fail(f"malformed ({label}): exit {code}, want 1")
    if {p: p.read_bytes() for p in TARGETS.rglob("*") if p.is_file()} != snapshot:
        fail(f"malformed ({label}): files were written")

# --- Broker unreachable -------------------------------------------------------
code, out, err = run(sock=TMP / "absent.sock")
if code != 1 or "The Bitwarden broker is not reachable." not in err:
    fail(f"unreachable: exit {code}, stderr={err.strip()!r}, want 1 and the fixed text")

# --- No value anywhere in the output -----------------------------------------
everything = "\n".join(outputs)
for v in VALUES:
    for piece in {v, v.split("\n")[0]}:
        if piece and piece in everything:
            fail("output contains a synthetic value")
            break

server.shutdown()
if failures:
    for f in failures:
        print(f"FAIL {f}", file=sys.stderr)
    sys.exit(1)
print("ok")
PY
