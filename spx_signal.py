#!/usr/bin/env python3
"""SPX bull-put-spread entry signal.

Scores today's entry conditions for a monthly 0.20-delta / $25-wide SPX put
credit spread using the factors that held up in the 2019-2023 SPY backtest:
volatility regime, day of week, week of month, prior-day move, drawdown from
the 60-day high, and where the next CPI lands inside the trade.

SPX itself is not quotable through OpenD ("US stock indices are not
supported"), so everything is derived from SPY x10. The strike shown is a
Black-Scholes estimate for the app's actual chain, not a live quote.

    python3 spx_signal.py            -> JSON on the last stdout line (bot use)
    python3 spx_signal.py --text     -> formatted Telegram HTML
    python3 spx_signal.py --send     -> send to Telegram (launchd use)
"""
import json
import sys
from datetime import date, datetime, timedelta
from math import exp, log, sqrt
from pathlib import Path
from statistics import NormalDist

import numpy as np
import pandas as pd

sys.path.insert(0, str(Path(__file__).parent))
# imported as tlog — bare `log` would shadow math.log used in the pricing code
from bb_telegram_alert import load_env, log as tlog, send_telegram  # noqa: E402

N = NormalDist().cdf
RISK_FREE = 0.0375
DELTA_TARGET = 0.20
WIDTH_SPX = 25
DTE_LO, DTE_HI = 30, 45
IV_OVER_RV = 1.15          # same assumption the backtest used; flagged in output


# ------------------------------------------------------------- pricing ----

def put_price(S, K, T, v):
    if T <= 0:
        return max(K - S, 0.0)
    d1 = (log(S / K) + (RISK_FREE + v * v / 2) * T) / (v * sqrt(T))
    d2 = d1 - v * sqrt(T)
    return K * exp(-RISK_FREE * T) * N(-d2) - S * N(-d1)


def strike_for_delta(S, T, v, target=DELTA_TARGET):
    lo, hi = S * 0.55, S
    for _ in range(70):
        mid = (lo + hi) / 2
        d1 = (log(S / mid) + (RISK_FREE + v * v / 2) * T) / (v * sqrt(T))
        if abs(N(d1) - 1) < target:
            lo = mid
        else:
            hi = mid
    return (lo + hi) / 2


# --------------------------------------------------------------- data ----


def live_iv_curve(expiry, spot):
    """Per-strike implied vol from the live SPY chain, as SPX points.

    SPX itself is not quotable through OpenD, but SPY is and tracks it at 1:10.
    Returns a callable iv(K) interpolating the real smile, or None if the chain
    is unavailable.

    This exists because a single flat IV is wrong in a way that matters. Real
    chains price the further-OTM long leg HIGHER than the short leg, and on a
    25-wide spread that differential is worth roughly a third of the credit.
    Pricing both legs at one vol overstated the credit by ~26% against a live
    chain on 2026-09-08.
    """
    try:
        from futu import OpenQuoteContext, OptionType, SubType, RET_OK
        import numpy as np
        q = OpenQuoteContext(host="127.0.0.1", port=11111)
        try:
            ret, ch = q.get_option_chain("US.SPY", start=str(expiry), end=str(expiry),
                                         option_type=OptionType.PUT)
            if ret != RET_OK or not len(ch):
                return None
            lo, hi = spot / 10 * 0.80, spot / 10 * 1.03
            ch = ch[(ch.strike_price >= lo) & (ch.strike_price <= hi)][["code", "strike_price"]]
            if len(ch) < 6:
                return None
            q.subscribe(list(ch.code), [SubType.QUOTE])
            ret, snap = q.get_stock_quote(list(ch.code))
            if ret != RET_OK:
                return None
            m = ch.merge(snap, on="code", suffixes=("", "_q"))
            m["K"] = m.strike_price.astype(float) * 10          # -> SPX points
            m["iv"] = m.implied_volatility.astype(float) / 100
            m = m.dropna(subset=["iv"])
            m = m[(m.iv > 0.01) & (m.iv < 3.0)].sort_values("K")
            if len(m) < 6:
                return None
            ks, ivs = m.K.values, m.iv.values
            return lambda K: float(np.interp(K, ks, ivs))
        finally:
            q.close()
    except Exception:
        return None          # never let a chain failure break the signal


