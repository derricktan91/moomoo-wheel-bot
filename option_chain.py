#!/usr/bin/env python3
"""Option-chain scanner for the /csp and /cc Telegram commands.

    python3 option_chain.py NVDA put
    python3 option_chain.py SOFI call
    python3 option_chain.py CRDO leaps

Emits one JSON line. Run as a subprocess by the bot so an OpenD stall can't
wedge the message loop (same reason as wheel_analysis.py).

Reports three things the raw chain doesn't:
  * IV/RV  — implied vs what the stock is actually realising. Below ~0.9 you
             are being paid less than the risk; that is how AMKR looked at 0.58.
  * OI     — a .25-delta strike nobody trades is not a tradeable strike.
  * fit    — puts: collateral vs cash. calls: strike vs cost basis, because
             writing below basis locks in a loss if called.
"""

import json
import os
import sys
import datetime as dt

import numpy as np
import pandas as pd
from futu import (OpenQuoteContext, OpenSecTradeContext, TrdMarket, SecurityFirm,
                  TrdEnv, SubType, OptionType, KLType, RET_OK)

# Broker entity and OpenD endpoint come from the environment so this runs
# outside Singapore. moomoo SG = FUTUSG, US = FUTUINC, HK = FUTUSECURITIES,
# AU = FUTUAU, JP = FUTUJP, MY = FUTUMY, CA = FUTUCA. See SETUP.md.
SECURITY_FIRM = os.environ.get("MOOMOO_SECURITY_FIRM", "FUTUSG")
OPEND_HOST = os.environ.get("OPEND_HOST", "127.0.0.1")
OPEND_PORT = int(os.environ.get("OPEND_PORT", "11111"))

ACC_ID = int(os.environ.get("MOOMOO_ACC_ID", "0"))
DTE_LO, DTE_HI = 30, 45
DELTA_LO, DELTA_HI = 0.20, 0.30      # the band Derrick actually sells
LEAPS_DTE_MIN = 365                   # "at least a year out"
LEAPS_DELTA_LO, LEAPS_DELTA_HI = 0.65, 0.75   # target 0.70

# Per-ticker overrides. QQQ has its own documented rulebook — Δ0.80 (0.75-0.85)
# at DTE 650-800, rolled at Δ≥0.90 — and the daily briefing enforces it, so the
# command must not quietly offer the generic Δ0.70/365+ contract instead.
LEAPS_RULES = {
    "QQQ": {"dte_min": 650, "dte_max": 800, "delta_lo": 0.75, "delta_hi": 0.85,
            "label": "QQQ rulebook: \u03940.80, DTE 650-800"},
}
OI_MIN = 100                          # below this, treat the strike as untradeable
HOST, PORT = "127.0.0.1", 11111


def realised_vol(closes, n=30):
    lr = np.diff(np.log(closes))
    return float(np.std(lr[-n:]) * np.sqrt(252) * 100)


def holdings(ticker):
    """Shares held and cost basis — only needed for the covered-call side."""
    try:
        t = OpenSecTradeContext(filter_trdmarket=TrdMarket.US, host=HOST, port=PORT,
                                security_firm=SECURITY_FIRM)
        try:
            ret, pos = t.position_list_query(acc_id=ACC_ID, trd_env=TrdEnv.REAL)
        finally:
            t.close()
        if ret != RET_OK:
            return None
        row = pos[pos.code == f"US.{ticker}"]
        if not len(row):
            return None
        r = row.iloc[0]
        return {"qty": float(r.qty), "cost": float(r.cost_price)}
    except Exception:
        return None            # never let the holdings lookup break the chain


