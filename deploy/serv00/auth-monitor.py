"""Bounded session-lifetime probe for the serv00 deployment."""
import json
import os
import re
import subprocess
import tempfile
import time
from datetime import datetime, timezone
from pathlib import Path
from urllib.error import HTTPError

from gemini_web2api.config import CONFIG, load_config
from gemini_web2api.gemini import generate
from gemini_web2api.models import resolve_model

ROOT = Path(__file__).resolve().parent
STATE = ROOT / "auth-monitor-state.json"
LOG = ROOT / "auth-monitor.tsv"
CRON_MARKER = "/home/igsshan/gemini-web2api/auth-monitor.py"
DURATION_SEC = 90 * 60


def save_state(state):
    fd, name = tempfile.mkstemp(prefix=".auth-monitor-", dir=ROOT)
    try:
        with os.fdopen(fd, "w") as out:
            json.dump(state, out, indent=2)
            out.write("\n")
        os.replace(name, STATE)
    finally:
        if os.path.exists(name):
            os.unlink(name)


def remove_cron_job():
    jobs = subprocess.run(["crontab", "-l"], capture_output=True, text=True)
    if jobs.returncode != 0:
        return
    remaining = "".join(line for line in jobs.stdout.splitlines(keepends=True) if CRON_MARKER not in line)
    if remaining != jobs.stdout:
        subprocess.run(["crontab", "-"], input=remaining, text=True, check=True)


def main():
    os.umask(0o077)
    os.chdir(ROOT)
    now = int(time.time())
    state = json.loads(STATE.read_text()) if STATE.exists() else {
        "started_at": now,
        "deadline": now + DURATION_SEC,
        "checks": 0,
        "last_success_at": None,
        "first_failure_at": None,
        "first_1096_at": None,
        "consecutive_1096": 0,
        "stopped_reason": None,
    }
    if state["stopped_reason"] or now >= state["deadline"]:
        state["stopped_reason"] = state["stopped_reason"] or "deadline"
        save_state(state)
        remove_cron_job()
        return

    load_config(str(ROOT / "config.json"))
    CONFIG.update(retry_attempts=1, request_timeout_sec=35, log_requests=False, temporary_chats=True)
    _, model, think, error, extra = resolve_model(CONFIG["default_model"])
    if error:
        raise ValueError(error)
    started = time.monotonic()
    try:
        result = "ok" if generate("Reply exactly: OK", model, think, extra_fields=extra).strip() else "empty"
    except HTTPError as exc:
        result = f"http_{exc.code}"
    except Exception as exc:
        match = re.search(r"BardErrorInfo\s*\[(\d+)\]", str(exc))
        result = f"bard_{match.group(1)}" if match else f"error_{type(exc).__name__}"

    state["checks"] += 1
    if result == "ok":
        state["last_success_at"] = now
        state["consecutive_1096"] = 0
    else:
        if state["first_failure_at"] is None:
            state["first_failure_at"] = now
        state["consecutive_1096"] = state["consecutive_1096"] + 1 if result == "bard_1096" else 0
        if result == "bard_1096" and state["first_1096_at"] is None:
            state["first_1096_at"] = now
        if state["consecutive_1096"] >= 3:
            state["stopped_reason"] = "three_consecutive_1096"
    with LOG.open("a") as out:
        out.write(f"{datetime.fromtimestamp(now, timezone.utc).isoformat()}\t{now}\t{result}\t{time.monotonic()-started:.1f}s\n")
    save_state(state)
    if state["stopped_reason"]:
        remove_cron_job()
    print(f"check {state['checks']}: {result}, elapsed {now-state['started_at']}s")


if __name__ == "__main__":
    main()