def solve_delta_live(spot, T, ivf, target=DELTA_TARGET):
    """Strike at `target` delta under a real (skewed) vol curve.

    strike_for_delta() assumes one vol everywhere; under a smile the vol at the
    strike depends on the strike, so this bisects using the local IV.
    """
    lo, hi = spot * 0.70, spot
    for _ in range(60):
        mid = (lo + hi) / 2
        v = ivf(mid)
        h = 1.0
        d = -(put_price(spot + h, mid, T, v) - put_price(spot - h, mid, T, v)) / (2 * h)
        if d > target:
            hi = mid
        else:
            lo = mid
    return mid


def fetch_spy(days=400):
    """Daily SPY closes. Explicit start/end — OpenD returns stale bars otherwise."""
    from futu import OpenQuoteContext, KLType, RET_OK
    end = date.today()
    start = end - timedelta(days=days)
    ctx = OpenQuoteContext(host="127.0.0.1", port=11111)
    try:
        ret, k, _ = ctx.request_history_kline(
            "US.SPY", start=str(start), end=str(end),
            ktype=KLType.K_DAY, max_count=400)
        if ret != RET_OK or len(k) < 80:
            raise RuntimeError(f"SPY kline failed: {k}")
        ret, snap = ctx.get_market_snapshot(["US.SPY"])
        live = float(snap.iloc[0]["last_price"]) if ret == RET_OK else None
    finally:
        ctx.close()
    k = k.reset_index(drop=True)
    k["d"] = pd.to_datetime(k["time_key"].str[:10])
    return k, live


def _nth_weekday(y, m, weekday, n):
    """nth (1-based) `weekday` of month m. n=-1 gives the last one."""
    if n > 0:
        d = date(y, m, 1)
        d += timedelta(days=(weekday - d.weekday()) % 7)
        return d + timedelta(weeks=n - 1)
    d = date(y, m + 1, 1) - timedelta(days=1) if m < 12 else date(y, 12, 31)
    return d - timedelta(days=(d.weekday() - weekday) % 7)


def market_holidays(y):
    """NYSE full-day closures. Good Friday is the only moveable feast we need
    and it is rarely near a monthly entry, so it is approximated per-year."""
    def observed(d):
        if d.weekday() == 5:
            return d - timedelta(days=1)
        if d.weekday() == 6:
            return d + timedelta(days=1)
        return d
    good_friday = {2025: date(2025, 4, 18), 2026: date(2026, 4, 3),
                   2027: date(2027, 3, 26), 2028: date(2028, 4, 14)}
    hol = {
        observed(date(y, 1, 1)),                    # New Year's Day
        _nth_weekday(y, 1, 0, 3),                   # MLK
        _nth_weekday(y, 2, 0, 3),                   # Presidents' Day
        _nth_weekday(y, 5, 0, -1),                  # Memorial Day
        observed(date(y, 6, 19)),                   # Juneteenth
        observed(date(y, 7, 4)),                    # Independence Day
        _nth_weekday(y, 9, 0, 1),                   # Labor Day
        _nth_weekday(y, 11, 3, 4),                  # Thanksgiving
        observed(date(y, 12, 25)),                  # Christmas
    }
    if y in good_friday:
        hol.add(good_friday[y])
    return hol


def is_trading_day(d):
    return d.weekday() < 5 and d not in market_holidays(d.year)


def is_monthly_opex(d):
    """SPX monthly expiry = 3rd Friday. Deepest liquidity of any expiry."""
    return d.weekday() == 4 and 15 <= d.day <= 21


def expiries_in_window(today, lo, hi):
    """SPX lists Mon/Wed/Fri expiries; Fridays carry the real open interest,
    so only Fridays are offered here. Holidays shift Friday expiry to Thursday."""
    out = []
    for n in range(lo, hi + 1):
        d = today + timedelta(days=n)
        if d.weekday() != 4:
            continue
        if d in market_holidays(d.year):
            d -= timedelta(days=1)
        out.append(d)
    return out


