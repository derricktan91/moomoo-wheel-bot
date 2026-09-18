
# Broker entity and OpenD endpoint come from the environment so this runs
# outside Singapore. moomoo SG = FUTUSG, US = FUTUINC, HK = FUTUSECURITIES,
# AU = FUTUAU, JP = FUTUJP, MY = FUTUMY, CA = FUTUCA. See SETUP.md.
SECURITY_FIRM = os.environ.get("MOOMOO_SECURITY_FIRM", "FUTUSG")
OPEND_HOST = os.environ.get("OPEND_HOST", "127.0.0.1")
OPEND_PORT = int(os.environ.get("OPEND_PORT", "11111"))
#!/usr/bin/env python3
"""
Interactive Telegram bot for the moomoo account.

Two tiers:
  1. Slash commands  -> deterministic Python, no LLM, always available.
  2. Plain English   -> data is fetched HERE, then handed to `claude -p` as
                        text for analysis. Claude runs with `--tools ""`, so a
                        Telegram message can never cause shell/file access.

STRICTLY READ-ONLY. There is no order-placing code path anywhere in this file
or the modules it imports, by design — a chat bot attached to a live brokerage
connection should not be able to trade.

Run:  python3 telegram_bot.py          (long-polls until killed)
      python3 telegram_bot.py --once   (drain pending updates, then exit)
"""
import html
import json
import os
import re
import subprocess
import sys
import time
import urllib.parse
import urllib.request
from datetime import datetime, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from bb_telegram_alert import (  # noqa: E402
    load_env, send_telegram, ssl_context, classify, pct_b_of,
    bollinger_and_rsi, WATCHLIST, BB_PERIOD, LEAPS_PCT_B,
)
import portfolio_telegram_briefing as pb  # noqa: E402
import wheel_analysis as wa  # noqa: E402

DTE_NOTE = "30-45 DTE, ranked by annualised yield"
LEAPS_NOTE = "365+ DTE, ranked by least time value"
CLAUDE_BIN = str(Path.home() / ".local/bin/claude")
CLAUDE_MODEL = "opus"  # switch to "sonnet" if this eats too much of the subscription quota
CLAUDE_TIMEOUT = 240
ANALYSE_TIMEOUT = 600
WHEEL_TIMEOUT = 75       # OpenD hangs forever on index-ETF chains; kill it      # web research is slow; needs far more headroom than chat
LOG_FILE = Path.home() / ".moomoo_bot.log"
OFFSET_FILE = Path.home() / ".moomoo_bot_offset"
TG_LIMIT = 4000

HELP = """<b>🤖 moomoo assistant</b>

<b>Commands</b>
/portfolio — live positions + P/L
/account — cash, assets, buying power
/watchlist — list the tracked tickers
/bb — Bollinger scan, whole watchlist
/bb SOFI — bands for one ticker
/quote NVDA — price, RSI, bands
/wheel NVDA — CSP entry check + live strikes
/csp NVDA — put chain, .20-.30 delta, with IV/RV + OI
/cc SOFI — call chain, checked against your cost basis
/leaps NVDA — LEAPS calls, 365+ DTE, ~0.70 delta
/spx — SPX put-spread entry check (vol, day, strike)
/analyse NVDA — full research report
/news — macro, geopolitics, your names
/news SOFI — recent news for one ticker
/orders — today's orders
/help — this message

<b>Or just ask</b>
"how risky is my PLTR call?"
"should I be worried about SOFI"
"analyse MU for a CSP"

<i>Read-only — this bot cannot place trades.</i>"""


def log(msg):
    line = f"{datetime.now():%Y-%m-%d %H:%M:%S} | {msg}"
    print(line, flush=True)
    with LOG_FILE.open("a") as f:
        f.write(line + "\n")


# ------------------------------------------------------------- telegram ----

def api(cfg, method, params=None, timeout=70):
    token = cfg["TELEGRAM_BOT_TOKEN"]
    url = f"https://api.telegram.org/bot{token}/{method}"
    data = urllib.parse.urlencode(params or {}).encode()
    req = urllib.request.Request(url, data=data)
    with urllib.request.urlopen(req, timeout=timeout, context=ssl_context()) as r:
        return json.loads(r.read().decode())


# Drives the "/" autocomplete menu in the Telegram client. Order here is the
# order shown. Descriptions are capped at 256 chars by the Bot API; keep them
# short so the menu stays scannable on a phone. Registered once at startup —
# Telegram stores them server-side, so this survives bot restarts.
BOT_COMMANDS = [
    ("portfolio", "Live positions + P/L"),
    ("account",   "Cash, assets, buying power"),
    ("watchlist", "List the tracked tickers"),
    ("bb",        "Bollinger scan — all, or /bb SOFI"),
    ("quote",     "Price, RSI, bands — /quote NVDA"),
    ("wheel",     "CSP entry check + live strikes — /wheel NVDA"),
    ("csp",       "Put chain for a CSP — /csp NVDA"),
    ("cc",        "Call chain for a covered call — /cc SOFI"),
    ("leaps",     "LEAPS calls 365+ DTE, ~0.70 delta — /leaps NVDA"),
    ("spx",       "SPX put-spread entry check: vol regime, day, strike"),
    ("analyse",   "Full research report — /analyse NVDA"),
    ("news",      "Macro + your names, or /news SOFI"),
    ("orders",    "Today's orders"),
    ("help",      "Show all commands"),
]


# Guests (friends) get market-data commands only. Anything touching the live
# account — positions, cash, orders — and anything that spends the owner's
# Claude token stays owner-only. Free-form text is owner-only too: it falls
# through to handle_question(), which feeds the whole portfolio to Claude.
GUEST_COMMANDS = frozenset({"start", "help", "quote", "bb", "watchlist",
                            "wheel", "spx", "csp", "leaps"})
