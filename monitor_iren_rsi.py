#!/usr/bin/env python3
"""Alert when IREN RSI(14) drops below 32 (oversold — LEAPS consideration)."""
import subprocess
import sys

def notify(title, msg):
    script = f'display notification "{msg}" with title "{title}" sound name "Ping"'
    subprocess.run(["osascript", "-e", script])

def get_rsi(code="US.IREN", period=14, lookback=60):
    try:
        from futu import OpenQuoteContext, RET_OK, KLType, AuType
    except ImportError:
        notify("Monitor Error", "futu-api not installed")
        sys.exit(1)

    ctx = OpenQuoteContext(host="127.0.0.1", port=11111)
    ret, data, _ = ctx.request_history_kline(
        code=code, ktype=KLType.K_DAY, autype=AuType.QFQ, max_count=lookback
    )
    ctx.close()

    if ret != RET_OK or data is None or len(data) < period + 1:
        return None, None

    close = data["close"]
    delta = close.diff()
    gain = delta.clip(lower=0).rolling(period).mean()
    loss = (-delta.clip(upper=0)).rolling(period).mean()
    rs = gain / loss
    rsi = (100 - 100 / (1 + rs)).iloc[-1]
    price = close.iloc[-1]
    return round(rsi, 2), round(price, 2)

if __name__ == "__main__":
    THRESHOLD = 32
    rsi, price = get_rsi()
    if rsi is None:
        notify("IREN RSI Monitor", "Could not fetch data — is OpenD running?")
        sys.exit(1)

    if rsi < THRESHOLD:
        notify(
            "IREN RSI OVERSOLD",
            f"RSI {rsi} < {THRESHOLD} | Price ${price} | Consider LEAPS entry"
        )
    # Always log current value
    with open("/tmp/iren_rsi_log.txt", "a") as f:
        from datetime import datetime
        f.write(f"{datetime.now().strftime('%Y-%m-%d %H:%M')} | RSI={rsi} | Price=${price} | alert={'YES' if rsi < THRESHOLD else 'no'}\n")
