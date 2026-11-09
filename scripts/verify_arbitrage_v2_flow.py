"""端到端确认 GL_ARBITRAGE_V2 与 V1 的三条行为（临时 SQLite，不动开发库）。

1. /init 返回策略信息 + config
2. 客户端设置的首单手数优先
3. admin 改参数后，下一次 /init 立即生效
"""
from __future__ import annotations

import json
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from fastapi.testclient import TestClient  # noqa: E402

from app.config import Settings  # noqa: E402
from app.main import create_app  # noqa: E402

KEY = "gl_arb_v2_verify_key"


def init(client: TestClient) -> dict:
    response = client.post(
        "/mt5/strategy/init",
        json={"deployment_key": KEY, "account": {"platform": "MT5", "login": "60064845"}},
    )
    assert response.status_code == 200, response.text
    return response.json()


def main() -> None:
    tmp = Path(tempfile.mkdtemp()) / "verify-v2.db"
    app = create_app(Settings(
        environment="development",
        database_path=tmp,
        auth_secret="x",
        admin_jwt_secret="",
    ))
    with TestClient(app) as client:
        store = app.state.store
        store.upsert_web_deployment(
            KEY,
            user_id="verify_local",
            strategy_code="GL_ARBITRAGE_V2",
            strategy_name="GL对冲套利策略V2",
            status="active",
            symbol="XAUUSD",
            timeframe="M5",
            config={"fixed_lot": 0.07},
        )

        print("=== 第 1 次 init ===")
        first = init(client)
        strategy = first["strategy"]
        print("strategy.code            :", strategy["code"])
        print("strategy.name            :", strategy["name"])
        print("strategy.status          :", strategy["status"])
        print("secondary_timeframes     :", [i["timeframe"] for i in strategy["secondary_timeframes"]])
        cfg = first["config"]
        print("config 键数              :", len(cfg))
        print("config 键                :", ", ".join(sorted(cfg)))
        print("fixed_lot（客户端设置 0.07）:", cfg["fixed_lot"])
        print("maPeriod1 / addOrderNum  :", cfg["maPeriod1"], "/", cfg["addOrderNum"])
        print("是否含 V1 专有键          :", "maStart" in cfg or "maPeriod" in cfg)

        print()
        print("=== 模拟 admin 修改参数（maPeriod1=35，addOrderNum=0.45，carryLose=0.25）===")
        save = client.post(
            "/api/admin/ai/official-strategy/save",
            json={
                "id": "ofs_gl_arbitrage_v2",
                "code": "GL_ARBITRAGE_V2",
                "name": "GL对冲套利策略V2",
                "default_config": {
                    "position_sizing_mode": "fixed",
                    "fixed_lot": 0.01,
                    "maPeriod1": 35,
                    "addOrderNum": 0.45,
                    "carryLose": 0.25,
                },
            },
        )
        print("admin save 状态码        :", save.status_code)

        print()
        print("=== 第 2 次 init（不做任何其它操作）===")
        second = init(client)["config"]
        print("maPeriod1                :", first["config"]["maPeriod1"], "->", second["maPeriod1"])
        print("addOrderNum              :", first["config"]["addOrderNum"], "->", second["addOrderNum"])
        print("carryLose                :", first["config"]["carryLose"], "->", second["carryLose"])
        print("fixed_lot 仍是客户端值    :", second["fixed_lot"])
        print("其余键是否保留默认        :", second["addOrderMultiple"], second["reversalAtr"], second["maPeriod2"])


if __name__ == "__main__":
    main()
