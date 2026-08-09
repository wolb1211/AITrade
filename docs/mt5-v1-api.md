# MT5 v1 API

## 1. Strategy init

`POST /mt5/strategy/init`

Request:

```json
{
  "deployment_key": "gl_demo_pa_key"
}
```

Response:

```json
{
  "status": "ok",
  "protocol_version": 1.0,
  "min_ea_version": 1.0,
  "ea_upgrade_required": false,
  "strategy": {
    "id": "dep_demo_pa_xauusd_m15",
    "name": "GainLab PA Base Demo",
    "status": "active",
    "summary": "Strategy description",
    "open_data_type": "kline",
    "open_kline_count": 100,
    "position_data_type": "kline",
    "position_kline_count": 100
  }
}
```

Init only validates the deployment key and returns strategy runtime requirements. `symbol` and `timeframe` are sent by MT5 on every decision request. Init does not lock a strategy to one chart symbol or one timeframe.

## 2. Open decision

`POST /mt5/strategy/open-decision`

Request:

```json
{
  "deployment_key": "gl_demo_pa_key",
  "account": {
    "platform": "MT5",
    "login": "123456",
    "server": "Demo-Server"
  },
  "symbol": "XAUUSD",
  "timeframe": "M15",
  "data_type": "kline",
  "market": {
    "bid": 3300.0,
    "ask": 3300.3,
    "spread": 30,
    "bars": [
      {
        "time": "2026-07-16 15:00:00",
        "open": 3297.5,
        "high": 3299.0,
        "low": 3297.0,
        "close": 3298.0,
        "volume": 100
      }
    ]
  }
}
```

```json
{
  "status": "ok",
  "should_open": true,
  "description": "PA Base deterministic trend candidate",
  "spread": 30,
  "decision_id": "dec_xxx",
  "request_id": "mt5_xxx",
  "orders_count": 1,
  "orders": [
    {
      "direction": "buy",
      "volume": 0.01,
      "order_type": "market",
      "price": 3300.3,
      "tp": 3308.3,
      "sl": 3295.3,
      "comment": "GainLabAI"
    }
  ]
}
```

When there is no open signal, `should_open` is `false`, `orders_count` is `0`, and `orders` is an empty array.

For local pending-order testing, use these `request_id` prefixes:

- `test_pending_buy_limit`: returns one buy limit order.
- `test_pending_sell_limit`: returns one sell limit order.
- `test_random`: randomly returns hold, market buy/sell, or pending buy/sell limit.

## 3. Position decision

`POST /mt5/strategy/position-decision`

Request:

```json
{
  "deployment_key": "gl_demo_pa_key",
  "account": {
    "platform": "MT5",
    "login": "123456",
    "server": "Demo-Server"
  },
  "symbol": "XAUUSD",
  "timeframe": "M15",
  "data_type": "kline",
  "market": {
    "bid": 3297.0,
    "ask": 3297.3,
    "spread": 30,
    "bars": []
  },
  "positions": [
    {
      "ticket": "123456",
      "symbol": "XAUUSD",
      "mt_type": 0,
      "volume": 0.01,
      "open_price": 3300.0,
      "current_price": 3297.0,
      "sl": 3295.0,
      "tp": 3308.0,
      "profit": -3.0,
      "open_time": 1784937000,
      "comment": "GainLabAI"
    }
  ]
}
```

Response:

```json
{
  "status": "ok",
  "has_action": true,
  "description": "PA Base detected bearish reversal",
  "spread": 30,
  "decision_id": "dec_xxx",
  "request_id": "mt5_xxx",
  "actions_count": 1,
  "actions": [
    {
      "action": "close",
      "ticket": "123456",
      "mt_type": 0,
      "volume": 0.01,
      "order_type": "market",
      "price": 0,
      "sl": null,
      "tp": null,
      "comment": "PA Base detected bearish reversal"
    }
  ]
}
```

When there is no position-management action, `has_action` is `false`, `actions_count` is `0`, and `actions` is an empty array.

For local MT5 branch testing, use these `request_id` prefixes:

- `test_random`: randomly returns hold, add buy/sell, or 1-6 close/modify/cancel actions depending on the submitted positions.
- `test_random_all`: returns one close/modify/cancel action for each submitted position/order, useful for testing MT5 action loops.
- `test_add_buy`: returns an `add` action with `direction: "buy"`.
- `test_add_sell`: returns an `add` action with `direction: "sell"`.
- `test_modify`: returns a `modify` action for the first position.
- `test_cancel`: returns a `cancel` action for the first pending order or position item.

`add` includes `direction` because it creates a new order. Other position-management actions can be handled by `ticket` and `mt_type`.
