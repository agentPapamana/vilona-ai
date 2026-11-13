from core.risk_lock import (bridge_daily_pl_usd, entry_lock_from_bridge, evaluate_entry_lock, get_server_day_start, FileKillSwitch, write_kill_switch)


def test_kill_switch_blocks_even_when_inputs_are_good():
    d = evaluate_entry_lock(kill_switch=True, daily_pl_usd=0, equity_usd=1000, max_daily_loss_pct=2)
    assert not d.allowed
    assert d.reason == "operator kill switch active"


def test_missing_authoritative_data_fails_closed():
    for pl, equity in [(None, 1000), (0, None)]:
        d = evaluate_entry_lock(kill_switch=False, daily_pl_usd=pl, equity_usd=equity, max_daily_loss_pct=2)
        assert not d.allowed
        assert "unavailable" in d.reason


def test_daily_loss_threshold_blocks_at_or_beyond_limit():
    at = evaluate_entry_lock(kill_switch=False, daily_pl_usd=-20, equity_usd=1000, max_daily_loss_pct=2)
    beyond = evaluate_entry_lock(kill_switch=False, daily_pl_usd=-21, equity_usd=1000, max_daily_loss_pct=2)
    assert not at.allowed and not beyond.allowed


def test_profit_and_loss_inside_limit_allow_entries():
    for pl in (30, -19.99):
        d = evaluate_entry_lock(kill_switch=False, daily_pl_usd=pl, equity_usd=1000, max_daily_loss_pct=2)
        assert d.allowed


def test_invalid_inputs_fail_closed():
    for equity, pct in [(0, 2), (-1, 2), (1000, 0)]:
        d = evaluate_entry_lock(kill_switch=False, daily_pl_usd=0, equity_usd=equity, max_daily_loss_pct=pct)
        assert not d.allowed


def test_bridge_history_includes_commission_swap_fee_and_uses_cutoff():
    class Bridge:
        def get_history_deals(self, since_epoch):
            assert since_epoch == 100
            return [
                {"profit": 10, "commission": -1, "swap": -2, "fee": -3},
                {"profit": -5, "commission": -1, "swap": 0, "fee": 0},
            ]
    assert bridge_daily_pl_usd(Bridge(), since_epoch=100) == -2


def test_missing_or_invalid_bridge_history_fails_closed():
    assert bridge_daily_pl_usd(object(), since_epoch=100) is None
    class Bridge:
        def get_history_deals(self, since_epoch):
            return None
    assert bridge_daily_pl_usd(Bridge(), since_epoch=100) is None
    class BadBridge:
        def get_history_deals(self, since_epoch):
            return [{"commission": -3}]
    assert bridge_daily_pl_usd(BadBridge(), since_epoch=100) is None


def test_day_start_requires_bridge_timezone_and_valid_epoch():
    class Bridge:
        def get_server_day_start(self, now_epoch):
            assert now_epoch == 150
            return 120
    assert get_server_day_start(Bridge(), now_epoch=150) == 120
    assert get_server_day_start(object(), now_epoch=150) is None
    class FutureBoundary:
        def get_server_day_start(self, now_epoch):
            return 151
    assert get_server_day_start(FutureBoundary(), now_epoch=150) is None
    class NonFiniteBoundary:
        def get_server_day_start(self, now_epoch): return float("nan")
    assert get_server_day_start(NonFiniteBoundary(), now_epoch=150) is None


def test_bridge_entry_lock_fails_closed_without_history_protocol():
    decision = entry_lock_from_bridge(object(), kill_switch=False, now_epoch=150,
                                      equity_usd=1000, max_daily_loss_pct=2)
    assert not decision.allowed


def test_bridge_entry_lock_enforces_loss_and_zero_is_valid_data():
    class Bridge:
        def get_server_day_start(self, now_epoch): return 100
        def get_history_deals(self, since_epoch): return [{"profit": -20}]
    d = entry_lock_from_bridge(Bridge(), kill_switch=False, now_epoch=150,
                               equity_usd=1000, max_daily_loss_pct=2)
    assert not d.allowed
    class ZeroBridge(Bridge):
        def get_history_deals(self, since_epoch): return []
    z = entry_lock_from_bridge(ZeroBridge(), kill_switch=False, now_epoch=150,
                               equity_usd=1000, max_daily_loss_pct=2)
    assert z.allowed


def test_file_kill_switch_toggle(tmp_path):
    path = str(tmp_path / "STOP_ENTRIES")
    assert not FileKillSwitch(path).is_active()
    write_kill_switch(path, True)
    assert FileKillSwitch(path).is_active()
    write_kill_switch(path, False)
    assert not FileKillSwitch(path).is_active()


def test_consecutive_losses_before_reset_are_ignored():
    """Four historical losses must not relock right after an operator reset."""
    from core.risk_lock import consecutive_loss_count
    trades = [{"id": i, "closed_at": 100 * i, "profit": -1.0} for i in range(1, 5)]

    assert consecutive_loss_count(trades) == 4
    assert consecutive_loss_count(trades, after_trade_id=4) == 0


def test_consecutive_losses_after_reset_still_count():
    """Losses closed after the reset start a fresh streak toward the limit."""
    from core.risk_lock import consecutive_loss_count
    trades = [{"id": i, "closed_at": 100 * i, "profit": -1.0} for i in range(1, 7)]

    assert consecutive_loss_count(trades, after_trade_id=4) == 2
