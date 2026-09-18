#!/usr/bin/env python3
"""Send the sector-grouped watchlist to Telegram.

Live prices/RSI/BB/realised-vol from moomoo OpenD; sectors and collateral rules
mirror the daily-briefing config. Splits across messages to stay under
Telegram's 4096-char limit.

    --dry-run   print, send nothing
    --link URL  append an artifact link to the last message
"""
import sys, datetime
import numpy as np, pandas as pd
from futu import OpenQuoteContext, KLType, RET_OK
from bb_telegram_alert import load_env, send_telegram, log

ARTIFACT = "https://claude.ai/code/artifact/c1140c6f-6ac5-4ae2-8174-b67b642053c9"

# (sector, [(ticker, business, wheel_ok)]) — wheel_ok mirrors the collateral rule
SECTORS = [
    ("AI SEMICONDUCTORS", [
        ("NVDA", "GPUs for AI training/inference", False),
        ("AMD",  "CPUs + MI-series accelerators",  False),
        ("AVGO", "Custom AI ASICs, networking",    False),
        ("INTC", "x86 CPUs + foundry",             True)]),
    ("MEMORY / HBM", [
        ("MU",   "DRAM and HBM",                   False),
        ("DRAM", "Roundhill Memory ETF",           None)]),
    ("AI INFRASTRUCTURE", [
        ("CRWV", "GPU cloud for AI labs",          True),
        ("IREN", "BTC mining -> AI datacentres",   True),
        ("NBIS", "AI cloud, Yandex spinoff",       False)]),
    ("CONNECTIVITY / EMS", [
        ("CRDO", "SerDes + optical DSP",           False),
        ("GLW",  "Optical fibre, display glass",   True),
        ("CLS",  "Contract mfg for hyperscalers",  False)]),
    ("AI SOFTWARE", [
        ("MSFT", "Azure + enterprise software",    False),
        ("PLTR", "Data/AI platforms, gov + comml", True)]),
    ("FINTECH / HEALTHCARE", [
        ("SOFI", "Digital bank, lending, brokerage", True),
        ("ISRG", "da Vinci surgical robotics",     False)]),
    ("SPACE / DEFENCE", [
        ("RKLB", "Small launch + spacecraft",      True),
        ("ASTS", "Direct-to-cell satellite",       True),
        ("LUNR", "Lunar landers",                  True),
        ("RDW",  "Space infrastructure",           True),
        ("PL",   "Earth-observation imagery",      True),
        ("VOYG", "Space infra + defence",          True),
        ("KTOS", "Defence drones, hypersonics",    True),
        ("SPCX", "SpaceX - sector bellwether",     None)]),
    ("BENCHMARKS", [
        ("QQQ",  "Nasdaq-100 + LEAPS entry rule",  None),
        ("SPY",  "S&amp;P 500 benchmark",          None)]),
]
HELD = {"PLTR": 110, "SOFI": 320, "MSFT": 5.3, "ISRG": 3, "IREN": 10, "DRAM": 10}


def rsi(c, n=14):
    d = c.diff(); up = d.clip(lower=0); dn = -d.clip(upper=0)
    return 100 - 100 / (1 + up.ewm(alpha=1/n, adjust=False).mean()
                            / dn.ewm(alpha=1/n, adjust=False).mean())


