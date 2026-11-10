#!/usr/bin/env python3
"""The email add-on end to end, through scripts/addon.sh, against GreenMail.

Synthetic mail only: GreenMail and the users, passwords and messages from
scripts/test-mail-imap.py exist only for this run. Everything runs in a
throwaway compose project, email-e2e-<random>, from a copy of this checkout in
a temp directory: its own .env (copied from .env.example), ASSISTANT_DATA_ROOT
in the temp directory, IMAGE_PREFIX and CONTAINER_PREFIX set to the project
name so no image tag or container name of the main stack, or of another run of
this test, is reused, and an override file that turns hermes-api-bootstrap and
hermes-browser-bootstrap into no-ops and adds GreenMail as service `greenmail`
on the project's email-egress network. The name is new for each run because
runs from two checkouts at once would otherwise share containers, networks,
volumes and image tags, and stop or recreate each other's services. At the end
`docker compose down -v` for the project always runs and the run's image tags
are removed. Skipped when Docker isn't running.

    python3 scripts/test-email-e2e.py      (make email-test)
"""

from __future__ import annotations

import email
import email.policy
import importlib.util
import json
import os
import secrets
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
REPO = HERE.parent
PROJECT = f"email-e2e-{secrets.token_hex(4)}"  # one per run; see the docstring
sys.path.insert(0, str(REPO / "compose" / "mail-drafts-mcp"))
sys.path.insert(0, str(REPO / "compose" / "mail-mcp"))

_spec = importlib.util.spec_from_file_location("test_mail_imap", HERE / "test-mail-imap.py")
tmi = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(tmi)

import drafts  # noqa: E402  compose/mail-drafts-mcp
import mail_imap_conn  # noqa: E402  scripts/lib, on the path through tmi
import mail_query as mq  # noqa: E402  compose/mail-mcp

PASSWORD = tmi.PASSWORD
WRONG_PASSWORD = "synthetic-wrong-pw"  # given to one account to check a refused login
# Instance slug -> GreenMail user in tmi.USERS, and the mailbox patterns
# test-mail-imap.py indexes for that user.
INSTANCES = {"home": "personal", "work": "work"}
SCOPE = {"home": ["*"], "work": ["**"]}
EMAIL_SERVICES = ("email-indexer", "email-mcp", "email-drafts-mcp", "email-outbox")
COLUMNS = ("mailbox", "subject", "from_addr", "from_name", "to_addrs", "cc_addrs", "is_read",
           "is_flagged", "date_received", "date_sent", "in_reply_to", "body")

OVERRIDE = """\
# Written by scripts/test-email-e2e.py for project email-e2e only.
services:
  hermes-api-bootstrap:
    image: busybox:1.36
    entrypoint: ["true"]
    command: !reset []
    volumes: !reset []
    environment: !reset []
    env_file: !reset []
    depends_on: !reset {}
  hermes-browser-bootstrap:
    image: busybox:1.36
    entrypoint: ["true"]
    command: !reset []
    volumes: !reset []
    environment: !reset []
    env_file: !reset []
    depends_on: !reset {}
  greenmail:
    image: %(image)s
    environment:
      GREENMAIL_OPTS: "%(opts)s"
    ports: ["127.0.0.1::3143"]
    networks: [email-egress]
"""

OUTPUT: list[str] = []  # everything addon.sh printed, checked for the password at the end


def fail(msg: str) -> None:
    print(f"FAIL {msg}", file=sys.stderr, flush=True)
    raise SystemExit(1)


def expect(cond: bool, msg: str) -> None:
    if not cond:
        fail(msg)
    print(f"ok {msg}", flush=True)