GUEST_BOT_COMMANDS = [(c, d) for c, d in BOT_COMMANDS if c in GUEST_COMMANDS]

GUEST_HELP = (
    "<b>Market data bot</b>\n\n"
    "/quote NVDA — price, RSI, Bollinger bands\n"
    "/bb — Bollinger scan (or /bb SOFI)\n"
    "/wheel NVDA — CSP entry check + live strikes\n"
    "/spx — SPX put-spread entry check\n"
    "/watchlist — tracked tickers\n"
    "/help — this message\n\n"
    "<i>Guest access: market data only.</i>"
)


def load_guests(cfg):
    """Guest chat IDs from TELEGRAM_GUEST_CHAT_IDS (comma-separated).

    Kept separate from TELEGRAM_CHAT_ID so a mistake here can never widen the
    owner's own access, and so removing a guest is a one-line env edit.
    """
    raw = str(cfg.get("TELEGRAM_GUEST_CHAT_IDS", "")).strip()
    guests = {x.strip() for x in raw.split(",") if x.strip()}
    guests.discard(str(cfg.get("TELEGRAM_CHAT_ID", "")).strip())
    return guests


def register_commands(cfg):
    """Publish the command list so typing '/' shows a menu in Telegram.

    Scoped to the owner's chat so the menu only appears for him; the bot
    already refuses every other chat_id. Failure is non-fatal — the bot runs
    fine without the menu, so a network blip at startup must not stop polling.
    """
    try:
        scope = json.dumps({"type": "chat", "chat_id": int(cfg["TELEGRAM_CHAT_ID"])})
        cmds = json.dumps([{"command": c, "description": d} for c, d in BOT_COMMANDS])
        r = api(cfg, "setMyCommands", {"commands": cmds, "scope": scope})
        if r.get("ok"):
            log(f"registered {len(BOT_COMMANDS)} commands for the / menu")
        else:
            log(f"setMyCommands rejected: {str(r)[:200]}")
        gcmds = json.dumps([{"command": c, "description": d}
                            for c, d in GUEST_BOT_COMMANDS])
        for g in load_guests(cfg):
            gs = json.dumps({"type": "chat", "chat_id": int(g)})
            gr = api(cfg, "setMyCommands", {"commands": gcmds, "scope": gs})
            log(f"guest menu for {g}: {'ok' if gr.get('ok') else str(gr)[:120]}")
    except Exception as e:
        log(f"setMyCommands failed (menu unavailable, bot still fine): {e}")


def reply(cfg, chat_id, text, parse_mode="HTML"):
    """Telegram caps messages ~4096 chars; split on line boundaries."""
    chunks, cur = [], ""
    for line in text.split("\n"):
        if len(cur) + len(line) + 1 > TG_LIMIT:
            chunks.append(cur)
            cur = line
        else:
            cur = f"{cur}\n{line}" if cur else line
    if cur:
        chunks.append(cur)
    for c in chunks:
        send_telegram(cfg, c, parse_mode=parse_mode, chat_id=chat_id)


# ------------------------------------------------------ moomoo (read-only) --

def quote_block(tickers):
    """Live price + Bollinger/RSI for specific tickers. Read-only."""
    from futu import OpenQuoteContext, RET_OK, KLType, AuType

    today = datetime.now().date()
    start = (today - timedelta(days=200)).isoformat()
    ctx = OpenQuoteContext(host="127.0.0.1", port=11111)
    out = []
    try:
        codes = [f"US.{t}" for t in tickers]
        live = {}
        ret, snap = ctx.get_market_snapshot(codes)
        if ret == RET_OK and snap is not None:
            for _, row in snap.iterrows():
                live[str(row["code"]).replace("US.", "")] = float(row["last_price"])
        for i, t in enumerate(tickers):
            ret, data, _ = ctx.request_history_kline(
                code=f"US.{t}", start=start, end=today.isoformat(),
                ktype=KLType.K_DAY, autype=AuType.QFQ, max_count=250)
            if ret != RET_OK or data is None or len(data) < BB_PERIOD + 1:
                out.append({"ticker": t, "error": "no data"})
                continue
            mid, upper, lower, rsi, last_close = bollinger_and_rsi(data["close"])
            price = live.get(t, last_close)
            pb_ = pct_b_of(price, lower, upper)
            zone, emoji, label = classify(pb_)
            out.append({"ticker": t, "price": price, "mid": mid, "upper": upper,
                        "lower": lower, "pct_b": pb_, "rsi": rsi, "label": label,
                        "emoji": emoji, "bar": str(data["time_key"].iloc[-1])[:10]})
            if i < len(tickers) - 1:
                time.sleep(3)
    finally:
        ctx.close()
    return out


def fmt_quotes(rows):
    lines = []
    for r in rows:
        if "error" in r:
            lines.append(f"❓ <b>{r['ticker']}</b> — {r['error']}")
            continue
        tag = f"{r['emoji']} " if r["emoji"] else ""
        lines.append(
            f"{tag}<b>{r['ticker']}</b>  ${r['price']:,.2f}\n"
            f"    {r['label']} · %B {r['pct_b']:.2f} · RSI {r['rsi']:.1f}\n"
            f"    lower ${r['lower']:,.2f} · mid ${r['mid']:,.2f} · upper ${r['upper']:,.2f}"
            + (f"\n    🎯 LEAPS zone" if r["pct_b"] <= LEAPS_PCT_B else "")
        )
    return "\n\n".join(lines)


