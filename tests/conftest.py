"""Suite-wide isolation from the live runtime.

`QuantScanner` opens `StateDB()` at the configured path (`vilona_v2.db`) in its
constructor, so any test that builds a scanner without overriding `db` used to
write fake tickets (e.g. 1002, 7001) into the production audit tables. The live
operator kill switch file is likewise shared: arming it during a trading
incident made unrelated tests fail. Both are redirected per test here; tests
that need their own path still override them explicitly.
"""
from __future__ import annotations

from types import SimpleNamespace

import pytest


@pytest.fixture(autouse=True)
def _isolate_runtime_state(tmp_path, monkeypatch):
    from core import quant_scanner
    from core import state_db as state_db_mod

    monkeypatch.setattr(state_db_mod, "get_config", lambda: SimpleNamespace(
        database=SimpleNamespace(path=str(tmp_path / "isolated_state.db"))
    ))

    live_kill_switch = "/tmp/vilona-STOP_ENTRIES"
    isolated_kill_switch = str(tmp_path / "STOP_ENTRIES")
    real_kill_switch = quant_scanner.FileKillSwitch

    def _kill_switch(path):
        # Tests that pass an explicit kill_switch_path keep it; only the shared
        # live default is swapped for a per-test file.
        return real_kill_switch(isolated_kill_switch if path == live_kill_switch else path)

    monkeypatch.setattr(quant_scanner, "FileKillSwitch", _kill_switch)
