#!/usr/bin/env python3
"""
Bollinger Band wheel-entry alerts -> Telegram.

Scans the wheel watchlist via moomoo OpenD and pings Telegram when a ticker
trades in the mid-to-lower Bollinger Band zone (%B <= 0.50), which is the
entry window for selling cash-secured puts.

Zones (%B = (price - lower) / (upper - lower)):
    %B < 0.00          BELOW LOWER BAND   strongest
    0.00 <= %B <= 0.20 AT LOWER BAND      strong
    0.20 <  %B <= 0.50 MID-TO-LOWER       watch

Usage:
    python3 bb_telegram_alert.py            # scan + alert on new signals
    python3 bb_telegram_alert.py --all      # send full report, ignore dedupe
    python3 bb_telegram_alert.py --dry-run  # print to stdout, send nothing
    python3 bb_telegram_alert.py --test     # send a Telegram connectivity test
"""
import json
import os
import ssl
import sys
import time
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path

ENV_FILE = Path.home() / ".moomoo_alerts.env"
STATE_FILE = Path.home() / ".moomoo_bb_alert_state.json"
LOG_FILE = Path.home() / ".moomoo_bb_alert.log"

OPEND_HOST = "127.0.0.1"
OPEND_PORT = 11111

BB_PERIOD = 20
BB_STDDEV = 2
RSI_PERIOD = 14
KLINE_LOOKBACK_DAYS = 200          # calendar days of history to request
THROTTLE_SEC = 3.0                 # OpenD limit is ~10 kline calls / 30s
RE_ALERT_AFTER_HOURS = 20          # re-ping a still-signalling ticker after this
ALERT_PCT_B = 0.50                 # ping once price is at/below the mid band
LEAPS_PCT_B = 0.30                 # lower half — flag as a LEAPS-entry zone too

# Wheel-strategy watchlist. csp=True means it's a live cash-secured-put
# candidate; csp=False tickers are tracked for context only (too much
# collateral, or an ETF rather than a wheel name).
WATCHLIST = [
    ("NVDA", True),
    ("AMD", True),
    ("AVGO", True),
    ("INTC", True),
    ("AMKR", True),
    ("MU", True),
    ("CRWV", True),
    ("IREN", True),
    ("NBIS", True),
    ("CRDO", True),
    ("GLW", True),
    ("CLS", True),
    ("ANET", True),
    ("MSFT", True),
    ("PLTR", True),
    ("SOFI", True),
    ("RKLB", True),
    # AI power / energy — datacentre electricity demand. VST/NRG/CCJ are
    # normal-vol (use the standard 10-15% OTM rule); OKLO/FLNC run 95-127%
    # and need the 1.28-sigma strike sizing, same as the Space names.
    ("VST", True),
    ("NRG", True),
    ("CCJ", True),
    ("OKLO", True),
    ("FLNC", True),
    ("FPS", True),   # Forgent Power Solutions, IPO Feb 2026 — short history
    ("QQQ", False),
    ("DRAM", False),
    ("ISRG", False),
    ("COHR", False),
    ("BE", False),
    ("CEG", False),
    ("VRT", False),
]


# ---------------------------------------------------------------- config ---

def load_env():
    cfg = {}
    if ENV_FILE.exists():
        for line in ENV_FILE.read_text().splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            k, v = line.split("=", 1)
            cfg[k.strip()] = v.strip()
    for k in ("TELEGRAM_BOT_TOKEN", "TELEGRAM_CHAT_ID"):
        cfg.setdefault(k, os.environ.get(k, ""))
    return cfg


def log(msg):
    stamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    line = f"{stamp} | {msg}"
    print(line)
    with LOG_FILE.open("a") as f:
        f.write(line + "\n")


# -------------------------------------------------------------- telegram ---

def ssl_context():
    """This Python install has no CA bundle (etc/openssl/cert.pem is missing),
    so fall back to certifi's roots rather than skipping verification."""
    try:
        import certifi
        return ssl.create_default_context(cafile=certifi.where())
    except ImportError:
        return ssl.create_default_context()


TG_LIMIT = 3800          # Telegram hard-caps at 4096; leave headroom for the
                         # part counter and any multi-byte characters.