def watchlist_block():
    """Membership only — instant, no OpenD calls. /bb is the scanning version."""
    csp = [t for t, is_csp in WATCHLIST if is_csp]
    ctx = [t for t, is_csp in WATCHLIST if not is_csp]
    held = held_tickers()
    def mark(t):
        return f"{t}*" if t in held else t
    return (
        f"<b>📋 Watchlist — {len(WATCHLIST)} tickers</b>\n\n"
        f"<b>CSP candidates ({len(csp)})</b>\n" + ", ".join(mark(t) for t in csp) + "\n\n"
        f"<b>Context only ({len(ctx)})</b>\n" + ", ".join(mark(t) for t in ctx) + "\n\n"
        "<i>* = currently held</i>\n"
        "<i>/bb scans them all · /bb TICKER or /news TICKER for one</i>\n"
        "<i>to add or remove, edit WATCHLIST in bb_telegram_alert.py</i>"
    )


def today_orders():
    from futu import (OpenSecTradeContext, TrdMarket, SecurityFirm, TrdEnv, RET_OK)
    ctx = OpenSecTradeContext(filter_trdmarket=TrdMarket.US, host="127.0.0.1",
                              port=11111, security_firm=SECURITY_FIRM)
    try:
        ret, d = ctx.order_list_query(trd_env=TrdEnv.REAL, acc_id=pb.ACC_ID)
        if ret != RET_OK:
            return f"⚠️ {d}"
        if d is None or len(d) == 0:
            return "No orders today."
        lines = ["<b>📋 Today's orders</b>", ""]
        for _, o in d.iterrows():
            lines.append(
                f"<b>{o['code'].replace('US.','')}</b> {o['trd_side']} "
                f"{float(o['qty']):g} @ {float(o['price']):.2f}\n"
                f"    {o['order_status']} · filled {float(o['dealt_qty']):g}")
        return "\n".join(lines)
    finally:
        ctx.close()


def account_block():
    pos, acc = pb.fetch()
    total_pl = float(pos["pl_val"].sum())
    return (
        "<b>🏦 Account</b>\n\n"
        f"Total assets     ${float(acc['total_assets']):,.2f}\n"
        f"Securities       ${float(acc['securities_assets']):,.2f}\n"
        f"Cash             ${float(acc['cash']):,.2f}\n"
        f"Buying power     ${float(acc['power']):,.2f}\n"
        f"Long MV          ${float(acc['long_mv']):,.2f}\n"
        f"Short MV         ${float(acc['short_mv']):,.2f}\n"
        f"Unrealized P/L   {pb.money(total_pl)}\n"
        f"Risk status      {acc['risk_status'] if str(acc['risk_status']) != 'N/A' else acc['risk_level']}\n"
        f"Maint. margin    ${float(acc['maintenance_margin']):,.2f}"
    )


# ------------------------------------------------------------- claude -p ----

KNOWN = {t for t, _ in WATCHLIST} | {"TSM", "AMZN", "GOOGL", "META", "TSLA", "AAPL"}


def find_tickers(text):
    words = set(re.findall(r"\b[A-Z]{2,5}\b", text.upper()))
    return sorted(words & KNOWN)[:4]


def ask_claude(cfg, prompt, tools="", timeout=CLAUDE_TIMEOUT):
    """Run a prompt through the Claude CLI.

    `tools` is the allow-list passed to --tools. It defaults to "" (no tools at
    all) so a free-form chat message can never reach the shell or filesystem;
    /analyse opts in to the research tools only — never Bash/Write/Edit.
    """
    if not Path(CLAUDE_BIN).exists():
        return None, "Claude CLI not found at ~/.local/bin/claude"

    # Long-lived token from `claude setup-token`, so the service authenticates
    # without an interactive login and survives session-token expiry. Stripping
    # ANTHROPIC_BASE_URL keeps the CLI on its own auth path.
    env = {k: v for k, v in os.environ.items() if k != "ANTHROPIC_BASE_URL"}
    token = cfg.get("CLAUDE_CODE_OAUTH_TOKEN")
    if token:
        env["CLAUDE_CODE_OAUTH_TOKEN"] = token
    try:
        p = subprocess.run(
            [CLAUDE_BIN, "-p", prompt, "--tools", tools, "--model", CLAUDE_MODEL],
            capture_output=True, text=True, timeout=timeout,
            cwd="/tmp", env=env,
        )
    except subprocess.TimeoutExpired:
        return None, "Claude timed out."
    out = (p.stdout or "").strip()
    err = (p.stderr or "").strip()
    if "Not logged in" in out or "Not logged in" in err or "/login" in out:
        return None, ("Claude CLI isn't logged in yet. Run `claude` in Terminal "
                      "on the Mac and complete /login once — then chat here works.")
    if p.returncode != 0 or not out:
        return None, f"Claude failed: {(err or out)[:300]}"
    return out, None


def handle_question(cfg, chat_id, text):
    reply(cfg, chat_id, "🤔 pulling your data…")
    parts = []
    try:
        pos, acc = pb.fetch()
        parts.append(pb.build(pos, acc).replace("<b>", "").replace("</b>", "")
                     .replace("<i>", "").replace("</i>", ""))
    except Exception as e:
        parts.append(f"(portfolio unavailable: {e})")

    for t in find_tickers(text):
        try:
            rows = quote_block([t])
            for r in rows:
                if "error" not in r:
                    parts.append(
                        f"{r['ticker']}: ${r['price']:.2f}, %B {r['pct_b']:.2f} "
                        f"({r['label']}), RSI {r['rsi']:.1f}, bands "
                        f"{r['lower']:.2f}/{r['mid']:.2f}/{r['upper']:.2f}, "
                        f"bars thru {r['bar']}")
        except Exception as e:
            parts.append(f"({t} quote unavailable: {e})")

    prompt = f"""You are a concise trading assistant answering over Telegram.

Rules:
- Be brief. Telegram, not a report. No markdown headers, no tables.
- The data below is live and authoritative. Never guess or recall prices.
- You are NOT a licensed advisor. Describe setups, risks and the user's own
  stated rules mechanically. Never tell them to buy or sell, and never suggest
  position sizing.
- If the data doesn't answer the question, say so plainly.

=== LIVE ACCOUNT DATA ({datetime.now():%Y-%m-%d %H:%M}) ===
{chr(10).join(parts)}
=== END DATA ===

User's question: {text}"""

    answer, err = ask_claude(cfg, prompt)
    reply(cfg, chat_id, answer if answer else f"⚠️ {err}", parse_mode=None)