def fetch():
    tickers = [t for _, rows in SECTORS for t, _, _ in rows]
    end = datetime.date.today(); start = end - datetime.timedelta(days=200)
    q = OpenQuoteContext(host="127.0.0.1", port=11111)
    snaps = []
    for i in range(0, len(tickers), 20):
        r, s = q.get_market_snapshot([f"US.{t}" for t in tickers[i:i+20]])
        if r == RET_OK:
            snaps.append(s)
    sn = pd.concat(snaps); sn["t"] = sn.code.str.replace("US.", "", regex=False)
    out = {}
    for t in tickers:
        row = sn[sn.t == t]
        if row.empty:
            continue
        x = row.iloc[0]; px = float(x.last_price)
        d = {"px": px,
             "chg": (px - x.prev_close_price) / x.prev_close_price * 100,
             "collat": round(px * 0.90, 0) * 100}
        r2, k, _ = q.request_history_kline(f"US.{t}", start=str(start), end=str(end),
                                           ktype=KLType.K_DAY, max_count=500)
        if r2 == RET_OK and len(k) >= 25:
            c = k["close"].astype(float)
            sma = c.rolling(20).mean(); sd = c.rolling(20).std()
            lb = (sma - 2*sd).iloc[-1]; ub = (sma + 2*sd).iloc[-1]
            d["rsi"] = float(rsi(c).iloc[-1])
            d["pctb"] = float((px - lb) / (ub - lb) * 100)
            d["rvol"] = float(c.pct_change().tail(20).std() * np.sqrt(252) * 100)
            d["signal"] = d["rsi"] <= 35 or px <= lb * 1.02
        out[t] = d
    q.close()
    return out


def build(data):
    """Return a list of message strings, each under Telegram's 4096 limit."""
    stamp = datetime.datetime.now().strftime("%d %b %Y %H:%M SGT")
    fired = [t for t, d in data.items() if d.get("signal")]
    wheel = sum(1 for _, rows in SECTORS for _, _, w in rows if w is True)

    head = (f"<b>WATCHLIST BY SECTOR</b>\n<i>{stamp} - live moomoo</i>\n\n"
            f"{len(data)} tickers - {len(HELD)} held - {wheel} wheel-viable\n"
            f"Signals firing: <b>{len(fired) if fired else 0}</b>"
            f"{' (' + ', '.join(fired) + ')' if fired else ' - dual filter needs RSI&lt;=35 or lower BB'}")

    msgs, cur = [], head
    for sector, rows in SECTORS:
        block = f"\n\n<b>{sector}</b>"
        for t, biz, wheel_ok in rows:
            d = data.get(t)
            if not d:
                block += f"\n<code>{t:<5}</code> no data"
                continue
            chg = f"{d['chg']:+.2f}%"
            tags = []
            if t in HELD:
                tags.append(f"held {HELD[t]:g}")
            if wheel_ok is True:
                tags.append(f"wheel ${d['collat']/1000:.1f}k")
            elif wheel_ok is False:
                tags.append(f"too big ${d['collat']/1000:.0f}k")
            hot = " *" if d.get("signal") else ""
            block += (f"\n<code>{t:<5} {d['px']:>8.2f} {chg:>7}"
                      f"  RSI {d.get('rsi', 0):>4.1f}  %B {d.get('pctb', 0):>3.0f}"
                      f"  vol {d.get('rvol', 0):>3.0f}%</code>{hot}"
                      f"\n      <i>{biz}</i>"
                      + (f"\n      {' | '.join(tags)}" if tags else ""))
        if len(cur) + len(block) > 3700:
            msgs.append(cur); cur = block.lstrip()
        else:
            cur += block
    msgs.append(cur)
    return msgs


def main():
    dry = "--dry-run" in sys.argv
    link = ARTIFACT
    if "--link" in sys.argv:
        link = sys.argv[sys.argv.index("--link") + 1]

    cfg = load_env()
    data = fetch()
    msgs = build(data)
    if link:
        msgs[-1] += f"\n\n<a href=\"{link}\">Full table with analyst targets</a>"

    for i, m in enumerate(msgs, 1):
        tag = f"[{i}/{len(msgs)}] {len(m)} chars"
        if dry:
            print(f"\n{'='*66}\n{tag}\n{'='*66}\n{m}")
        else:
            ok = send_telegram(cfg, m)
            print(f"{tag} -> {'sent' if ok else 'FAILED'}")
            if not ok:
                log("watchlist_telegram: send failed")
                sys.exit(1)


if __name__ == "__main__":
    main()