def main():
    if len(sys.argv) < 3:
        print(json.dumps({"error": "usage: option_chain.py TICKER put|call"}))
        return 1
    ticker = sys.argv[1].upper().lstrip("$")
    side = sys.argv[2].lower()
    if side not in ("put", "call", "leaps"):
        print(json.dumps({"error": "side must be put, call or leaps"}))
        return 1
    is_leaps = side == "leaps"

    q = OpenQuoteContext(host=HOST, port=PORT)
    try:
        code = f"US.{ticker}"
        ret, snap = q.get_market_snapshot([code])
        if ret != RET_OK or not len(snap):
            print(json.dumps({"error": f"{ticker}: no quote ({str(snap)[:80]})"}))
            return 1
        spot = float(snap.iloc[0].last_price)

        end = dt.date.today()
        ret, k, _ = q.request_history_kline(
            code, start=str(end - dt.timedelta(days=200)), end=str(end),
            ktype=KLType.K_DAY, max_count=200)
        rv = realised_vol(k.close.astype(float).values) if ret == RET_OK and len(k) > 35 else None

        ret, ex = q.get_option_expiration_date(code)
        if ret != RET_OK or not len(ex):
            print(json.dumps({"error": f"{ticker}: no option chain"}))
            return 1
        ex["d"] = pd.to_datetime(ex.strike_time).dt.date
        ex["dte"] = [(d - end).days for d in ex.d]
        rule = LEAPS_RULES.get(ticker) if is_leaps else None
        dte_min = rule["dte_min"] if rule else LEAPS_DTE_MIN
        dte_max = rule.get("dte_max") if rule else None
        d_lo = rule["delta_lo"] if rule else LEAPS_DELTA_LO
        d_hi = rule["delta_hi"] if rule else LEAPS_DELTA_HI

        if is_leaps:
            window = ex[ex.dte >= dte_min]
            if dte_max:
                window = window[window.dte <= dte_max]
            if not len(window):
                # Name the neighbours — a bare "no expiry" hides whether the
                # window is genuinely empty or the rule is simply mistimed.
                span = f"{dte_min}-{dte_max}" if dte_max else f"{dte_min}+"
                below = ex[ex.dte < dte_min].sort_values("dte").tail(1)
                above = ex[ex.dte > (dte_max or 10**6)].sort_values("dte").head(1)
                nb = [f"{r.d} ({int(r.dte)}d)" for _, r in below.iterrows()]
                na = [f"{r.d} ({int(r.dte)}d)" for _, r in above.iterrows()]
                msg = (f"{ticker}: no listed expiry inside the {span} DTE window. "
                       f"Nearest below: {nb[0] if nb else 'none'}. "
                       f"Nearest above: {na[0] if na else 'none'}.")
                if na:
                    days = int(above.iloc[0].dte) - (dte_max or 0)
                    if days > 0:
                        msg += f" That one enters the window in ~{days} days."
                print(json.dumps({"error": msg}))
                return 1
        else:
            window = ex[(ex.dte >= DTE_LO) & (ex.dte <= DTE_HI)]
            if not len(window):
                print(json.dumps({"error": f"{ticker}: no expiry between {DTE_LO}-{DTE_HI} DTE"}))
                return 1

        otype = OptionType.PUT if side == "put" else OptionType.CALL
        if side == "put":
            lo, hi = spot * 0.60, spot * 1.02
        elif is_leaps:
            lo, hi = spot * 0.35, spot * 1.05   # deep ITM to slightly OTM
        else:
            lo, hi = spot * 0.98, spot * 1.45
        rows = []
        for _, e in window.iterrows():
            ret, ch = q.get_option_chain(code, start=str(e.d), end=str(e.d), option_type=otype)
            if ret != RET_OK or not len(ch):
                continue
            ch = ch[(ch.strike_price >= lo) & (ch.strike_price <= hi)][["code", "strike_price"]]
            if not len(ch):
                continue
            q.subscribe(list(ch.code), [SubType.QUOTE])
            ret, s = q.get_stock_quote(list(ch.code))
            if ret != RET_OK:
                continue
            m = ch.merge(s, on="code", suffixes=("", "_q"))
            for _, x in m.iterrows():
                prem = float(x.last_price)
                if prem <= 0:
                    continue
                K = float(x.strike_price)
                try:
                    delta = float(x.delta)
                    iv = float(x.implied_volatility)
                    oi = int(x.open_interest)
                except Exception:
                    continue
                collateral = K * 100 if side == "put" else None
                base = K if side == "put" else spot
                row = dict(
                    expiry=str(e.d), dte=int(e.dte), strike=K, premium=prem,
                    dollars=round(prem * 100), delta=round(delta, 3), iv=round(iv, 1),
                    iv_rv=round(iv / rv, 2) if rv else None, oi=oi,
                    otm=round((K / spot - 1) * 100, 1),
                    collateral=collateral,
                    yld=round(prem / base * 100, 2),
                    ann=round(prem / base * 365 / e.dte * 100, 1))
                if is_leaps:
                    intrinsic = max(spot - K, 0.0)
                    extrinsic = prem - intrinsic
                    breakeven = K + prem
                    row.update(
                        intrinsic=round(intrinsic * 100),
                        extrinsic=round(extrinsic * 100),
                        extr_pct=round(extrinsic / prem * 100) if prem else None,
                        breakeven=round(breakeven, 2),
                        need=round((breakeven / spot - 1) * 100, 1))
                rows.append(row)

        held = holdings(ticker) if side == "call" else None
        # Delta band is a hard filter — Derrick only sells .20-.30, so a strike
        # outside it is never a suggestion, however attractive the premium.
        # Thin open interest is reported per-strike, not used to exclude.
        if is_leaps:
            # Rank by the share of the price that is time value. On a high-IV
            # name a .70-delta call can be 78% extrinsic — that is a directional
            # bet with heavy carry, not the stock substitute it looks like.
            band = [r for r in rows if d_lo <= abs(r["delta"]) <= d_hi]
            band.sort(key=lambda r: (r["extr_pct"] if r["extr_pct"] is not None else 999))
        else:
            band = [r for r in rows if DELTA_LO <= abs(r["delta"]) <= DELTA_HI]
            band.sort(key=lambda r: -r["ann"])
        near = []

        print(json.dumps(dict(
            ticker=ticker, side=side, spot=round(spot, 2), rv30=round(rv, 1) if rv else None,
            expiries=[{"d": str(e.d), "dte": int(e.dte)} for _, e in window.iterrows()],
            strikes=len(rows), band=band[:8], near=near, held=held,
            oi_min=OI_MIN,
            delta_lo=d_lo if is_leaps else DELTA_LO,
            delta_hi=d_hi if is_leaps else DELTA_HI,
            dte_min=dte_min if is_leaps else DTE_LO,
            dte_max=dte_max if is_leaps else DTE_HI,
            rule=(rule or {}).get("label") if is_leaps else None)))
        return 0
    finally:
        q.close()


if __name__ == "__main__":
    sys.exit(main())