# ----------------------------------------------------------------- wheel ----

def fmt_wheel(r):
    """Render wheel-entry findings. Reports the rules and the chain; decides nothing."""
    t = r["ticker"]
    csp_flag = dict(WATCHLIST).get(t)
    out = [f"🎡 <b>WHEEL — {t}</b>  ${r['price']:,.2f}", ""]

    # --- entry gate: the documented %B <= 0.50 window -------------------
    if r["in_zone"]:
        out.append(f"✅ <b>In entry zone</b> — %B {r['pct_b']:.2f} (rule: ≤ 0.50)")
    else:
        out.append(f"⏸️ <b>Outside entry zone</b> — %B {r['pct_b']:.2f} (rule: ≤ 0.50)")
    out.append(f"    RSI {r['rsi']:.1f} · bands ${r['lower']:,.2f} / ${r['mid']:,.2f} / ${r['upper']:,.2f}")
    if csp_flag is False:
        out.append("    ⚠️ tagged <i>context only</i> on your watchlist, not a CSP name")
    elif csp_flag is None:
        out.append("    ℹ️ not on your watchlist")

    # --- which strike rule applies -------------------------------------
    out.append("")
    out.append(f"📐 <b>Vol {r['vol']*100:.0f}% annualised → {r.get('regime','?').upper()}</b>")
    out.append(f"    strike rule: {r.get('rule','n/a')}")

    # --- live chain -----------------------------------------------------
    if not r["candidates"]:
        out.append("")
        out.append("📉 No liquid put strikes matched (need OI ≥ 50 and a live bid).")
        for n in r["notes"]:
            out.append(f"    {n}")
    else:
        cur = None
        for c in r["candidates"]:
            if c["expiry"] != cur:
                cur = c["expiry"]
                out.append("")
                out.append(f"<b>📅 {cur}  ({c['dte']}d)</b>")
            out.append(
                f"  ${c['strike']:g}  {c['pct_otm']:.1f}% OTM  Δ{c['delta']:+.2f}\n"
                f"     ${c['credit']:,.0f} credit · {c['ann_pct']:.1f}%/yr · "
                f"${c['collateral']:,.0f} collateral\n"
                f"     IV {c['iv']:.0f} · OI {c['oi']:,} · basis if assigned ${c['eff_basis']:,.2f}")

    pos = position_context(t)
    if pos != "No position held.":
        out.append("")
        out.append("<b>📌 Current position</b>")
        for line in pos.split("\n"):
            out.append(f"  {line}")

    out.append("")
    out.append("<i>Your rules applied to live data — credit is bid-side, one contract. "
               "Earnings dates are not checked here; use /news " + t + ".</i>")
    return "\n".join(out)


# futu logs as "2026-09-06 17:52:00,123 [file.py] ..." — never our own output,
# which is HTML and always starts with '<' or is blank.
FUTU_LOG = re.compile(r"^\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}")


def spx_check(cfg, chat_id):
    """SPX bull-put-spread entry scoring. Subprocess for the same reason as
    /wheel — an OpenD hang must not take the whole bot down."""
    reply(cfg, chat_id, "📐 Scoring today's SPX spread setup…")
    try:
        p_ = subprocess.run(
            [sys.executable, str(Path(__file__).parent / "spx_signal.py"), "--text"],
            capture_output=True, text=True, timeout=WHEEL_TIMEOUT)
        # --text prints only the HTML block, but futu writes its own log lines
        # to stdout. Drop those; keep blank lines, they carry the formatting.
        out = [ln for ln in p_.stdout.splitlines()
               if not FUTU_LOG.match(ln) and not ln.startswith("{")]
        while out and not out[0].strip():
            out.pop(0)
        if not any(ln.strip() for ln in out):
            raise ValueError(f"no output: {p_.stderr[-300:]}")
        reply(cfg, chat_id, "\n".join(out).rstrip())
    except subprocess.TimeoutExpired:
        reply(cfg, chat_id, f"⚠️ SPX check timed out after {WHEEL_TIMEOUT}s — is OpenD up?")
    except Exception as e:
        reply(cfg, chat_id, f"⚠️ SPX check failed: {str(e)[:300]}")


