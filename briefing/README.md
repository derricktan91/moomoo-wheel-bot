# The briefing agent

The Telegram bot is mostly deterministic Python. **This is the agentic part.**

Every morning a Claude Code session runs unattended against `SKILL.md` as its
prompt. It is not a single call — it decides what to research, runs its own
tools, produces artefacts and delivers them:

1. **Fetches live broker data first** — positions, account, quotes, chains,
   via Bash into the OpenD API. This is step 0 for a reason: web results about
   a stock are worthless if the position data underneath them is stale.
2. **Researches what it decides matters** — per-ticker news, macro and
   geopolitical risk, earnings dates, analyst moves. It chooses the searches;
   they are not a fixed list.
3. **Applies documented rules mechanically** — the Bollinger/RSI entry filter,
   volatility-adjusted strike sizing for high-vol names, a long-LEAPS time
   check, an index credit-spread signal.
4. **Renders an HTML dashboard** and writes a catalyst file the Telegram bot
   reads later, so the compact briefing carries current news.
5. **Delivers twice** — comprehensive by email, compact by Telegram — and is
   told to attempt both even if one fails.

## Files

| File | Purpose |
|---|---|
| `SKILL.md` | The agent's prompt. This *is* the program. |
| `send_email.py` | Delivery via the Resend API, 5 attempts, fail-fast on 4xx |
| `briefing_watchdog.py` | Catches the case where the briefing is built but never sent |

## What went wrong, and what it taught

Most of the engineering here is failure handling, because an unattended agent
fails in ways a supervised one does not.

**A hung tool call cost two days.** The agent finished the dashboard, then
opened a browser preview to look at it. The call never returned. That day's
email and Telegram were never sent — and the *next* day's run never started,
because the scheduler will not begin a run while the previous one is alive. One
optional verification step, placed before delivery, took out two days of
output. `SKILL.md` now forbids browser previews outright and states the general
rule: **nothing optional runs before delivery.**

**Retries could not help.** `send_email.py` already retried on failure. It was
never called — the run died upstream. Retry logic protects against a step that
fails, not against a step that is never reached. That distinction is why
`briefing_watchdog.py` exists: it runs separately on a schedule and asks one
question — *was a briefing built today but not delivered?* — then sends what is
missing, email and Telegram independently, up to five attempts each. It is
idempotent, so running it repeatedly is safe.

**Cost is a failure mode.** One run died on an account spend limit before
producing anything. The watchdog correctly reported it had nothing to deliver.
An agent that can fail for economic reasons rather than technical ones needs
that distinction visible in its logs.

**Wall-clock time is not runtime.** Runs varied from 4 seconds to 2.5 hours for
the same work. The machine was sleeping between wake windows, so the agent got
CPU in slices. Anything timing-sensitive has to tolerate that.

## Running it

The prompt has placeholders: `__REPO_PATH__`, `__TASK_PATH__`,
`__MOOMOO_ACC_ID__`. Delivery needs `BRIEFING_TO_EMAIL` and a Resend API key at
`~/.config/portfolio-briefing/resend_api_key.txt`.

The strategy rules inside `SKILL.md` — the watchlist, the filters, the strike
sizing — are one account's, not recommendations. See the root README.