def _split_for_telegram(text, limit=TG_LIMIT):
    """Split on line boundaries so HTML tags are never cut mid-tag.

    A single alert covering the whole watchlist can exceed Telegram's 4096-char
    cap — when that happens the API returns HTTP 400 and the send is lost
    silently. Chunking on newlines keeps each <b>/<code> pair inside one message.
    """
    if len(text) <= limit:
        return [text]
    chunks, cur = [], ""
    for line in text.split("\n"):
        # A single line longer than the limit would still overflow; hard-cut it.
        while len(line) > limit:
            if cur:
                chunks.append(cur); cur = ""
            chunks.append(line[:limit]); line = line[limit:]
        if len(cur) + len(line) + 1 > limit:
            chunks.append(cur); cur = line
        else:
            cur = f"{cur}\n{line}" if cur else line
    if cur:
        chunks.append(cur)
    return chunks


def send_telegram(cfg, text, parse_mode="HTML", chat_id=None):
    token = cfg.get("TELEGRAM_BOT_TOKEN")
    chat_id = chat_id or cfg.get("TELEGRAM_CHAT_ID")
    if not token or not chat_id:
        log("ERROR: TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID not set in ~/.moomoo_alerts.env")
        return False
    url = f"https://api.telegram.org/bot{token}/sendMessage"
    parts = _split_for_telegram(text)
    if len(parts) > 1:
        log(f"message {len(text)} chars -> splitting into {len(parts)} sends")
    ok_all = True
    for i, part in enumerate(parts, 1):
        body_text = part if len(parts) == 1 else f"{part}\n\n<i>({i}/{len(parts)})</i>"
        fields = {
            "chat_id": chat_id,
            "text": body_text,
            "disable_web_page_preview": "true",
        }
        if parse_mode:
            fields["parse_mode"] = parse_mode
        payload = urllib.parse.urlencode(fields).encode()
        try:
            req = urllib.request.Request(url, data=payload)
            with urllib.request.urlopen(req, timeout=20, context=ssl_context()) as r:
                body = json.loads(r.read().decode())
            if not body.get("ok"):
                log(f"Telegram API error (part {i}/{len(parts)}): {body}")
                ok_all = False
        except Exception as e:
            # Log the length too — HTTP 400 here is almost always over-length
            # or malformed HTML, and the size makes that immediately obvious.
            log(f"Telegram send failed (part {i}/{len(parts)}, {len(body_text)} chars): {e}")
            ok_all = False
    return ok_all


# ------------------------------------------------------------ indicators ---

def bollinger_and_rsi(close):
    """Return (mid, upper, lower, rsi, last_close) from a close-price Series.

    Bands are built from completed daily bars only; the caller decides whether
    to score %B against the last close or a live intraday price.
    """
    mid = close.rolling(BB_PERIOD).mean().iloc[-1]
    sd = close.rolling(BB_PERIOD).std(ddof=0).iloc[-1]
    upper = mid + BB_STDDEV * sd
    lower = mid - BB_STDDEV * sd

    delta = close.diff()
    gain = delta.clip(lower=0).rolling(RSI_PERIOD).mean()
    loss = (-delta.clip(upper=0)).rolling(RSI_PERIOD).mean()
    rs = gain / loss
    rsi = (100 - 100 / (1 + rs)).iloc[-1]

    return float(mid), float(upper), float(lower), float(rsi), float(close.iloc[-1])


def pct_b_of(price, lower, upper):
    width = upper - lower
    return (price - lower) / width if width else 0.5


def classify(pct_b):
    if pct_b < 0:
        return "BELOW_LOWER", "🔴", "Below lower band"
    if pct_b <= 0.20:
        return "AT_LOWER", "🟠", "At lower band"
    if pct_b <= ALERT_PCT_B:
        return "MID_TO_LOWER", "🟡", "Mid-to-lower zone"
    return "ABOVE_MID", "", "Above mid band"


ZONE_RANK = {"ABOVE_MID": 0, "MID_TO_LOWER": 1, "AT_LOWER": 2, "BELOW_LOWER": 3}


# ------------------------------------------------------------ market cal ---

def market_is_open():
    """True during the US regular session (Mon-Fri 09:30-16:00 ET).

    The launchd schedule fires in SGT, which straddles midnight and would
    otherwise run through weekends; this is the authoritative gate. Exchange
    holidays aren't enumerated — those are caught by the stale-bar check.
    """
    from zoneinfo import ZoneInfo
    et = datetime.now(ZoneInfo("America/New_York"))
    if et.weekday() >= 5:
        return False, f"weekend in ET ({et:%a %H:%M})"
    minutes = et.hour * 60 + et.minute
    if not (9 * 60 + 25 <= minutes <= 16 * 60 + 5):
        return False, f"outside RTH ({et:%a %H:%M ET})"
    return True, f"{et:%a %H:%M ET}"