def chain(cfg, chat_id, ticker, side):
    """Render a 30-45 DTE option chain around the .20-.30 delta band.

    Subprocess + timeout for the same reason as /wheel: OpenD's chain call can
    stall indefinitely on some names and this bot handles messages serially.
    """
    label = {"put": "CSP put", "call": "covered call", "leaps": "LEAPS calls"}[side]
    reply(cfg, chat_id, f"⛓️ Pulling <b>{ticker}</b> {label} chain…")
    try:
        p_ = subprocess.run(
            [sys.executable, str(Path(__file__).parent / "option_chain.py"), ticker, side],
            capture_output=True, text=True, timeout=WHEEL_TIMEOUT)
        r = None
        for line in reversed(p_.stdout.splitlines()):
            line = line.strip()
            if line.startswith("{"):
                try:
                    r = json.loads(line)
                    break
                except ValueError:
                    continue
        if r is None:
            raise ValueError(f"no JSON in output: {p_.stderr[-200:]}")
    except subprocess.TimeoutExpired:
        reply(cfg, chat_id, f"⚠️ {ticker} chain timed out after {WHEEL_TIMEOUT}s — is OpenD up?")
        return
    except Exception as e:
        reply(cfg, chat_id, f"⚠️ chain failed: {html.escape(str(e))[:300]}")
        return
    if "error" in r:
        reply(cfg, chat_id, f"⚠️ {html.escape(r['error'])}")
        return

    icon = {"put": "🔻", "call": "🔺", "leaps": "💎"}[side]
    out = [f"{icon} <b>{r['ticker']} {label.upper()}</b>  ${r['spot']:,.2f}"]
    if r.get("rv30"):
        note = LEAPS_NOTE if side == "leaps" else DTE_NOTE
        if r.get("rule"):
            note = f"{r['rule']}, ranked by least time value"
        out.append(f"<i>realised vol {r['rv30']:.0f}% · {note}</i>")
    out.append(f"<i>Δ{r['delta_lo']:.2f}-{r['delta_hi']:.2f} only</i>")
    out.append("")

    held = r.get("held")
    basis = None
    if side == "call":
        if held and held["qty"] > 0:
            basis = held["cost"]
            out.append(f"You hold <b>{held['qty']:,.0f} sh</b> @ ${basis:,.2f} "
                       f"({int(held['qty'] // 100)} contract(s) coverable)")
            out.append(f"<i>⚠️ = strike below your ${basis:,.2f} basis — being called "
                       f"there locks in a loss</i>")
        else:
            out.append("<i>⚠️ You hold no shares — these calls would be NAKED.</i>")
        out.append("")

    rows = r.get("band") or []
    if not rows:
        reply(cfg, chat_id, "\n".join(out) +
              f"\nNo strike sits in Δ{r['delta_lo']:.2f}-{r['delta_hi']:.2f} "
              f"in the 30-45 DTE window. Nothing to suggest — strikes outside "
              f"that band are not offered.")
        return

    if side == "leaps":
        for b in rows[:6]:
            thin = " ·thin" if b["oi"] < r["oi_min"] else ""
            out.append(f"<b>${b['strike']:,.0f}C</b>  {b['expiry']} · {b['dte']}d · "
                       f"Δ{b['delta']:.2f}")
            out.append(f"<code>cost ${b['dollars']:,}   IV {b['iv']:.0f}%   "
                       f"OI {b['oi']:,}{thin}</code>")
            out.append(f"<code>time value ${b['extrinsic']:,} = {b['extr_pct']}% "
                       f"of the price</code>")
            out.append(f"<code>breakeven ${b['breakeven']:,.2f}  "
                       f"({b['need']:+.0f}% from here)</code>")
            if b["extr_pct"] is not None and b["extr_pct"] >= 60:
                out.append("<i>⚠️ mostly time value — a directional bet with carry, "
                           "not a stock substitute. Go deeper ITM for less decay.</i>")
            if b.get("iv_rv") is not None:
                # Buying, so the sign flips: cheap vol helps you here.
                v = ("cheap vs realised — good side for a buyer" if b["iv_rv"] < 0.9
                     else "fair" if b["iv_rv"] < 1.2 else "expensive — you are overpaying for vol")
                out.append(f"<code>IV/RV {b['iv_rv']:.2f}</code> — <i>{v}</i>")
            out.append("")
        out.append("<i>Last-traded prices; verify the live bid/ask before ordering.</i>")
        reply(cfg, chat_id, "\n".join(out).rstrip())
        return

    for b in rows[:7]:
        flag = ""
        if basis is not None and b["strike"] < basis:
            flag = " ⚠️"
        thin = " ·thin" if b["oi"] < r["oi_min"] else ""
        out.append(f"<b>${b['strike']:,.2f}{'P' if side=='put' else 'C'}</b>  "
                   f"{b['expiry'][5:]} · {b['dte']}d · {b['otm']:+.1f}%{flag}")
        line = (f"<code>${b['dollars']:,}  Δ{abs(b['delta']):.2f}  "
                f"IV {b['iv']:.0f}%  OI {b['oi']:,}{thin}</code>")
        out.append(line)
        extra = f"<code>ann {b['ann']:.1f}%</code>"
        if b.get("collateral"):
            extra = f"<code>collateral ${b['collateral']:,.0f}   ann {b['ann']:.1f}%</code>"
        out.append(extra)
        if b.get("iv_rv") is not None:
            verdict = ("underpaid — IV below realised" if b["iv_rv"] < 0.9
                       else "fairly paid" if b["iv_rv"] < 1.2 else "rich")
            out.append(f"<code>IV/RV {b['iv_rv']:.2f}</code> — <i>{verdict}</i>")
        out.append("")

    out.append("<i>Last-traded prices; verify the live bid/ask before sending an order.</i>")
    reply(cfg, chat_id, "\n".join(out).rstrip())


def wheel(cfg, chat_id, ticker):
    reply(cfg, chat_id, f"🎡 Checking <b>{ticker}</b> for wheel entry…")
    # Subprocess + hard timeout: OpenD's option-chain call blocks forever on
    # some index ETFs (QQQ), and this bot handles messages serially — an
    # in-process hang would take the whole bot down until it was restarted.
    try:
        p_ = subprocess.run(
            [sys.executable, str(Path(__file__).parent / "wheel_analysis.py"), ticker],
            capture_output=True, text=True, timeout=WHEEL_TIMEOUT)
        # futu logs to stdout, so the JSON is not reliably the last line
        r = None
        for line in reversed(p_.stdout.splitlines()):
            line = line.strip()
            if line.startswith("{"):
                try:
                    r = json.loads(line)
                    break
                except ValueError:
                    continue
        if r is None:
            raise ValueError("no JSON in wheel_analysis output")
    except subprocess.TimeoutExpired:
        reply(cfg, chat_id,
              f"⚠️ Timed out reading {ticker}'s option chain after {WHEEL_TIMEOUT}s.\n"
              "Index ETFs (QQQ etc.) have chains OpenD won't return — try a single stock.")
        return
    except Exception as e:
        reply(cfg, chat_id, f"⚠️ Wheel check failed: {html.escape(str(e))[:300]}")
        return
    if "error" in r:
        reply(cfg, chat_id, f"⚠️ {r['error']}")
        return
    reply(cfg, chat_id, fmt_wheel(r))


