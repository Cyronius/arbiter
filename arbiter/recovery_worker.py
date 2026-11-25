"""Private entry point for a single already-registered recovery attempt."""
import json
import os
import sys
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from arbiter import adapters, transcripts, workers
from arbiter.client import Client
from arbiter.recovery import OPTIONS


def main():
    raw = sys.stdin.buffer.read()
    if not raw:
        return
    ticket = json.loads(raw)
    sid = ticket["session"]
    client = Client(url=ticket["url"], token=ticket["token"], patient=60)
    os.environ.update(ARBITER_URL=client.url, ARBITER_TOKEN=client.token, ARBITER_SESSION=sid)
    try:
        os.chdir(ticket["cwd"])
    except OSError:
        client.action("finish", session=sid, safe=True, failed=True, status="Original working directory unavailable")
        return
    if ticket["kind"] == "job":
        record = ticket["record"]
        args = SimpleNamespace(**{key: record[key] for key in OPTIONS},
                               label=record["bundle"].get("label"))
        workers.execute_job(args, record["argv"], sid, client, record["bundle"], ticket["reservation"])
    else:
        workers.catch_signals()
        try:
            workers.acquire(client, sid, ticket["reservation"])
        except workers.Stopped:
            workers.ignore_signals()
            client.action("finish", session=sid, safe=True, failed=True, cancelled=True, status="Cancelled before provider launch")
            return
        if not transcripts.can_resume(ticket["kind"], ticket["guid"]):
            client.action("finish", session=sid, safe=True, failed=True, status="Provider transcript unavailable at launch")
            return
        args = SimpleNamespace(provider=ticket["kind"], cwd=ticket["cwd"], resume=ticket["guid"],
                               prompt=ticket["prompt"], model=None, once=True)
        adapters.run_agent(args, client=client, registered={"id": sid, "token": client.token, "external_id": ticket["guid"]})


if __name__ == "__main__":
    main()
