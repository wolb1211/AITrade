"""Shared test fixtures.

The US entry window is on by default, which would make any test that runs
through an entry decision depend on the wall clock: the same test would pass in
the afternoon and fail during 08:30-10:30 New York. Pinning "now" outside the
window keeps the suite reproducible; tests that exercise the window itself patch
the same hook with a time inside it.
"""

from __future__ import annotations

from datetime import datetime, timezone

import pytest

from app.strategies import time_windows

# 21:00 UTC is outside 08:30-10:30 New York in either season.
_OUTSIDE_WINDOW = datetime(2026, 7, 15, 21, 0, tzinfo=timezone.utc)


@pytest.fixture(autouse=True)
def _fixed_clock(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(time_windows, "now_utc", lambda: _OUTSIDE_WINDOW)
