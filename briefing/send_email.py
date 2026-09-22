#!/usr/bin/env python3
"""Emails the rendered daily portfolio dashboard via the Resend API."""
import datetime
import json
import os
import subprocess
import sys
import tempfile
import time

TO_EMAIL = os.environ.get("BRIEFING_TO_EMAIL", "")
FROM_EMAIL = "onboarding@resend.dev"  # Resend sandbox sender; replace with a verified domain address once you have one
API_KEY_PATH = os.path.expanduser("~/.config/portfolio-briefing/resend_api_key.txt")
DASHBOARD_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "latest_dashboard.html")
LOG_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "email.log")


def log(msg, err=False):
    """Log to file as well as stdout - the 9am run is unattended, so a bare
    stdout message is lost unless the caller happens to redirect it."""
    stamped = f"{datetime.datetime.now():%Y-%m-%d %H:%M:%S} | {msg}"
    print(stamped, file=sys.stderr if err else sys.stdout)
    try:
        with open(LOG_PATH, "a") as f:
            f.write(stamped + "\n")
    except OSError:
        pass  # never let logging break delivery


def read_api_key():
    if not os.path.exists(API_KEY_PATH):
        print(f"ERROR: Resend API key not found at {API_KEY_PATH}", file=sys.stderr)
        print("Create the file and paste your Resend API key into it (no quotes, no trailing newline needed).", file=sys.stderr)
        sys.exit(1)
    with open(API_KEY_PATH) as f:
        key = f.read().strip()
    if not key:
        print(f"ERROR: {API_KEY_PATH} is empty.", file=sys.stderr)
        sys.exit(1)
    return key


def read_dashboard_html():
    if not os.path.exists(DASHBOARD_PATH):
        print(f"ERROR: Dashboard HTML not found at {DASHBOARD_PATH}", file=sys.stderr)
        sys.exit(1)
    with open(DASHBOARD_PATH) as f:
        return f.read()


def send(subject, html):
    api_key = read_api_key()
    payload = {
        "from": FROM_EMAIL,
        "to": [TO_EMAIL],
        "subject": subject,
        "html": html,
    }

    with tempfile.NamedTemporaryFile(mode="w", suffix=".json", delete=False) as f:
        json.dump(payload, f)
        payload_path = f.name

    # The 9am run has already been lost once to a transient DNS failure
    # ("Could not resolve host: api.resend.com"), so retry before giving up.
    attempts = 5
    try:
        for attempt in range(1, attempts + 1):
            result = subprocess.run(
                [
                    "curl", "-sS", "-w", "\n%{http_code}",
                    "--connect-timeout", "10", "--retry", "2", "--retry-delay", "3",
                    "https://api.resend.com/emails",
                    "-H", f"Authorization: Bearer {api_key}",
                    "-H", "Content-Type: application/json",
                    "--data", f"@{payload_path}",
                ],
                capture_output=True, text=True, timeout=60,
            )

            if result.returncode != 0:
                err = f"curl failed: {result.stderr.strip()}"
            else:
                *body_lines, status_code = result.stdout.rsplit("\n", 1)
                body = "\n".join(body_lines)
                status_code = status_code.strip()
                if status_code in ("200", "201"):
                    log(f"Sent OK ({status_code}): {body}")
                    return
                # 4xx is our fault (bad key, bad payload) - retrying won't help.
                if status_code.startswith("4"):
                    log(f"ERROR: Resend API returned {status_code}: {body}", err=True)
                    sys.exit(1)
                err = f"Resend API returned {status_code}: {body}"

            if attempt < attempts:
                log(f"WARN: attempt {attempt}/{attempts} failed ({err}) - retrying in {attempt * 5}s", err=True)
                time.sleep(attempt * 5)
            else:
                log(f"ERROR: all {attempts} attempts failed. Last: {err}", err=True)
                sys.exit(1)
    finally:
        os.unlink(payload_path)


if __name__ == "__main__":
    from datetime import date
    html = read_dashboard_html()
    subject = f"AI Portfolio Daily Briefing — {date.today():%b %d, %Y}"
    send(subject, html)
