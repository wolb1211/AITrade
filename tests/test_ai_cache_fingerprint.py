"""Two accounts describing the same market should share one cached verdict.

The cache key used to include the whole request, so prices a few points apart - the
normal difference between two brokers quoting the same bar - produced different
keys and the cache hardly ever hit. Only the shape of the market decides the
verdict, so account-specific numbers and snapshot timestamps are dropped and
prices are rounded.
"""

from __future__ import annotations

from app.services.ai_service import _cache_fingerprint


def _request(bid: float, ask: float, closes: list[float], login: str = "111") -> dict:
    return {
        "symbol": "XAUUSD",
        "timeframe": "M15",
        "bid": bid,
        "ask": ask,
        "account_login": login,
        "candles": [
            {"time": 1_700_000_000 + index * 900, "open": close, "high": close + 0.2,
             "low": close - 0.2, "close": close, "volume": 12}
            for index, close in enumerate(closes)
        ],
    }


def test_the_account_type_suffix_is_ignored() -> None:
    """Gold arrives as XAUUSD, XAUUSD.c, XAUUSDm or XAUUSDs - all one market."""
    closes = [4374.6, 4375.1]
    plain = _cache_fingerprint(_request(4374.63, 4374.85, closes))
    for suffix in (".c", "m", "s", ".f", "_c", "#c"):
        other = _cache_fingerprint(
            {**_request(4374.63, 4374.85, closes), "symbol": f"XAUUSD{suffix}"}
        )
        assert other == plain, suffix


def test_the_tag_rule_leaves_real_instrument_names_alone() -> None:
    """EURUSD and USDJPY end in a letter and are already upper case, so the tag
    rule - a lower-case letter after an upper-case name - never touches them."""
    from app.services.ai_service import _cache_symbol

    assert _cache_symbol("EURUSD") == "EURUSD"
    assert _cache_symbol("USDJPY") == "USDJPY"
    assert _cache_symbol("NAS100") == "NAS100"
    assert _cache_symbol("XAUUSDm") == "XAUUSD"
    assert _cache_symbol("XAUUSDs") == "XAUUSD"
    assert _cache_symbol("US30cash") == "US30CASH"


def test_two_different_instruments_do_not_merge() -> None:
    first = {**_request(4374.6, 4374.8, [4374.6]), "symbol": "NAS100"}
    second = {**_request(4374.6, 4374.8, [4374.6]), "symbol": "US500"}

    assert _cache_fingerprint(first) != _cache_fingerprint(second)


def test_two_accounts_on_the_same_market_produce_the_same_key() -> None:
    closes = [4374.6, 4375.1, 4374.9]
    first = _request(4374.63, 4374.85, closes, login="111")
    # Another broker, another account, three points away.
    second = _request(4374.66, 4374.88, [4374.62, 4375.12, 4374.92], login="999")

    assert _cache_fingerprint(first) == _cache_fingerprint(second)


def test_a_different_market_is_a_different_key() -> None:
    assert _cache_fingerprint(_request(4374.6, 4374.8, [4374.6, 4375.1])) != _cache_fingerprint(
        _request(4374.6, 4374.8, [4360.0, 4361.0])
    )


def test_a_different_symbol_is_a_different_key() -> None:
    gold = _request(4374.6, 4374.8, [4374.6])
    silver = {**_request(4374.6, 4374.8, [4374.6]), "symbol": "XAGUSD"}

    assert _cache_fingerprint(gold) != _cache_fingerprint(silver)


def test_the_positions_reduce_to_symbol_side_and_count() -> None:
    """The same book in another size is the same situation to review."""
    def book(symbols: list[tuple[str, str]]) -> dict:
        return {
            "direction": "buy",
            "positions": [
                {"symbol": symbol, "side": side, "volume": 0.1 + index, "ticket": str(index)}
                for index, (symbol, side) in enumerate(symbols)
            ],
        }

    assert _cache_fingerprint(book([("XAUUSD", "BUY"), ("XAUUSD", "BUY")])) == _cache_fingerprint(
        book([("XAUUSD", "BUY"), ("XAUUSD", "BUY")])
    )
    # A different count is a different situation.
    assert _cache_fingerprint(book([("XAUUSD", "BUY")])) != _cache_fingerprint(
        book([("XAUUSD", "BUY"), ("XAUUSD", "BUY")])
    )
    # So is a different book.
    assert _cache_fingerprint(book([("XAUUSD", "BUY")])) != _cache_fingerprint(
        book([("XAUUSD", "SELL")])
    )


def test_snapshot_timestamps_do_not_split_the_cache() -> None:
    first = _request(4374.6, 4374.8, [4374.6, 4375.1])
    shifted = {
        **first,
        "candles": [
            {**item, "time": item["time"] + 5}
            for item in first["candles"]
        ],
    }

    assert _cache_fingerprint(first) == _cache_fingerprint(shifted)
