"""Unit tests for the dashboard account-currency label."""
from types import SimpleNamespace

from ui.terminal_dashboard import TerminalDashboard


class _Bridge:
    def __init__(self, pl, currency=None, account=None):
        self._pl = pl
        self._currency = currency
        self._account = account

    def calculate_daily_pl(self, symbol):
        return self._pl, 0, 0

    def get_account_info(self):
        return self._account

    @property
    def account_currency(self):
        return self._currency


def _dashboard(bridge, symbols):
    config = SimpleNamespace(
        paper_trading=True,
        exchange=SimpleNamespace(name="MT5", market_type=""),
        trading=SimpleNamespace(symbols=symbols),
        database=SimpleNamespace(path="vilona_v2.db"),
    )
    return TerminalDashboard(bridge, config)


def test_cent_symbols_are_labelled_as_cent_account():
    panel = _dashboard(_Bridge(-543.61, account=SimpleNamespace(currency="USD")), ["XAUUSDc"])._get_header()
    text = str(panel.renderable)

    assert "USD cents" in text
    assert "+USDT" not in text


def test_plain_symbols_keep_the_account_currency():
    panel = _dashboard(_Bridge(12.5, account=SimpleNamespace(currency="EUR")), ["EURUSD"])._get_header()

    assert "EUR" in str(panel.renderable)
    assert "cents" not in str(panel.renderable)


def test_broker_account_currency_wins_over_cent_suffix_when_already_marked():
    panel = _dashboard(_Bridge(1.0, account=SimpleNamespace(currency="USC")), ["XAUUSDc"])._get_header()
    text = str(panel.renderable)

    assert "USC" in text
    assert "cents" not in text


def test_missing_currency_never_claims_usdt():
    panel = _dashboard(_Bridge(0.0), ["EURUSD"])._get_header()

    assert "USDT" not in str(panel.renderable)


def test_missing_pl_method_renders_placeholder():
    bridge = SimpleNamespace(get_account_info=lambda: None)
    panel = _dashboard(bridge, ["EURUSD"])._get_header()
    text = str(panel.renderable)

    assert "P/L: +0.00" in text
    assert "account currency" in text
    assert "USDT" not in text
