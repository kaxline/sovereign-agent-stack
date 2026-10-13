"""Create, show or remove a triage cron job inside the hermes container.

Run by scripts/mail-triage-cron.sh (and scripts/messages-triage-cron.sh) as
the hermes user with the venv Python, with one JSON argument. The job is
mail-triage with the mail tools unless the request names another job and
tool list (messages-triage with the messages tools). Uses Hermes's own
cronjob tool so the job record is exactly what Hermes would write, then reads
it back and fails if the tool allowlist didn't stick. Prints job settings,
never prompts of other jobs. See docs/email.md and docs/messages.md.
"""

import json
import sys

from cron.jobs import get_job
from tools.cronjob_tools import cronjob

NAME = "mail-triage"
TOOLSETS = ["mail"]


def call(**kwargs) -> dict:
    return json.loads(cronjob(**kwargs))


def job_id(job: dict) -> str:
    return job.get("job_id") or job.get("id")


def mine(name: str = NAME) -> list:
    listed = call(action="list", include_disabled=True)
    if not listed.get("success", True):
        raise SystemExit(f"listing cron jobs failed: {listed.get('error')}")
    return [j for j in listed.get("jobs", []) if j.get("name") == name]


def summary(job: dict) -> dict:
    return {k: job.get(k) for k in ("id", "name", "schedule_display", "deliver", "enabled_toolsets",
                                     "monitor_url", "enabled", "next_run_at")}


def main() -> int:
    req = json.loads(sys.argv[1])
    name, toolsets = req.get("name", NAME), req.get("toolsets", TOOLSETS)
    existing = mine(name)
    if req["action"] == "show":
        print(json.dumps([summary(get_job(job_id(j)) or {}) for j in existing], indent=2))
        return 0
    for job in existing:
        removed = call(action="remove", job_id=job_id(job))
        if not removed.get("success"):
            raise SystemExit(f"removing the old {name} job failed: {removed.get('error')}")
    if req["action"] == "remove":
        print(json.dumps({"removed": len(existing)}))
        return 0
    created = call(action="create", name=name, schedule=req["schedule"], prompt=req["prompt"],
                   deliver=req["deliver"], enabled_toolsets=toolsets, monitor_url=req["monitor_url"])
    if not created.get("success"):
        raise SystemExit(f"creating the {name} job failed: {created.get('error')}")
    stored = get_job(created["job_id"]) or {}
    if stored.get("enabled_toolsets") != toolsets or stored.get("monitor_url") != req["monitor_url"]:
        call(action="remove", job_id=created["job_id"])
        raise SystemExit(f"Hermes didn't store the {'/'.join(toolsets)}-only tool allowlist or the monitor URL; "
                         "removed the job. This Hermes version may handle cron jobs differently.")
    print(json.dumps({"replaced": len(existing), "job": summary(stored)}, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
