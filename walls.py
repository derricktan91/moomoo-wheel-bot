#!/usr/bin/env python3
"""Put/call wall scanner for the /walls Telegram command.

    python3 walls.py NBIS

Emits one JSON line. Run as a subprocess by the bot so an OpenD stall can't
wedge the message loop (same reason as option_chain.py).

moomoo has no "wall" endpoint. A wall is just the strike holding the most open
interest, so this sums OI per strike across every expiry inside MAX_DTE and
reports the biggest strikes on each side of spot:

  * call walls — strikes at or above spot (overhead positioning)
  * put walls  — strikes at or below spot (positioning underneath)
  * peak gamma — strike with the most gamma x OI, where dealer hedging is
                 most concentrated

OI is published once a day after the close, so this is yesterday's
positioning. It says where contracts sit, not who is long or short them.
"""

import json
import os
import sys
import time

import pandas as pd
from futu import OpenQuoteContext, RET_OK

HOST = os.environ.get("OPEND_HOST", "127.0.0.1")
PORT = int(os.environ.get("OPEND_PORT", "11111"))
MAX_DTE = 45            # same horizon as the 30-45 DTE trades this supports
MAX_EXPIRIES = 8        # weeklies on index ETFs would blow the time budget
BAND = 0.30             # only strikes within +/-30% of spot
CHAIN_PAUSE = 3.1       # get_option_chain is limited to 10 calls per 30s
TOP_N = 3


def main():
    if len(sys.argv) < 2:
        print(json.dumps({"error": "usage: walls.py TICKER"}))
        return
    ticker = sys.argv[1].upper()
    code = f"US.{ticker}"
    q = OpenQuoteContext(host=HOST, port=PORT)
    try:
        ret, snap = q.get_market_snapshot([code])
        if ret != RET_OK or snap.empty:
            print(json.dumps({"error": f"{ticker}: no quote ({str(snap)[:80]})"}))
            return
        spot = float(snap["last_price"][0])

        ret, exp = q.get_option_expiration_date(code)
        if ret != RET_OK or exp.empty:
            print(json.dumps({"error": f"{ticker}: no option chain"}))
            return
        exps = [d for d, n in zip(exp["strike_time"], exp["option_expiry_date_distance"])
                if 0 < n <= MAX_DTE][:MAX_EXPIRIES]
        if not exps:
            print(json.dumps({"error": f"{ticker}: no expiry within {MAX_DTE} days"}))
            return

        frames = []
        for i, d in enumerate(exps):
            if i:
                time.sleep(CHAIN_PAUSE)
            ret, ch = q.get_option_chain(code, start=d, end=d)
            if ret != RET_OK:
                continue
            ch = ch[(ch["strike_price"] > spot * (1 - BAND)) &
                    (ch["strike_price"] < spot * (1 + BAND))]
            codes = list(ch["code"])
            for j in range(0, len(codes), 300):
                ret, s = q.get_market_snapshot(codes[j:j + 300])
                if ret == RET_OK:
                    frames.append(s[["option_type", "option_strike_price",
                                     "option_open_interest", "option_gamma"]])
        if not frames:
            print(json.dumps({"error": f"{ticker}: chain returned no contracts"}))
            return

        d = pd.concat(frames)
        d["gex"] = d["option_gamma"] * d["option_open_interest"] * 100 * spot * spot * 0.01
        calls, puts = d[d.option_type == "CALL"], d[d.option_type == "PUT"]

        def top(df, in_play):
            g = df.groupby("option_strike_price")["option_open_interest"].sum()
            g = g[in_play(g.index)].sort_values(ascending=False).head(TOP_N)
            return [{"strike": float(k), "oi": int(v),
                     "dist_pct": round((float(k) / spot - 1) * 100, 1)}
                    for k, v in g.items() if v > 0]

        gex = d.groupby("option_strike_price")["gex"].sum()
        call_oi, put_oi = int(calls.option_open_interest.sum()), int(puts.option_open_interest.sum())
        print(json.dumps(dict(
            ticker=ticker, spot=spot, first_expiry=exps[0], last_expiry=exps[-1],
            n_expiries=len(exps), n_contracts=len(d),
            call_walls=top(calls, lambda k: k >= spot),
            put_walls=top(puts, lambda k: k <= spot),
            peak_gamma=float(gex.idxmax()) if gex.max() > 0 else None,
            call_oi=call_oi, put_oi=put_oi,
            pc_ratio=round(put_oi / call_oi, 2) if call_oi else None,
        )))
    finally:
        q.close()


if __name__ == "__main__":
    main()