# ------------------------------------------------------------------ news ----

NEWS_PROMPT = """Produce a market news digest for a retail options trader, current
as of {when} (US Eastern market context).

FORMAT — follow exactly:
- PLAIN TEXT ONLY. No markdown, no HTML, no **bold**, no # headings, no tables.
- NO markdown links. Never write [title](url). Never paste a raw URL.
- Output ONLY the digest — no preamble, no sign-off, and no commentary about
  delivery, tooling, or what you can or cannot do. Your output is piped straight
  to the reader unmodified; anything that isn't the digest is a defect.
- Under 3500 characters total.
- Use these sections, in this order, with these exact emoji markers:

🌍 MARKET NEWS — {when}

📉 TAPE
<index levels and moves: S&P 500, Nasdaq, Dow. Then VIX, US 10-year yield,
 dollar index, oil, gold. One compact line each or grouped — numbers, not adjectives.>

🏦 MACRO & POLICY
<2-4 bullets: Fed/rate expectations, latest inflation or jobs prints, central
 bank moves, fiscal or regulatory news that moved markets>

🌐 GEOPOLITICS
<1-3 bullets: only items with a real market transmission channel — trade,
 tariffs, conflict affecting energy or supply chains, sanctions, elections.
 Skip if nothing material; write "Nothing market-moving." >

⚡ SECTOR MOVERS
<2-4 bullets: what drove the big sectors, especially semis/AI infrastructure,
 memory, software, fintech>

📌 YOUR NAMES
<Only headlines from the last 48h that touch these tickers: {tickers}
 One line per ticker that actually has news. Name the ticker first. If none of
 them had news, write "No stock-specific headlines.">

📅 NEXT UP
<Upcoming catalysts in the next 1-2 weeks: economic releases with dates, Fed
 meetings, notable earnings — especially for the tickers above>

RULES:
- Search the web. Report only what you can verify, and prefer items from the
  last 24-48 hours. Include the date for anything older.
- Never invent a number, a level, or a headline. If you cannot verify something,
  omit it rather than estimating.
- Report facts and what analysts/markets are pricing. Do NOT tell the reader to
  buy, sell, hedge, or adjust anything, and do NOT suggest position sizing.
- Be dense. This is a briefing, not an essay.
"""


TICKER_NEWS_PROMPT = """Produce a recent-news digest for {ticker}, current as of {when}.

This is a NEWS digest, not a valuation report — focus on what has actually
happened lately and what is scheduled next. Do not assess whether the stock is
cheap or expensive.

FORMAT — follow exactly:
- PLAIN TEXT ONLY. No markdown, no HTML, no **bold**, no # headings, no tables.
- NO markdown links. Never write [title](url). Never paste a raw URL.
- Output ONLY the digest — no preamble, no sign-off, and no commentary about
  delivery, tooling, or what you can or cannot do. Your output is piped straight
  to the reader unmodified; anything that isn't the digest is a defect.
- Under 3000 characters total.
- Use these sections, in this order, with these exact emoji markers:

🗞️ {ticker} NEWS — {when}
<one line: current price and recent move, from the live broker data below>

📰 HEADLINES
<3-6 bullets, most recent first, each starting with its date (e.g. "08/06 — ").
 Prioritise the last 7 days. Company news, not sector commentary.>

📊 WHY IT MOVED
<1-3 bullets tying recent price action to specific causes. If the move was
 sector- or market-wide rather than company-specific, say so.>

🎯 ANALYST ACTIONS
<Upgrades, downgrades and price-target changes in the last ~30 days, each with
 the firm name and date. If there were none, write "None in the last 30 days.">

📅 NEXT UP
<Next earnings date, plus any scheduled events, lockups, product launches or
 index changes. Say if the earnings date is estimated rather than confirmed.>

📰 SOURCES
<one line: publication names only, comma-separated. No URLs, no brackets.>

RULES:
- Search the web. Report only what you can verify, with dates.
- Never invent a headline, number, price target or date. Omit rather than guess.
- Report facts and what analysts are saying. Do NOT tell the reader to buy,
  sell, hedge or adjust anything, and do NOT suggest position sizing.
- If the reader holds a position, state it factually without advising on it.
"""


def held_tickers():
    """Stock tickers the reader actually holds (excludes option legs)."""
    try:
        pos, _ = pb.fetch()
    except Exception:
        return set()
    out = set()
    for _, p in pos.iterrows():
        code = str(p["code"])
        if not pb.parse_option(code):
            out.add(code.replace("US.", ""))
    return out


def news(cfg, chat_id):
    reply(cfg, chat_id, "🌍 Gathering market news — ~1-2 min…")
    tickers = sorted({t for t, _ in WATCHLIST} | held_tickers())
    prompt = NEWS_PROMPT.format(
        when=f"{datetime.now():%a %d %b %Y, %H:%M}",
        tickers=", ".join(tickers),
    )
    answer, err = ask_claude(cfg, prompt, tools="WebSearch,WebFetch",
                             timeout=ANALYSE_TIMEOUT)
    reply(cfg, chat_id, answer if answer else f"⚠️ {err}", parse_mode=None)


