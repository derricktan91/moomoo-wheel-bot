#!/usr/bin/env python3
"""
Simple daily portfolio briefing -> Telegram.

Reads the live moomoo account (real, FUTUSG) and sends one compact message:
account totals, stock positions, and short-call assignment risk.

Usage:
    python3 portfolio_telegram_briefing.py            # send to Telegram
    python3 portfolio_telegram_briefing.py --dry-run  # print, send nothing
"""
import json
import os
import re
import sys
from datetime import datetime, date
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from bb_telegram_alert import load_env, send_telegram  # noqa: E402  (shares the certifi-aware sender)


# Broker entity and OpenD endpoint come from the environment so this runs
# outside Singapore. moomoo SG = FUTUSG, US = FUTUINC, HK = FUTUSECURITIES,
# AU = FUTUAU, JP = FUTUJP, MY = FUTUMY, CA = FUTUCA. See SETUP.md.
_FIRMS = ("FUTUSG", "FUTUINC", "FUTUSECURITIES", "FUTUAU",
          "FUTUJP", "FUTUMY", "FUTUCA")
SECURITY_FIRM = os.environ.get("MOOMOO_SECURITY_FIRM", "").strip().upper()
if SECURITY_FIRM not in _FIRMS:
    # Guessing the region silently is worse than stopping: the wrong entity
    # fails authentication deep inside the API with no useful error.
    raise SystemExit(
        f"MOOMOO_SECURITY_FIRM must be set to your moomoo entity "
        f"({', '.join(_FIRMS)}). Got {SECURITY_FIRM or '<unset>'}. See SETUP.md.")
OPEND_HOST = os.environ.get("OPEND_HOST", "127.0.0.1")
OPEND_PORT = int(os.environ.get("OPEND_PORT", "11111"))

ACC_ID = int(os.environ.get("MOOMOO_ACC_ID", "0"))
LOG_FILE = Path.home() / ".moomoo_briefing.log"

# Written by the 9am scheduled briefing, which does the web search. Read-only here
# so this script stays dependency-free and safe to run unattended.
CATALYSTS_FILE = Path.home() / ".moomoo_catalysts.json"
CATALYST_HORIZON_DAYS = 45
CATALYSTS_STALE_DAYS = 3

OPT_RE = re.compile(r"^US\.([A-Z]+)(\d{6})([CP])(\d+)$")

TAG_ICON = {"bull": "🟢", "bear": "🔴", "macro": "🌐",
            "earnings": "📊", "expiry": "⏳"}


def log(msg):
    line = f"{datetime.now():%Y-%m-%d %H:%M:%S} | {msg}"
    print(line)
    with LOG_FILE.open("a") as f:
        f.write(line + "\n")


def parse_option(code):
    """US.PLTR260904C165000 -> ('PLTR', date(2026,9,4), 'C', 165.0)"""
    m = OPT_RE.match(code)
    if not m:
        return None
    sym, yymmdd, cp, strike = m.groups()
    try:
        exp = datetime.strptime(yymmdd, "%y%m%d").date()
    except ValueError:
        return None
    return sym, exp, cp, int(strike) / 1000.0


def money(v):
    return f"{'+' if v >= 0 else '-'}${abs(v):,.0f}"


def load_catalysts():
    """News/earnings written by the 9am briefing. Never fatal - returns None if absent."""
    try:
        with CATALYSTS_FILE.open() as f:
            data = json.load(f)
    except FileNotFoundError:
        return None
    except (json.JSONDecodeError, OSError) as e:
        log(f"catalysts unreadable: {e}")
        return None
    try:
        data["_age"] = (date.today() - datetime.strptime(data["updated"], "%Y-%m-%d").date()).days
    except (KeyError, ValueError):
        data["_age"] = None
    return data


