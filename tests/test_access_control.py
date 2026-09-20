"""Access-control tests for the Telegram bot.

The guest tier is the security boundary in this project: guests may read
market data, but never the account, and never free-text — which would
otherwise put the whole portfolio into an LLM prompt. These tests pin that
boundary down.

No broker connection or network is needed. Commands that would reach OpenD
are asserted only on whether the gate lets them through, not on their output.
"""

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import bb_telegram_alert as bb          # noqa: E402
import telegram_bot as tb               # noqa: E402

OWNER = "560949139"
GUEST = "111222333"

ACCOUNT_COMMANDS = ["/portfolio", "/p", "/account", "/orders", "/cc SOFI"]
ADMIN_COMMANDS   = ["/allow 999", "/revoke 999", "/guests"]
FREE_TEXT        = ["should I buy PLTR?", "what is my P&L", "hello"]
MARKET_COMMANDS  = ["/help", "/start", "/quote NVDA", "/bb", "/watchlist",
                    "/wheel NVDA", "/csp NVDA", "/leaps NVDA", "/spx"]


@pytest.fixture
def bot(tmp_path, monkeypatch):
    """Bot with replies captured, network stubbed, and a throwaway env file."""
    env = tmp_path / "env"
    env.write_text(
        "TELEGRAM_BOT_TOKEN=x\n"
        f"TELEGRAM_CHAT_ID={OWNER}\n"
        f"TELEGRAM_GUEST_CHAT_IDS={GUEST}\n"
    )
    monkeypatch.setattr(bb, "ENV_FILE", env)
    monkeypatch.setattr(tb, "log", lambda *a, **k: None)
    monkeypatch.setattr(tb, "api", lambda *a, **k: {"ok": True, "result": {}})
    sent = []
    monkeypatch.setattr(tb, "reply", lambda cfg, cid, text, **k: sent.append((cid, text)))

    # Stub every handler that would reach the broker. These tests are about the
    # gate, not the data — and a suite that needs OpenD running is not a suite
    # anyone else can run.
    monkeypatch.setattr(tb, "quote_block", lambda tickers: [])
    monkeypatch.setattr(tb, "watchlist_block", lambda: "watchlist")
    monkeypatch.setattr(tb, "chain", lambda cfg, cid, tk, side: sent.append((cid, f"chain {tk} {side}")))
    monkeypatch.setattr(tb, "wheel", lambda cfg, cid, tk: sent.append((cid, f"wheel {tk}")))
    monkeypatch.setattr(tb, "spx_check", lambda cfg, cid: sent.append((cid, "spx")))
    tb.GUESTS.clear()
    tb.GUESTS.add(GUEST)
    cfg = {"TELEGRAM_CHAT_ID": OWNER, "TELEGRAM_GUEST_CHAT_IDS": GUEST}
    return cfg, sent, env


def _reply_to_guest(bot, msg):
    cfg, sent, _ = bot
    sent.clear()
    try:
        tb.handle(cfg, GUEST, msg, guest=True)
    except Exception:
        # Reached a real handler, so the gate allowed it. That is the assertion.
        return "__ALLOWED__"
    return sent[0][1] if sent else ""


def _is_refusal(text):
    return text.startswith("🔒")


# --- guests must not reach the account ------------------------------------

@pytest.mark.parametrize("cmd", ACCOUNT_COMMANDS)
def test_guest_blocked_from_account(bot, cmd):
    assert _is_refusal(_reply_to_guest(bot, cmd)), f"{cmd} leaked to a guest"


@pytest.mark.parametrize("cmd", ADMIN_COMMANDS)
def test_guest_cannot_manage_guests(bot, cmd):
    assert _is_refusal(_reply_to_guest(bot, cmd)), f"{cmd} let a guest change access"


@pytest.mark.parametrize("msg", FREE_TEXT)
def test_guest_blocked_from_free_text(bot, msg):
    # Free text reaches handle_question(), which feeds the portfolio to an LLM.
    assert _is_refusal(_reply_to_guest(bot, msg)), "free text reached the LLM path"


# --- but must still get market data ---------------------------------------

@pytest.mark.parametrize("cmd", MARKET_COMMANDS)
def test_guest_allowed_market_data(bot, cmd):
    assert not _is_refusal(_reply_to_guest(bot, cmd)), f"{cmd} wrongly blocked"


def test_guest_help_hides_owner_commands(bot):
    out = _reply_to_guest(bot, "/help")
    for hidden in ("portfolio", "account", "orders"):
        assert hidden not in out.lower(), f"guest help mentions {hidden}"


def test_owner_help_lists_account_commands(bot):
    cfg, sent, _ = bot
    sent.clear()
    tb.handle(cfg, OWNER, "/help")
    assert "portfolio" in sent[0][1].lower()


# --- the guest list itself -------------------------------------------------

def test_owner_is_never_demoted_to_guest():
    cfg = {"TELEGRAM_CHAT_ID": OWNER, "TELEGRAM_GUEST_CHAT_IDS": f"{OWNER},{GUEST}"}
    assert tb.load_guests(cfg) == {GUEST}


def test_save_guests_is_atomic_and_preserves_other_keys(bot):
    _, _, env = bot
    tb.save_guests({GUEST, "444555666"})
    text = env.read_text()
    assert "TELEGRAM_BOT_TOKEN=x" in text          # untouched
    assert f"TELEGRAM_CHAT_ID={OWNER}" in text     # untouched
    assert "444555666" in text
    assert oct(env.stat().st_mode & 0o777) == "0o600"
    assert not list(env.parent.glob("*.tmp")), "temp file left behind"


def test_allow_then_revoke_round_trips(bot):
    cfg, sent, env = bot
    new = "777888999"

    sent.clear()
    tb.handle(cfg, OWNER, f"/allow {new}")
    assert new in tb.GUESTS
    assert new in env.read_text()

    sent.clear()
    tb.handle(cfg, OWNER, f"/revoke {new}")
    assert new not in tb.GUESTS
    assert new not in env.read_text()


def test_allow_rejects_junk_and_self(bot):
    cfg, sent, _ = bot
    sent.clear(); tb.handle(cfg, OWNER, "/allow abc")
    assert "not a chat id" in sent[-1][1]
    sent.clear(); tb.handle(cfg, OWNER, f"/allow {OWNER}")
    assert "your own chat id" in sent[-1][1].lower()
