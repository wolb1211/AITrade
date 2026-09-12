"""Admin cost comparison: platform-funded AI cost vs the margin kept from caching.

The two sides of this report come from the same table, split by whether the row
belongs to a user. Mixing them would report the platform's own spend as user
revenue (or hide it entirely), so the split is asserted directly.
"""

from __future__ import annotations

from decimal import Decimal
from pathlib import Path

import pytest

from app.store import SqliteStore


def _seed_endpoint(store: SqliteStore, *, endpoint_id: str, input_price: str, output_price: str) -> None:
    store.save_ai_endpoint({
        "id": endpoint_id,
        "owner_type": "gl",
        "name": endpoint_id,
        "base_url": "https://example.com/v1",
        "model": endpoint_id,
        "api_key": f"sk-{endpoint_id}",
        "input_price_per_million": input_price,
        "output_price_per_million": output_price,
        "cache_input_price_per_million": "0",
    })


def _log(
    store: SqliteStore,
    *,
    user_id: str,
    endpoint_id: str,
    input_tokens: int,
    cached_tokens: int,
    output_tokens: int,
    strategy_code: str = "PA_AGENT_V1",
) -> None:
    # Mirrors what AiDecisionClient._save_usage sends: the price snapshot is what
    # the charge is computed from, so a call without one is billed at zero. The
    # snapshot is looked up rather than passed in, so it cannot drift from the
    # endpoint the row is attributed to.
    endpoint = store.get_private_ai_endpoint(endpoint_id) or {}
    store.save_ai_usage_log({
        "user_id": user_id,
        "model_id": endpoint_id,
        "provider_id": endpoint_id,
        "deployment_id": "dep_1" if user_id else "",
        "strategy_code": strategy_code,
        "billing_source": "official",
        "input_tokens": input_tokens,
        "cached_input_tokens": cached_tokens,
        "output_tokens": output_tokens,
        "input_price_snapshot": str(endpoint.get("input_price_per_million") or "0"),
        "output_price_snapshot": str(endpoint.get("output_price_per_million") or "0"),
        "cache_input_price_snapshot": "0",
        "success": True,
        "provider_called": True,
        "response_source": "provider",
    })


@pytest.fixture()
def store(tmp_path: Path) -> SqliteStore:
    instance = SqliteStore(tmp_path / "cost-comparison.db")
    instance.initialize()
    _seed_endpoint(instance, endpoint_id="aie_cheap", input_price="2", output_price="8")
    _seed_endpoint(instance, endpoint_id="aie_pricey", input_price="10", output_price="40")
    return instance


def test_platform_calls_and_user_calls_are_kept_apart(store: SqliteStore) -> None:
    user = store.save_user({"email": "compare@example.com", "status": "active"})
    _log(store, user_id=str(user["id"]), endpoint_id="aie_cheap", input_tokens=10000, cached_tokens=0, output_tokens=0)
    # Platform-funded authoring call: no user, so it must never look like revenue.
    _log(
        store,
        user_id="",
        endpoint_id="aie_cheap",
        input_tokens=2000,
        cached_tokens=0,
        output_tokens=0,
        strategy_code="WORKFLOW_BUILDER",
    )

    report = store.admin_ai_cost_comparison(months=6)
    month = report["months"][0]

    assert month["user_calls"] == 1
    assert Decimal(month["user_charged"]) == Decimal("0.02")
    assert month["platform_calls"] == 1
    assert Decimal(month["platform_cost"]) == Decimal("0.004")
    assert month["platform_input_tokens"] == 2000
    # Only the user row contributes to the billed token totals.
    assert month["input_tokens"] == 10000


def test_cache_saving_uses_the_endpoint_price_and_the_discount_ratio(store: SqliteStore) -> None:
    user = store.save_user({"email": "saving@example.com", "status": "active"})
    _log(store, user_id=str(user["id"]), endpoint_id="aie_cheap", input_tokens=10000, cached_tokens=8000, output_tokens=0)

    store.save_ai_cache_discount_ratio("0.75")
    report = store.admin_ai_cost_comparison(months=6)
    month = report["months"][0]

    assert month["cached_input_tokens"] == 8000
    assert month["cache_hit_rate"] == 0.8
    # 8000 tokens x 2 / 1e6 x 0.75
    assert Decimal(month["estimated_cache_saving"]) == Decimal("0.012")