def bars_are_stale(results):
    """Exchange holiday / data outage guard: the newest daily bar should be
    within a few sessions of today, otherwise the indicators aren't current."""
    if not results:
        return True
    try:
        newest = max(datetime.fromisoformat(r["bar"]).date() for r in results)
    except Exception:
        return False
    return (datetime.now().date() - newest).days > 4


# ----------------------------------------------------------------- scan ----

def scan():
    from futu import OpenQuoteContext, RET_OK, KLType, AuType

    today = datetime.now().date()
    start = (today - timedelta(days=KLINE_LOOKBACK_DAYS)).isoformat()
    end = today.isoformat()

    ctx = OpenQuoteContext(host=OPEND_HOST, port=OPEND_PORT)
    results, errors = [], []
    try:
        # One batched snapshot call gives live prices for the whole watchlist,
        # so an intraday dip into the band fires while it's still actionable.
        live = {}
        try:
            ret, snap = ctx.get_market_snapshot([f"US.{t}" for t, _ in WATCHLIST])
            if ret == RET_OK and snap is not None:
                for _, row in snap.iterrows():
                    px = float(row["last_price"])
                    if px > 0:
                        live[str(row["code"]).replace("US.", "")] = px
        except Exception as e:
            errors.append(f"snapshot: {e}")

        for i, (ticker, is_csp) in enumerate(WATCHLIST):
            code = f"US.{ticker}"
            try:
                # Explicit start/end is mandatory — without a date range OpenD
                # returns a stale cached window and the indicators come out wrong.
                ret, data, _ = ctx.request_history_kline(
                    code=code, start=start, end=end,
                    ktype=KLType.K_DAY, autype=AuType.QFQ, max_count=250,
                )
                if ret != RET_OK or data is None or len(data) < BB_PERIOD + 1:
                    errors.append(f"{ticker}: no data ({data if ret != RET_OK else len(data)} bars)")
                    continue

                last_bar = str(data["time_key"].iloc[-1])[:10]
                mid, upper, lower, rsi, last_close = bollinger_and_rsi(data["close"])
                price = live.get(ticker, last_close)
                pct_b = pct_b_of(price, lower, upper)
                zone, emoji, label = classify(pct_b)
                results.append({
                    "ticker": ticker, "csp": is_csp, "price": price,
                    "mid": mid, "upper": upper, "lower": lower,
                    "pct_b": pct_b, "rsi": rsi, "zone": zone,
                    "emoji": emoji, "label": label, "bar": last_bar,
                    "live": ticker in live, "last_close": last_close,
                })
            except Exception as e:
                errors.append(f"{ticker}: {e}")

            if i < len(WATCHLIST) - 1:
                time.sleep(THROTTLE_SEC)
    finally:
        ctx.close()

    return results, errors


# ---------------------------------------------------------------- state ----

def load_state():
    if STATE_FILE.exists():
        try:
            return json.loads(STATE_FILE.read_text())
        except Exception:
            pass
    return {}


def save_state(state):
    STATE_FILE.write_text(json.dumps(state, indent=2))


def is_new_signal(state, r, now):
    """Alert if the ticker just entered/deepened the zone, or it's been a while."""
    prev = state.get(r["ticker"])
    if not prev:
        return True
    if ZONE_RANK[r["zone"]] > ZONE_RANK.get(prev.get("zone", "ABOVE_MID"), 0):
        return True
    try:
        last = datetime.fromisoformat(prev["alerted_at"])
    except Exception:
        return True
    return now - last >= timedelta(hours=RE_ALERT_AFTER_HOURS)


# --------------------------------------------------------------- format ----

def fmt_line(r):
    tag = "" if r["csp"] else "  <i>(context only)</i>"
    to_lower = (r["price"] / r["lower"] - 1) * 100
    src = "live" if r["live"] else "close"
    out = (
        f"{r['emoji']} <b>{r['ticker']}</b>  ${r['price']:.2f} <i>({src})</i>  —  {r['label']}{tag}\n"
        f"    %B {r['pct_b']:.2f} · RSI {r['rsi']:.1f}\n"
        f"    lower ${r['lower']:.2f} · mid ${r['mid']:.2f} · upper ${r['upper']:.2f}\n"
        f"    {to_lower:+.1f}% from lower band"
    )
    if r["pct_b"] <= LEAPS_PCT_B:
        note = "🎯 <b>LEAPS zone</b> — lower half of the band"
        if r["rsi"] < 35:
            note += f", RSI {r['rsi']:.0f} oversold"
        out += f"\n    {note}"
        if r["csp"]:
            out += "\n    <i>CSP + LEAPS both in range</i>"
    return out


