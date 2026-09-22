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

## The loop

```mermaid
flowchart TB
    cron["scheduler<br/>09:00 daily"] --> agent

    agent["<b>agent session</b><br/><i>SKILL.md is the prompt</i>"]

    agent -- "step 0, always first" --> broker[("broker<br/>positions · quotes · chains")]
    agent -- "decides what to look up" --> web["web search"]
    broker --> agent
    web --> agent

    agent --> dash["dashboard.html<br/>+ catalyst file"]
    dash --> email["email"]
    dash --> tg["Telegram"]

    wd["watchdog<br/>09:40 · 10:15 · 11:00"] -. "built today<br/>but not sent?" .-> dash
    wd --> email
    wd --> tg

    classDef ext fill:#eef2ff,stroke:#6366f1,color:#1e1b4b
    classDef ai fill:#f5f3ff,stroke:#7c3aed,color:#2e1065
    classDef out fill:#ecfdf5,stroke:#059669,color:#022c22
    classDef guard fill:#fef3c7,stroke:#d97706,color:#451a03
    class cron,broker,web ext
    class agent ai
    class dash,email,tg out
    class wd guard
```

Two arrows carry the whole idea.

**Broker data comes back into the agent before any research happens.** That is
step 0 in the prompt, and it is ordered that way because news about a stock is
worthless if the position underneath it is stale.

**The agent decides what to search.** Nobody wrote the query list. Reading that
one holding just fell 20% is what prompts the search that explains it, which
may prompt another. That loop — act, observe, decide, act again — is the whole
difference between this and the scheduled Bollinger scan, which runs just as
automatically and decides nothing.

The watchdog is deliberately **outside** the agent. It is fixed code asking one
question on a timer, because a guard that depends on the thing it guards is not
a guard: when the agent wedged, it was the agent's own retry logic that never
ran.

## Files

| File | Purpose |
|---|---|
| `SKILL.md` | The agent's prompt. This *is* the program. |
| `send_email.py` | Delivery via the Resend API, 5 attempts, fail-fast on 4xx |
| `briefing_watchdog.py` | Catches the case where the briefing is built but never sent |


## Running it

The prompt has placeholders: `__REPO_PATH__`, `__TASK_PATH__`,
`__MOOMOO_ACC_ID__`. Delivery needs `BRIEFING_TO_EMAIL` and a Resend API key at
`~/.config/portfolio-briefing/resend_api_key.txt`.

The strategy rules inside `SKILL.md` — the watchlist, the filters, the strike
sizing — are one account's, not recommendations. See the root README.