def test_changing_the_discount_ratio_changes_only_the_estimate(store: SqliteStore) -> None:
    user = store.save_user({"email": "ratio@example.com", "status": "active"})
    _log(store, user_id=str(user["id"]), endpoint_id="aie_cheap", input_tokens=10000, cached_tokens=8000, output_tokens=0)

    store.save_ai_cache_discount_ratio("0.5")
    half = store.admin_ai_cost_comparison(months=6)["months"][0]
    assert Decimal(half["estimated_cache_saving"]) == Decimal("0.008")

    # The charge to the user is untouched: caching is not passed through.
    assert Decimal(half["user_charged"]) == Decimal("0.02")


def test_net_is_the_saving_minus_the_platform_cost(store: SqliteStore) -> None:
    user = store.save_user({"email": "net@example.com", "status": "active"})
    _log(store, user_id=str(user["id"]), endpoint_id="aie_pricey", input_tokens=10000, cached_tokens=10000, output_tokens=0)
    _log(store, user_id="", endpoint_id="aie_pricey", input_tokens=1000, cached_tokens=0, output_tokens=0)

    store.save_ai_cache_discount_ratio("1")
    month = store.admin_ai_cost_comparison(months=6)["months"][0]

    # saving = 10000 x 10 / 1e6 x 1 = 0.1 ; platform cost = 1000 x 10 / 1e6 = 0.01
    assert Decimal(month["estimated_cache_saving"]) == Decimal("0.1")
    assert Decimal(month["platform_cost"]) == Decimal("0.01")
    assert Decimal(month["net"]) == Decimal("0.09")


def test_deleted_endpoint_does_not_break_the_estimate(store: SqliteStore) -> None:
    """The report must survive a model that was removed after the calls ran."""
    user = store.save_user({"email": "gone@example.com", "status": "active"})
    _log(store, user_id=str(user["id"]), endpoint_id="aie_cheap", input_tokens=1000, cached_tokens=500, output_tokens=0)
    _log(store, user_id=str(user["id"]), endpoint_id="aie_deleted", input_tokens=1000, cached_tokens=500, output_tokens=0)

    month = store.admin_ai_cost_comparison(months=6)["months"][0]

    # Only the surviving endpoint contributes a price; the unknown one is worth 0.
    assert month["cached_input_tokens"] == 1000
    assert Decimal(month["estimated_cache_saving"]) == Decimal("0.00075")


def test_discount_ratio_is_validated_and_clamped(store: SqliteStore) -> None:
    assert Decimal(store.get_ai_cache_discount_ratio()) == Decimal("0.75")

    store.save_ai_cache_discount_ratio("0.4")
    assert Decimal(store.get_ai_cache_discount_ratio()) == Decimal("0.4")

    with pytest.raises(RuntimeError):
        store.save_ai_cache_discount_ratio("1.5")
    with pytest.raises(RuntimeError):
        store.save_ai_cache_discount_ratio("-0.1")
    # A bad value in the settings table must not crash the reader.
    with store._connect() as connection:
        connection.execute(
            "UPDATE system_settings SET setting_value = 'not-a-number' WHERE setting_key = ?",
            ("ai_cache_discount_ratio",),
        )
    assert Decimal(store.get_ai_cache_discount_ratio()) == Decimal("0.75")


def test_report_lists_months_newest_first_and_respects_the_limit(store: SqliteStore) -> None:
    user = store.save_user({"email": "months@example.com", "status": "active"})
    _log(store, user_id=str(user["id"]), endpoint_id="aie_cheap", input_tokens=100, cached_tokens=0, output_tokens=0)

    for month_key in ("2026-06", "2026-07", "2026-08"):
        with store._connect() as connection:
            connection.execute(
                """
                INSERT INTO ai_usage_monthly_summaries (
                    user_id, month_key, model_id, provider_id, deployment_id, strategy_code,
                    billing_source, calls, success_calls, input_tokens, output_tokens,
                    cached_input_tokens, official_tokens, custom_tokens, charged_amount, updated_at
                ) VALUES (?, ?, 'aie_cheap', 'aie_cheap', 'dep_1', 'PA_AGENT_V1',
                          'official', 1, 1, 500, 0, 100, 500, 0, 0.001, '2026-01-01')
                """,
                (str(user["id"]), month_key),
            )

    report = store.admin_ai_cost_comparison(months=2)
    keys = [item["month_key"] for item in report["months"]]

    assert len(keys) == 2
    assert keys == sorted(keys, reverse=True)
    assert report["discount_ratio"] == "0.75"
    assert "不影响用户计费" in report["estimate_note"]