class Stack:
    """The copied checkout, its data root and the compose environment."""

    def __init__(self, tmp: Path) -> None:
        self.tmp = tmp
        self.root = tmp / "stack"
        self.data = tmp / "data"
        self.override = tmp / "compose.e2e.yml"
        env = {k: v for k, v in os.environ.items()
               if not k.startswith("COMPOSE_") and k not in ("ASSISTANT_DATA_ROOT", "IMAGE_PREFIX",
                                                             "CONTAINER_PREFIX", "STACK_PYTHON")}
        env.update({
            "COMPOSE_PROJECT_NAME": PROJECT,
            "ASSISTANT_DATA_ROOT": str(self.data),
            "COMPOSE_FILE": f"{self.root / 'docker-compose.yml'}:{self.override}",
            "IMAGE_PREFIX": PROJECT,
            "CONTAINER_PREFIX": f"{PROJECT}-",
        })
        self.env = env

    def build(self) -> None:
        self.root.mkdir(parents=True)
        self.data.mkdir()
        files = subprocess.run(["git", "ls-files", "-co", "--exclude-standard", "-z"], cwd=REPO,
                               check=True, capture_output=True).stdout
        for rel in filter(None, files.decode().split("\0")):
            src = REPO / rel
            if not src.is_file():  # deleted in the working tree
                continue
            dst = self.root / rel
            dst.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(src, dst, follow_symlinks=False)
        # An exported ASSISTANT_DATA_ROOT wins over .env in addon.sh, compose and
        # crm.sh, so the run's environment sets it; the copy's .env names the
        # same temp data root as well, for tools that read only the file.
        ours = {"COMPOSE_PROJECT_NAME": PROJECT, "ASSISTANT_DATA_ROOT": str(self.data),
                "IMAGE_PREFIX": PROJECT, "CONTAINER_PREFIX": f"{PROJECT}-"}
        lines = [line for line in (REPO / ".env.example").read_text().splitlines()
                 if line.partition("=")[0] not in ours]
        lines += [f"{k}={v}" for k, v in ours.items()]
        (self.root / ".env").write_text("\n".join(lines) + "\n")
        for name in ("browser.env", "api-server.env"):
            shutil.copy(self.root / "compose" / "hermes" / f"{name}.example", self.root / "compose" / "hermes" / name)
        self.override.write_text(OVERRIDE % {"image": tmi.IMAGE, "opts": tmi.greenmail_opts()})

    def compose(self, *args: str, check: bool = True, timeout: float | None = 600, stdin: str | None = None,
                ) -> subprocess.CompletedProcess:
        out = subprocess.run(["docker", "compose", "-p", PROJECT, "--profile", "*", *args], cwd=self.root, env=self.env,
                             capture_output=True, text=True, timeout=timeout, input=stdin)
        if check and out.returncode != 0:
            fail(f"docker compose {' '.join(args)} exited {out.returncode}: {out.stderr.strip()[-2000:]}")
        return out

    def addon(self, *args: str, values: dict | None = None, timeout: float = 900) -> subprocess.CompletedProcess:
        out = subprocess.run(["bash", str(self.root / "scripts" / "addon.sh"), *args], cwd=self.root, env=self.env,
                             capture_output=True, text=True, timeout=timeout,
                             input=json.dumps(values) if values is not None else "")
        OUTPUT.append(out.stdout + out.stderr)
        return out

    def addon_ok(self, *args: str, values: dict | None = None) -> subprocess.CompletedProcess:
        out = self.addon(*args, values=values)
        if out.returncode != 0:
            fail(f"addon.sh {' '.join(args)} exited {out.returncode}: {(out.stdout + out.stderr).strip()[-3000:]}")
        return out

    def env_file(self) -> dict[str, str]:
        out = {}
        for line in (self.root / ".env").read_text().splitlines():
            if "=" in line and not line.startswith("#"):
                key, _, val = line.partition("=")
                out.setdefault(key, val)
        return out

    def states(self) -> dict[str, str]:
        """Service -> container state for every container of the project."""
        text = self.compose("ps", "-a", "--format", "json").stdout.strip()
        if not text:
            return {}
        items = json.loads(text) if text.startswith("[") else [json.loads(x) for x in text.splitlines() if x]
        return {i["Service"]: i["State"] for i in items}

    def exec_py(self, service: str, code: str, *args: str) -> str:
        return self.compose("exec", "-T", service, "python", "-c", code, *args).stdout.strip()

    def project_objects(self) -> list[str]:
        """Containers, volumes and networks labelled with the project."""
        found = []
        label = f"label=com.docker.compose.project={PROJECT}"
        for kind, cmd in (("container", ["docker", "ps", "-aq", "--filter", label]),
                          ("volume", ["docker", "volume", "ls", "-q", "--filter", label]),
                          ("network", ["docker", "network", "ls", "-q", "--filter", label])):
            out = subprocess.run(cmd, capture_output=True, text=True).stdout.split()
            found += [f"{kind} {x}" for x in out]
        return found

    def down(self) -> None:
        if not self.root.is_dir():  # nothing was copied, so nothing was started
            return
        self.compose("down", "-v", "--remove-orphans", "--timeout", "5", check=False, timeout=300)
        # Only this run's tags; the layers stay in the build cache for the next run.
        tags = subprocess.run(["docker", "image", "ls", "--format", "{{.Repository}}:{{.Tag}}",
                               "--filter", f"reference={PROJECT}-*"], capture_output=True, text=True).stdout.split()
        if tags:
            subprocess.run(["docker", "image", "rm", *tags], capture_output=True)