def ticker_news(cfg, chat_id, ticker):
    """/news TICKER — recent headlines for one name, not the macro digest."""
    reply(cfg, chat_id, f"🗞️ Fetching <b>{ticker}</b> news — ~1 min…")

    rows = quote_block([ticker])
    r = rows[0] if rows else {"error": "no rows"}
    if "error" in r:
        reply(cfg, chat_id, f"⚠️ No market data for {ticker} — check the ticker.")
        return

    live = (f"LIVE BROKER DATA (authoritative, bars thru {r['bar']}):\n"
            f"price ${r['price']:,.2f} · %B {r['pct_b']:.2f} ({r['label']}) · "
            f"RSI {r['rsi']:.1f}")
    prompt = (TICKER_NEWS_PROMPT.format(ticker=ticker,
                                        when=f"{datetime.now():%a %d %b %Y, %H:%M}")
              + f"\n\n{live}\n\nReader's position in {ticker}:\n{position_context(ticker)}")

    answer, err = ask_claude(cfg, prompt, tools="WebSearch,WebFetch",
                             timeout=ANALYSE_TIMEOUT)
    reply(cfg, chat_id, answer if answer else f"⚠️ {err}", parse_mode=None)


# --------------------------------------------------------------- analyse ----

ANALYSE_PROMPT = """Produce a stock analysis for {ticker}.

FORMAT — follow exactly:
- PLAIN TEXT ONLY. No markdown, no HTML, no **bold**, no # headings, no tables.
- NO markdown links. Never write [title](url). Never paste a raw URL.
- Output ONLY the report itself — no preamble, no sign-off, and no commentary
  about delivery, tooling, or what you can or cannot do. Your output is piped
  straight to the reader unmodified; anything that isn't the report is a defect.
- Under 3000 characters total.
- Use these sections, in this order, with these exact emoji markers:

🔍 {ticker} — <price>
<one line on what the company does>

📊 FUNDAMENTALS
<3-5 lines: valuation multiples, revenue growth, margins, balance sheet>

📈 TECHNICALS
<use the live broker data below verbatim — do not recompute or contradict it>

🟢 BULL
• <2-4 bullets>

🔴 BEAR
• <2-4 bullets>

⚠️ RISKS
• <2-3 bullets>

🎯 STREET VIEW
<analyst rating split, average price target, recent revisions>

📰 SOURCES
<one line: publication names only, comma-separated. No URLs, no brackets.>

RULES:
- Search the web for current figures. Never state a financial metric from memory.
  If you cannot verify a number, omit it or write "n/a" — do not estimate.
- Report the analyst consensus as the market's view, attributed to analysts.
  Do NOT give a personal buy/sell recommendation and do NOT suggest position
  sizing — the reader is not asking you to make the decision for them.
- If the reader holds a position, state it factually without advising on it.
"""


def position_context(ticker):
    """Factual summary of the reader's holding in `ticker`, if any."""
    try:
        pos, _ = pb.fetch()
    except Exception as e:
        return f"(position data unavailable: {e})"
    lines = []
    for _, p in pos.iterrows():
        code = str(p["code"])
        qty = float(p["qty"])
        opt = pb.parse_option(code)
        if opt:
            sym, exp, cp, strike = opt
            if sym != ticker:
                continue
            side = "SHORT" if qty < 0 else "LONG"
            lines.append(
                f"{side} {int(abs(qty))}x {sym} ${strike:g}{cp} exp {exp:%d %b %Y} — "
                f"opened ${float(p['cost_price']):.2f}, now ${float(p['nominal_price']):.2f}")
        elif code.replace("US.", "") == ticker:
            lines.append(
                f"{qty:g} shares at ${float(p['cost_price']):.2f} cost — "
                f"now ${float(p['nominal_price']):.2f} ({float(p['pl_ratio']):+.1f}%)")
    return "\n".join(lines) if lines else "No position held."


def analyse(cfg, chat_id, ticker):
    reply(cfg, chat_id, f"🔍 Analysing <b>{ticker}</b> — researching fundamentals, ~1-2 min…")

    rows = quote_block([ticker])
    r = rows[0] if rows else {"error": "no rows"}
    if "error" in r:
        reply(cfg, chat_id, f"⚠️ No market data for {ticker} — check the ticker.")
        return

    live = (
        f"LIVE BROKER DATA (authoritative, bars thru {r['bar']}):\n"
        f"price ${r['price']:,.2f} · %B {r['pct_b']:.2f} ({r['label']}) · RSI {r['rsi']:.1f}\n"
        f"Bollinger: lower ${r['lower']:,.2f} / mid ${r['mid']:,.2f} / upper ${r['upper']:,.2f}"
    )
    prompt = (ANALYSE_PROMPT.format(ticker=ticker)
              + f"\n\n{live}\n\nReader's position in {ticker}:\n{position_context(ticker)}")

    answer, err = ask_claude(cfg, prompt, tools="WebSearch,WebFetch",
                             timeout=ANALYSE_TIMEOUT)
    reply(cfg, chat_id, answer if answer else f"⚠️ {err}", parse_mode=None)


# ------------------------------------------------------------- dispatch ----

