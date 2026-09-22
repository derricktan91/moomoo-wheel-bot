#!/usr/bin/env python3
"""Deliver the daily briefing if the 9am run built it but never sent it.

The 9am task is a Claude session: it fetches data, renders the dashboard, then
emails and pushes to Telegram. Those last two steps are the ones that matter,
and they sit at the END of a long run. On 2026-09-21 the session wedged on an
optional browser preview call immediately after writing the dashboard, so the
briefing existed on disk and was never delivered. send_email.py's own retry
loop could not help — it was never called.

This closes that gap. It asks one question: was a briefing built today but not
sent? If so it sends it, retrying up to MAX_TRIES. If the dashboard is stale or
delivery already happened, it exits quietly.

Idempotent by design, so it can run on a schedule without risk of duplicates.
"""

import datetime as dt
import subprocess
import sys
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
DASHBOARD = HERE / "latest_dashboard.html"
EMAIL_LOG = HERE / "email.log"
TG_LOG = Path.home() / ".moomoo_briefing.log"
TG_SCRIPT = Path("__REPO_PATH__/portfolio_telegram_briefing.py")
LOG = HERE / "watchdog.log"
MAX_TRIES = 5
PY = sys.executable


def log(msg):
    line = f"{dt.datetime.now():%Y-%m-%d %H:%M:%S} | {msg}"
    print(line)
    try:
        LOG.open("a").write(line + "\n")
    except OSError:
        pass


def sent_today(path, today):
    """True if the log's last line is from today. Both logs append one line
    per successful delivery, so the last line is the authority."""
    try:
        lines = [l for l in path.read_text().splitlines() if l.strip()]
    except OSError:
        return False
    return bool(lines) and lines[-1].startswith(today)


def built_today(today):
    if not DASHBOARD.exists():
        return False
    mtime = dt.datetime.fromtimestamp(DASHBOARD.stat().st_mtime)
    return mtime.strftime("%Y-%m-%d") == today


def attempt(label, cmd):
    for i in range(1, MAX_TRIES + 1):
        try:
            r = subprocess.run(cmd, capture_output=True, text=True, timeout=180)
            if r.returncode == 0:
                log(f"{label}: sent on attempt {i}/{MAX_TRIES}")
                return True
            err = (r.stderr or r.stdout).strip().splitlines()[-1:] or ["no output"]
            err = err[0][:160]
        except subprocess.TimeoutExpired:
            err = "timed out after 180s"
        except Exception as e:                      # noqa: BLE001
            err = f"{type(e).__name__}: {e}"[:160]
        if i < MAX_TRIES:
            wait = i * 10
            log(f"{label}: attempt {i}/{MAX_TRIES} failed ({err}) - retrying in {wait}s")
            time.sleep(wait)
        else:
            log(f"{label}: ALL {MAX_TRIES} attempts failed. Last: {err}")
    return False


def main():
    today = dt.date.today().strftime("%Y-%m-%d")
    if not built_today(today):
        log("no dashboard built today - nothing to deliver, the 9am run has not got that far")
        return 0
    todo = []
    if not sent_today(EMAIL_LOG, today):
        todo.append(("email", [PY, str(HERE / "send_email.py")]))
    if not sent_today(TG_LOG, today):
        todo.append(("telegram", [PY, str(TG_SCRIPT)]))
    if not todo:
        log("already delivered today - nothing to do")
        return 0
    log(f"dashboard built today but {', '.join(t[0] for t in todo)} not sent - delivering")
    ok = all(attempt(label, cmd) for label, cmd in todo)
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
