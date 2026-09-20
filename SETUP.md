# Setup

Running this needs three things wired together: a moomoo brokerage account with
OpenD running locally, a Telegram bot, and an environment file joining them.

Tested on macOS with Python 3.14. The scheduling is `launchd`-specific; the
scripts themselves are portable.

---

## 1. Dependencies

```bash
pip install -r requirements.txt
```

## 2. moomoo OpenD

The scripts talk to **OpenD**, moomoo's local API gateway. It must be running
before anything here works.

1. Download it from the [moomoo OpenAPI site](https://openapi.moomoo.com/) and
   install. A funded moomoo account is required — market data and trading
   entitlements follow your account, not this code.
2. Launch OpenD and log in with your moomoo credentials.
3. Confirm it is listening on port `11111` (the default).

Check it from Python:

```python
from futu import OpenQuoteContext, RET_OK
q = OpenQuoteContext(host="127.0.0.1", port=11111)
print(q.get_market_snapshot(["US.AAPL"])[0] == RET_OK)
q.close()
```

**OpenD stores your broker password in `OpenD.xml` in plaintext.** Restrict it:

```bash
chmod 600 /path/to/OpenD.xml
```

### Which `MOOMOO_SECURITY_FIRM`?

moomoo operates separate legal entities and the API needs to know yours:

| Entity | Value |
|---|---|
| Singapore | `FUTUSG` |
| United States | `FUTUINC` |
| Hong Kong | `FUTUSECURITIES` |
| Australia | `FUTUAU` |
| Japan | `FUTUJP` |
| Malaysia | `FUTUMY` |
| Canada | `FUTUCA` |

`MOOMOO_SECURITY_FIRM` is **required** — the scripts refuse to start without a
valid value rather than guessing, because the wrong entity fails authentication
deep inside the API with no useful error:

```
MOOMOO_SECURITY_FIRM must be set to your moomoo entity
(FUTUSG, FUTUINC, FUTUSECURITIES, FUTUAU, FUTUJP, FUTUMY, FUTUCA).
Got <unset>. See SETUP.md.
```

### Finding `MOOMOO_ACC_ID`

With OpenD running:

```bash
python3 -c "
from futu import *
t = OpenSecTradeContext(filter_trdmarket=TrdMarket.US, host='127.0.0.1',
                        port=11111, security_firm='FUTUSG')  # your entity
print(t.get_acc_list()[1][['acc_id','trd_env','acc_type']])
t.close()"
```

Use the `acc_id` whose `trd_env` is `REAL`.

## 3. Telegram bot

1. Message [@BotFather](https://t.me/BotFather) and send `/newbot`.
2. Pick a name and a username ending in `bot`.
3. Copy the token into `TELEGRAM_BOT_TOKEN`.

**Finding your chat id** — the bot tells you. Start it with
`TELEGRAM_CHAT_ID` left blank or wrong, send it any message, and read the log:

```
2026-09-18 12:53:23 | DENIED chat 123456789: '/start'
```

That number is your chat id. Put it in `TELEGRAM_CHAT_ID` and restart. The same
trick is how you collect a friend's id for `TELEGRAM_GUEST_CHAT_IDS`.

## 4. Environment file

```bash
cp .env.example ~/.moomoo_alerts.env
chmod 600 ~/.moomoo_alerts.env
```

Fill it in, then:

```bash
python3 telegram_bot.py
```

Send `/help`. If it answers, everything is wired.

---

## Optional: Claude CLI

`/analyse`, `/news` and free-text questions shell out to the
[Claude CLI](https://claude.com/claude-code). Without it, every other command
still works.

```bash
claude setup-token   # paste the sk-ant-oat01... value into the env file
```

`update_claude_token.sh` rotates that token without it touching shell history.

## Optional: scheduling

Templates are in `launchd/`. Replace the three placeholders and install:

```bash
for f in launchd/com.example.*.plist; do
  sed -e "s|__REPO_PATH__|$PWD|g" \
      -e "s|__PYTHON__|$(which python3)|g" \
      -e "s|__HOME__|$HOME|g" \
      "$f" > ~/Library/LaunchAgents/$(basename "$f")
done
launchctl load ~/Library/LaunchAgents/com.example.*.plist
```

| Job | Cadence | Notes |
|---|---|---|
| `moomoo-telegram-bot` | always on | `KeepAlive` + `ThrottleInterval 15` — restarts 15s after the bot fail-fast exits |
| `bb-telegram-alert` | every 30 min | US market hours only, in SGT |
| `spx-monday-signal` | weekly | Monday, before the US open |

The bot job is the one that matters: the bot exits after repeated poll
failures rather than retrying inside a dead process, and `KeepAlive` is what
turns that exit into a recovery.

Check status with `launchctl list | grep com.example`. On Linux, systemd
timers or cron do the same job.

---|---|
| `telegram_bot.py` | always on, `KeepAlive` |
| `bb_telegram_alert.py` | every 30 min during market hours |
| `spx_signal.py --send` | weekly |

On Linux, systemd timers or cron do the same job.

---

## What you will want to change

The defaults encode one person's strategy. The interesting constants:

| Where | What |
|---|---|
| `bb_telegram_alert.py` → `WATCHLIST` | 30 tickers, an AI/semis book. Replace with yours. |
| `option_chain.py` → `DELTA_LO/HI` | `0.20, 0.30` — the delta band suggested for puts and calls |
| `option_chain.py` → `LEAPS_*` | `0.65-0.75` delta, 365+ DTE, plus a per-ticker override table |
| `option_chain.py` → `DTE_LO/HI` | `30, 45` |
| `wheel_analysis.py` → `MIN_DTE/MAX_DTE` | `30, 45` — the floor `/wheel` will quote |
| `bb_telegram_alert.py` → `BB_PERIOD` | Bollinger lookback, 20 |

None of these are recommendations — they are the parameters one account happens
to trade.