def catalyst_lines(cat, held):
    """Render news + upcoming events. Held tickers are marked so they stand out."""
    if not cat:
        return ["", "<i>No catalyst file yet - run the 9am briefing to populate it.</i>"]

    L = []
    age = cat.get("_age")
    stale = age is None or age > CATALYSTS_STALE_DAYS

    headlines = cat.get("headlines") or []
    if headlines:
        L.append("")
        L.append("<b>📰 NEWS</b>")
        if stale:
            L.append(f"<i>⚠️ stale - updated {cat.get('updated', 'unknown')}</i>")
        for h in headlines[:6]:
            icon = TAG_ICON.get(h.get("tag", ""), "•")
            L.append(f"{icon} {h.get('text', '')}")

    today = date.today()
    upcoming = []
    for e in cat.get("events") or []:
        try:
            d = datetime.strptime(e["date"], "%Y-%m-%d").date()
        except (KeyError, ValueError):
            continue
        days = (d - today).days
        if 0 <= days <= CATALYST_HORIZON_DAYS:
            upcoming.append((days, d, e))
    upcoming.sort(key=lambda x: x[0])

    if upcoming:
        L.append("")
        L.append(f"<b>📅 NEXT {CATALYST_HORIZON_DAYS} DAYS</b>")
        for days, d, e in upcoming:
            icon = TAG_ICON.get(e.get("tag", ""), "•")
            tkr = e.get("ticker") or ""
            own = " 📌" if tkr and tkr in held else ""
            when = "today" if days == 0 else ("tomorrow" if days == 1 else f"in {days}d")
            label = f"<b>{tkr}</b> {e['event']}" if tkr else f"<b>{e['event']}</b>"
            L.append(f"{icon} {d:%d %b} ({when}) · {label}{own}")
            if e.get("note"):
                L.append(f"     <i>{e['note']}</i>")

    return L


def fetch():
    from futu import (OpenSecTradeContext, TrdMarket, SecurityFirm, TrdEnv,
                      Currency, RET_OK)

    ctx = OpenSecTradeContext(filter_trdmarket=TrdMarket.US, host="127.0.0.1",
                              port=11111, security_firm=SECURITY_FIRM)
    try:
        ret, pos = ctx.position_list_query(trd_env=TrdEnv.REAL, acc_id=ACC_ID)
        if ret != RET_OK:
            raise RuntimeError(f"position_list_query: {pos}")
        ret, acc = ctx.accinfo_query(trd_env=TrdEnv.REAL, acc_id=ACC_ID,
                                     currency=Currency.USD)
        if ret != RET_OK:
            raise RuntimeError(f"accinfo_query: {acc}")
    finally:
        ctx.close()
    return pos, acc.iloc[0]


def quote_spots(syms):
    """Last price for option underlyings that aren't held as stock."""
    if not syms:
        return {}
    from futu import OpenQuoteContext, RET_OK
    q = OpenQuoteContext(host="127.0.0.1", port=11111)
    try:
        ret, snap = q.get_market_snapshot([f"US.{s}" for s in sorted(syms)])
        if ret != RET_OK:
            return {}
        return {str(r["code"])[3:]: float(r["last_price"]) for _, r in snap.iterrows()}
    except Exception:
        return {}
    finally:
        q.close()


def earnings_before(cat, sym, exp):
    """Earnings date for `sym` falling on/before `exp` - a short call held through
    a print is the main assignment-gap risk in this book."""
    if not cat:
        return None
    today = date.today()
    for e in cat.get("events") or []:
        if e.get("ticker") != sym or e.get("tag") != "earnings":
            continue
        try:
            d = datetime.strptime(e["date"], "%Y-%m-%d").date()
        except ValueError:
            continue
        if today <= d <= exp:
            return d
    return None


