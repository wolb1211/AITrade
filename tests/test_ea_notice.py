"""The "update your EA" notice shown when a client reports no contract data.

Builds that predate the contract metadata serialize the metadata block on the
position interface but never fill it in, so the request arrives with an empty
object and the server cannot turn a stop distance into a position size. Refusing
the order is correct, but the client has to be told why instead of seeing the
strategy quietly do nothing.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from app.api.router import _EA_UPDATE_NOTICE, _ea_update_notice, _mt5_position_response
from app.models import TradeDecision

_GOOD_INFO = {"tick_size": 0.01, "tick_value": 1.0, "contract_size": 100}


def _hold() -> TradeDecision:
    return TradeDecision(
        decision_id="dec_notice",
        request_id="req_notice",
        status="HOLD",
        action="HOLD",
        symbol="XAUUSD",
        confidence=0.3,
        reason="趋势结构未被破坏，继续持有",
        expires_at=datetime.now(timezone.utc) + timedelta(minutes=5),
    )


def test_empty_contract_data_asks_for_an_ea_update() -> None:
    assert _ea_update_notice({}) == _EA_UPDATE_NOTICE
    # Zeros are what an unpopulated metadata block serializes to.
    assert _ea_update_notice({"tick_size": 0, "tick_value": 0}) == _EA_UPDATE_NOTICE
    # A client that reports any usable contract field is left alone.
    assert _ea_update_notice(_GOOD_INFO) == ""
    assert _ea_update_notice({"contract_size": 100}) == ""
    assert _ea_update_notice({"tick_value": 1.0}) == ""
    # No metadata handed over at all means there is nothing to judge.
    assert _ea_update_notice(None) == ""


def test_position_response_carries_the_notice_in_its_text() -> None:
    decision = _hold()

    stale = _mt5_position_response(decision, spread=1.0, positions=[], metadata={})
    assert stale.notice == _EA_UPDATE_NOTICE
    assert stale.description == f"{decision.reason}；{_EA_UPDATE_NOTICE}"

    current = _mt5_position_response(decision, spread=1.0, positions=[], metadata=_GOOD_INFO)
    assert current.notice == ""
    assert current.description == decision.reason
