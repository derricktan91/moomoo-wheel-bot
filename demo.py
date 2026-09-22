#!/usr/bin/env python3
"""Run the strategy maths on synthetic prices — no broker, no keys, no network.

    python3 demo.py

Everything the live system decides with is pure functions over a price series.
This exercises them on a fabricated stock so the logic can be inspected without
a funded brokerage account.
"""

import math
import os
import sys

import numpy as np
import pandas as pd

os.environ.setdefault("MOOMOO_SECURITY_FIRM", "FUTUSG")
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from bb_telegram_alert import bollinger_and_rsi, pct_b_of, classify   # noqa: E402
from wheel_analysis import realised_vol, target_strike                # noqa: E402
from spx_signal import put_price, strike_for_delta                    # noqa: E402

rng = np.random.default_rng(7)


def synthetic(days=120, start=100.0, vol=0.45, drift=-0.15):
    """A price path with a mild downtrend, so the entry filter has something to see."""
    dt = 1 / 252
    steps = rng.normal((drift - vol**2 / 2) * dt, vol * math.sqrt(dt), days)
    return pd.Series(start * np.exp(np.cumsum(steps)))


def rule(label, ok):
    return f"{'PASS' if ok else 'fail'}  {label}"


print("=" * 66)
print("  1. ENTRY FILTER — Bollinger(20,2) + RSI(14)")
print("=" * 66)
close = synthetic()
mid, upper, lower, rsi, last = bollinger_and_rsi(close)
pb = pct_b_of(last, lower, upper)
zone, _, desc = classify(pb)
print(f"  price {last:>8.2f}   bands  {lower:.2f} / {mid:.2f} / {upper:.2f}")
print(f"  %B    {pb:>8.2f}   RSI {rsi:.1f}   zone: {desc}")
print(f"  {rule('%B <= 0.50 (selling into weakness, not strength)', pb <= 0.50)}")
print(f"  {rule('RSI <= 35 (momentum stretched)', rsi <= 35)}")

print()
print("=" * 66)
print("  2. STRIKE SIZING — one rule cannot fit every name")
print("=" * 66)
vol = realised_vol(close)
print(f"  this series realised {vol*100:.0f}% annualised\n")
print(f"  {'vol':>6}{'regime':>9}{'strike (30d)':>14}{'% OTM':>8}   rule applied")
for v in (0.25, vol, 0.75, 1.10):
    tgt, regime, why = target_strike(100.0, v, 30)
    print(f"  {v*100:>5.0f}%{regime:>9}{tgt:>14.2f}{(tgt/100-1)*100:>7.1f}%   {why}")
print("\n  Below 60% vol the playbook uses a flat 12.5% OTM. Above it, the strike")
print("  is set at 1.28 sigma so the assignment probability stays ~10% instead")
print("  of drifting with volatility — at 110% vol, 12.5% OTM is well under")
print("  half a standard deviation.")

print()
print("=" * 66)
print("  3. OPTION PRICING — index-scale, matching a real SPX spread")
print("=" * 66)
S, T, v = 7700.0, 38 / 365, 0.16
k20 = strike_for_delta(S, T, v, target=0.20)
print(f"  SPX {S:,.0f}, {int(T*365)} DTE, IV {v*100:.0f}%")
print(f"  0.20-delta put: {k20:,.0f}  ({(k20/S-1)*100:+.1f}% OTM)\n")
print(f"  {'width':>6}{'credit':>9}{'max loss':>10}{'RoR':>8}{'breakeven win%':>16}")
for w in (25, 50, 75):
    cr = (put_price(S, k20, T, v) - put_price(S, k20 - w, T, v)) * 100
    ml = w * 100 - cr
    print(f"  {w:>6}{cr:>9,.0f}{ml:>10,.0f}{cr/ml*100:>7.1f}%{ml/(ml+cr*0.5)*100:>15.1f}%")
print("\n  Credit does not scale with width; max loss does. Two 25-wides beat")
print("  one 50-wide at the same risk — which is why the chain tools cap width.")

print()
print("=" * 66)
print("  4. THE POINT — a high win rate is not an edge")
print("=" * 66)
# Flat IV vs the real smile. A chain prices the further-OTM long leg HIGHER,
# so pricing both legs at one vol overstates the credit — by ~26% against a
# live chain on 2026-09-22. This is the defect the README documents, shown here.
cr_flat = (put_price(S, k20, T, v) - put_price(S, k20 - 25, T, v)) * 100
# 25 SPX points is ~0.32% of the index; the measured skew slope is ~0.88 vol
# points per 1% further OTM, so the long leg sits ~0.28 vol points higher.
cr_skew = (put_price(S, k20, T, v) - put_price(S, k20 - 25, T, v + 0.0028)) * 100

print(f"  {'':<22}{'credit':>9}{'max loss':>10}{'breakeven win%':>16}")
for label, cr in (("flat IV (wrong)", cr_flat), ("real smile", cr_skew)):
    ml = 2500 - cr
    print(f"  {label:<22}{cr:>9,.0f}{ml:>10,.0f}{ml/(ml+cr*0.5)*100:>15.1f}%")

ml = 2500 - cr_skew
tp = cr_skew * 0.5
print(f"\n  Priced properly: win +${tp:,.0f}, lose -${ml:,.0f}  ({ml/tp:.1f}x)")
print(f"  You must win {ml/(ml+tp)*100:.1f}% of the time just to break even.\n")
print(f"  {'win rate':>10}{'EV / trade':>14}{'over 100 trades':>18}")
for wr in (0.90, 0.92, 0.93, 0.94, 0.96):
    ev = wr * tp - (1 - wr) * ml
    print(f"  {wr*100:>9.0f}%{ev:>+14,.0f}{ev*100:>+18,.0f}")
print("\n  The observed win rate over 20 years was 93.0%. Breakeven is 93.1%.")
print("  One point of win rate flips the sign, and nobody can measure their own")
print("  win rate to that precision — which is how a backtest reporting +$892/yr")
print("  became -$268/yr once the skew was priced in. See the README.")
