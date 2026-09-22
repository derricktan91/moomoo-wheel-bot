---
name: daily-portfolio-briefing-9am
description: Daily 9am portfolio briefing with latest prices, news, and risk updates for wheel strategy holdings — delivered twice, comprehensive by email and compact by Telegram
---

Run the daily AI portfolio briefing for the owner's wheel strategy portfolio.

Use the `ai-portfolio-daily-briefing` skill by invoking: /ai-portfolio-daily-briefing

**Two deliverables every run** — a comprehensive HTML dashboard by email, and a compact companion briefing to
Telegram. Both are mandatory; see the "Delivery — TWO briefings every run" section at the end for the exact
order and scripts. The run is not complete until both have been sent (or a send failure has been reported).

## MANDATORY STEP 0 — Fetch live moomoo OpenD data FIRST (before any web searches)

**Real account is FUTUSG, NOT FUTUINC. Always use these exact params:**
- `security_firm=SecurityFirm.FUTUSG`
- `acc_id=__MOOMOO_ACC_ID__`
- `trd_env=TrdEnv.REAL`

Run this Python snippet via Bash tool:

```python
import json
from futu import OpenSecTradeContext, TrdMarket, TrdEnv, RET_OK, SecurityFirm

ACC_ID = __MOOMOO_ACC_ID__
ctx = OpenSecTradeContext(filter_trdmarket=TrdMarket.US, host='127.0.0.1', port=11111, security_firm=SecurityFirm.FUTUSG)

def safe_float(v, d=0.0):
    try: return float(v)
    except: return d

ret_acc, acc = ctx.accinfo_query(trd_env=TrdEnv.REAL, acc_id=ACC_ID)
ret_pos, pos = ctx.position_list_query(trd_env=TrdEnv.REAL, acc_id=ACC_ID)
ret_ord, ord_data = ctx.history_order_list_query(trd_env=TrdEnv.REAL, acc_id=ACC_ID)
ctx.close()

result = {}
if ret_acc == RET_OK:
    r = acc.iloc[0]
    result['account'] = {
        'total_assets_hkd': safe_float(r.get('total_assets')),
        'usd_assets':        safe_float(r.get('usd_assets')),
        'us_cash':           safe_float(r.get('us_cash')),
        'fund_assets_hkd':   safe_float(r.get('fund_assets')),
    }
if ret_pos == RET_OK:
    result['positions'] = [{
        'code': str(r['code']), 'name': str(r.get('stock_name','')),
        'qty': safe_float(r['qty']), 'cost_price': safe_float(r.get('cost_price')),
        'nominal_price': safe_float(r.get('nominal_price')),
        'market_val': safe_float(r.get('market_val')), 'pl_val': safe_float(r.get('pl_val')),
        'pl_ratio': safe_float(r.get('pl_ratio')), 'today_pl': safe_float(r.get('today_pl_val')),
        'side': str(r.get('position_side','')),
    } for _, r in pos.iterrows()]
if ret_ord == RET_OK and len(ord_data) > 0:
    result['recent_orders'] = ord_data[['code','stock_name','trd_side','qty','price','create_time']].head(10).to_dict(orient='records')
print(json.dumps(result, indent=2))
```

Use `nominal_price` as the **live price** for each position. All prices, P&L, and option status in the briefing must come from this live data — never use yesterday's or hardcoded values.

The skill will then:
1. Use live moomoo positions (Step 0 above) for all portfolio data
2. Web-search for current news, analyst updates, and RSI/BB estimates for all holdings
3. Render a dark-themed HTML dashboard with per-stock cards showing live price, RSI, news, tags, and CSP/wheel strategy opportunity blocks
4. Include a macro bar, live portfolio table, summary table