def handle(cfg, chat_id, text, guest=False):
    t = text.strip()
    low = t.lower()
    cmd = low.split()[0].lstrip("/").split("@")[0] if low else ""

    if guest and cmd not in GUEST_COMMANDS:
        # Covers unknown commands AND plain text (which would otherwise reach
        # handle_question and hand the whole portfolio to Claude).
        log(f"guest {chat_id} blocked: {cmd or '(free text)'!r}")
        reply(cfg, chat_id, "🔒 Not available on guest access.\n\n" + GUEST_HELP)
        return
    if guest and cmd in ("start", "help"):
        reply(cfg, chat_id, GUEST_HELP)
        return

    if cmd in ("start", "help"):
        reply(cfg, chat_id, HELP)
    elif cmd in ("portfolio", "p"):
        pos, acc = pb.fetch()
        reply(cfg, chat_id, pb.build(pos, acc))
    elif cmd == "account":
        reply(cfg, chat_id, account_block())
    elif cmd == "orders":
        reply(cfg, chat_id, today_orders())
    elif cmd in ("watchlist", "wl"):
        reply(cfg, chat_id, watchlist_block())
    elif cmd == "bb":
        syms = [s.upper().lstrip("$") for s in t.split()[1:]][:4]
        if syms:
            # /bb SOFI -> that name only, skip the full-watchlist scan
            reply(cfg, chat_id, fmt_quotes(quote_block(syms)))
        else:
            mins = max(1, round(len(WATCHLIST) * 3 / 60))
            reply(cfg, chat_id,
                  f"⏳ scanning {len(WATCHLIST)} tickers (~{mins} min)…")
            rows = [r for r in quote_block([tk for tk, _ in WATCHLIST])
                    if "error" not in r]
            rows.sort(key=lambda r: r["pct_b"])
            reply(cfg, chat_id, "<b>📊 BB scan</b>\n\n" + fmt_quotes(rows))
    elif cmd in ("csp", "cc", "leaps"):
        parts = t.split()
        if len(parts) < 2:
            eg = {"csp": "NVDA", "cc": "SOFI", "leaps": "NVDA"}[cmd]
            reply(cfg, chat_id, f"Give me a ticker — e.g. <code>/{cmd} {eg}</code>")
            return
        tk = re.sub(r"[^A-Za-z.]", "", parts[1]).upper()
        if not tk:
            reply(cfg, chat_id, f"'{html.escape(parts[1])[:20]}' is not a ticker.")
            return
        chain(cfg, chat_id, tk,
              {"csp": "put", "cc": "call", "leaps": "leaps"}[cmd])
    elif cmd in ("wheel", "w"):
        args = t.split()[1:]
        if not args:
            reply(cfg, chat_id, "Usage: /wheel NVDA")
        else:
            wheel(cfg, chat_id, args[0].upper().lstrip("$"))
    elif cmd == "spx":
        spx_check(cfg, chat_id)
    elif cmd == "news":
        args = t.split()[1:]
        if args:
            # /news SOFI -> that name only; extra tickers ignored, one at a time
            ticker_news(cfg, chat_id, args[0].upper().lstrip("$"))
        else:
            news(cfg, chat_id)
    elif cmd in ("analyse", "analyze", "a"):
        parts_ = t.split()[1:]
        if not parts_:
            reply(cfg, chat_id, "Usage: /analyse NVDA")
        else:
            analyse(cfg, chat_id, parts_[0].upper().lstrip("$"))
    elif cmd == "quote":
        syms = [s.upper().lstrip("$") for s in t.split()[1:]][:4]
        if not syms:
            reply(cfg, chat_id, "Usage: /quote NVDA")
        else:
            reply(cfg, chat_id, fmt_quotes(quote_block(syms)))
    elif t.startswith("/"):
        reply(cfg, chat_id, f"Unknown command. {HELP}")
    else:
        handle_question(cfg, chat_id, t)


def main():
    cfg = load_env()
    allowed = str(cfg.get("TELEGRAM_CHAT_ID", "")).strip()
    if not allowed:
        log("FATAL: TELEGRAM_CHAT_ID not set — refusing to run unauthenticated")
        sys.exit(1)
    once = "--once" in sys.argv[1:]

    offset = 0
    if OFFSET_FILE.exists():
        try:
            offset = int(OFFSET_FILE.read_text().strip())
        except Exception:
            offset = 0

    guests = load_guests(cfg)
    log(f"bot up (owner {allowed}"
        f"{', guests ' + ','.join(sorted(guests)) if guests else ', no guests'})"
        f"{' [once]' if once else ''}")
    register_commands(cfg)
    # After the Mac sleeps, the long-poll socket comes back dead and retrying
    # in-process never recovers — the bot logged timeouts for 8 hours straight
    # on 2026-09-15 while the network was fine. Exiting hands the problem to
    # launchd, which has KeepAlive + a 15s ThrottleInterval and gives us a
    # fresh process with fresh sockets.
    POLL_FAIL_LIMIT = 3
    fails = 0
    while True:
        try:
            r = api(cfg, "getUpdates", {"offset": offset, "timeout": 50})
            if fails:
                log(f"poll recovered after {fails} failure(s)")
            fails = 0
        except Exception as e:
            fails += 1
            log(f"poll error ({fails}/{POLL_FAIL_LIMIT}): {e}")
            if fails >= POLL_FAIL_LIMIT:
                log("consecutive poll failures — exiting for launchd restart")
                sys.exit(1)
            time.sleep(5)
            continue

        updates = r.get("result", [])
        for u in updates:
            offset = u["update_id"] + 1
            OFFSET_FILE.write_text(str(offset))
            msg = u.get("message") or u.get("edited_message")
            if not msg or "text" not in msg:
                continue
            cid = str(msg["chat"]["id"])
            is_guest = cid in guests
            if cid != allowed and not is_guest:
                # Anyone who learns the bot's username can message it; only the
                # owner's chat and named guests are ever served.
                log(f"DENIED chat {cid}: {msg['text'][:60]!r}")
                continue
            log(f"msg{' [guest ' + cid + ']' if is_guest else ''}: {msg['text'][:80]!r}")
            try:
                handle(cfg, cid, msg["text"], guest=is_guest)
            except Exception as e:
                log(f"handler error: {e}")
                reply(cfg, cid, f"⚠️ Error: {html.escape(str(e))[:400]}")

        if once and not updates:
            log("drained")
            return


if __name__ == "__main__":
    main()