def build(pos, acc, cat=None):
    stocks, shorts, longs = [], [], []

    for _, p in pos.iterrows():
        code = str(p["code"])
        row = {
            "code": code, "name": str(p["stock_name"]), "qty": float(p["qty"]),
            "price": float(p["nominal_price"]), "cost": float(p["cost_price"]),
            "mv": float(p["market_val"]), "pl": float(p["pl_val"]),
            "plpct": float(p["pl_ratio"]), "today": float(p["today_pl_val"]),
        }
        opt = parse_option(code)
        if opt:
            row["sym"], row["exp"], row["cp"], row["strike"] = opt
            row["dte"] = (row["exp"] - datetime.now().date()).days
            (shorts if row["qty"] < 0 else longs).append(row)
        else:
            row["ticker"] = code.replace("US.", "")
            stocks.append(row)

    spot = {s["ticker"]: s["price"] for s in stocks}
    spot.update(quote_spots({o["sym"] for o in shorts + longs} - set(spot)))
    stocks.sort(key=lambda r: -r["mv"])

    total_pl = float(pos["pl_val"].sum())
    today_pl = float(pos["today_pl_val"].sum())

    L = []
    L.append(f"💼 <b>Portfolio Briefing</b>")
    L.append(f"<i>{datetime.now():%a %d %b %Y, %H:%M}</i>")
    L.append("")
    L.append(f"<b>Total assets</b>   ${float(acc['total_assets']):,.0f}")
    L.append(f"Market value    ${float(acc['market_val']):,.0f}")
    L.append(f"Cash            ${float(acc['cash']):,.0f}")
    L.append(f"Unrealized      {money(total_pl)}")
    L.append(f"Today           {money(today_pl)}")

    L.append("")
    L.append("<b>📈 STOCKS</b>")
    for s in stocks:
        mark = "🟢" if s["pl"] >= 0 else "🔴"
        qty = f"{s['qty']:.4g}"
        L.append(f"{mark} <b>{s['ticker']}</b>  {qty} @ ${s['price']:,.2f}")
        L.append(f"     {money(s['pl'])} ({s['plpct']:+.1f}%) · cost ${s['cost']:,.2f}")

    if shorts:
        L.append("")
        L.append("<b>📝 SHORT OPTIONS</b>")
        for o in shorts:
            und = spot.get(o["sym"])
            n = int(abs(o["qty"]))
            head = (f"<b>{o['sym']} ${o['strike']:g}{o['cp']}</b> {o['exp']:%d %b}"
                    f"  ×{n}  ${o['price']:.2f}")
            if und is None:
                L.append(f"❓ {head}")
                L.append(f"     {o['dte']}d left · no spot price")
                continue
            # a call is OTM above spot, a put is OTM below it
            otm = ((o["strike"] / und - 1) if o["cp"] == "C"
                   else (1 - o["strike"] / und)) * 100
            if otm < 0:
                flag, note = "🔴", f"ITM by {abs(otm):.1f}%"
            elif otm < 3:
                flag, note = "🟠", f"only {otm:.1f}% OTM"
            elif otm < 8:
                flag, note = "🟡", f"{otm:.1f}% OTM"
            else:
                flag, note = "🟢", f"{otm:.1f}% OTM"
            L.append(f"{flag} {head}")
            left = f"{o['dte']}d left" if o['dte'] >= 0 else "EXPIRED"
            L.append(f"     stock ${und:,.2f} · {note} · {left}")
            L.append(f"     open {money(o['pl'])} ({o['plpct']:+.0f}%)")
            ed = earnings_before(cat, o["sym"], o["exp"])
            if ed:
                L.append(f"     ⚠️ earnings {ed:%d %b} before expiry")

    if longs:
        L.append("")
        L.append("<b>📗 LONG CALLS</b>")
        for o in longs:
            und = spot.get(o["sym"])
            spot_txt = f" · stock ${und:,.2f}" if und else ""
            L.append(f"<b>{o['sym']} ${o['strike']:g}C</b> {o['exp']:%d %b}"
                     f"  ×{int(o['qty'])}  ${o['price']:.2f}")
            L.append(f"     {money(o['pl'])} ({o['plpct']:+.0f}%) · {o['dte']}d left{spot_txt}")

    L.extend(catalyst_lines(cat, {s["ticker"] for s in stocks}))

    return "\n".join(L)


def main():
    dry = "--dry-run" in sys.argv[1:]
    cfg = load_env()
    try:
        pos, acc = fetch()
    except Exception as e:
        log(f"fetch failed: {e}")
        if not dry:
            send_telegram(cfg, f"⚠️ <b>Portfolio briefing failed</b>\n<code>{e}</code>\nIs OpenD running?")
        sys.exit(1)

    msg = build(pos, acc, load_catalysts())
    if dry:
        print("\n--- would send ---\n" + msg)
        return
    if send_telegram(cfg, msg):
        log(f"briefing sent ({len(pos)} positions)")
    else:
        sys.exit(1)


if __name__ == "__main__":
    main()
