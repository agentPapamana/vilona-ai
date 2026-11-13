from __future__ import annotations

from core.bridge_heartbeat import evaluate_bridge_heartbeat


def test_heartbeat_healthy_when_recent():
    dec = evaluate_bridge_heartbeat(last_tick_epoch_s=100.0, now_epoch_s=120.0, threshold_s=60.0)
    assert dec.healthy is True
    assert dec.should_alert is False


def test_heartbeat_alerts_on_outage_transition():
    dec = evaluate_bridge_heartbeat(last_tick_epoch_s=100.0, now_epoch_s=170.0, threshold_s=60.0, previously_down=False)
    assert dec.healthy is False
    assert dec.should_alert is True
    assert "stalled" in dec.reason


def test_heartbeat_suppresses_repeat_alerts_while_down():
    dec = evaluate_bridge_heartbeat(last_tick_epoch_s=100.0, now_epoch_s=250.0, threshold_s=60.0, previously_down=True)
    assert dec.healthy is False
    assert dec.should_alert is False


def test_heartbeat_recovers_and_rearms():
    dec = evaluate_bridge_heartbeat(last_tick_epoch_s=300.0, now_epoch_s=310.0, threshold_s=60.0, previously_down=True)
    assert dec.healthy is True
    assert dec.should_alert is False

    dec2 = evaluate_bridge_heartbeat(last_tick_epoch_s=300.0, now_epoch_s=380.0, threshold_s=60.0, previously_down=False)
    assert dec2.healthy is False
    assert dec2.should_alert is True


def test_heartbeat_fails_closed_when_no_tick():
    dec = evaluate_bridge_heartbeat(last_tick_epoch_s=None, now_epoch_s=100.0)
    assert dec.healthy is False
    assert dec.should_alert is True