# --------------------------------------------------------------------------- helpers

def guard(stack: Stack) -> None:
    """Ruling V: the project and data root are the throwaway ones before anything starts."""
    cfg = json.loads(stack.compose("config", "--format", "json").stdout)
    if cfg.get("name") != PROJECT:
        fail(f"compose project is {cfg.get('name')!r}, not {PROJECT}; aborting before any up")
    data = str(stack.data)
    if not data.startswith(str(stack.tmp) + os.sep) or stack.env_file().get("ASSISTANT_DATA_ROOT") != data:
        fail("the data root is outside the temp directory or not the copy's .env value; aborting")
    for name in ("email-indexer", "email-outbox"):
        for vol in cfg["services"][name].get("volumes", []):
            if vol.get("type") == "bind" and not vol["source"].startswith(data + os.sep):
                fail(f"{name} binds {vol['source']}, outside the temp data root; aborting")
    for name in EMAIL_SERVICES:
        svc = cfg["services"][name]
        if not svc.get("image", "").startswith(f"{PROJECT}-") \
                or svc.get("container_name") != f"{PROJECT}-{name}":
            fail(f"{name} image {svc.get('image')!r} or container_name {svc.get('container_name')!r}"
                 f" lacks the {PROJECT} prefix; aborting")
    for name in ("hermes-api-bootstrap", "hermes-browser-bootstrap"):
        svc = cfg["services"][name]
        if svc.get("image") != "busybox:1.36" or svc.get("entrypoint") != ["true"] \
                or svc.get("volumes") or svc.get("depends_on"):
            fail(f"{name} is not the no-op from the override; aborting")
    # vault.sh reload-plugins, which addon.sh runs after the bootstraps, acts
    # only on marker files under the copy's data/hermes and a running hermes.
    markers = list((stack.root / "data" / "hermes").glob("**/.addon-plugins-changed"))
    if markers or "hermes" in stack.states():
        fail("the copy has plugin markers or a hermes container; reload-plugins would not be a no-op")
    print(f"ok project {PROJECT}, data root and bind sources under {stack.tmp}, no-op bootstraps", flush=True)


def wait_for(what: str, fn, timeout: float, every: float = 2.0, why=None):
    deadline = time.time() + timeout
    while True:
        got = fn()
        if got:
            return got
        if time.time() > deadline:
            fail(f"timed out after {int(timeout)} s waiting for {what}" + (f"\n{why()}" if why else ""))
        time.sleep(every)


def greenmail_port(stack: Stack) -> int:
    out = stack.compose("port", "greenmail", "3143").stdout.strip().splitlines()[0]
    return int(out.rsplit(":", 1)[1])