Portfolio tickers (wheel/CSP strategy): NVDA, AMD, AVGO, INTC, AMKR (AI semis — AMKR = Amkor Technology, watchlist addition 2026-08-13, not held; OSAT/advanced-packaging pure play, the assembly-and-test bottleneck for AI accelerators, so it reads the same capex cycle as NVDA/AVGO from the back end. **IS a wheel/CSP candidate on collateral** at ~$56/share = ~$4,700 per contract — apply the normal BB+RSI dual filter — BUT flag option liquidity on every recommendation: total OI across near-dated puts is only ~3,700 with a median bid/ask spread near $0.85, so quote the specific strike's OI and spread and prefer strikes with OI >100; if the best available strike is thinner than that, say so and recommend waiting rather than crossing a wide spread), MU, DRAM (memory/HBM — DRAM = Roundhill Memory ETF, held position; ETF, so no CSP dual-filter recommendation — track price/RSI/BB + memory-sector news as context, card in Memory/HBM section and a row in the summary table), CRWV, IREN, NBIS (AI infra), CRDO, GLW, CLS (connectivity — CLS = Celestica, watchlist addition 2026-08-11, not held; NOT a wheel/CSP candidate at ~$400+/share = $40K+ collateral per contract, same capital-size exclusion as MU/ISRG — track price/RSI/BB, analyst PTs and AI-compute/EMS news as context, card in the Connectivity section + a row in the summary table), ANET (Arista Networks — watchlist addition 2026-09-17, not held; AI-datacentre ethernet switching, so it sits in the Connectivity section beside CRDO/GLW/CLS/COHR and reads the same AI-interconnect cycle from the switch side rather than the optics/SerDes side. **IS a wheel/CSP candidate at the owner's explicit request (2026-09-17)** — but at ~$198/share a 12% OTM contract is ~$17,400 collateral, which currently exceeds his securities equity, so on every CSP recommendation state the strike's collateral and whether it fits the account rather than quoting a strike he cannot fund. Normal-vol name (RV30 ~49%), so apply the standard 10-15% OTM rule, not the 1.28-sigma sizing used for the Space/Defence and OKLO/FLNC names), COHR (Coherent Corp — watchlist addition 2026-08-19, not held; optical transceivers, lasers and photonics for AI datacentre interconnect, so it belongs in the Connectivity section beside CRDO/GLW/CLS and reads the same 800G→1.6T optics cycle. **NOT a wheel/CSP candidate** at ~$306/share = ~$26K collateral per contract, same capital-size exclusion as MU/ISRG/CLS/CRDO — track price/RSI/BB, analyst PTs and optics-cycle news as context only. Two things to carry in the card: (i) it is a **very high-beta expression of the AI optics trade** — it ran +105% YTD into Aug 2026 then fell 11.6% on 10 Aug in an "optics vs memory" rotation and another ~12.8% on 19 Aug, both on positioning rather than fundamentals; (ii) FQ4 2026 **beat** ($1.74 EPS vs $1.61, revenue $2.05B vs $1.99B) with FQ1 2027 guidance **above** consensus ($2.2–2.4B revenue, $1.85–2.05 EPS) and management targeting >$3B quarterly revenue by end-FY2027 — so drawdowns here have so far been sell-the-news and rotation, not thesis breaks. Say which one is driving any given move rather than assuming), MSFT, PLTR (AI software), ISRG (Intuitive Surgical — held position, bought 2026-07-22 @ $344.80/3sh; own-card in a dedicated "Watchlist Addition" section + summary table row; NOT a wheel/CSP candidate — collateral per contract too large for the $17K account — track price/RSI/BB and news as context only, same treatment as DRAM/MU on the capital-size exclusion), RKLB (Rocket Lab — watchlist addition 2026-08-11, not held; **IS a full wheel/CSP candidate** at ~$78/share = ~$7,100 collateral per contract, which fits the account — apply the normal BB+RSI dual filter and give a CSP strike recommendation when it fires, unlike the context-only names above; own card in a dedicated "Space / Defence" section + a row in the summary table), and the rest of the Space / Defence track alongside it — ASTS (AST SpaceMobile, direct-to-cell satellite), LUNR (Intuitive Machines, lunar landers), RDW (Redwire, space infrastructure), PL (Planet Labs, earth observation), VOYG (Voyager Technologies, space infra/defence), KTOS (Kratos, defence drones/hypersonics) — all added 2026-08-11, none held. Every one of these fits the account on collateral (~$1.3K–$8.0K per contract at current prices), so they are **full wheel/CSP candidates like RKLB**, not context-only names. Use SPCX (SpaceX) as the sector bellwether in the section intro — recent IPO, thin history, do not run the dual filter on it.

**AI Power / Energy track (added 2026-08-13, none held).** Own card section titled "AI Power" plus summary-table rows. The thesis is datacentre electricity demand — AI compute is power-constrained, so these read the same capex cycle as the semis from the grid side. Two sub-groups with DIFFERENT strike rules:

- **Normal-vol independent power producers and fuel — VST (Vistra, nuclear + gas IPP, ~$147, ~$12.5K collateral), NRG (NRG Energy, IPP, ~$121, ~$10.3K), CCJ (Cameco, uranium, ~$99, ~$8.4K).** These run 51–53% realised vol, so the **standard 10–15% OTM CSP rule applies** — do NOT use the Space/Defence 1.28-sigma sizing on them, it would push strikes absurdly far out. All three are full wheel/CSP candidates that fit the account.
- **High-vol nuclear-SMR and storage — OKLO (Oklo, small modular reactors, ~$45, ~$3.8K collateral, 95% vol), FLNC (Fluence Energy, grid storage, ~$13, ~$1.1K collateral, 127% vol).** These are full wheel/CSP candidates on collateral but run 95–127% realised vol, so they **MUST use the same 1.28-sigma volatility-adjusted strike rule as the Space / Defence track** — compute `sigma_30d = rvol_ann × sqrt(30/365)`, set the strike at 1.28 × sigma_30d OTM, and show the realised vol and implied assignment probability on the card.
- **FPS (Forgent Power Solutions) — watchlist addition 2026-09-17, not held.** Electrical distribution equipment (switchgear, transformers) for data centres, the grid and heavy industry — the datacentre-power build-out from the equipment side, beside VRT. IPO'd 2026-02-05 at $27 (NYSE), controlled by private-equity sponsor Neos Partners, so flag any secondary-offering or lock-up news as supply overhang. **Full wheel/CSP candidate** (~$35/share, ~$3K collateral) but runs ~74-83% realised vol, so it **MUST use the 1.28-sigma strike rule** like OKLO/FLNC, not the 10-15% OTM rule. Two data caveats to state on the card: (i) only ~7 months of price history, so there is no 200-day average — skip any 200d trend check and say why; (ii) the option chain is **monthly-only** (Oct-16, Nov-20, Jan-15...), so there is often no expiry inside 30-45 DTE — name the nearest monthly and its DTE rather than forcing a strike.
- **Context-only on capital size — BE (Bloom Energy, fuel cells, ~$237/share = ~$20.2K per contract), CEG (Constellation Energy, nuclear IPP, ~$279 = ~$23.7K), VRT (Vertiv, datacentre power and cooling, ~$288 = ~$24.5K).** Track price/RSI/BB and news, no CSP recommendation — same capital-size exclusion as MU/ISRG/CLS. the owner asked for BE specifically; it is in the section for thesis coverage but does not fit one contract.

Note when first rendering this section: on 2026-08-13 the power complex was the **least extended** group on the whole board — NRG RSI 41.3 / %B 26%, FLNC RSI 41.8 / %B 34%, VST RSI 45.8 / %B 37% — while every AI-infra name sat at %B 85–113%. Worth flagging in the briefing whenever that divergence persists, since it is where the dual filter is most likely to fire first.

**MANDATORY volatility-adjusted strike rule for the Space / Defence track.** These names run 70–120% annualised realised vol versus 25–40% for the semis, so the standard "10–15% OTM" CSP rule does NOT give equivalent safety — at 96% vol a 15% OTM 30-DTE strike is only ~0.5σ, roughly a **30% chance of finishing ITM**. Every briefing, compute 20-day realised vol for each Space name and size the recommended strike off it: `sigma_30d = rvol_ann × sqrt(30/365)`, then set the strike at **1.28 × sigma_30d OTM** for a ~10% assignment probability (the same protection the 10–15% rule gives on a normal-vol name). Show the strike, the % OTM it implies, the realised vol used, and the implied assignment probability on the card. Never quote a bare 10–15% OTM strike for these tickers. Also flag that assignment on a 100%-vol name means owning a position that can move 30%+ in a month — position sizing matters more here than premium yield.

Market benchmarks (ALWAYS include): QQQ and SPY — fetch from moomoo OpenD (`US.QQQ`, `US.SPY`) with the same RSI/BB computation as the stocks, and render a Market Pulse strip under the header (close, day %, 5d %, RSI, BB position, one-line interpretation of how the whole market is reacting to the day's news). Also list them as the first two rows of the summary table and include them in the email. No CSP/CC recommendations on these — context only.

QQQ LEAPS entry check (ALWAYS run — the owner's rulebook): if QQQ's last-session day change ≤ −1.00%, flag 🟢 ENTRY MET in the Market Pulse strip and pull the QQQ call chain from moomoo OpenD (get_option_expiration_date → DTE ≥ 650, ideal 650–800; get_option_chain CALL + get_market_snapshot for Greeks) and report the best-fit contract (delta closest to 0.80, range 0.75–0.85): expiry, strike, last, delta, IV, OI. Signal only — NO sizing or affordability commentary; the owner decides that himself. If not met, one dim line: "QQQ LEAPS: no entry — day −X.XX%". Roll Out check: if the moomoo positions include a long QQQ call, snapshot its delta — if Δ ≥ 0.90, flag 🔁 ROLL OUT (contract, delta, and the current Δ0.80/DTE 650–800 replacement); Δ 0.85–0.90 gets a dim "approaching roll" note. Include entry status and any roll signal in the email. Details in the ai-portfolio-daily-briefing skill's "QQQ LEAPS Entry Check" section.

**MANDATORY — SPX bull-put-spread entry check (every briefing).** the owner runs a monthly SPX put credit spread (0.20Δ short, $25 wide, 30–35 DTE, 50% take-profit, NO stop — the width is the stop). Run `python3 __REPO_PATH__/spx_signal.py` via Bash and parse the JSON on its last stdout line. Render a compact "SPX spread" strip under the Market Pulse: the **verdict** (GO / OK / WAIT / CLOSED) with the six factor lines (vol regime, day of week, week of month, prior-day move, drawdown from 60d high, CPI placement) as ✅/▫️/❌, plus the two suggested strike pairs for 30 and 35 DTE with their modelled credit and max loss. Rules that came out of the 2019–2023 SPY backtest and must be respected in the wording: (i) **the 17–24% 60-day-realised-vol band lost money** — never soften a WAIT that is driven by it; (ii) **Monday in the last week of the month is the ideal entry** — if the verdict is not GO, always print the next ideal Monday from the JSON `next_ideal`; (iii) **no stop-loss** — never suggest one; the backtest showed every stop rule cut profit 50–80% because the short strike is touched ~32% of months but finishes ITM only ~7%; (iv) SPX is not quotable via OpenD, so the strikes are Black-Scholes estimates from SPY×10 at IV = realised×1.15 — say so in one line and tell him to verify in the moomoo `.SPX` chain. Include the strip in the email; the Monday launchd job (`com.example.spx-monday-signal`, 20:45 SGT) already sends the same check to Telegram, so the briefing should NOT re-send it to Telegram on Mondays.

**MANDATORY — long-LEAPS time check (every briefing).** For any LONG call in the moomoo positions with an expiry more than 6 months out, compute days-to-expiry and the extrinsic portion of its mark (`mark − max(spot − strike, 0)`), and render a one-line status. Escalate as DTE falls:

- **DTE > 400** — dim one-liner only: contract, DTE, delta, mark, P&L%.
- **DTE 365–400** — amber "⏳ approaching the 12-month line" note. This is the point the owner chose as his hard review trigger (decision made 2026-08-14): theta accelerates below 12 months, and holding a LEAPS into its final year gives back gains even when the thesis is intact.
- **DTE < 365** — 🔴 **REVIEW NOW** callout at the top of the position section, every day until it is closed or rolled. State the extrinsic value still at risk in dollars, since that is the amount decaying to zero.

Apply this to whatever long LEAPS the book holds at the time — read the contract, entry price and expiry from the live moomoo positions rather than from any hard-coded example. Where a position has a documented per-position exit plan (take-profit level or review date), state it alongside the DTE status; the delta-0.90 roll rule from [[project-qqq-leaps-rules]] applies only to QQQ and must not be substituted for another name's plan. Also note in the briefing that time decay raises the bar on any fixed mark target: the underlying has to travel further to hit the same option price as expiry approaches.

_As of 2026-08-21 the book holds no long LEAPS (the SOFI Dec 2027 $13C was closed), so this check currently has nothing to report — skip it silently until a new long-dated call is opened._

Flag any new risks, earnings surprises, analyst downgrades, or macro events that could affect open positions or upcoming CSP/CC strikes. Highlight any tickers where the wheel strategy opportunity has changed significantly since the prior day.

## MANDATORY — Macro & Geopolitical News section (every briefing)

Always include a dedicated "Macro & Geopolitical" section in the HTML dashboard (place it after the Live Portfolio Panel, same spot as the existing macro bars) — not just AI-sector-specific macro (capex/tariffs), but broader macro/geopolitical drivers each day:

- **Iran / Middle East / Strait of Hormuz**: current state of the conflict, any Hormuz or Red Sea shipping disruptions, oil price moves (Brent/WTI) and the read-through to inflation and tech-capex confidence.
- **Fed / FOMC**: current rate, next meeting date, hawkish/dovish tone, rate-hike/cut odds from fed funds futures.
- **Other standing macro checks**: hyperscaler capex announcements, analyst upgrades/PT changes on semis, NDX/S&P rebalancing, VIX level — per the existing "Macro Factors to Always Check" list in the ai-portfolio-daily-briefing skill.

Web-search for each of these every run (e.g. "Iran Israel Strait of Hormuz oil news [date]", "FOMC Fed rate decision news [date]") even when nothing dramatic is happening — render a shorter/dimmer bar noting "no major escalation" rather than omitting the section. Use red macro-bar styling for active risks, green for tailwinds. Include a one-line rollup of the most important item in the header summary line if it's market-moving (e.g. war escalation, oil spike, surprise Fed move).

## Delivery — TWO briefings every run

the owner gets a comprehensive one by email and a compact one on Telegram. **Both are mandatory.** Run in this
order; the Telegram message reads the catalyst file, so writing it first is what keeps its news current:

| # | Action | Script |
|---|--------|--------|
| A | Save the rendered dashboard HTML | — |
| B | Write `~/.moomoo_catalysts.json` | — |
| C | Send the email (comprehensive) | `send_email.py` |
| D | Send the Telegram briefing (compact) | `portfolio_telegram_briefing.py` |

C and D are independent: if one fails, still do the other and report the failure in the output. Never skip D
because C succeeded, or vice versa.

**NEVER open a browser preview of the dashboard — not `preview_start`, not `navigate`, not a screenshot,
not at any point in the run.** On 2026-09-21 a `preview_start` call placed right after step A never
returned. The session hung for 29 hours, which meant (i) that day's email and Telegram were never sent,
and (ii) the 2026-09-22 run never started at all, because the scheduler will not begin a run while the
previous one is still alive. One hung tool call cost two days of briefings.

The dashboard needs no visual check. Verify it the cheap way instead — after writing the file, confirm
with Bash that it is non-empty, contains `</html>`, and has the expected number of cards. If something
looks wrong, say so in the output text; never open a browser to look at it.

More generally: **nothing optional may run before C and D.** Delivery is the point of this task. Any
verification, formatting flourish or extra research belongs after the briefings are sent, so that if it
hangs it costs a nicety rather than the whole run.

### A — Save the dashboard

Save the exact same rendered HTML (full standalone document, not just the widget snippet — wrap with
`<html><head>` including the same CSS) to:
`__TASK_PATH__/latest_dashboard.html`

### B — Catalyst file for the Telegram bot

The Telegram briefing (`~/Downloads/moomoo_OpenD_10.8.6808_Mac/portfolio_telegram_briefing.py`) has no web
access — it reads news and earnings from a file this task writes. **Refresh it every run** so the Telegram
message never shows stale catalysts.

Write `~/.moomoo_catalysts.json` using the news, earnings dates and macro gathered in the web searches above:

```json
{
  "updated": "YYYY-MM-DD",
  "source": "9am scheduled briefing (web search)",
  "headlines": [{"tag": "bull|bear|macro", "text": "one line, <=160 chars, plain ASCII"}],
  "events":    [{"date": "YYYY-MM-DD", "ticker": "NVDA", "event": "Q2 earnings",
                 "tag": "earnings|expiry|macro", "note": "short qualifier or \"\""}],
  "reported":  [{"date": "YYYY-MM-DD", "ticker": "AMD", "event": "what happened"}]
}
```

Rules:
- **`updated` must be today's date** — the script shows a stale warning if it's more than 3 days old.
- 4–6 `headlines`, ordered most important first. `bull`/`bear` for single-name or sector moves, `macro` for
  Fed / geopolitical / oil. No HTML, no emoji (the script adds icons), avoid `<`, `>`, `&`.
- `events` covers the next ~45 days: confirmed earnings dates for portfolio and watchlist tickers, **the
  user's own option expiries** (tag `expiry`), FOMC, and index rebalances. Verify earnings dates by web
  search each run — do not carry forward an unverified date.
- Any `earnings` event dated on or before a short call's expiry triggers an automatic
  "⚠️ earnings before expiry" warning on that position in the Telegram message. This is the main
  assignment-gap risk in the book, so earnings dates for tickers with open short calls matter most.
- Move events into `reported` once they've happened, with the actual outcome.

Preview the result with:
`python3 ~/Downloads/moomoo_OpenD_10.8.6808_Mac/portfolio_telegram_briefing.py --dry-run`

Writing this file does not send anything by itself — step D does the sending.

### C — Email delivery (comprehensive)

Run: `python3 __TASK_PATH__/send_email.py`

This sends the saved dashboard via the Resend API to you@example.com, reading the API key from
`~/.config/portfolio-briefing/resend_api_key.txt`. If it errors (e.g. missing/invalid key), report the error
in the output but do not block the rest of the briefing — and still do step D.

**Send the email once.** If you spot an error after sending, fix the HTML and re-send, but say clearly in the
output that an earlier version went out and which one is authoritative.

### D — Telegram delivery (compact)

Send the compact companion briefing to @RICHHHMAN_BOT:

```bash
python3 ~/Downloads/moomoo_OpenD_10.8.6808_Mac/portfolio_telegram_briefing.py
```

This is the short-format counterpart to the email: live account totals, stock positions, short-call
assignment risk (colour-coded by % OTM), long calls, then the 📰 NEWS and 📅 NEXT 45 DAYS sections from the
catalyst file written in step B.

- It reads its own credentials from `~/.moomoo_alerts.env` and pulls positions live from OpenD — no
  arguments and no data need to be passed in.
- **Requires OpenD running.** If it exits non-zero, the script has already sent its own failure notice to
  Telegram; just report the error in the output and continue. Do not retry in a loop.
- Note the 9am SGT run happens **before** the US open (21:30 SGT), so position marks reflect the previous
  session's close/after-hours — same basis as the email. That's expected, not an error.
- Do not add `--dry-run` in the scheduled run; that previews without sending.

<!-- NOTE 2026-07-24: an unresolved fragment "allow remote control" was left here with no object — unclear if this meant a moomoo OpenD setting, a scheduled-task permission, or something else. Left unactioned pending clarification from the owner; see chat output from the 2026-07-24 run. -->