def next_cpi(today):
    """CPI is released around the 10th-14th; approximate as the first weekday
    on/after the 11th. Good enough to place it inside or outside the trade."""
    for months_ahead in (0, 1):
        y, m = today.year, today.month + months_ahead
        if m > 12:
            y, m = y + 1, m - 12
        d = date(y, m, 11)
        while d.weekday() > 4:
            d += timedelta(days=1)
        if d >= today:
            return d
    return None


# -------------------------------------------------------------- scoring ----

def analyse():
    k, live = fetch_spy()
    close = k["close"].astype(float)
    today = date.today()
    spy = live if live else float(close.iloc[-1])
    spx = spy * 10

    ret = close.pct_change().dropna()
    rv60 = float(ret.tail(60).std() * np.sqrt(252) * 100)
    hi60 = float(close.tail(60).max())
    dd60 = (spy / hi60 - 1) * 100
    prior = float(ret.iloc[-1] * 100)                     # last completed session
    dow = today.weekday()                                 # 0 = Monday
    dom = today.day
    cpi = next_cpi(today)
    cpi_in = (cpi - today).days if cpi else None

    f = {}   # factor -> (rating, note)  rating: 'good' | 'ok' | 'bad'
    if rv60 >= 24:
        f["vol"] = ("good", f"60d realised {rv60:.1f}% — richest regime (87% win, $221/mo in test)")
    elif 17 <= rv60 < 24:
        f["vol"] = ("bad", f"60d realised {rv60:.1f}% — the 17-24% trap band (lost money in test)")
    elif 13 <= rv60 < 17:
        f["vol"] = ("ok", f"60d realised {rv60:.1f}% — decent (79% win, $121/mo)")
    else:
        f["vol"] = ("weak", f"60d realised {rv60:.1f}% — bottom quartile, thin premium ($26/mo)")

    # A holiday Monday makes Tuesday the week's first session, so it inherits
    # the Monday edge AND carries an extra day of weekend decay.
    holiday = today in market_holidays(today.year)
    prev = today - timedelta(days=1)
    while prev.weekday() > 4 or prev in market_holidays(prev.year):
        prev -= timedelta(days=1)
    first_session = (today - prev).days >= 3

    if holiday:
        f["day"] = ("bad", f"{today.strftime('%A')} — market holiday, closed")
    elif dow > 4:
        f["day"] = ("bad", "weekend — market closed")
    elif dow == 0:
        f["day"] = ("good", "Monday — collect the weekend theta priced in Friday")
    elif first_session:
        f["day"] = ("good", f"{today.strftime('%A')} — first session after a long weekend "
                            f"({(today - prev).days}d of decay, better than a normal Monday)")
    elif dow == 4:
        f["day"] = ("bad", "Friday — worst day in test; you pay the weekend")
    else:
        f["day"] = ("ok", f"{today.strftime('%A')} — neutral")

    if dom > 21:
        f["week"] = ("good", "last week of month — post-OpEx, best bucket (76.8%)")
    elif 8 <= dom <= 14:
        f["week"] = ("bad", "2nd week — weakest bucket (68.2%)")
    else:
        f["week"] = ("ok", f"day {dom} — neutral")

    if prior <= -2:
        f["prior"] = ("good", f"prior session {prior:+.1f}% — IV spike, fatter credit")
    elif prior >= 2:
        f["prior"] = ("bad", f"prior session {prior:+.1f}% — big rip; worst bucket (61% win)")
    else:
        f["prior"] = ("ok", f"prior session {prior:+.1f}%")

    if dd60 <= -10:
        f["dd"] = ("good", f"{dd60:.1f}% off 60d high — washed out (82% win, $188/mo)")
    elif dd60 <= -3:
        f["dd"] = ("bad", f"{dd60:.1f}% off 60d high — mid-pullback trap zone")
    else:
        f["dd"] = ("ok", f"{dd60:.1f}% off 60d high — near highs, workable")

    if cpi_in is not None:
        if 15 <= cpi_in <= 21:
            f["cpi"] = ("good", f"CPI ~{cpi.strftime('%b %d')}, lands day {cpi_in} — mid-trade with runway")
        elif cpi_in >= 25:
            f["cpi"] = ("bad", f"CPI ~{cpi.strftime('%b %d')}, day {cpi_in} — sits at expiry, no recovery time")
        else:
            f["cpi"] = ("ok", f"CPI ~{cpi.strftime('%b %d')}, lands day {cpi_in}")

    # Derrick sells every month regardless of setup, so the verdict is about
    # SIZE, not go/no-go. Only the 17-24% realised-vol band is a genuine
    # stand-aside — it lost money in the backtest under every exit rule.
    good = sum(1 for r, _ in f.values() if r == "good")
    bad = sum(1 for r, _ in f.values() if r == "bad")
    # Market-closed is a separate axis from size: the strikes are still worth
    # showing on a Sunday so the setup can be planned before the open.
    closed = dow > 4 or holiday
    if f["vol"][0] == "bad":
        size = "CAUTION"
    elif rv60 >= 24:
        size = "FULL+"
    elif rv60 >= 13:
        size = "FULL"
    else:
        size = "LIGHT"
    verdict = "CLOSED" if closed else size

    # Next ideal entry: first session of the last week of a month. Normally the
    # Monday, but if that Monday is a holiday the Tuesday inherits the slot.
    nxt = today
    for _ in range(60):
        nxt += timedelta(days=1)
        if nxt.day <= 21 or not is_trading_day(nxt):
            continue
        prv = nxt - timedelta(days=1)
        while prv.weekday() > 4 or prv in market_holidays(prv.year):
            prv -= timedelta(days=1)
        if nxt.weekday() == 0 or (nxt - prv).days >= 3:
            break

    iv = rv60 / 100 * IV_OVER_RV
    candidates = []
    for expiry in expiries_in_window(today, DTE_LO, DTE_HI):
        dte = (expiry - today).days
        T = dte / 365
        ivf = live_iv_curve(expiry, spx)
        if ivf is not None:
            # Real smile: solve and price both legs at their own vol.
            ks5 = round(solve_delta_live(spx, T, ivf) / 5) * 5
            kl5 = ks5 - WIDTH_SPX
            cr = put_price(spx, ks5, T, ivf(ks5)) - put_price(spx, kl5, T, ivf(kl5))
            src, iv_used = "live chain", ivf(ks5) * 100
        else:
            # Fallback only — overstates credit because it ignores skew.
            ks5 = round(strike_for_delta(spx, T, iv) / 5) * 5
            kl5 = ks5 - WIDTH_SPX
            cr = put_price(spx, ks5, T, iv) - put_price(spx, kl5, T, iv)
            src, iv_used = "MODELLED (flat IV, overstates credit)", iv * 100
        maxloss = (WIDTH_SPX - cr) * 100
        # Backtest showed 30 and 43 DTE returned the same per year, but the
        # longer end was touched far less often (19% vs 25%). So rank on
        # return-on-risk and break ties toward the longer expiry.
        candidates.append(dict(
            expiry=str(expiry), label=expiry.strftime("%d %b"), dte=dte,
            monthly=is_monthly_opex(expiry),
            short=ks5, long=kl5, otm=(ks5 / spx - 1) * 100,
            credit=cr * 100, max_loss=maxloss,
            ror=cr * 100 / maxloss * 100,
            cr_per_day=cr * 100 / dte,
            source=src, iv_used=round(iv_used, 1),
            breakeven_wr=round(maxloss / (maxloss + cr * 100 * 0.5) * 100, 1),
            score=(cr * 100 / maxloss * 100) + dte * 0.02 + (0.35 if is_monthly_opex(expiry) else 0)))
    # A modelled candidate must never outrank a live-priced one: its credit is
    # inflated by the missing skew, so it would always win on score and would
    # be recommending a number the market will not pay.
    candidates.sort(key=lambda c: (c["source"] != "live chain", -c["score"]))
    for i, c in enumerate(candidates):
        c["best"] = (i == 0)

    return dict(date=str(today), spy=spy, spx=spx, rv60=rv60, dd60=dd60, prior=prior,
                factors=f, good=good, bad=bad, verdict=verdict, size=size, closed=closed,
                next_ideal=str(nxt), candidates=candidates, iv_assumed=iv * 100)


