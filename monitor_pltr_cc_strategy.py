#!/usr/bin/env python3
"""
Monitor PLTR and alert when conditions favour switching to a long-dated CC
(3-6 month CC at $150+) over the bi-weekly roll-up approach.

Conditions checked:
  1. PLTR is in the $128–$148 range (close enough to $150 to be meaningful)
  2. Stock has been grinding sideways: 10-day price change < 5%
  3. Bi-weekly 0.2-delta premium is thin (< $1.20) — not worth the management overhead
  4. OR: IV is elevated on the Nov $150C (making longer-dated premium attractive)
"""

import subprocess, sys, json
from datetime import datetime

LOG = "/tmp/pltr_cc_strategy_log.txt"

def notify(title, msg):
    script = f'display notification "{msg}" with title "{title}" sound name "Ping"'
    subprocess.run(["osascript", "-e", script])

def log(msg):
    ts = datetime.now().strftime("%Y-%m-%d %H:%M")
    with open(LOG, "a") as f:
        f.write(f"{ts} | {msg}\n")

def run():
    try:
        from futu import OpenQuoteContext, RET_OK, KLType, AuType, OptionType
    except ImportError:
        notify("Monitor Error", "futu-api not installed")
        sys.exit(1)

    ctx = OpenQuoteContext(host="127.0.0.1", port=11111)

    # --- 1. Current price via snapshot (reliable) + 10-day history ---
    from datetime import date, timedelta
    today = date.today()
    start = (today - timedelta(days=20)).isoformat()
    end   = today.isoformat()

    ret_snap, snap_now = ctx.get_market_snapshot(["US.PLTR"])
    if ret_snap != RET_OK:
        log("ERROR: could not fetch snapshot")
        ctx.close()
        return
    price_now = float(snap_now.iloc[0]["last_price"])

    ret, hist, _ = ctx.request_history_kline(
        code="US.PLTR", ktype=KLType.K_DAY, autype=AuType.NONE,
        start=start, end=end, max_count=20
    )
    if ret != RET_OK or len(hist) < 10:
        log(f"ERROR: history fetch failed, price_now=${price_now:.2f}")
        ctx.close()
        return

    closes    = hist["close"].tolist()
    price_10d = closes[0] if len(closes) >= 10 else closes[0]
    change_10d = (price_now - price_10d) / price_10d * 100

    # --- 2. Bi-weekly 0.2-delta premium (next expiry ~14 DTE) ---
    # Approximate: get snapshot of the nearest 0.2-delta call
    ret2, exps = ctx.get_option_expiration_date(code="US.PLTR")
    biweekly_premium = None
    if ret2 == RET_OK:
        # Pick expiry 10-20 DTE
        upcoming = exps[(exps["option_expiry_date_distance"] >= 8) &
                        (exps["option_expiry_date_distance"] <= 22)]
        if len(upcoming) > 0:
            exp_date = upcoming.iloc[0]["strike_time"]
            ret3, chain = ctx.get_option_chain(
                code="US.PLTR", start=exp_date, end=exp_date, option_type=OptionType.CALL
            )
            if ret3 == RET_OK:
                # Filter strikes 5-12% OTM (rough 0.2 delta zone)
                target_lo = price_now * 1.05
                target_hi = price_now * 1.12
                candidates = chain[chain["strike_price"].between(target_lo, target_hi)]
                if len(candidates) > 0:
                    snap_codes = candidates["code"].tolist()[:5]
                    ret4, snap = ctx.get_market_snapshot(snap_codes)
                    if ret4 == RET_OK and len(snap) > 0:
                        # Pick closest to 0.20 delta
                        snap["delta_diff"] = (snap["option_delta"] - 0.20).abs()
                        best = snap.sort_values("delta_diff").iloc[0]
                        biweekly_premium = (best["bid_price"] + best["ask_price"]) / 2

    # --- 3. Nov $150C IV & premium ---
    nov_premium, nov_iv, nov_delta = None, None, None
    ret5, nov_chain = ctx.get_option_chain(
        code="US.PLTR", start="2026-11-20", end="2026-11-20", option_type=OptionType.CALL
    )
    if ret5 == RET_OK:
        row = nov_chain[nov_chain["strike_price"] == 150.0]
        if len(row) > 0:
            ret6, snap2 = ctx.get_market_snapshot([row.iloc[0]["code"]])
            if ret6 == RET_OK and len(snap2) > 0:
                s = snap2.iloc[0]
                nov_premium = (s["bid_price"] + s["ask_price"]) / 2
                nov_delta   = s.get("option_delta", None)
                nov_iv      = s.get("option_iv", None)

    ctx.close()

    # --- 4. Evaluate conditions ---
    in_range        = 128 <= price_now <= 148
    grinding        = abs(change_10d) < 5.0
    thin_premium    = biweekly_premium is not None and biweekly_premium < 1.20
    good_nov_prem   = nov_premium is not None and nov_premium > 5.0

    conditions_met = in_range and grinding and (thin_premium or good_nov_prem)

    # Build log line
    log_parts = [
        f"PLTR=${price_now:.2f}",
        f"10d_chg={change_10d:+.1f}%",
        f"biweekly_mid=${biweekly_premium:.2f}" if biweekly_premium else "biweekly=n/a",
        f"Nov150C_mid=${nov_premium:.2f}" if nov_premium else "Nov150C=n/a",
        f"nov_delta={nov_delta:.3f}" if nov_delta else "",
        f"alert={'YES' if conditions_met else 'no'}",
        f"reason={'in_range+grinding' if conditions_met else '-'}"
    ]
    log(" | ".join(x for x in log_parts if x))

    if conditions_met:
        reason_parts = []
        if in_range:    reason_parts.append(f"PLTR ${price_now:.2f} near $150")
        if grinding:    reason_parts.append(f"grinding ({change_10d:+.1f}% in 10 days)")
        if thin_premium: reason_parts.append(f"bi-wk premium thin (${biweekly_premium:.2f})")
        if good_nov_prem: reason_parts.append(f"Nov $150C = ${nov_premium:.2f}")

        notify(
            "PLTR: Consider Long-Dated CC",
            f"${price_now:.2f} | {' · '.join(reason_parts)} | Switch to Nov $150C?"
        )

if __name__ == "__main__":
    run()
