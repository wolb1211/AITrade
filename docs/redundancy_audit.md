# 冗余审计报告

对 `app/` 与 `tests/` 做了一次 AST 静态审计（扫描 37 个文件、968 个定义），目的是找出真正的冗余，而不是靠印象判断。

## 审计方法

1. 解析全部 `.py`，收集定义（函数 / 方法 / 类 / 模块级常量）与全部引用（`Name` 读取、`Attribute` 访问、字符串字面量）。
2. 判定"零引用"：排除定义自身行号范围内的自引用（递归），同时排除出现在任意字符串字面量中的名字（防止 `getattr` / 字符串派发被误判）。
3. 人工复核全部命中项，逐条 `grep` 验证。

## 一、已清理：确认无调用的死代码（11 个函数 + 1 个常量）

全部经 grep 复核：除定义处外，仓库内**没有任何引用**。

已于本次清理中删除（`ai_service.py` -69 行、`router.py` -38 行、`store.py` -115 行，合计 -222 行）。删除后重新扫描确认未产生新的孤儿代码，全量测试 `131 passed`。

| 文件 | 行 | 名称 |
| --- | --- | --- |
| `app/services/ai_service.py` | 1726 | `_workflow_stage_compact_contract` |
| `app/services/ai_service.py` | 2139 | `_as_list` |
| `app/services/ai_service.py` | 2145 | `_stage_unsupported_conditions` |
| `app/services/ai_service.py` | 2231 | `_clean_stage_summary` |
| `app/services/ai_service.py` | 3122 | `_with_indicator_request_preview` |
| `app/services/ai_service.py` | 33 | `_VISION_TEST_IMAGE_PNG_DATA_URL`（常量） |
| `app/api/router.py` | 2188 | `_mt5_trade_type` |
| `app/api/router.py` | 2214 | `_mt5_response` |
| `app/store.py` | 182 | `_extract_profit` |
| `app/store.py` | 2186 | `SqliteStore.admin_deployment_history_orders` |
| `app/store.py` | 3177 | `SqliteStore.account_matches` |
| `app/store.py` | 6297 | `SqliteStore._format_symbol_set` |

说明：`_as_list`、`_stage_unsupported_conditions`、`_clean_stage_summary` 原本只被旧的自然语言编译链路使用，该链路删除后它们成为孤儿。

## 二、已清理：未使用的 import

| 文件 | 行 | 内容 |
| --- | --- | --- |
| `tests/test_custom_strategy.py` | 3 | `from datetime import datetime, timezone`（两者均已不用） |

## 三、跨文件重复实现

| 名称 | 位置 | 说明 |
| --- | --- | --- |
| `_normalize_epoch_seconds` | `app/api/router.py:1545`、`app/strategies/pa_agent_lite.py:967` | **函数体完全相同**，可提取为公共工具 |
| `_positive_float` | `app/strategies/custom_ai.py:271`、`pa_agent_lite.py:1148`、`turtle_agent.py:418` | 三份实现 |
| `_atr` | `pa_agent_lite.py:1176`、`turtle_agent.py:197` | 两份实现 |
| `_close` | `pa_agent_lite.py:487`、`turtle_agent.py:596` | 两份实现 |
| `_normalize_volume` | `pa_agent_lite.py:1167`、`turtle_agent.py:435` | 两份实现 |
| `_indicator_alias` | `app/services/custom_indicators.py:582`、`custom_workflow.py:674` | 两份实现 |

测试文件内的重复辅助函数（`_candles`、`_open_request`、`_position_request`、`_deployment`、`fake_chat_json`）可以合并到 `tests/workflow_fixture.py` 之类的共享模块，但收益有限。

## 四、结构与拆分依据

行数最多的文件：

| 行数 | 文件 |
| --- | --- |
| 7263 | `app/store.py` |
| 3207 | `app/services/ai_service.py` |
| 2244 | `app/api/router.py` |
| 2174 | `app/strategies/pa_agent_lite.py` |
| 762 | `app/services/auth_service.py` |

最长的函数（拆分时优先处理）：

| 行数 | 位置 | 函数 |
| --- | --- | --- |
| 491 | `app/store.py:263` | `SqliteStore.initialize` |
| 462 | `app/api/router.py:649` | `create_admin_ai_router` |
| 440 | `app/store.py:2641` | `SqliteStore._admin_strategy_code_stats` |
| 358 | `app/api/router.py:1113` | `create_auth_router` |
| 297 | `app/api/router.py:94` | `create_api_router` |
| 231 | `app/store.py:4661` | `SqliteStore.get_user_portal_data` |
| 219 | `app/store.py:4893` | `SqliteStore.list_user_orders` |
| 217 | `app/services/ai_service.py:2489` | `_workflow_computed_facts` |
| 216 | `app/strategies/pa_agent_lite.py:585` | `_compute_features` |
| 214 | `app/api/router.py:393` | `create_mt5_router` |

`SqliteStore.initialize` 一个函数 491 行，涵盖了建表、迁移、回填与种子数据，是 `store.py` 拆分最直接的切入点。

## 五、审计中的误报分类（供后续复跑参考）

以下命中项不是冗余，已排除：

| 类别 | 数量 | 原因 |
| --- | --- | --- |
| FastAPI 路由处理器 | 75 | 通过 `@router.get/post/patch/delete` 注册，无显式调用 |
| Pydantic 校验器 | 3 | 通过 `@model_validator` 注册 |
| pytest 测试函数 | 124 | pytest 按命名约定收集 |
| `AsciiJSONResponse.render` | 1 | 框架回调的重写方法 |
| `from __future__ import annotations` | 33 | 名字不出现在引用中，属于惯用写法 |
| 函数内局部变量 | 1 | 扫描器将局部赋值误判为模块级常量（`turtle_agent.py:223 previous_close`） |

后续复跑审计时，应继续按"装饰器注册 / 命名约定收集 / 框架回调"三类过滤。
