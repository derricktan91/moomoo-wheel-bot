#!/usr/bin/env python3
"""
Wheel-entry analysis for a single ticker.

Applies Derrick's own documented wheel rules mechanically against live moomoo
data, and prices real cash-secured-put candidates off the live option chain.
This reports what the rules say and what the chain pays; it does not decide.

Rules encoded (from the wheel playbook, not invented here):
  * Entry window  : %B <= 0.50  (mid-to-lower Bollinger band)
  * Strike, normal vol : 10-15% OTM
  * Strike, high vol   : 1.28 sigma  (~90% chance of finishing OTM)
                         used for the 70%+ vol names (Space, OKLO/FLNC, etc.)
READ-ONLY. No order placement anywhere in this module.
"""
import math
import time
from datetime import datetime, timedelta

BB_PERIOD = 20
RSI_PERIOD = 14
HV_WINDOW = 30                  # trading days for realised vol
HIGH_VOL_CUTOFF = 0.60          # >=60% annualised -> 1.28-sigma sizing
NORMAL_OTM = 0.125              # midpoint of the 10-15% rule
SIGMA_MULT = 1.28
# 30 is the floor Derrick actually sells at — anything shorter pays too little
# for the assignment risk and was only ever noise in the output.
MIN_DTE, MAX_DTE = 30, 45
MAX_EXPIRIES = 2                # each extra expiry costs a chain + snapshot call
MIN_OI = 10                     # skip only the truly untradeable


def realised_vol(close, window=HV_WINDOW):
    """Annualised realised volatility from daily closes."""
    rets = (close / close.shift(1)).apply(math.log).dropna()
    if len(rets) < window:
        window = len(rets)
    return float(rets.tail(window).std() * math.sqrt(252))


def target_strike(price, vol, dte):
    """Strike the playbook points at, given the volatility regime."""
    if vol >= HIGH_VOL_CUTOFF:
        move = SIGMA_MULT * vol * math.sqrt(dte / 365.0)
        return price * (1 - move), "high", f"1.28σ ({move*100:.1f}% for {dte}d)"
    return price * (1 - NORMAL_OTM), "normal", f"{NORMAL_OTM*100:.1f}% OTM"


def analyse(ticker, ctx=None):
    """Return a dict of wheel-entry findings for `ticker`. Read-only."""
    from futu import (OpenQuoteContext, RET_OK, KLType, AuType, OptionType,
                      SortField)
    own = ctx is None
    if own:
        ctx = OpenQuoteContext(host="127.0.0.1", port=11111)
    try:
        code = f"US.{ticker}"
        today = datetime.now().date()

        ret, kl, _ = ctx.request_history_kline(
            code=code, start=(today - timedelta(days=200)).isoformat(),
            end=today.isoformat(), ktype=KLType.K_DAY, autype=AuType.QFQ,
            max_count=250)
        if ret != RET_OK or kl is None or len(kl) < BB_PERIOD + 1:
            return {"error": f"no price history for {ticker}"}

        close = kl["close"]
        mid = float(close.rolling(BB_PERIOD).mean().iloc[-1])
        sd = float(close.rolling(BB_PERIOD).std(ddof=0).iloc[-1])
        upper, lower = mid + 2 * sd, mid - 2 * sd
        delta_ = close.diff()
        gain = delta_.clip(lower=0).rolling(RSI_PERIOD).mean()
        loss = (-delta_.clip(upper=0)).rolling(RSI_PERIOD).mean()
        rsi = float((100 - 100 / (1 + gain / loss)).iloc[-1])
        vol = realised_vol(close)
        bar = str(kl["time_key"].iloc[-1])[:10]

        price = float(close.iloc[-1])
        ret, snap = ctx.get_market_snapshot([code])
        if ret == RET_OK and snap is not None and len(snap):
            lp = float(snap["last_price"].iloc[0])
            if lp > 0:
                price = lp

        pct_b = (price - lower) / (upper - lower) if upper > lower else 0.5

        out = {
            "ticker": ticker, "price": price, "bar": bar,
            "mid": mid, "upper": upper, "lower": lower,
            "pct_b": pct_b, "rsi": rsi, "vol": vol,
            "in_zone": pct_b <= 0.50,
            "candidates": [], "notes": [],
        }

        ret, exp = ctx.get_option_expiration_date(code=code)
        if ret != RET_OK or exp is None or not len(exp):
            out["notes"].append("no option chain available")
            return out

        exps = [(str(r["strike_time"])[:10], int(r["option_expiry_date_distance"]))
                for _, r in exp.iterrows()
                if MIN_DTE <= int(r["option_expiry_date_distance"]) <= MAX_DTE]
        if not exps:
            out["notes"].append(f"no expiries between {MIN_DTE} and {MAX_DTE} days out")
            return out

        for exp_date, dte in exps[:MAX_EXPIRIES]:
            tgt, regime, rule = target_strike(price, vol, dte)
            out["regime"], out["rule"] = regime, rule

            ret, chain = ctx.get_option_chain(
                code=code, start=exp_date, end=exp_date, option_type=OptionType.PUT)
            if ret != RET_OK or chain is None or not len(chain):
                continue

            chain = chain[chain["strike_price"] <= price]          # OTM puts only
            if not len(chain):
                continue
            chain = chain.assign(_d=(chain["strike_price"] - tgt).abs())
            picks = chain.nsmallest(6, "_d")

            ret, s = ctx.get_market_snapshot(list(picks["code"]))
            if ret != RET_OK or s is None:
                continue
            for _, o in s.iterrows():
                bid = float(o.get("bid_price") or 0)
                oi = int(o.get("option_open_interest") or 0)
                strike = float(o.get("option_strike_price") or 0)
                if bid <= 0 or oi < MIN_OI or strike <= 0:
                    continue
                collateral = strike * 100
                credit = bid * 100
                out["candidates"].append({
                    "expiry": exp_date, "dte": dte, "strike": strike,
                    "bid": bid, "credit": credit, "collateral": collateral,
                    "delta": float(o.get("option_delta") or 0),
                    "iv": float(o.get("option_implied_volatility") or 0),
                    "oi": oi,
                    "pct_otm": (1 - strike / price) * 100,
                    "ann_pct": (credit / collateral) * (365.0 / dte) * 100,
                    "eff_basis": strike - bid,
                })
            time.sleep(1)

        out["candidates"].sort(key=lambda c: (c["dte"], -c["strike"]))
        return out
    finally:
        if own:
            ctx.close()


if __name__ == "__main__":
    # Run as a subprocess so the caller can impose a hard timeout: some
    # underlyings (QQQ and other index ETFs) make OpenD's option-chain calls
    # block forever, and the bot's message loop is single-threaded — an
    # un-killable call in-process would freeze the whole bot.
    import json
    import sys
    try:
        print(json.dumps(analyse(sys.argv[1].upper())))
    except Exception as e:                      # noqa: BLE001
        print(json.dumps({"error": str(e)}))