def drafts_on_server(port: int, account: str) -> dict[str, tuple[set, email.message.EmailMessage]]:
    conn = tmi.admin(port, account)
    typ, _ = conn.select("Drafts", readonly=True)
    saved = {}
    if typ == "OK":
        _, data = conn.uid("FETCH", "1:*", "(UID FLAGS BODY.PEEK[])")
        for meta, raw in mail_imap_conn.fetch_items(data or []):
            if raw:
                msg = email.message_from_bytes(raw, policy=email.policy.default)
                saved[str(msg["Subject"])] = (mail_imap_conn.flags_of(meta), msg)
    conn.logout()
    return saved


def pass_lines(stack: Stack) -> list[dict]:
    """The indexer's per-pass JSON lines (mail-index.py run --every prints one per pass)."""
    out = []
    for line in stack.compose("logs", "--no-color", "--no-log-prefix", "email-indexer").stdout.splitlines():
        line = line.strip()
        if line.startswith("{"):
            try:
                out.append(json.loads(line))
            except ValueError:
                pass
    return out


def wait_for_pass(stack: Stack, after: int) -> dict:
    def ready():
        lines = pass_lines(stack)
        return lines[after:] if len(lines) > after else None
    new = wait_for("an index pass in docker compose logs email-indexer", ready, 180)
    if "error" in new[0]:
        logs = stack.compose("logs", "--no-color", "email-indexer").stdout[-2000:]
        fail(f"index pass failed: {new[0]}\n{logs}")
    return new[0]


def outbox_state(stack: Stack) -> str:
    """The outbox side's logs (status lines only) and the queue and status listings."""
    logs = stack.compose("logs", "--no-color", "--tail", "30", "email-outbox", "email-drafts-mcp",
                         check=False).stdout
    ls = stack.compose("exec", "-T", "email-drafts-mcp", "sh", "-c",
                       "ls -la /data/mail-drafts /data/mail-drafts/queue /data/mail-drafts/status;"
                       " cat /data/mail-drafts/status/*.json", check=False)
    return f"{logs}\n{ls.stdout}{ls.stderr}"


def queue_file(stack: Stack, local_root: Path, request_id: str) -> None:
    """docker compose cp keeps the host uid and drafts.py's 0600 mode; the
    outbox runs as HERMES_UID, so the copy is made world-readable first."""
    src = local_root / "queue" / f"{request_id}.json"
    src.chmod(0o644)
    stack.compose("cp", str(src),
                  f"email-drafts-mcp:/data/mail-drafts/queue/{request_id}.json")


# --------------------------------------------------------------------------- the run