def build_message(fired, all_results, errors, full=False):
    stamp = datetime.now().strftime("%a %d %b %Y, %H:%M")
    bar = all_results[0]["bar"] if all_results else "n/a"
    head = f"📉 <b>Bollinger Band Wheel Alert</b>\n<i>{stamp} · bars thru {bar}</i>\n"

    if full:
        body = "\n\n".join(fmt_line(r) for r in sorted(all_results, key=lambda x: x["pct_b"]))
        head = f"📊 <b>BB Watchlist Report</b>\n<i>{stamp} · bars thru {bar}</i>\n"
    else:
        csp = [r for r in fired if r["csp"]]
        ctx_only = [r for r in fired if not r["csp"]]
        blocks = []
        if csp:
            blocks.append("<b>CSP candidates in the zone</b>\n\n" +
                          "\n\n".join(fmt_line(r) for r in csp))
        if ctx_only:
            blocks.append("<b>Context watchlist</b>\n\n" +
                          "\n\n".join(fmt_line(r) for r in ctx_only))
        body = "\n\n".join(blocks)

    msg = head + "\n" + body
    if errors:
        msg += "\n\n⚠️ <i>Skipped: " + "; ".join(errors) + "</i>"
    return msg


# ----------------------------------------------------------------- main ----

def main():
    cfg = load_env()
    args = set(sys.argv[1:])

    if "--test" in args:
        ok = send_telegram(cfg, "✅ <b>Wheel BB alert bot connected.</b>\nYou'll get a ping when a watchlist ticker drops into the mid-to-lower Bollinger Band.")
        log("test message sent" if ok else "test message FAILED")
        sys.exit(0 if ok else 1)

    dry = "--dry-run" in args
    full = "--all" in args
    forced = full or dry or "--force" in args

    if not forced:
        open_now, why = market_is_open()
        if not open_now:
            log(f"skipped — {why}")
            sys.exit(0)

    state = load_state()

    try:
        results, errors = scan()
    except Exception as e:
        log(f"scan failed: {e}")
        # At a 30-min cadence a persistent OpenD outage would otherwise ping
        # all night, so the failure notice is rate-limited to once every 6h.
        last_err = state.get("_last_error_alert")
        stale = True
        if last_err:
            try:
                stale = datetime.now() - datetime.fromisoformat(last_err) >= timedelta(hours=6)
            except Exception:
                stale = True
        if not dry and stale:
            send_telegram(cfg, f"⚠️ <b>BB alert scan failed</b>\n<code>{e}</code>\nIs OpenD running?")
            state["_last_error_alert"] = datetime.now().isoformat()
            save_state(state)
        sys.exit(1)

    state.pop("_last_error_alert", None)

    if not results:
        log(f"no results; errors={errors}")
        sys.exit(1)

    if bars_are_stale(results) and not forced:
        log(f"skipped — newest bar {max(r['bar'] for r in results)} is stale (holiday or data outage)")
        sys.exit(0)

    now = datetime.now()
    in_zone = [r for r in results if r["zone"] != "ABOVE_MID"]
    fired = [r for r in in_zone if is_new_signal(state, r, now)]

    summary = ", ".join(f"{r['ticker']}={r['pct_b']:.2f}" for r in sorted(results, key=lambda x: x["pct_b"]))
    log(f"scanned {len(results)} | in-zone {len(in_zone)} | firing {len(fired)} | {summary}")

    if full:
        msg = build_message(fired, results, errors, full=True)
    elif fired:
        msg = build_message(fired, results, errors)
    else:
        log("nothing new to alert")
        sys.exit(0)

    if dry:
        print("\n--- would send ---\n" + msg)
        sys.exit(0)

    if send_telegram(cfg, msg):
        for r in fired:
            state[r["ticker"]] = {"zone": r["zone"], "pct_b": round(r["pct_b"], 3),
                                  "alerted_at": now.isoformat()}
        # clear tickers that recovered above the mid band
        for r in results:
            if r["zone"] == "ABOVE_MID":
                state.pop(r["ticker"], None)
        save_state(state)
        log(f"alert sent for {[r['ticker'] for r in fired] or 'full report'}")
    else:
        sys.exit(1)


if __name__ == "__main__":
    main()
