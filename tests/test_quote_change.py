"""Daily change % on /quote lines."""
import telegram_bot as tb


def test_fmt_chg_up_down_and_missing():
    assert tb.fmt_chg(5.4812) == "  ▲ +5.48%"
    assert tb.fmt_chg(-6.333) == "  ▼ -6.33%"
    assert tb.fmt_chg(None) == ""