def run(stack: Stack) -> None:
    guard(stack)
    print(f"building the email-imap, mail-mcp and mail-drafts-mcp images (tagged {PROJECT}-*)", flush=True)
    stack.compose("build", "email-indexer", "email-mcp", "email-drafts-mcp", timeout=1800)

    stack.compose("up", "-d", "greenmail")
    port = greenmail_port(stack)
    wait_for("GreenMail", lambda: tmi.greenmail_ready(port), 90, every=1)
    tmi.seed(port)
    for account in INSTANCES.values():
        conn = tmi.admin(port, account)
        conn.create("Drafts")
        conn.logout()
    before = tmi.server_state(port)
    print(f"ok GreenMail seeded with {len(before)} synthetic messages for two users", flush=True)

    # Fresh data root: enable refuses before any configure, and Docker creates no bind source.
    out = stack.addon("enable", "email")
    err = out.stdout + out.stderr
    expect(out.returncode == 3 and "since" in err and "instances:accounts" in err,
           "enable on a fresh data root exits 3 naming since and instances:accounts")
    expect(not (stack.data / "addons" / "email" / "secrets").exists(),
           "the refused enable created no bind source under data/addons/email")

    # docs/email.md's calling order: work is configured without mailboxes, listed, then scoped.
    for slug, user in INSTANCES.items():
        values = {"host": "greenmail", "port": 3143, "security": "plain", "user": tmi.USERS[user],
                  "address": tmi.USERS[user], "password": PASSWORD}
        if slug != "work":
            values["mailboxes"] = SCOPE[slug]
        stack.addon_ok("configure", "email", "--instance", slug, values=values)
    secrets = sorted(p.name for p in (stack.data / "addons" / "email" / "secrets").iterdir())
    expect(secrets == ["home.password", "work.password"], "configure --instance stores one password file per instance")
    stored = json.loads((stack.data / "addons" / "email" / "instances" / "work.json").read_text())
    expect("mailboxes" not in stored, "work is stored without mailboxes")

    out = stack.addon_ok("run", "email", "mailboxes", "--instance", "work")
    listing = json.loads(out.stdout)
    inbox = [r for r in listing if r["mailbox"] == "INBOX"]
    expect(len(inbox) == 1 and isinstance(inbox[0]["messages"], int) and inbox[0]["messages"] > 0
           and {r["account"] for r in listing} == {"work"},
           "run email mailboxes --instance work, before work has mailboxes, lists INBOX with a count"
           f" ({inbox[0]['messages'] if inbox else '-'})")
    stack.addon_ok("configure", "email", "--instance", "work", values={"mailboxes": SCOPE["work"]})
    stored = json.loads((stack.data / "addons" / "email" / "instances" / "work.json").read_text())
    expect(stored.get("mailboxes") == SCOPE["work"] and stored.get("host") == "greenmail",
           "configure with only mailboxes sets them and keeps work's other values")

    stack.addon_ok("configure", "email", values={"since": "2026-01-01", "bodies": True, "attachments": True,
                                                  "interval": 300, "drafts": True})
    stack.addon_ok("enable", "email")
    flags = stack.env_file()
    expect(flags.get("EMAIL_ENABLED") == "1" and flags.get("EMAIL_DRAFTS_ENABLED") == "1"
           and {"email", "email-drafts"} <= set(flags.get("COMPOSE_PROFILES", "").split(",")),
           "enable sets EMAIL_ENABLED, EMAIL_DRAFTS_ENABLED and both profiles in the copy's .env")
    states = stack.states()
    expect(all(states.get(s) == "running" for s in EMAIL_SERVICES), f"all four services running: {states}")
    first = wait_for_pass(stack, 0)
    expect(first.get("added", 0) > 0, f"the indexer's first pass added messages ({first.get('added')})")

    # Volume seeding: each named volume took the image's 1777 mode.
    mode = stack.compose("exec", "-T", "email-indexer", "stat", "-c", "%a", "/data/index").stdout.strip()
    expect(mode == "1777", f"/data/index on email-indexer has mode 1777 ({mode})")
    mode = stack.compose("exec", "-T", "email-outbox", "stat", "-c", "%a", "/data/drafts").stdout.strip()
    expect(mode == "1777", f"/data/drafts on email-outbox has mode 1777 ({mode})")

    # The add-on's index matches the host IMAP source's for the same accounts.
    copied = stack.tmp / "addon-index"
    stack.compose("cp", "email-indexer:/data/index", str(copied))
    got = tmi.rows(copied / "index.sqlite")
    os.environ[tmi.PASSWORD_ENV] = PASSWORD
    host_cfg = tmi.write_config(stack.tmp, port, {user: SCOPE[slug] for slug, user in INSTANCES.items()},
                                index=stack.tmp / "host" / "index.sqlite", name="host")
    tmi.mi.run(tmi.mi.load_config(host_cfg))
    want = tmi.rows(stack.tmp / "host" / "index.sqlite")
    expect(set(got) == set(want) and len(got) > 0,
           f"the add-on indexes the same {len(want)} messages as the host IMAP source")
    for mid, w in want.items():
        g = got[mid]
        for col in COLUMNS:
            if g[col] != w[col]:
                fail(f"{mid} {col}: add-on {g[col]!r} != host {w[col]!r}")
        names = [[a["name"] for a in json.loads(r["attachments"] or "[]")] for r in (g, w)]
        if names[0] != names[1]:
            fail(f"{mid}: attachment names {names}")
    expect({r["account"] for r in got.values()} == set(INSTANCES), "rows match column by column; accounts are the slugs")
    expect(tmi.server_state(port) == before, "indexing changed nothing on the server")

    # Drafts: one new (work) and one reply (home), in mail-drafts-mcp's queue format.
    db = mq.connect(copied / "index.sqlite")
    hit = mq.search(db, "lease", sender="priya")["results"][0]
    db.close()
    local = stack.tmp / "requests"
    new_id = drafts.enqueue(local, {"to": ["alex.chen@example.com"], "subject": "Saturday",
                                    "body": "See you then.", "from": tmi.USERS["work"]})
    reply_id = drafts.enqueue(local, {"reply_to": hit["reply_ref"], "body": "Signed and attached."})
    stack.exec_py("email-drafts-mcp", "import drafts, pathlib; drafts.dirs(pathlib.Path('/data/mail-drafts'))")
    for rid in (new_id, reply_id):
        queue_file(stack, local, rid)
    reply_subject = f"Re: {hit['subject']}"

    def both_saved():
        return ("Saturday" in drafts_on_server(port, "work")
                and reply_subject in drafts_on_server(port, "personal"))
    wait_for("both drafts in GreenMail", both_saved, 60, why=lambda: outbox_state(stack))
    work_drafts, home_drafts = drafts_on_server(port, "work"), drafts_on_server(port, "personal")
    flags_new, msg_new = work_drafts["Saturday"]
    flags_reply, msg_reply = home_drafts[reply_subject]
    expect("\\draft" in flags_new and str(msg_new["From"]) == tmi.USERS["work"],
           "within 60 s the new draft is in work's Drafts, flagged \\Draft")
    expect("\\draft" in flags_reply and "<synthetic-1205@example.net>" in str(msg_reply["References"]),
           "within 60 s the reply draft is in home's Drafts, threaded")
    after = tmi.server_state(port)
    expect({k: v for k, v in after.items() if k[1] != "Drafts"} == {k: v for k, v in before.items() if k[1] != "Drafts"},
           "nothing changed outside Drafts (nothing in Sent)")

    # Isolation, with all four services running.
    for name in ("email-mcp", "email-drafts-mcp"):
        cid = stack.compose("ps", "-q", name).stdout.strip()
        mounts = json.loads(subprocess.run(["docker", "inspect", "-f", "{{json .Mounts}}", cid], check=True,
                                           capture_output=True, text=True).stdout)
        expect(not any(m.get("Destination", "").startswith("/run/addon") for m in mounts),
               f"{name} has no mount under /run/addon")
    probe = ("import socket, sys\n"
             "try:\n    socket.create_connection((sys.argv[1], int(sys.argv[2])), timeout=5).close(); print('open')\n"
             "except OSError:\n    print('closed')\n")
    resolve = ("import socket, sys\n"
               "try:\n    socket.getaddrinfo(sys.argv[1], None); print('resolved')\n"
               "except OSError:\n    print('unresolved')\n")
    expect(stack.exec_py("email-indexer", probe, "greenmail", "3143") == "open",
           "email-indexer reaches greenmail:3143 (the probe works)")
    expect(stack.exec_py("email-mcp", probe, "greenmail", "3143") == "closed", "email-mcp can't reach greenmail:3143")
    expect(stack.exec_py("email-mcp", probe, "1.1.1.1", "443") == "closed", "email-mcp can't reach 1.1.1.1:443")
    expect(stack.exec_py("email-indexer", resolve, "greenmail") == "resolved", "email-indexer resolves greenmail")
    for host in ("hermes", "email-mcp"):
        expect(stack.exec_py("email-indexer", resolve, host) == "unresolved", f"email-indexer can't resolve {host}")

    # Drafts off: the outbox and drafts MCP stop; the index side keeps running.
    stack.addon_ok("configure", "email", values={"drafts": False})
    stack.addon_ok("enable", "email")
    states = stack.states()
    expect(states.get("email-outbox") != "running" and states.get("email-drafts-mcp") != "running"
           and states.get("email-indexer") == "running" and states.get("email-mcp") == "running",
           f"drafts off stops email-outbox and email-drafts-mcp only ({states})")
    expect(stack.env_file().get("EMAIL_DRAFTS_ENABLED") == "0", "drafts off writes EMAIL_DRAFTS_ENABLED=0")
    # A request waiting in the queue while the outbox is stopped (copied into
    # the stopped email-drafts-mcp container, i.e. onto email_drafts).
    held_id = drafts.enqueue(local, {"to": ["alex.chen@example.com"], "subject": "Held while off",
                                     "body": "Later.", "from": tmi.USERS["work"]})
    queue_file(stack, local, held_id)
    listing = stack.compose("run", "--rm", "--no-deps", "-T", "email-outbox", "ls", "/data/drafts",
                            "/data/drafts/queue").stdout
    expect(f"{held_id}.json" in listing, "the queued request is on email_drafts while the outbox is stopped")
    time.sleep(5)
    expect("Held while off" not in drafts_on_server(port, "work"), "nothing reaches GreenMail while drafts is off")
    stack.addon_ok("configure", "email", values={"drafts": True})
    stack.addon_ok("enable", "email")
    wait_for("the held draft in GreenMail", lambda: "Held while off" in drafts_on_server(port, "work"), 90,
             why=lambda: outbox_state(stack))
    print("ok after drafts on and enable, the held request reaches work's Drafts", flush=True)

    # A refused password for home: the pass reports it, work's new mail is indexed, home's rows stay.
    def index_rows(name: str) -> dict[str, sqlite3.Row]:
        stack.compose("cp", "email-indexer:/data/index", str(stack.tmp / name))
        return tmi.rows(stack.tmp / name / "index.sqlite")
    home_before = {mid for mid, r in index_rows("before-refused").items() if r["account"] == "home"}
    stack.addon_ok("configure", "email", "--instance", "home", values={"password": WRONG_PASSWORD})
    conn = tmi.admin(port, "work")
    fresh = tmi.sm.build_message(tmi.sm.Spec(
        rowid=998, account="work", mailbox="INBOX", sender=("Avery Kim", "avery@example.com"), to=[tmi.sm.WORK_ME],
        subject="New for work while home is refused", date="Fri, 09 Oct 2026 09:00:00 +0000", text="Hello."))
    conn.append("INBOX", "()", tmi.imaplib.Time2Internaldate(time.time()),
                fresh.as_bytes(policy=tmi.sm.POLICY.clone(linesep="\r\n")))
    conn.logout()
    seen = len(pass_lines(stack))
    stack.compose("restart", "email-indexer")
    line = wait_for("a pass after the refused password",
                    lambda: (pass_lines(stack)[seen:] or [None])[0], 180)
    logs = stack.compose("logs", "--no-color", "--no-log-prefix", "email-indexer").stdout
    expect("mail-index: home: " in logs and WRONG_PASSWORD not in logs,
           "the refused login logs a mail-index: line naming home, without the password")
    expect(line.get("error") == "4" and line.get("failed") == {"home": 4},
           f"the pass shows code 4 for home ({ {k: line.get(k) for k in ('error', 'failed', 'added')} })")
    after_rows = index_rows("after-refused")
    expect("<synthetic-998@example.net>" in after_rows and after_rows["<synthetic-998@example.net>"]["account"] == "work",
           "work's new mail is indexed in the same pass")
    home_after = {mid for mid, r in after_rows.items() if r["account"] == "home"}
    expect(home_after == home_before and home_before,
           f"home's {len(home_before)} rows remain in the index")
    status = json.loads(stack.exec_py("email-mcp", "import json, server; print(json.dumps(server.mail_status()))"))
    expect(status.get("failed_accounts") == {"home": 4}, f"mail_status lists home under failed_accounts ({status.get('failed_accounts')})")

    # Removing an instance drops it from the next pass.
    stack.addon_ok("configure", "email", "--instance", "home", "--remove")
    expect(not (stack.data / "addons" / "email" / "instances" / "home.json").exists()
           and not (stack.data / "addons" / "email" / "secrets" / "home.password").exists(),
           "configure --remove deletes home's instance and password files")
    seen = len(pass_lines(stack))
    stack.compose("restart", "email-indexer")
    wait_for_pass(stack, seen)
    status = json.loads(stack.exec_py("email-mcp", "import json, server; print(json.dumps(server.mail_status()))"))
    expect({b["account"] for b in status["mailboxes"]} == {"work"} and status.get("failed_accounts") == {},
           "the next pass's mail_status lists only work, with no failed accounts")
    hits = json.loads(stack.exec_py("email-mcp", "import json, server; print(json.dumps(server.mail_search('lease')))"))
    expect(hits.get("results") == [], "home's messages no longer show in search")

    # Disable stops all four even though sync-profiles.sh has dropped their profiles.
    stack.addon_ok("disable", "email")
    flags = stack.env_file()
    expect(flags.get("EMAIL_ENABLED") == "0"
           and not {"email", "email-drafts"} & set(flags.get("COMPOSE_PROFILES", "").split(",")),
           "disable writes EMAIL_ENABLED=0 and drops both profiles")
    states = stack.states()
    expect(all(states.get(s) not in ("running", "restarting") for s in EMAIL_SERVICES),
           f"disable stops all four services ({states})")
    vols = {f"{PROJECT}_email_index", f"{PROJECT}_email_drafts"}
    have = set(subprocess.run(["docker", "volume", "ls", "-q"], capture_output=True, text=True).stdout.split())
    expect(vols <= have and (stack.data / "addons" / "email").is_dir(),
           "email_index, email_drafts and data/addons/email remain after disable")

    # No password in any log, docker inspect or addon.sh output. GreenMail is
    # left out: it is the server, and its GREENMAIL_OPTS hold the test users.
    stack_services = [s for s in stack.states() if s != "greenmail"]
    logs = stack.compose("logs", "--no-color", *stack_services).stdout
    cids = [c for s in stack_services for c in stack.compose("ps", "-aq", s).stdout.split()]
    inspect = subprocess.run(["docker", "inspect", *cids], capture_output=True, text=True).stdout
    expect(set(EMAIL_SERVICES) <= set(stack_services), f"services checked for the password: {sorted(stack_services)}")
    expect(PASSWORD not in logs and WRONG_PASSWORD not in logs, "no password in docker compose logs")
    expect(PASSWORD not in inspect, f"no password in docker inspect of {len(cids)} containers")
    expect(not any(PASSWORD in o or WRONG_PASSWORD in o for o in OUTPUT), "no password in addon.sh output")

    stack.addon_ok("disable", "email", "--purge")
    have = set(subprocess.run(["docker", "volume", "ls", "-q"], capture_output=True, text=True).stdout.split())
    expect(not vols & have, "disable --purge removes email_index and email_drafts")
    expect(not (stack.data / "addons" / "email").exists(), "disable --purge removes data/addons/email")


def main() -> int:
    if not tmi.docker_ok():
        print("SKIP email add-on end to end: Docker isn't running")
        return 77
    tmp = Path(os.path.realpath(tempfile.mkdtemp(prefix="email-e2e-")))
    stack = Stack(tmp)
    try:
        stack.build()
        run(stack)
    finally:
        stack.down()
        left = stack.project_objects()
        for _ in range(3):
            shutil.rmtree(tmp, ignore_errors=True)
            if not tmp.exists():
                break
            time.sleep(2)
        if tmp.exists():
            print(f"note: could not remove {tmp}", file=sys.stderr)
        if left:
            print(f"FAIL left behind after down -v: {left}", file=sys.stderr)
        else:
            print(f"ok docker compose -p {PROJECT} down -v left nothing behind", flush=True)
    if left:
        return 1
    print("OK email add-on end to end")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
