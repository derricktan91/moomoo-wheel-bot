#!/usr/bin/env python3
"""Waits until 8:31 AM ET, fetches CPI data, sends macOS notification."""

import time
import subprocess
import urllib.request
import json
from datetime import datetime
import pytz

et = pytz.timezone("America/New_York")
sgt = pytz.timezone("Asia/Singapore")

def notify(title, message):
    script = f'display notification "{message}" with title "{title}" sound name "Glass"'
    subprocess.run(["osascript", "-e", script])

def wait_until_cpi():
    now = datetime.now(et)
    target = et.localize(datetime(now.year, now.month, now.day, 8, 31, 0))
    delta = (target - now).total_seconds()
    if delta > 0:
        print(f"Sleeping {int(delta)}s until 8:31 AM ET ({target.astimezone(sgt).strftime('%H:%M SGT')})")
        time.sleep(delta)

def fetch_cpi():
    # BLS public API — CPI-U All Items (CUSR0000SA0) and Core CPI (CUSR0000SA0L1E)
    url = "https://api.bls.gov/publicAPI/v1/timeseries/data/CUSR0000SA0,CUSR0000SA0L1E"
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
        with urllib.request.urlopen(req, timeout=15) as resp:
            data = json.loads(resp.read())
        results = {}
        for series in data.get("Results", {}).get("series", []):
            sid = series["seriesID"]
            latest = series["data"][0]
            prev = series["data"][1]
            mom = float(latest["value"]) - float(prev["value"])
            results[sid] = {
                "value": float(latest["value"]),
                "period": latest["periodName"] + " " + latest["year"],
                "mom": mom
            }
        return results
    except Exception as e:
        return {"error": str(e)}

def main():
    wait_until_cpi()
    print("Fetching CPI data...")
    data = fetch_cpi()

    if "error" in data:
        # BLS API may not be instant — send reminder notification
        notify(
            "📊 CPI Just Released — 8:30 AM ET",
            "Check moomoo or Bloomberg for June CPI numbers. Key: headline vs 3.3% prior, core vs 3.5% prior."
        )
        print(f"API error: {data['error']} — sent fallback notification")
        return

    headline = data.get("CUSR0000SA0", {})
    core = data.get("CUSR0000SA0L1E", {})

    h_val = headline.get("value", "?")
    h_mom = headline.get("mom", 0)
    c_val = core.get("value", "?")
    c_mom = core.get("mom", 0)

    msg = (
        f"Headline CPI: {h_val} ({h_mom:+.1f}pts MoM) | "
        f"Core CPI: {c_val} ({c_mom:+.1f}pts MoM)"
    )
    title = "📊 CPI Released — Check PLTR & SOFI reaction"
    notify(title, msg)
    print(f"Notified: {title} | {msg}")

if __name__ == "__main__":
    main()