# ------------------------------------------------------------ rendering ----

VERDICT_LINE = {
    "FULL+":   "🟢 <b>Sell it — premium is rich.</b> Best vol regime in the backtest.",
    "FULL":    "🟢 <b>Sell it — normal month.</b>",
    "LIGHT":   "🟡 <b>Sell it, but keep size light.</b> Low vol = thin credit for the same risk.",
    "CAUTION": "🔴 <b>Consider standing aside.</b> Realised vol is in the 17-24% band, "
               "the only regime that LOST money in the backtest under every exit rule.",
    "CLOSED":  "⚪ Market closed today.",
}


def format_message(r):
    f = r["factors"]
    lines = [f"<b>📐 SPX put spread</b>",
             f"<i>{r['date']} · SPY ${r['spy']:.2f} → SPX ~{r['spx']:,.0f} · "
             f"60d vol {r['rv60']:.1f}%</i>", "",
             VERDICT_LINE[r["verdict"]], ""]
    if r["closed"]:
        # Still show the setup — it is what to enter at the next open.
        lines.append(VERDICT_LINE[r["size"]])
        lines.append("")

    if r["candidates"]:
        best = next(c for c in r["candidates"] if c["best"])
        lines.append(f"<b>➤ BEST: {best['label']} · {best['dte']} DTE"
                     f"{' · monthly' if best['monthly'] else ''}</b>")
        lines.append(f"<code>sell {best['short']:,.0f}P / buy {best['long']:,.0f}P   ({best['otm']:+.1f}% OTM)</code>")
        lines.append(f"<code>credit ~${best['credit']:,.0f}   max loss ~${best['max_loss']:,.0f}   "
                     f"return {best['ror']:.1f}%</code>")
        if best.get("breakeven_wr") is not None:
            lines.append(f"<code>needs {best['breakeven_wr']:.1f}% wins to break even</code>")
        if best.get("source") == "live chain":
            lines.append(f"<i>priced off the live chain, IV {best['iv_used']:.1f}% "
                         f"at the short strike</i>")
        else:
            lines.append("<i>⚠️ no live chain for this expiry — modelled at flat IV, "
                         "which overstates the credit by roughly a quarter</i>")
        lines.append("")
        others = [c for c in r["candidates"] if not c["best"]]
        if others:
            lines.append(f"<i>Others in the {DTE_LO}-{DTE_HI} DTE window:</i>")
            for c in sorted(others, key=lambda x: x["dte"]):
                tag = " ·mth" if c["monthly"] else ""
                warn = "" if c.get("source") == "live chain" else "  ⚠️modelled"
                lines.append(f"<code>{c['label']}  {c['dte']:>2}d{tag:<5} {c['short']:,.0f}/{c['long']:,.0f}  "
                             f"${c['credit']:>4,.0f}  {c['ror']:>4.1f}%</code>{warn}")
            lines.append("")
        lines.append("Close at <b>50% of credit</b>. No stop — the width is the stop.")
        if r["closed"]:
            lines.append("<i>Priced off the last close — strikes will shift at the open.</i>")

    # Everything below is context, not a gate. Only shown when it says something.
    notes = [note for key in ("day", "prior", "dd", "cpi")
             if key in f for rating, note in [f[key]] if rating in ("good", "bad")]
    if notes:
        lines.append("")
        lines.append("<i>Minor factors (worth &lt;$30/mo each, not a reason to skip):</i>")
        for nt in notes:
            lines.append(f"<i>· {nt}</i>")
    lines.append("")
    live = sum(1 for c in r.get("candidates", []) if c.get("source") == "live chain")
    tot = len(r.get("candidates", []))
    lines.append(f"<i>Priced from the live SPY chain ×10 ({live}/{tot} expiries) — "
                 f"verify the bid/ask in the moomoo .SPX chain before sending.</i>")
    return "\n".join(lines)


def main():
    r = analyse()
    if "--send" in sys.argv:
        cfg = load_env()
        ok = send_telegram(cfg, format_message(r))
        tlog(f"spx_signal: verdict {r['verdict']} -> {'sent' if ok else 'SEND FAILED'}")
    elif "--text" in sys.argv:
        print(format_message(r))
    else:
        print(json.dumps(r, default=str))


if __name__ == "__main__":
    main()
